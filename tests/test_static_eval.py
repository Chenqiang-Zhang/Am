"""F6: eval/static_eval.py, its cache, analysis and report on the fake graph."""

import functools
import gzip
import json
import math
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from neo4j.exceptions import TransientError

from app.api import static_recommend
from app.api.static_recommend import STUDIO_NORMALIZATIONS, SeedScheme, collect_path_scores, combine
from eval import static_cache, static_eval, static_plan, static_report
from eval.static_cache import (
    HiddenRank,
    StageCache,
    encode,
    full_ranking,
    make_record,
    path_scores_from_record,
    read_record,
)
from eval.static_eval import Setup
from eval.static_plan import config_id, make_config, preference, select
from eval.static_split import build_split, dumps
from tests.fake_graph import GAMES, PLAYS, FakeGraphReader

APPIDS = [game["appid"] for game in GAMES]
VALVE = {"10", "50"}  # Alpha and Echo: Valve developed and published both


def synthetic_plays(players=60, seed=3):
    """The fixture's plays plus players with 2 to 13 games, so that every stratum occurs."""
    rng = random.Random(seed)
    plays = list(PLAYS)
    sizes = [2, 3, 4, 6, 9, 12, 13]
    for index in range(players):
        for appid in rng.sample(APPIDS, sizes[index % len(sizes)]):
            plays.append((f"s{index:02d}", appid, rng.choice([0.0, 0.4, 0.9, 3.0, 25.0, 140.0]), 0.0))
    return plays


PLAYS_ALL = synthetic_plays()
READER = functools.partial(FakeGraphReader, plays=PLAYS_ALL)
SMALL_SPLIT = {
    "validation_size": 21,
    "test_sample_sizes": {"1": None, "2-3": None, "4-10": 8, "11+": 8},
    "subsets": {"v0": ("per_stratum", 3), "explanations": ("per_stratum", 2), "beyond_accuracy": ("proportional", 9)},
}
SCHEMES = {
    "log1p_10": {"weighting": "log_hours", "peer_limit": 10},
    "log1p_30": {"weighting": "log_hours", "peer_limit": 30},
    "log1p_all": {"weighting": "log_hours", "peer_limit": None},
    "uniform_10": {"weighting": "uniform", "peer_limit": 10},
    "uniform_30": {"weighting": "uniform", "peer_limit": 30},
    "uniform_all": {"weighting": "uniform", "peer_limit": None},
}


def small_plan(split_file, split_sha256):
    """The plan's structure with a smaller grid and bootstrap."""
    return {
        "name": "static_plan_test",
        "split": {"file": str(split_file), "sha256": split_sha256, "salt": "static-rec-eval-v1"},
        "schemes": SCHEMES,
        "grid": {
            "weights": {"peer": [1.0], "tag": [0.0, 1.0], "studio": [0.0, 0.5], "popular": [0.0, 0.1]},
            "studio_normalization": "seed_weight",
            "schemes": list(SCHEMES),
            "configs": 48,
        },
        "reference": {
            "name": "graph_v1_default",
            "scheme": "log1p_10",
            "weights": {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1},
            "studio_normalization": "max",
        },
        "objective": {"stage": "validation", "metric": "NDCG", "k": 10, "group": "all"},
        "selection": {"bootstrap": {"B": 200, "seed": 0}},
        "test_rows": [{"name": name, "definition": f"row {name}"} for name in static_plan.ROW_NAMES],
        "metrics": {"names": ["HR", "NDCG"], "k": [10, 20, 50]},
        "statistics": {
            "bootstrap": {"B": 300, "seed": 0, "level": 0.95},
            "groups": ["all", "1-3", "1", "2-3", "4-10", "11+"],
        },
        "contrasts": {
            "test_sample": [
                ["peer_only", "item_knn"],
                ["graph_tuned", "peer_only"],
                ["graph_tuned", "popularity"],
                ["pop_only", "popularity"],
            ],
            "v0_subset": [["graph_tuned", "graph_v0"]],
        },
        "claims": {
            "C1": {
                "checks": {
                    "peer_only_uniform_all": {"scheme": "uniform_all", "weights": {"peer": 1.0}, "baseline": "item_knn"},
                    "pop_only": {"scheme": "log1p_10", "weights": {"popular": 1.0}, "baseline": "popularity"},
                },
                "threshold": 0.999,
            },
            "C2": {
                "contrast": ["peer_only", "item_knn"],
                "metric": "NDCG",
                "k": 10,
                "group": "all",
                "level": 0.95,
                "margin": 0.01,
            },
            "C3": {
                "contrast": ["graph_tuned", "peer_only"],
                "metric": "NDCG",
                "k": 10,
                "tests": {"primary": "all", "secondary": "1-3"},
                "alpha": 0.05,
                "ci_level": 0.975,
            },
            "C4": {
                "metric": "NDCG",
                "k": 10,
                "group": "all",
                "level": 0.95,
                "checks": [
                    {"contrast": ["graph_tuned", "popularity"], "lower_bound_above": 0.0},
                    {"contrast": ["graph_tuned", "item_knn"], "lower_bound_above": -0.01},
                ],
            },
        },
        "descriptive": {
            "hours_threshold": 1.0,
            "studio_id": "valve",
            "groups": {
                "hidden_under_1h": "under 1 h",
                "hidden_1h_or_more": "1 h or more",
                "hidden_valve": "Valve",
                "hidden_not_valve": "not Valve",
            },
            "metrics": [["NDCG", 10], ["HR", 10]],
            "contrasts": [["graph_tuned", "item_knn"], ["graph_tuned", "peer_only"]],
        },
        "beyond_accuracy": {
            "subset": "beyond_accuracy",
            "rep": 0,
            "k": 10,
            "coverage": {"denominator": len(APPIDS)},
            "popular_head": {"size": 3},
        },
        "outputs": {
            "tuning": "eval/results/static_tuning_v1.json",
            "test": "eval/results/static_test_v1.json",
            "explanations": "eval/results/static_explanations_v1.json",
        },
    }


def fake_graph_counts(reader):
    records = reader.all_play_records()
    return {
        "players": len({player_id for player_id, _, _ in records}),
        "games": len({appid for _, appid, _ in records}),
        "played": len({(player_id, appid) for player_id, appid, _ in records}),
    }


ROLE_LINKS = {"developer": "DEVELOPED_BY", "publisher": "PUBLISHED_BY"}


def fake_graph_links(reader):
    rows = [
        {
            "appid": appid,
            "links": [["HAS_TAG", tag_id, rank] for tag_id, _, _, rank in reader.tag_edges(appid)]
            + [[ROLE_LINKS[role], studio_id] for studio_id, _, role in reader.studio_edges(appid)],
        }
        for appid in reader.games
    ]
    return static_eval.links_digest(rows)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A split, a plan and an empty cache on the fake graph; the Cypher-only helpers answered here."""
    monkeypatch.setattr(static_eval, "graph_counts", fake_graph_counts)
    monkeypatch.setattr(static_eval, "graph_links", fake_graph_links)
    monkeypatch.setattr(static_eval, "studio_games", lambda reader, studio_id: set(VALVE))
    split_file = tmp_path / "split.json"
    split_file.write_text(dumps(build_split(READER().all_play_records(), "fake", **SMALL_SPLIT)), encoding="utf-8")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(small_plan(split_file, static_eval.file_sha256(split_file))), encoding="utf-8")
    return Setup.load(plan_file, tmp_path / "cache", 1, READER)


def cached(cache, target):
    return {key: read_record(entry) for key, entry in cache.entries(target).items()}


# ---- records -----------------------------------------------------------------------------------------

CASES = [("u1", "30"), ("p2", "60"), ("s05", None), ("s06", None), ("s00", None)]


def record_cases():
    reader = READER()
    for player_id, hidden in CASES:
        hidden = hidden or sorted(game["appid"] for game in reader.played_games(player_id, []))[0]
        for name in ("log1p_10", "uniform_all", "log1p_30"):
            scheme = SeedScheme(**SCHEMES[name])
            path_scores = collect_path_scores(reader, player_id, frozenset({hidden}), scheme)
            yield make_record(player_id, 0, hidden, name, path_scores), path_scores


def random_weights(rng):
    return {path: rng.choice([0.0, 0.1, 0.25, 0.5, 1.0, 2.0]) for path in static_recommend.PATHS}


def test_combine_on_a_record_equals_combine_on_the_full_path_scores():
    rng = random.Random(0)
    for record, path_scores in record_cases():
        restored = path_scores_from_record(json.loads(gzip.decompress(encode(record))))
        everything = len(path_scores["paths"]["popular"])
        for _ in range(12):
            weights = random_weights(rng)
            for normalization in STUDIO_NORMALIZATIONS:
                for limit in (10, everything):
                    expected = combine(path_scores, limit, weights, normalization)
                    assert combine(restored, limit, weights, normalization) == expected
        assert combine(restored, everything) == combine(path_scores, everything)


def test_the_fast_hidden_rank_equals_combine():
    rng = random.Random(1)
    for record, path_scores in record_cases():
        table = HiddenRank(record)
        for _ in range(12):
            weights = random_weights(rng)
            for normalization in STUDIO_NORMALIZATIONS:
                ranking = full_ranking(path_scores, weights, normalization)
                assert table.rank(weights, normalization) == ranking.index(record["hidden"]) + 1


def tie_record(hidden):
    """Totals that tie exactly, within the 12-decimal rounding, or just outside it."""
    peer = {"a": 0.5, "b": 0.5, "c": 0.5 + 4e-13, "d": 0.5 + 3e-12, "e": 0.5 - 4e-13, "f": 0.25}
    players = {appid: 3 for appid in "abcdefghij"} | {"g": 9, "h": 0, "i": 5}
    return {"player_id": "x", "rep": 0, "hidden": hidden, "target": "t", "seed_weights": [1.0, 2.0],
            "scores": {"peer": peer, "tag": {"a": 1.0, "g": 1.0}, "studio": {"b": 0.3}}, "players": players}


@pytest.mark.parametrize("hidden", list("abcdefghij"))
def test_the_fast_hidden_rank_breaks_near_ties_like_combine(hidden):
    record = tie_record(hidden)
    path_scores = path_scores_from_record(record)
    tried = ({"peer": 1.0}, {"peer": 1.0, "tag": 1e-12}, {"peer": 1.0, "studio": 2.0, "popular": 0.1}, {"popular": 1.0}, {})
    for weights in tried:
        for normalization in STUDIO_NORMALIZATIONS:
            expected = full_ranking(path_scores, weights, normalization).index(hidden) + 1
            assert HiddenRank(record).rank(weights, normalization) == expected


# ---- collect -----------------------------------------------------------------------------------------


def test_equivalent_schemes_are_collected_once():
    reader = READER()
    hidden = sorted(game["appid"] for game in reader.played_games("s01", []))[0]  # s01 has 3 games
    targets = tuple((name, SeedScheme(**SCHEMES[name])) for name in ("log1p_10", "log1p_30", "log1p_all", "uniform_10"))
    with static_cache.local_reader(READER):
        results = static_cache.collect_task(("s01", 0, hidden, targets))
        assert static_cache.worker_reader().calls.count("co_play_scores") == 2
    for (name, scheme), (target, member, error, _) in zip(targets, results):
        assert (target, error) == (name, None)
        expected = make_record("s01", 0, hidden, name, collect_path_scores(reader, "s01", frozenset({hidden}), scheme))
        assert json.loads(gzip.decompress(member)) == json.loads(json.dumps(expected))


def test_collect_writes_every_case_once_and_records_the_run(setup):
    summary = static_eval.run_collect(setup, "smoke", ["log1p_10", "uniform_all"])
    cache = StageCache(setup.cache_root, "smoke")
    cases = static_plan.stage_cases(setup.split, static_plan.stage_players(setup.split, "smoke"))
    assert summary["failures"] == [] and summary["written"] == 2 * len(cases)
    for target in ("log1p_10", "uniform_all"):
        assert sorted(cache.entries(target)) == sorted((player_id, rep) for player_id, _, rep, _ in cases)
    assert static_eval.run_collect(setup, "smoke", ["log1p_10", "uniform_all"])["planned"] == 0
    assert [run["written"] for run in cache.runs()] == [2 * len(cases), 0]


def test_a_killed_run_is_completed_not_duplicated(setup, tmp_path):
    static_eval.run_collect(setup, "smoke", ["log1p_10"])
    complete = cached(StageCache(setup.cache_root, "smoke"), "log1p_10")
    # Cut the part in the middle of a record and the index in the middle of a line, as a kill would.
    killed = tmp_path / "killed"
    shutil.copytree(setup.cache_root, killed)
    index = next((killed / "smoke" / "log1p_10").glob("part-*.idx"))
    lines = index.read_text().splitlines(keepends=True)
    keep = len(lines) // 2
    part = index.with_suffix(".jsonl.gz")
    offset, length = map(int, lines[keep].split("\t")[3:5])
    with open(part, "r+b") as handle:
        handle.truncate(offset + length // 2)
    index.write_text("".join(lines[:keep]) + lines[keep][:7])
    resumed = Setup.load(setup.plan_path, killed, 1, READER)
    summary = static_eval.run_collect(resumed, "smoke", ["log1p_10"])
    assert summary["planned"] == len(lines) - keep
    cache = StageCache(killed, "smoke")
    assert cached(cache, "log1p_10") == complete
    indexes = (killed / "smoke" / "log1p_10").glob("part-*.idx")
    listed = [line for path in indexes for line in path.read_text().splitlines()]
    assert len([line for line in listed if line.count("\t") == 4]) == len(complete)


class FlakyReader(FakeGraphReader):
    """Fails co-play for some players: transiently for the first `transient` calls, or always."""

    def __init__(self, transient=0, broken=(), **kwargs):
        super().__init__(plays=PLAYS_ALL, **kwargs)
        self.transient, self.broken = transient, set(broken)

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        if player_id in self.broken:
            raise TransientError("Neo.TransientError.General.MemoryPoolOutOfMemoryError")
        if self.transient:
            self.transient -= 1
            raise TransientError("Neo.TransientError.Transaction.DeadlockDetected")
        return super().co_play_scores(player_id, seeds, played_appids, evidence_limit)


def test_transient_errors_are_retried_and_a_failing_key_is_reported(setup, monkeypatch):
    monkeypatch.setattr(static_cache, "BACKOFF_SECONDS", 0.0)
    smoke = static_plan.stage_players(setup.split, "smoke")
    broken = smoke["4-10"][0]
    reader = functools.partial(FlakyReader, transient=2, broken=[broken])
    flaky = Setup.load(setup.plan_path, setup.cache_root, 1, reader)
    summary = static_eval.run_collect(flaky, "smoke", ["uniform_10"])
    cases = static_plan.stage_cases(setup.split, smoke)
    failed = sorted((failure["player_id"], failure["rep"]) for failure in summary["failures"])
    assert failed == [(player_id, rep) for player_id, _, rep, _ in cases if player_id == broken]
    assert "TransientError" in summary["failures"][0]["error"]
    cache = StageCache(setup.cache_root, "smoke")
    assert len(cache.entries("uniform_10")) == len(cases) - len(failed)
    retried = static_eval.run_collect(setup, "smoke", ["uniform_10"])
    assert retried["planned"] == len(failed) and retried["failures"] == []
    with pytest.raises(SystemExit):  # the other schemes are missing
        static_eval.run_tune(setup, "smoke", setup.cache_root / "tuning.json", ["uniform_10"])


class CrashingReader(FakeGraphReader):
    """Ends its worker process in the middle of a query, as an out-of-memory kill would."""

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        os._exit(1)


def test_a_dead_worker_stops_the_run_and_reports_its_keys(setup):
    crashing = Setup.load(setup.plan_path, setup.cache_root, 2, functools.partial(CrashingReader, plays=PLAYS_ALL))
    with pytest.raises(SystemExit):
        static_eval.run_collect(crashing, "smoke", ["log1p_10"])
    run = StageCache(setup.cache_root, "smoke").runs()[-1]
    assert run["written"] == 0 and run["failures"]
    assert all("BrokenProcessPool" in failure["error"] for failure in run["failures"])
    assert static_eval.run_collect(setup, "smoke", ["log1p_10"])["failures"] == []


def test_a_worker_pool_writes_the_same_records_as_one_process(setup, tmp_path):
    static_eval.run_collect(setup, "smoke", ["log1p_all", "v0"])
    pooled = Setup.load(setup.plan_path, tmp_path / "pooled", 2, READER)
    static_eval.run_collect(pooled, "smoke", ["log1p_all", "v0"])
    for target in ("log1p_all", "v0"):
        expected = cached(StageCache(setup.cache_root, "smoke"), target)
        assert cached(StageCache(tmp_path / "pooled", "smoke"), target) == expected


def test_the_links_digest_changes_with_any_link_but_not_with_their_order():
    rows = [
        {"appid": "10", "links": [["HAS_TAG", "genre:action", 1], ["DEVELOPED_BY", "valve"]]},
        {"appid": "20", "links": [["HAS_TAG", "tag:co_op", None]]},
    ]
    digest = static_eval.links_digest(rows)
    assert static_eval.links_digest([rows[1], dict(rows[0], links=rows[0]["links"][::-1])]) == digest
    studio = ["DEVELOPED_BY", "valve"]
    ranked = [dict(rows[0], links=[["HAS_TAG", "genre:action", 2], studio]), rows[1]]
    moved = [dict(rows[0], links=rows[0]["links"][:1]), dict(rows[1], links=[*rows[1]["links"], studio])]
    renamed = [dict(rows[0], links=[["HAS_TAG", "genre:action", 1], ["DEVELOPED_BY", "valve_corporation"]]), rows[1]]
    assert len({digest, *(static_eval.links_digest(other) for other in (ranked, moved, renamed))}) == 4


def test_a_cache_of_another_split_or_scheme_is_refused(setup):
    static_eval.run_collect(setup, "smoke", ["log1p_10"])
    plan = json.loads(setup.plan_path.read_text())
    plan["schemes"]["log1p_10"]["peer_limit"] = 5
    setup.plan_path.write_text(json.dumps(plan))
    with pytest.raises(SystemExit):
        static_eval.run_collect(Setup.load(setup.plan_path, setup.cache_root, 1, READER), "smoke", ["log1p_10"])


# ---- selection -------------------------------------------------------------------------------------


def candidate(scheme, tag, studio, popular):
    return make_config(scheme, {"peer": 1.0, "tag": tag, "studio": studio, "popular": popular}, "seed_weight")


SCHEME_OBJECTS = {name: SeedScheme(**spec) for name, spec in SCHEMES.items()}


@pytest.mark.parametrize(
    "table,expected",
    [
        # (config, objective, SE of its difference to the best); the best is A.
        (
            [
                (candidate("uniform_all", 0.5, 1.0, 0.1), 0.300, 0.0),  # A, the arg-max
                (candidate("log1p_30", 0.5, 0.0, 0.0), 0.295, 0.010),  # within 1 SE
                (candidate("log1p_10", 1.0, 0.0, 0.0), 0.270, 0.010),  # not within 1 SE
                (candidate("uniform_10", 0.25, 0.0, 0.0), 0.292, 0.010),  # peer_limit 10 beats 30
            ],
            3,
        ),
        (
            [
                (candidate("uniform_all", 0.5, 1.0, 0.1), 0.300, 0.0),
                (candidate("uniform_10", 0.25, 0.0, 0.0), 0.292, 0.010),
                (candidate("log1p_10", 0.25, 0.5, 0.0), 0.291, 0.010),  # log_hours beats uniform
            ],
            2,
        ),
        (
            [
                (candidate("uniform_all", 0.5, 1.0, 0.1), 0.300, 0.0),
                (candidate("log1p_10", 0.25, 0.5, 0.0), 0.291, 0.010),
                (candidate("log1p_10", 0.25, 0.0, 0.0), 0.2905, 0.010),  # more λ = 0
                (candidate("log1p_10", 0.1, 0.0, 0.0), 0.2901, 0.010),  # the same zeros, smaller Σλ
            ],
            3,
        ),
        (
            [
                (candidate("log1p_all", 0.5, 0.5, 0.1), 0.300, 0.0),
                (candidate("log1p_10", 0.25, 0.0, 0.1), 0.299, 0.002),
                (candidate("log1p_10", 0.1, 0.0, 0.25), 0.2985, 0.002),  # the same zeros and Σλ, smaller (λ_tag, ...)
                (candidate("log1p_10", 0.0, 0.0, 0.1), 0.250, 0.002),  # too far below
            ],
            2,
        ),
    ],
)
def test_the_selection_rule_on_a_synthetic_table(table, expected):
    objective = {config_id(config): value for config, value, _ in table}
    se = {config_id(config): error for config, _, error in table}
    shuffled = [config for config, _, _ in table][::-1]
    best, chosen = select(shuffled, objective, lambda config, best: se[config_id(config)], SCHEME_OBJECTS)
    assert best == table[0][0]
    assert chosen == table[expected][0]


def test_the_arg_max_ties_follow_the_preference():
    first, second = candidate("uniform_30", 0.5, 0.0, 0.0), candidate("log1p_30", 0.5, 0.0, 0.0)
    objective = {config_id(first): 0.3, config_id(second): 0.3}
    best, chosen = select([first, second], objective, lambda *_: 0.0, SCHEME_OBJECTS)
    assert best == chosen == second
    assert preference(second, SCHEME_OBJECTS) < preference(first, SCHEME_OBJECTS)


# ---- the test stage's guard ------------------------------------------------------------------------


@pytest.fixture
def tuned(setup, tmp_path):
    """collect and tune the validation stage; the result is also in tmp_path / "tuning.json"."""
    static_eval.run_collect(setup, "validation", ["all"])
    return static_eval.run_tune(setup, "validation", tmp_path / "tuning.json")


def as_defaults(monkeypatch, config):
    """Make a tuned config the code default, as the commit after tune does."""
    monkeypatch.setattr(static_recommend, "DEFAULT_PATH_WEIGHTS", dict(config["weights"]))
    monkeypatch.setattr(static_recommend, "DEFAULT_SEED_SCHEME", SeedScheme(**SCHEMES[config["scheme"]]))


def edited(tuning, path, edit):
    """A copy of the tuning result with one edit."""
    copy = json.loads(json.dumps(tuning))
    edit(copy)
    path.write_text(json.dumps(copy))
    return path


def row_of(tuning, config):
    return next(row for row in tuning["configs"] if row["id"] == config["id"])


def config_fields(row):
    return {key: row[key] for key in ("id", "scheme", "weights", "studio_normalization")}


def test_the_test_stage_takes_only_the_whole_tuned_grid_and_its_choice_as_the_code_default(
    setup, tuned, tmp_path, monkeypatch
):
    path = tmp_path / "tuning.json"
    with pytest.raises(SystemExit, match="code default"):
        static_eval.check_frozen(setup, path, "test")
    as_defaults(monkeypatch, tuned["chosen"])
    assert static_eval.check_frozen(setup, path, "test") == tuned
    named = {tuned["chosen"]["id"], tuned["best"]["id"], tuned["reference"]}
    other = next(row for row in tuned["configs"] if row["id"] not in named)
    edits = [
        ("another plan", lambda tuning: tuning["plan"].update(sha256="0" * 64)),
        ("another split", lambda tuning: tuning["split"].update(sha256="0" * 64)),
        ("whole grid", lambda tuning: tuning.update(stage="smoke")),
        ("whole grid", lambda tuning: tuning["configs"].remove(row_of(tuning, other))),
        ("whole grid", lambda tuning: tuning["configs"].append(row_of(tuning, other))),
        ("whole grid", lambda tuning: tuning["grid"].update(schemes=tuning["grid"]["schemes"][:1])),
        ("selection rule", lambda tuning: tuning.update(best=config_fields(other))),
        ("selection rule", lambda tuning: row_of(tuning, other).update({"NDCG@10": 1.0})),
        ("selection rule", lambda tuning: row_of(tuning, other).update(qualifies=not other["qualifies"])),
        ("selection rule", lambda tuning: row_of(tuning, other)["weights"].update(tag=3.0)),
    ]
    for index, (reason, edit) in enumerate(edits):
        with pytest.raises(SystemExit, match=reason):
            static_eval.check_frozen(setup, edited(tuned, tmp_path / f"edit{index}.json", edit), "test")
    as_defaults(monkeypatch, other)
    chosen = edited(tuned, tmp_path / "chosen.json", lambda tuning: tuning.update(chosen=config_fields(other)))
    with pytest.raises(SystemExit, match="selection rule"):
        static_eval.check_frozen(setup, chosen, "test")
    with pytest.raises(SystemExit, match="does not exist"):
        static_eval.check_frozen(setup, tmp_path / "missing.json", "test")


def test_tune_takes_each_grid_scheme_once_and_the_test_stage_refuses_a_part_of_the_grid(setup, tmp_path, monkeypatch):
    output = tmp_path / "part.json"
    for schemes in (["log1p_10", "log1p_10"], ["log1p_10", "log1p_5"]):
        with pytest.raises(SystemExit, match="each once"):
            static_eval.run_tune(setup, "smoke", output, schemes)
    static_eval.run_collect(setup, "smoke", ["log1p_10", "uniform_all"])
    part = static_eval.run_tune(setup, "smoke", output, ["log1p_10"])
    assert part["grid"]["configs"] == 8
    as_defaults(monkeypatch, part["chosen"])
    assert static_eval.check_frozen(setup, output, "smoke") == part
    with pytest.raises(SystemExit, match="whole grid"):
        static_eval.check_frozen(setup, output, "test")


def test_the_test_command_needs_the_tuning_result_and_the_defaults_committed(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    tuning = tmp_path / "tuning.json"
    tuning.write_text("{}")
    uncommitted = set()
    monkeypatch.setattr(
        static_eval, "committed_file", lambda path: {"commit": None if Path(path).name in uncommitted else "0" * 40}
    )
    runs = []
    monkeypatch.setattr(static_eval, "run_test", lambda *args: runs.append(args))
    arguments = ["test", "--plan", str(setup.plan_path), "--tuning", str(tuning), "--cache-dir", str(setup.cache_root)]
    for name in ("plan.json", "tuning.json", "static_recommend.py"):
        uncommitted = {name}
        with pytest.raises(SystemExit, match=f"commit .*{name}"):
            static_eval.main(arguments)
        static_eval.main([*arguments, "--stage", "smoke"])
    uncommitted = set()
    static_eval.main(arguments)
    assert [args[1] for args in runs] == ["smoke"] * 3 + ["test"]
    with pytest.raises(SystemExit, match="does not exist"):
        static_eval.main([*arguments[:3], "--tuning", str(tmp_path / "none.json"), *arguments[5:]])


def test_a_cache_or_tuning_result_of_other_tag_or_studio_links_is_refused(setup, tuned, tmp_path, monkeypatch):
    as_defaults(monkeypatch, tuned["chosen"])
    assert static_eval.check_frozen(setup, tmp_path / "tuning.json", "test") == tuned
    monkeypatch.setattr(static_eval, "graph_links", lambda reader: "0" * 64)
    rebuilt = Setup.load(setup.plan_path, setup.cache_root, 1, READER)
    with pytest.raises(SystemExit, match="another graph"):
        static_eval.run_collect(rebuilt, "validation", ["log1p_10"])
    with pytest.raises(SystemExit, match="another graph"):
        static_eval.run_tune(rebuilt, "validation", tmp_path / "again.json")
    with pytest.raises(SystemExit, match="another graph"):
        static_eval.check_frozen(rebuilt, tmp_path / "tuning.json", "test")


# ---- the whole run ---------------------------------------------------------------------------------


@pytest.fixture
def run(setup, tuned, tmp_path, monkeypatch):
    """Freeze the tuned choice as the code defaults, collect the test stage and run it."""
    as_defaults(monkeypatch, tuned["chosen"])
    static_eval.run_collect(setup, "test", sorted({tuned["chosen"]["scheme"], "log1p_10"}))
    explanations = tmp_path / "explanations.json"
    test = static_eval.run_test(setup, "test", tmp_path / "tuning.json", tmp_path / "results", explanations)
    return tuned, test


def test_tune_writes_every_config_the_choice_and_c1(setup, run):
    tuning, _ = run
    assert tuning == json.loads(json.dumps(tuning))
    assert len(tuning["configs"]) == 48 + 1 and tuning["grid"]["configs"] == 48
    ids = [row["id"] for row in tuning["configs"]]
    assert tuning["chosen"]["id"] in ids and tuning["best"]["id"] in ids and ids[-1] == tuning["reference"]
    chosen = next(row for row in tuning["configs"] if row["id"] == tuning["chosen"]["id"])
    validation = static_plan.stage_cases(setup.split, static_plan.stage_players(setup.split, "validation"))
    assert chosen["qualifies"] and tuning["evaluations"] == len(validation)
    for row in tuning["configs"]:
        assert set(row) >= {"NDCG@10", "se", "HR@10", "to_best", "qualifies", "vs_reference"}
        assert set(row["vs_reference"]) >= {"delta", "lo", "hi", "se", "p"}
    pop_only = tuning["C1"]["checks"]["pop_only"]
    assert pop_only["agreement"] == 1.0 and pop_only["same_rank"] == pop_only["evaluations"]
    assert tuning["C1"]["status"] in ("pass", "fail") and tuning["plan"]["sha256"]
    played = len({appid for _, appid, _, _ in PLAYS_ALL})
    graph = {"players": setup.split["graph"]["players"], "played_games": played, "all_games": len(APPIDS)}
    assert tuning["graph"].items() >= graph.items() and tuning["graph"]["links_sha256"] == setup.graph["links_sha256"]
    assert tuning["wall_seconds"]["total"] >= 0


def test_the_test_stage_writes_every_row_contrast_and_claim(setup, run, tmp_path):
    tuning, test = run
    rows = [name for name in static_plan.ROW_NAMES if name != "graph_v0"]
    assert {row["method"] for row in test["estimates"]} == set(rows)
    assert len(test["estimates"]) == len(rows) * 2 * 3 * 6
    assert len(test["contrasts"]) == 4 * 2 * 3 * 6 and len(test["v0_subset"]["contrasts"]) == 1 * 2 * 3 * 6
    assert {row["method"] for row in test["v0_subset"]["estimates"]} == set(static_plan.ROW_NAMES)
    assert set(test["claims"]) == {"C1", "C2", "C3", "C4", "C5"} and test["claims"]["C5"]["status"] == "pending"
    assert test["claims"]["C3"]["tests"]["primary"]["ci_975"]["level"] == 0.975
    assert test["pop_only_agreement"]["agreement"] == 1.0
    assert test["code_defaults"]["weights"] == tuning["chosen"]["weights"] and test["graph"] == tuning["graph"]
    groups = {"hidden_under_1h", "hidden_1h_or_more", "hidden_valve", "hidden_not_valve"}
    assert set(test["descriptive"]["groups"]) == groups
    assert set(test["beyond_accuracy"]["rows"]) == set(static_plan.ROW_NAMES)
    cases = static_plan.stage_cases(setup.split, static_plan.stage_players(setup.split, "test"))
    with gzip.open(tmp_path / "results" / "static_test_v1_ranks.jsonl.gz", "rt") as handle:
        lines = [json.loads(line) for line in handle]
    assert [(line["player_id"], line["rep"]) for line in lines] == [(player_id, rep) for player_id, _, rep, _ in cases]
    v0_users = {player_id for players in static_plan.v0_players(setup.split, "test").values() for player_id in players}
    owned = {}
    for player_id, appid, _, _ in PLAYS_ALL:
        owned.setdefault(player_id, set()).add(appid)
    played = len({appid for _, appid, _, _ in PLAYS_ALL})
    for line in lines:
        v0 = {"graph_v0"} if line["rep"] == 0 and line["player_id"] in v0_users else set()
        assert set(line["ranks"]) == set(rows) | v0
        if line["rep"]:
            assert "top10" not in line
            continue
        assert set(line["top10"]) == set(line["ranks"])
        candidates = played - (len(owned[line["player_id"]]) - 1)
        for name, top in line["top10"].items():
            rank = line["ranks"][name]
            listed = top.index(line["hidden"]) + 1 if line["hidden"] in top else None
            assert len(top) <= 10 and listed == (rank if rank is not None and rank <= 10 else None)
            if name != "graph_v0":  # v0 has no popularity backfill, so its list can be shorter
                assert len(top) >= min(10, candidates)


def test_graph_rows_of_the_test_equal_recommend_games(setup, run):
    """The cached-record ranking is the API's ranking: run verify on the test cache."""
    _, test = run
    scheme = test["tuning"]["chosen"]["scheme"]
    report = static_eval.run_verify(setup, "test", [scheme])
    assert report[scheme]["top50_equal"] == report[scheme]["records"] == report[scheme]["cases"] > 0
    assert report[scheme]["rank_mismatches"] == [] and report[scheme]["errors"] == []
    sampled = static_eval.run_verify(setup, "test", [scheme], sample=5)[scheme]
    assert sampled["records"] == sampled["top50_equal"] == 5


def test_the_test_results_do_not_depend_on_the_run(setup, run, tmp_path):
    _, test = run
    explanations = tmp_path / "explanations.json"
    again = static_eval.run_test(setup, "test", tmp_path / "tuning.json", tmp_path / "again", explanations)
    for name in ("wall_seconds", "collection"):
        test.pop(name), again.pop(name)
    assert again == test
    first = gzip.decompress((tmp_path / "results" / "static_test_v1_ranks.jsonl.gz").read_bytes())
    assert gzip.decompress((tmp_path / "again" / "static_test_v1_ranks.jsonl.gz").read_bytes()) == first


def test_the_report_is_the_same_bytes_every_time(setup, run, tmp_path, capsys, monkeypatch):
    tuning, test = run
    c5 = {"status": "pass", "current": 1.0, "v0": 0.53, "v0_ci": [0.45, 0.61]}
    for lang in static_report.LANGUAGES:
        text = static_report.render(lang, tuning, test, c5)
        copies = json.loads(json.dumps(tuning)), json.loads(json.dumps(test)), dict(c5)
        assert text == static_report.render(lang, *copies)
        assert "graph_tuned" in text and "C5" in text
    assert static_report.render("ja", tuning, test, c5) != static_report.render("zh", tuning, test, c5)
    paths = ["--plan", str(setup.plan_path), "--tuning", str(tmp_path / "tuning.json")]
    paths += ["--test", str(tmp_path / "results" / "static_test_v1.json")]
    outputs = []
    for _ in range(2):
        static_eval.main(["report", "--lang", "zh", *paths, "--explanations", str(tmp_path / "none.json")])
        outputs.append(capsys.readouterr().out.encode())
    assert outputs[0] == outputs[1] and "C1" in outputs[0].decode()
    missing = ["--tuning", str(tmp_path / "no"), "--test", str(tmp_path / "no")]  # not the committed results
    with pytest.raises(SystemExit, match="neither"):
        static_eval.main(["report", "--lang", "ja", "--plan", str(setup.plan_path), *missing])


def test_the_claims_table_shows_the_post_stratified_v0_share_and_why_c5_is_unusable(run):
    tuning, test = run
    c5 = {
        "status": "pass",
        "current": 1.0,
        "v0": 0.74,
        "v0_ci": [0.665, 0.81],
        "v0_post_stratified": 0.69,
        "v0_post_stratified_ci": [0.6, 0.77],
    }
    assert "1.0000（v0 0.7400 [0.6650, 0.8100]; 事後層別 0.6900 [0.6000, 0.7700]）" in static_report.render(
        "ja", tuning, test, c5
    )
    assert "事后分层 0.6900 [0.6000, 0.7700]" in static_report.render("zh", tuning, test, c5)
    # A replicate without recommendations in a stratum of the post-stratified share leaves it without a CI.
    without_ci = dict(c5, v0_post_stratified_ci=None)
    assert "（v0 0.7400 [0.6650, 0.8100]; 事後層別 0.6900）" in static_report.render("ja", tuning, test, without_ci)
    unusable = static_report.render("ja", tuning, test, {"status": "error", "error": "made from uncommitted code"})
    assert "| C5 |" in unusable and "made from uncommitted code | 使えない |" in unusable


def test_git_state_keeps_the_first_changed_path_whole(monkeypatch):
    outputs = {"rev-parse": "abc123\n", "status": " M eval/x.py\n?? notes.txt\n?? new.py\nA  app/y.py\n"}

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=outputs[command[1]], stderr="")

    monkeypatch.setattr(static_eval.subprocess, "run", run)
    state = static_eval.git_state()
    assert state == {"commit": "abc123", "dirty": True, "changes": ["eval/x.py", "new.py", "app/y.py"]}


# ---- the committed plan ----------------------------------------------------------------------------


def test_the_plan_file_matches_the_runner_and_the_split():
    if not static_eval.DEFAULT_PLAN.exists():
        pytest.skip("eval/results/static_plan_v1.json is not written yet")
    plan = static_plan.load_plan(static_eval.DEFAULT_PLAN)
    assert plan["grid"]["configs"] == len(static_plan.grid_configs(plan)) == 480
    assert static_eval.file_sha256(static_eval.ROOT / plan["split"]["file"]) == plan["split"]["sha256"]
    schemes = static_plan.plan_schemes(plan)
    assert set(plan["grid"]["schemes"]) == set(schemes) and len(schemes) == 6
    reference = static_plan.reference_config(plan)
    assert reference["weights"] == {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}
    assert (reference["scheme"], reference["studio_normalization"]) == ("log1p_10", "max")
    assert set(plan["claims"]) == {"C1", "C2", "C3", "C4", "C5"}
    for pair in plan["contrasts"]["test_sample"] + plan["contrasts"]["v0_subset"]:
        assert set(pair) <= set(static_plan.ROW_NAMES)
    assert math.isclose(plan["statistics"]["bootstrap"]["level"], 0.95)
