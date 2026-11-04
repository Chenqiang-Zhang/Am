"""M4: eval/explanation_check.py, against an in-memory copy of the graph facts."""

import functools
import hashlib
import json
import random
import sys
from collections import Counter

import numpy as np
import pytest
from neo4j.exceptions import TransientError

import eval.evaluate
import eval.explanation_check as explanation_check
from app.api.models import Evidence
from app.api.static_recommend import SeedScheme, recommend_games
from eval.explanation_check import (
    VersionReport,
    check_evidence,
    check_recommendations,
    sample_players,
    validity_statistics,
)
from eval.rankers import build_graph_ranker, build_graph_v0_ranker
from eval.static_analysis import c5_claim
from eval.static_cache import with_retries
from eval.static_eval import code_defaults
from eval.static_split import STRATA, build_split, dumps, stratum
from tests.fake_graph import GAMES, PLAYS, FakeGraphReader, tag_ja
from utils.graph_reader import TAG_MAX_RANK


class FakeFacts:
    """The facts eval.explanation_check.CypherFacts reads, computed from the fixture."""

    def __init__(self, reader: FakeGraphReader) -> None:
        self.reader = reader

    def played(self, player_id):
        return {appid: self.reader.games[appid]["name"] for appid in self.reader.games_by_player.get(player_id, ())}

    def co_players(self, player_id, first_appid, second_appid):
        return len(self.reader.others(player_id, first_appid) & self.reader.others(player_id, second_appid))

    def players(self, player_id, appid):
        return len(self.reader.others(player_id, appid))

    def merged_tag_names(self, appid, tag_key):
        names = set()
        for current_tag_id, kind, name, rank in self.reader.tag_edges(appid):
            if current_tag_id.endswith(":" + tag_key) and kind != "feature" and (rank is None or rank <= TAG_MAX_RANK):
                names |= {name, tag_ja(name)} - {None}
        return names

    def has_tag_named(self, appid, name):
        return any(
            tag_name == name and (rank is None or rank <= TAG_MAX_RANK)
            for _, _, tag_name, rank in self.reader.tag_edges(appid)
        )

    def studio_links(self, appid, wanted_studio_id):
        links = [(name, role) for current, name, role in self.reader.studio_edges(appid) if current == wanted_studio_id]
        relations = {"DEVELOPED_BY" if role == "developer" else "PUBLISHED_BY" for _, role in links}
        return (links[0][0] if links else None), relations

    def studio_relations_named(self, appid, name):
        return {
            "DEVELOPED_BY" if role == "developer" else "PUBLISHED_BY"
            for _, studio_name, role in self.reader.studio_edges(appid)
            if studio_name == name
        }

    def studio_titles(self, wanted_studio_id):
        return sum(
            1
            for appid in self.reader.games
            if any(current == wanted_studio_id for current, _, _ in self.reader.studio_edges(appid))
        )


READER = FakeGraphReader()
FACTS = FakeFacts(READER)
PLAYERS = ["u1", "p1", "p2", "p3", "p4", "p5", "p6", "solo"]
HIDDEN = {"u1": "30", "p2": "60", "p6": "90", "p4": "80"}
# Every path on, so that every kind of assertion occurs (the tuned defaults switch studio and popular off).
ALL_PATHS = {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}
EVERY_PATH = functools.partial(recommend_games, weights=ALL_PATHS, scheme=SeedScheme())


def assertions_for(item, appid="50", player_id="u1", hidden=frozenset({"30"})):
    visible = {key: value for key, value in FACTS.played(player_id).items() if key not in hidden}
    return {name: holds for name, holds, _ in check_evidence(FACTS, player_id, visible, appid, item)}


def test_current_recommender_evidence_is_fully_valid(monkeypatch):
    monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", EVERY_PATH)
    reports = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current"])
    report = reports["current"]
    assert report.failures == []
    assert report.validity == 1.0
    assert set(report.assertions) == {
        "seed_played",
        "co_players",
        "seed_has_tag",
        "game_has_tag",
        "seed_studio",
        "game_studio",
        "studio_titles",
        "players",
    }
    assert 0 < report.explained < report.recommendations  # small catalogue: some lists end with popular games


def test_the_tuned_defaults_state_only_valid_evidence():
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current"])["current"]
    assert report.failures == []
    assert report.validity == 1.0
    assert {"seed_played", "co_players", "seed_has_tag", "game_has_tag"} <= set(report.assertions)


def test_v0_text_fails_where_the_proposal_says():
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["v0"])["v0"]
    assert report.validity is not None and report.validity < 1.0
    assert {failure["assertion"] for failure in report.failures} == {"co_players", "seed_has_tag"}
    # The peer count is not the number of players of the named game: p1 and p2 played Alpha and Echo.
    echo = next(failure for failure in report.failures if (failure["player_id"], failure["appid"]) == ("u1", "50"))
    assert (echo["evidence"]["count"], echo["graph"]) == (1, 2)
    # The second and third tags need not belong to the named game: Alpha has no Multiplayer tag.
    india = next(failure for failure in report.failures if (failure["player_id"], failure["appid"]) == ("p1", "90"))
    assert india["explanation"] == "Because you played Alpha. Shared tags: Shooter, Action, Multiplayer"
    assert india["evidence"]["bridge_name"] == "Multiplayer"


def recommended_evidence(path):
    for item in EVERY_PATH(READER, "u1", 13, "en", frozenset({"30"})):
        for evidence in item.evidence:
            if evidence.path == path:
                return item.appid, evidence
    raise AssertionError(path)


@pytest.mark.parametrize(
    "path,change,broken",
    [
        ("peer", {"count": 99}, "co_players"),
        ("peer", {"seed_appid": "30", "seed_name": "Charlie"}, "seed_played"),  # the hidden game
        ("peer", {"seed_name": "Alpha 2"}, "seed_played"),
        ("tag", {"bridge_id": "strategy", "bridge_name": "Strategy"}, "seed_has_tag"),
        ("studio", {"seed_role": "publisher"}, "seed_studio"),
        ("studio", {"candidate_role": "publisher"}, "game_studio"),
        ("studio", {"bridge_name": "Valve Corp"}, "seed_studio"),
        ("studio", {"count": 7}, "studio_titles"),
        ("popular", {"count": 4}, "players"),
    ],
)
def test_tampered_evidence_is_caught(path, change, broken):
    appid, evidence = recommended_evidence(path)
    assert all(assertions_for(evidence, appid).values())
    assert not assertions_for(evidence.model_copy(update=change), appid)[broken]


def test_a_merged_tag_may_be_spelled_like_either_of_its_tags():
    # Echo(50) has the genre "Free To Play", Foxtrot(60) the user tag "Free to Play".
    stated = Evidence(path="tag", seed_appid="50", seed_name="Echo", bridge_id="free_to_play", bridge_name="Free To Play")
    checked = assertions_for(stated, appid="60", player_id="p1", hidden=frozenset())
    assert checked["seed_has_tag"] and checked["game_has_tag"]
    wrong = stated.model_copy(update={"bridge_name": "Free to Win"})
    assert not assertions_for(wrong, appid="60", player_id="p1", hidden=frozenset())["game_has_tag"]
    elsewhere = stated.model_copy(update={"seed_appid": "10", "seed_name": "Alpha"})  # Alpha is not free to play
    assert not assertions_for(elsewhere, appid="60", player_id="p1", hidden=frozenset())["seed_has_tag"]


def test_feature_tags_and_far_ranks_do_not_count_as_owned():
    # Charlie(30) carries user tag Action only at rank 11; Lima(120) only a Feature tag.
    action = Evidence(path="tag", seed_appid="10", seed_name="Alpha", bridge_id="action", bridge_name="Action")
    assert not assertions_for(action, appid="30", hidden=frozenset())["game_has_tag"]
    feature = Evidence(path="tag", seed_appid="10", seed_name="Alpha", bridge_id="single_player", bridge_name="Single-player")
    assert not assertions_for(feature, appid="120", hidden=frozenset())["game_has_tag"]


def test_sampling_matches_run_zero_of_the_evaluation():
    play_records = READER.all_play_records()
    players, hidden = sample_players(play_records, 4, 0)
    assert len(players) == 4 and set(hidden) == set(players)
    assert all(hidden[player_id] in READER.games_by_player[player_id] for player_id in players)
    assert sample_players(play_records, 4, 0) == (players, hidden)


def test_command_line_prints_and_writes_a_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(explanation_check, "GraphReader", lambda graph_name: FakeGraphReader())
    monkeypatch.setattr(explanation_check, "CypherFacts", FakeFacts)
    monkeypatch.setattr(explanation_check, "graph_counts", fake_graph_counts)
    monkeypatch.setattr(explanation_check, "graph_links", fake_graph_links)
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["explanation_check", "--users", "5", "--output", str(output)])
    explanation_check.main()
    printed = capsys.readouterr().out
    assert "current:" in printed and "v0:" in printed
    report = json.loads(output.read_text())
    assert report["versions"]["current"]["recommendation_validity"] == 1.0
    assert report["versions"]["current"]["per_assertion"]["validity"] == 1.0
    assert set(report["versions"]) == {"current", "v0"}
    players, hidden = sample_players(FakeGraphReader().all_play_records(), 5, 0)
    assert [(row["player_id"], row["hidden"]) for row in report["users"]] == [(p, hidden[p]) for p in players]
    assert report["users_from"] == {"sample": "run 0 of eval/evaluate.py", "users": 5, "seed": 0}


# ---- per-recommendation validity and the split's users (claim C5) -------------------------------------

V1_WEIGHTS = ALL_PATHS  # every path on, whatever the defaults


def break_one_assertion(item):
    """A copy of the evidence item of which exactly one assertion is false."""
    return item.model_copy(update={"count": item.count + 1} if item.path == "popular" else {"seed_name": "Nothing"})


@pytest.mark.parametrize("in_headline", [True, False], ids=["headline_path", "other_path"])
def test_one_false_assertion_makes_the_recommendation_invalid(monkeypatch, in_headline):
    recommendations = recommend_games(READER, "u1", 10, "en", frozenset({"30"}), weights=V1_WEIGHTS)
    target = next(item for item in recommendations if len({evidence.path for evidence in item.evidence}) > 1)
    headline = target.evidence[0].path
    index = next(i for i, evidence in enumerate(target.evidence) if (evidence.path == headline) == in_headline)
    target.evidence[index] = break_one_assertion(target.evidence[index])
    monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", lambda *args: recommendations)
    report = check_recommendations(READER, FACTS, ["u1"], {"u1": "30"}, 10, ["current"])["current"]
    assert sum(report.assertions.values()) - sum(report.verified.values()) == 1
    assert (report.recommendations, report.valid, report.by_user) == (10, 9, {"u1": [10, 9]})
    assert report.recommendation_validity == 0.9 and report.validity > 0.9
    for path, row in report.summary()["headline_validity"].items():
        assert row["valid"] == row["recommendations"] - (path == headline and in_headline)


@pytest.mark.parametrize("weights", [None, V1_WEIGHTS, {"peer": 1.0}], ids=["defaults", "v1", "peer_only"])
def test_every_current_recommendation_holds(monkeypatch, weights):
    if weights is not None:
        recommend = functools.partial(recommend_games, weights=weights)
        monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", recommend)
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current"])["current"]
    assert report.recommendation_validity == 1.0 and report.valid == report.recommendations == 10 * len(PLAYERS)
    assert report.without_evidence == 0 and report.by_user == {player_id: [10, 10] for player_id in PLAYERS}
    assert all(row["validity"] == 1.0 for row in report.summary()["headline_validity"].values())


def test_a_v0_recommendation_with_any_false_claim_is_invalid():
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["v0"])["v0"]
    invalid = {(failure["player_id"], failure["appid"]) for failure in report.failures}
    assert 0 < len(invalid) == report.recommendations - report.valid
    assert report.recommendation_validity < report.validity  # the share of true assertions hides the false lists
    headline = report.summary()["headline_validity"]
    assert headline["peer"]["valid"] < headline["peer"]["recommendations"]
    # v0 names one path per recommendation, so its headline is all of its evidence.
    assert sum(row["valid"] for row in headline.values()) == report.valid


FIXTURE_STRATA = {player_id: "2-3" for player_id in PLAYERS} | {"p3": "4-10", "solo": "1"}  # visible games


def test_the_bootstrap_is_reproducible_and_exact_when_every_recommendation_holds():
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current"])["current"]
    statistics = validity_statistics(report, FIXTURE_STRATA)
    assert statistics == validity_statistics(report, FIXTURE_STRATA)
    assert statistics["recommendation_validity_ci"] == [1.0, 1.0]
    by_stratum = statistics["by_stratum"]
    assert {name: row["users"] for name, row in by_stratum.items()} == {"1": 1, "2-3": 6, "4-10": 1, "11+": 0}
    assert all(by_stratum[name]["recommendation_validity_ci"] == [1.0, 1.0] for name in ("1", "2-3", "4-10"))
    assert by_stratum["11+"]["recommendation_validity"] is None
    assert by_stratum["11+"]["recommendation_validity_ci"] is None


def test_the_bootstrap_resamples_users_within_strata_and_pools_their_recommendations():
    report = VersionReport(by_user={"a": [10, 10], "b": [10, 10], "c": [10, 0], "d": [10, 0], "e": [4, 1], "f": [2, 2]})
    two_strata = validity_statistics(report, {"a": "1", "b": "1", "c": "11+", "d": "11+"})
    # Every replicate draws two users of each stratum: 20 valid of 40 recommendations, whatever the draw.
    assert two_strata["recommendation_validity_ci"] == [0.5, 0.5]
    assert two_strata["by_stratum"]["1"]["recommendation_validity_ci"] == [1.0, 1.0]
    assert two_strata["by_stratum"]["11+"]["recommendation_validity_ci"] == [0.0, 0.0]
    pooled = validity_statistics(report, {"e": "4-10", "f": "4-10"})["by_stratum"]["4-10"]
    assert pooled["recommendation_validity"] == 3 / 6  # not the mean of the users' shares, 0.625
    assert pooled["recommendation_validity_ci"] == [1 / 4, 1.0]  # draws ee, ef and ff: 1/4, 1/2 and 1


def test_the_statistic_is_the_pooled_share_not_the_mean_of_the_users_shares():
    report = VersionReport(by_user={"a": [1, 1], "b": [9, 0]})
    statistics = validity_statistics(report, {"a": "11+", "b": "11+"}, level=0.4)
    # Draws aa, ab or ba, bb (1/4, 1/2, 1/4): pooled 1, 1/10, 0; the mean of the users' shares would give 1/2.
    assert statistics["by_stratum"]["11+"]["recommendation_validity_ci"] == [0.1, 0.1]
    assert statistics["recommendation_validity_ci"] == [0.1, 0.1]


def reference_statistics(by_user, strata, pool, replicates, seed, level):
    """validity_statistics written out: per stratum in order, one replicates x n_s draw of users."""
    rng = np.random.default_rng(seed)
    totals = np.zeros((replicates, 2))
    weighted, weights = np.zeros(replicates), {}
    for name in STRATA:
        users = [player_id for player_id in strata if strata[player_id] == name]
        if not users:
            continue
        draws = rng.integers(0, len(users), size=(replicates, len(users)))
        tallies = np.array([[sum(by_user[users[i]][j] for i in row) for j in (0, 1)] for row in draws])
        totals += tallies
        weights[name] = pool[name]
        weighted += pool[name] * tallies[:, 1] / tallies[:, 0]
    low, high = (1 - level) / 2, (1 + level) / 2
    whole = sum(weights.values())
    return (
        [float(np.quantile(totals[:, 1] / totals[:, 0], q)) for q in (low, high)],
        [float(np.quantile(weighted / whole, q)) for q in (low, high)],
    )


def test_the_bootstrap_is_the_written_out_stratified_resampling():
    rng = random.Random(11)
    names = ["1"] * 3 + ["2-3"] * 4 + ["11+"] * 5
    strata = {f"u{index}": name for index, name in enumerate(names)}
    by_user = {}
    for player_id in strata:
        recommendations = rng.randint(3, 10)
        by_user[player_id] = [recommendations, rng.randint(0, recommendations)]
    pool = {"1": 30, "2-3": 50, "4-10": 70, "11+": 600}
    report = VersionReport(by_user=by_user)
    first = validity_statistics(report, strata, replicates=400, level=0.9, pool=pool)
    second = validity_statistics(report, strata, replicates=400, level=0.9, pool=pool)
    assert first == second  # a new generator on every call
    pooled, post_stratified = reference_statistics(by_user, strata, pool, 400, 0, 0.9)
    assert first["recommendation_validity_ci"] == pytest.approx(pooled, abs=1e-12)
    assert first["recommendation_validity_post_stratified_ci"] == pytest.approx(post_stratified, abs=1e-12)
    shares = {
        name: sum(by_user[p][1] for p in strata if strata[p] == name) / sum(by_user[p][0] for p in strata if strata[p] == name)
        for name in ("1", "2-3", "11+")
    }
    assert first["post_stratification_weights"] == {"1": 30 / 680, "2-3": 50 / 680, "11+": 600 / 680}
    assert first["recommendation_validity_post_stratified"] == pytest.approx(
        sum(pool[name] * share for name, share in shares.items()) / 680, abs=1e-15
    )
    assert "recommendation_validity_post_stratified" not in validity_statistics(report, strata, replicates=400)


def test_post_stratification_weighs_the_strata_by_the_pool():
    report = VersionReport(by_user={"a": [10, 10], "b": [10, 10], "c": [10, 0], "d": [10, 0]})
    pool = {"1": 1, "2-3": 5, "4-10": 7, "11+": 3}
    statistics = validity_statistics(report, {"a": "1", "b": "1", "c": "11+", "d": "11+"}, pool=pool)
    assert statistics["recommendation_validity_ci"] == [0.5, 0.5]  # equal strata in the subset
    assert statistics["recommendation_validity_post_stratified"] == 1 / 4  # W_1 = 1 / (1 + 3) over the strata with users
    assert statistics["recommendation_validity_post_stratified_ci"] == [1 / 4, 1 / 4]


def test_a_recommendation_without_evidence_is_valid_and_counted_apart():
    report = VersionReport()
    bare = recommend_games(READER, "u1", 1, "en", frozenset({"30"}))[0].model_copy(update={"evidence": None})
    assert report.add("u1", bare, []) is True
    assert (report.recommendations, report.valid, report.without_evidence, report.explained) == (1, 1, 1, 0)
    assert report.summary()["headline_validity"] == {}


def test_sampled_users_have_the_stratum_of_their_visible_games():
    users = explanation_check.sampled_users(READER.all_play_records(), len(PLAYERS), 0)
    games = {player_id: len(appids) for player_id, appids in READER.games_by_player.items()}
    assert users and all(name == stratum(games[player_id] - 1) for player_id, name, _ in users)
    assert {name for _, name, _ in users} >= {"2-3"}


def test_a_user_whose_queries_fail_transiently_is_retried_and_counted_once(monkeypatch):
    calls = Counter()

    def flaky(reader, player_id, k, lang, hidden_appids):
        calls[player_id] += 1
        if player_id == "p2" and calls[player_id] == 1:
            raise TransientError("the database is busy")
        return EVERY_PATH(reader, player_id, k, lang, hidden_appids)

    monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", flaky)
    monkeypatch.setattr(explanation_check, "with_retries", functools.partial(with_retries, sleep=lambda seconds: None))
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current", "v0"])
    assert calls["p2"] == 2 and all(calls[player_id] == 1 for player_id in PLAYERS if player_id != "p2")
    monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", EVERY_PATH)
    once = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current", "v0"])
    for version in ("current", "v0"):
        assert report[version].by_user == once[version].by_user
        assert report[version].assertions == once[version].assertions


APPIDS = [game["appid"] for game in GAMES]


def synthetic_plays(players=28, seed=5):
    """The fixture's plays plus players with 2 to 13 games, so that every stratum occurs."""
    rng = random.Random(seed)
    plays = list(PLAYS)
    sizes = [2, 3, 4, 6, 9, 12, 13]
    for index in range(players):
        for appid in rng.sample(APPIDS, sizes[index % len(sizes)]):
            plays.append((f"s{index:02d}", appid, rng.choice([0.0, 0.5, 3.0, 25.0, 140.0]), 0.0))
    return plays


SPLIT_PLAYS = synthetic_plays()
SPLIT_SIZES = {
    "validation_size": 6,
    "test_sample_sizes": {"1": None, "2-3": None, "4-10": 5, "11+": 5},
    "subsets": {"v0": ("per_stratum", 3), "explanations": ("per_stratum", 2), "beyond_accuracy": ("proportional", 6)},
}


def fake_graph_links(reader):
    return hashlib.sha256(json.dumps(sorted(reader.games)).encode()).hexdigest()


def fake_graph_counts(reader):
    records = reader.all_play_records()
    return {
        "players": len({player_id for player_id, _, _ in records}),
        "games": len({appid for _, appid, _ in records}),
        "played": len({(player_id, appid) for player_id, appid, _ in records}),
    }


@pytest.fixture
def split_file(tmp_path):
    path = tmp_path / "split.json"
    path.write_text(dumps(build_split(FakeGraphReader(plays=SPLIT_PLAYS).all_play_records(), "fake", **SPLIT_SIZES)))
    return path


def run_command_line(monkeypatch, output, *arguments, plays=SPLIT_PLAYS, facts=FakeFacts):
    monkeypatch.setattr(explanation_check, "GraphReader", lambda graph_name: FakeGraphReader(plays=plays))
    monkeypatch.setattr(explanation_check, "CypherFacts", facts)
    monkeypatch.setattr(explanation_check, "graph_counts", fake_graph_counts)
    monkeypatch.setattr(explanation_check, "graph_links", fake_graph_links)
    argv = ["explanation_check", *(str(argument) for argument in arguments), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    explanation_check.main()
    return json.loads(output.read_text())


def test_split_users_are_the_explanations_subset_in_order_with_their_rep_0_games(split_file, tmp_path, monkeypatch):
    split = json.loads(split_file.read_text())
    subset = split["subsets"]["explanations"]
    rep_0 = {record["player_id"]: record["hidden"][0] for name in STRATA for record in split["test_sample"][name]}
    expected = [(player_id, name, rep_0[player_id]) for name in STRATA for player_id in subset[name]]
    assert all(subset[name] for name in STRATA)
    calls = []

    def recording(reader, player_id, k, lang, hidden_appids):
        calls.append((player_id, hidden_appids))
        return recommend_games(reader, player_id, k, lang, hidden_appids)

    monkeypatch.setitem(explanation_check.RECOMMENDERS, "current", recording)
    result = run_command_line(monkeypatch, tmp_path / "all.json", "--split", split_file)
    assert [(row["player_id"], row["stratum"], row["hidden"]) for row in result["users"]] == expected
    assert calls == [(player_id, frozenset({hidden})) for player_id, _, hidden in expected]
    assert result["users_from"] == {
        "split": str(split_file.resolve()),
        "split_sha256": hashlib.sha256(split_file.read_bytes()).hexdigest(),
        "subset": "explanations",
        "rep": 0,
        "limit_per_stratum": None,
    }
    for version in ("current", "v0"):
        by_stratum = result["versions"][version]["by_stratum"]
        assert {name: by_stratum[name]["users"] for name in STRATA} == Counter(name for _, name, _ in expected)
    first = run_command_line(monkeypatch, tmp_path / "first.json", "--split", split_file, "--limit-per-stratum", 1)
    expected_first = [next(user for user in expected if user[1] == name) for name in STRATA]
    assert [(row["player_id"], row["stratum"], row["hidden"]) for row in first["users"]] == expected_first


def test_a_split_of_another_graph_and_a_limit_without_a_split_are_refused(split_file, tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run_command_line(monkeypatch, tmp_path / "other.json", "--split", split_file, plays=PLAYS)
    with pytest.raises(SystemExit):
        run_command_line(monkeypatch, tmp_path / "sampled.json", "--limit-per-stratum", "1")


def test_the_same_input_gives_the_same_output_but_for_the_times(split_file, tmp_path, monkeypatch):
    first = run_command_line(monkeypatch, tmp_path / "first.json", "--split", split_file)
    second = run_command_line(monkeypatch, tmp_path / "second.json", "--split", split_file)
    assert first.pop("wall_seconds")["total"] >= 0 and second.pop("wall_seconds")["per_user"] >= 0
    assert first == second
    assert set(first) == {
        "graph",
        "users_from",
        "k",
        "lang",
        "bootstrap",
        "code_defaults",
        "environment",
        "git",
        "versions",
        "users",
    }
    assert first["bootstrap"]["B"] == 10_000 and first["bootstrap"]["seed"] == 0
    assert first["code_defaults"] == code_defaults()
    split = json.loads(split_file.read_text())
    assert first["graph"]["played_sha256"] == split["graph"]["played_sha256"]
    assert first["graph"]["links_sha256"] == fake_graph_links(FakeGraphReader(plays=SPLIT_PLAYS))
    pool = split["counts"]["test_pool"]
    for version in ("current", "v0"):
        by_stratum = first["versions"][version]["by_stratum"]
        present = [name for name in STRATA if by_stratum[name]["recommendations"]]  # v0 lists nothing for "1"
        weights = first["versions"][version]["post_stratification_weights"]
        assert weights == {name: pool[name] / sum(pool[s] for s in present) for name in present}
    assert tuple(first["versions"]["current"]["post_stratification_weights"]) == tuple(STRATA)


def test_a_graph_with_other_played_edges_but_the_same_counts_is_refused(split_file, tmp_path, monkeypatch):
    # One play moved to another game the player had not played: the same counts, other PLAYED edges.
    owned = {}
    for player_id, appid, _, _ in SPLIT_PLAYS:
        owned.setdefault(player_id, set()).add(appid)
    index = next(i for i, play in enumerate(SPLIT_PLAYS) if play[0] == "s00")
    player_id, appid, hours, last = SPLIT_PLAYS[index]
    other = next(candidate for candidate in APPIDS if candidate not in owned[player_id])
    moved = list(SPLIT_PLAYS)
    moved[index] = (player_id, other, hours, last)
    assert fake_graph_counts(FakeGraphReader(plays=moved)) == fake_graph_counts(FakeGraphReader(plays=SPLIT_PLAYS))
    with pytest.raises(SystemExit, match="PLAYED edges"):
        run_command_line(monkeypatch, tmp_path / "moved.json", "--split", split_file, plays=moved)


class MisspelledGames(FakeFacts):
    """FakeFacts that misspell every played game, so that every seed_played assertion is false."""

    def played(self, player_id):
        return {appid: f"{name}?" for appid, name in super().played(player_id).items()}


def test_c5_reads_the_written_result(split_file, tmp_path, monkeypatch):
    passing, failing = tmp_path / "passing.json", tmp_path / "failing.json"
    v0 = run_command_line(monkeypatch, passing, "--split", split_file)["versions"]["v0"]
    claim = c5_claim(passing, "explanations")
    assert claim == {
        "source": "explanations",
        "current": 1.0,
        "v0": v0["recommendation_validity"],
        "v0_ci": v0["recommendation_validity_ci"],
        "v0_post_stratified": v0["recommendation_validity_post_stratified"],
        "v0_post_stratified_ci": v0["recommendation_validity_post_stratified_ci"],
        "status": "pass",
    }
    assert v0["recommendation_validity"] < 1.0 and len(claim["v0_ci"]) == 2
    run_command_line(monkeypatch, failing, "--split", split_file, facts=MisspelledGames)
    claim = c5_claim(failing, "explanations")
    assert claim["status"] == "fail" and claim["current"] < 1.0


def test_c5_refuses_a_result_made_otherwise_than_the_test_run(split_file, tmp_path, monkeypatch):
    path = tmp_path / "explanations.json"
    result = run_command_line(monkeypatch, path, "--split", split_file)
    expected = {
        "split_sha256": hashlib.sha256(split_file.read_bytes()).hexdigest(),
        "k": 10,
        "code_defaults": code_defaults(),
        "links_sha256": result["graph"]["links_sha256"],
    }
    clean = dict(result, git={"commit": "0" * 40, "dirty": False, "changes": []})
    path.write_text(json.dumps(clean))
    assert c5_claim(path, "explanations", expected)["status"] == "pass"
    changes = {
        "made from another split": {"users_from": dict(clean["users_from"], split_sha256="0" * 64)},
        "not the whole explanations subset": {"users_from": dict(clean["users_from"], limit_per_stratum=1)},
        "k = 5": {"k": 5},
        "other code defaults": {"code_defaults": dict(clean["code_defaults"], weighting="log_hours")},
        "other tag or studio links": {"graph": dict(clean["graph"], links_sha256="0" * 64)},
        "uncommitted code": {"git": dict(clean["git"], dirty=True)},
    }
    for reason, change in changes.items():
        path.write_text(json.dumps(clean | change))
        claim = c5_claim(path, "explanations", expected)
        assert claim["status"] == "error" and reason in claim["error"], reason


def test_evaluate_runs_all_four_rankers(monkeypatch, capsys):
    monkeypatch.setattr(eval.evaluate, "GraphReader", lambda graph_name: FakeGraphReader())
    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate", "--users", "4", "--runs", "2", "--k", "3", "--rankers", "popularity", "item_knn", "graph_v0", "graph"],
    )
    eval.evaluate.main()
    printed = capsys.readouterr().out
    for name in ("popularity", "item_knn", "graph_v0", "graph"):
        assert f"\n{name}\n" in printed


def test_graph_rankers_take_weights_and_return_appids():
    peer_only = build_graph_ranker(READER, weights={"peer": 1.0})("u1", 3, {"30"})
    assert peer_only == [item.appid for item in recommend_games(READER, "u1", 3, "en", frozenset({"30"}), weights={"peer": 1.0})]
    assert len(build_graph_v0_ranker(READER)("u1", 3, {"30"})) == 3
