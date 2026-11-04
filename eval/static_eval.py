"""The static recommendation evaluation of README §8, run as eval/results/static_plan_v1.json says.

    python -m eval.static_eval collect --stage validation --schemes all --workers 8
    python -m eval.static_eval tune --workers 8
    python -m eval.static_eval collect --stage test --schemes log1p_10,<tuned scheme> --workers 8
    python -m eval.static_eval test --workers 8
    python -m eval.static_eval report --lang ja

- collect: the path scores of every user x rep of a stage under each seed scheme, into the cache of
  eval/static_cache.py; resumable.
- tune: ranks every grid config and graph_v1_default on the validation cache, applies the selection
  rule and checks C1; writes static_tuning_v1.json.
- test: refuses to run unless the tuned config is the code default; ranks every test row (graph rows
  with combine() on the cache, v0 in workers, the baselines here) and writes static_test_v1.json and
  static_test_v1_ranks.jsonl.gz.
- verify: recommend_games() equals combine() on the cached records, and the fast hidden rank equals
  combine()'s for every planned config, for every cached record of a stage.
- report: README §8's tables from the result files (eval/static_report.py).

Stages: validation and test as in the split, and smoke: the 30 validation users with the smallest user
keys, whose results go to the cache directory. Item-KNN sums float32 in set order, so tune and test
restart themselves with PYTHONHASHSEED=0. Tune and test of the real stages need the plan committed.
"""

import argparse
import functools
import gzip
import hashlib
import inspect
import json
import logging
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from app.api import static_recommend
from app.api.static_recommend import PATHS, SeedScheme, recommend_games
from utils.graph_reader import GraphReader

from . import static_report
from .rankers import PlayRecord, build_item_knn_ranker, build_popularity_ranker, group_appids_by_player
from .static_analysis import (
    Ranked,
    agreement,
    beyond_accuracy,
    c1_claim,
    c5_claim,
    contrast_claims,
    descriptive_groups,
)
from .static_cache import (
    V0,
    V0_TOP,
    Case,
    Entry,
    HiddenRank,
    Key,
    Progress,
    Spec,
    StageCache,
    check_complete,
    collect,
    full_ranking,
    log,
    path_scores_from_record,
    rank_hidden,
    rank_with_combine,
    read_record,
    unordered_results,
    with_retries,
    worker_reader,
)
from .static_plan import (
    ROW_NAMES,
    Config,
    c1_configs,
    collect_targets,
    config_id,
    grid_configs,
    load_plan,
    make_config,
    plan_schemes,
    reference_config,
    row_configs,
    select,
    spec,
    stage_cases,
    stage_players,
    subset_players,
    v0_players,
)
from .static_split import STRATA, load_split, played_digest
from .static_stats import contrast_rows, estimate_rows, paired_bootstrap

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / "eval" / "results" / "static_plan_v1.json"
DEFAULT_CACHE = ROOT / "data" / "static_eval_cache"
TOP = 10
RESULT_FILES = ("tuning", "test", "explanations")
GRAPH_COUNTS = (
    "MATCH (p:Player) WHERE EXISTS { (p)-[:PLAYED]->() } WITH count(p) AS players "
    "MATCH (g:Game) WHERE EXISTS { (g)<-[:PLAYED]-() } WITH players, count(g) AS games "
    "MATCH ()-[r:PLAYED]->() RETURN players, games, count(r) AS played"
)
STUDIO_GAMES = (
    "MATCH (g:Game)-[:DEVELOPED_BY|PUBLISHED_BY]->(:Studio {studio_id: $studio_id}) RETURN DISTINCT g.appid AS appid"
)


@dataclass
class Setup:
    plan: dict[str, Any]
    plan_path: Path
    split: dict[str, Any]
    split_path: Path
    split_sha256: str
    cache_root: Path
    workers: int
    reader_factory: Callable[[], Any]

    @classmethod
    def load(cls, plan_path: Path, cache_root: Path, workers: int, reader_factory: Callable[[], Any]) -> "Setup":
        plan = load_plan(plan_path)
        split_path = ROOT / plan["split"]["file"]
        split_sha256 = file_sha256(split_path)
        if split_sha256 != plan["split"]["sha256"]:
            raise SystemExit(f"{split_path} is not the split of {plan_path}")
        split = load_split(split_path)
        return cls(plan, Path(plan_path), split, split_path, split_sha256, Path(cache_root), workers, reader_factory)


# ---- the cache and the frozen config ----------------------------------------------------------------


def cache_manifest(stage: str, split_sha256: str, targets: Mapping[str, SeedScheme | None]) -> dict[str, Any]:
    return {
        "stage": stage,
        "split_sha256": split_sha256,
        "targets": {
            name: {"recommender": "recommend_games_v0", "top": V0_TOP}
            if scheme is None
            else {"weighting": scheme.weighting, "peer_limit": scheme.peer_limit}
            for name, scheme in targets.items()
        },
        "scoring": {
            "TAG_PREFERENCE_LIMIT": static_recommend.TAG_PREFERENCE_LIMIT,
            "STUDIO_ROLE_WEIGHTS": static_recommend.STUDIO_ROLE_WEIGHTS,
        },
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def open_cache(setup: Setup, stage: str, names: Sequence[str], write: bool = False) -> StageCache:
    cache = StageCache(setup.cache_root, stage)
    cache.check_manifest(cache_manifest(stage, setup.split_sha256, collect_targets(setup.plan, names)), write)
    return cache


def code_defaults() -> dict[str, Any]:
    scheme = static_recommend.DEFAULT_SEED_SCHEME
    return {
        "weights": {path: float(static_recommend.DEFAULT_PATH_WEIGHTS.get(path, 0.0)) for path in PATHS},
        "weighting": scheme.weighting,
        "peer_limit": scheme.peer_limit,
        "studio_normalization": inspect.signature(static_recommend.combine).parameters["studio_normalization"].default,
    }


def check_frozen(setup: Setup, tuning_path: Path, stage: str) -> dict[str, Any]:
    """The tuning result the test runs with. Refuses unless it was made from this plan and split, over
    the whole grid on the validation users (for the test stage), and chose the code defaults."""
    if not Path(tuning_path).exists():
        raise SystemExit(f"{tuning_path} does not exist; run tune first")
    tuning = json.loads(Path(tuning_path).read_text(encoding="utf-8"))
    if tuning["plan"]["sha256"] != file_sha256(setup.plan_path):
        raise SystemExit(f"{tuning_path} was made from another plan than {setup.plan_path}")
    if tuning["split"]["sha256"] != setup.split_sha256:
        raise SystemExit(f"{tuning_path} was made from another split than {setup.split_path}")
    whole_grid = tuning["grid"]["configs"] == setup.plan["grid"]["configs"]
    if stage == "test" and (tuning["stage"] != "validation" or not whole_grid):
        raise SystemExit(f"{tuning_path} is not a tuning run over the whole grid on the validation users")
    chosen, defaults = tuning["chosen"], code_defaults()
    scheme = plan_schemes(setup.plan)[chosen["scheme"]]
    frozen = {
        "weights": chosen["weights"],
        "weighting": scheme.weighting,
        "peer_limit": scheme.peer_limit,
        "studio_normalization": chosen["studio_normalization"],
    }
    if frozen != defaults:
        raise SystemExit(
            f"the tuned config {frozen} is not the code default {defaults}; commit it as DEFAULT_PATH_WEIGHTS / "
            "DEFAULT_SEED_SCHEME before the test run"
        )
    return tuning


# ---- graph and run metadata -------------------------------------------------------------------------


def graph_counts(reader: Any) -> dict[str, int]:
    """Players with a game, games with a player and PLAYED edges, as in the split's graph block."""
    row = reader.query(GRAPH_COUNTS)[0]
    return {"players": row["players"], "games": row["games"], "played": row["played"]}


def studio_games(reader: Any, studio_id: str) -> set[str]:
    return {row["appid"] for row in reader.query(STUDIO_GAMES, studio_id=studio_id)}


def load_play_records(reader: Any, split: Mapping[str, Any]) -> list[PlayRecord]:
    """Every play record by (appid, player), so that the popularity baseline breaks ties by appid like
    combine(). Refuses a graph whose PLAYED edges are not the split's."""
    records = sorted(reader.all_play_records(), key=lambda record: (record[1], record[0]))
    if played_digest(group_appids_by_player(records)) != split["graph"]["played_sha256"]:
        raise SystemExit("the PLAYED edges are not those of the split; the graph changed")
    return records


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def git_state() -> dict[str, Any]:
    """HEAD, and whether the code differs from it: a changed tracked file or an untracked .py file."""
    try:
        commit, lines = git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=all").splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "changes": []}
    changes = [line[3:] for line in lines if not line.startswith("??") or line.endswith(".py")]
    return {"commit": commit, "dirty": bool(changes), "changes": changes[:20]}


def committed_file(path: Path) -> dict[str, Any]:
    """The file's sha256 and the last commit that changed it; no commit while it has local changes."""
    try:
        clean = not git("status", "--porcelain", "--", str(path))
        commit = git("log", "-1", "--format=%H", "--", str(path)) or None
    except (OSError, subprocess.CalledProcessError):
        clean, commit = False, None
    return {"path": relative(path), "sha256": file_sha256(path), "commit": commit if clean else None}


def relative(path: Path) -> str:
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
    }


class Timer:
    def __init__(self) -> None:
        self.started = self.last = time.perf_counter()
        self.seconds: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        now = time.perf_counter()
        self.seconds[stage] = round(now - self.last, 1)
        self.last = now

    def summary(self) -> dict[str, float]:
        return dict(self.seconds, total=round(time.perf_counter() - self.started, 1))


def read_result(path: Path) -> dict[str, Any] | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else None


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---- collect and verify -----------------------------------------------------------------------------


def run_collect(setup: Setup, stage: str, names: Sequence[str]) -> dict[str, Any]:
    targets = collect_targets(setup.plan, names)
    cache = open_cache(setup, stage, names, write=True)
    cases = stage_cases(setup.split, stage_players(setup.split, stage))
    v0_cases = stage_cases(setup.split, v0_players(setup.split, stage), reps=(0,)) if V0 in targets else []
    reader = setup.reader_factory()
    try:
        counts = graph_counts(reader)
    finally:
        reader.close()
    expected = {field: setup.split["graph"][field] for field in counts}
    if counts != expected:
        raise SystemExit(f"the graph {counts} is not the graph of the split {expected}")
    summary = collect(
        cache,
        targets,
        {name: v0_cases if scheme is None else cases for name, scheme in targets.items()},
        setup.reader_factory,
        setup.workers,
    )
    log(
        f"collect {stage}: wrote {summary['written']:,} of {summary['planned']:,} records in "
        f"{summary['wall_seconds']:,} s, {len(summary['failures'])} failed; "
        f"{summary['reused']} reused from an equivalent scheme; seconds per query run: {summary['seconds_per_query']}"
    )
    return summary


def verify_task(task: tuple[Entry, tuple[str, int | None], tuple[Spec, ...]]) -> tuple[bool, list[str]]:
    entry, (weighting, peer_limit), specs = task
    record = read_record(entry)
    path_scores = path_scores_from_record(record)
    scheme, hidden = SeedScheme(weighting, peer_limit), frozenset({entry.hidden})
    recommendations = with_retries(
        lambda: recommend_games(worker_reader(), entry.player_id, 50, "en", hidden, scheme=scheme),
        f"{entry.player_id} rep {entry.rep}",
    )
    api = [item.appid for item in recommendations]
    cached = [item["appid"] for item in static_recommend.combine(path_scores, 50)]
    table = HiddenRank(record)
    wrong = []
    for name, weights, normalization in specs:
        ranking = full_ranking(path_scores, weights, normalization)
        if table.rank(weights, normalization) != ranking.index(entry.hidden) + 1:
            wrong.append(name)
    return api == cached, wrong


def run_verify(setup: Setup, stage: str, names: Sequence[str], sample: int | None = None) -> dict[str, Any]:
    """For every cached record of the stage, also of an incomplete cache, or of `sample` user x reps spread
    over it: recommend_games()'s top 50 (code defaults) equals combine()'s on the record, and HiddenRank
    equals combine()'s full ranking for every planned config of the scheme."""
    targets = {name: scheme for name, scheme in collect_targets(setup.plan, names).items() if scheme is not None}
    cache = open_cache(setup, stage, list(targets))
    cases = stage_cases(setup.split, stage_players(setup.split, stage))
    if sample is not None and sample < len(cases):
        cases = [cases[index * len(cases) // sample] for index in range(sample)]
    planned = grid_configs(setup.plan) + [reference_config(setup.plan), *c1_configs(setup.plan).values()]
    report: dict[str, Any] = {}
    for name, scheme in targets.items():
        chosen = {config_id(config): config for config in planned if config["scheme"] == name}
        specs = tuple(spec(key, config) for key, config in chosen.items())
        listed = cache.entries(name)
        entries = [listed[(player_id, rep)] for player_id, _, rep, _ in cases if (player_id, rep) in listed]
        tasks = [(entry, (scheme.weighting, scheme.peer_limit), specs) for entry in entries]
        progress = Progress(f"verify {stage} {name}", len(tasks))
        equal, mismatches, errors = 0, [], []
        for task, result, error in unordered_results(verify_task, tasks, setup.workers, setup.reader_factory):
            key = (task[0].player_id, task[0].rep)
            if error is not None:
                errors.append(f"{key}: {type(error).__name__}: {error}")
            else:
                equal += result[0]
                mismatches += [(key, wrong) for wrong in result[1]]
                if not result[0]:
                    log(f"verify {name}: {key} recommend_games() differs from combine() on the record")
            progress.advance()
        report[name] = {
            "cases": len(cases),
            "records": len(tasks),
            "top50_equal": equal,
            "configs_per_record": len(specs),
            "rank_mismatches": [f"{key} {wrong}" for key, wrong in mismatches],
            "errors": errors,
        }
        log(f"verify {stage} {name}: {report[name]['top50_equal']}/{len(tasks)} top 50 equal, "
            f"{len(mismatches)} rank mismatches in {len(tasks) * len(specs):,}, {len(errors)} errors")
    return report


# ---- tune -------------------------------------------------------------------------------------------


def baseline_rankings(play_records: list[PlayRecord], cases: Sequence[Case]) -> dict[str, Ranked]:
    """Rank and rep-0 top 10 of popularity and Item-KNN over every played game."""
    rankers = {"popularity": build_popularity_ranker(play_records), "item_knn": build_item_knn_ranker(play_records)}
    everything = len({appid for _, appid, _ in play_records})
    results: dict[str, dict[Key, tuple[int, list[str] | None]]] = {name: {} for name in rankers}
    progress = Progress("baselines", len(cases))
    for player_id, _, rep, hidden in cases:
        for name, ranker in rankers.items():
            ranking = ranker(player_id, everything, {hidden})
            results[name][(player_id, rep)] = (ranking.index(hidden) + 1, ranking[:TOP] if rep == 0 else None)
        progress.advance()
    return results


def by_player(ranked: Ranked, cases: Sequence[Case]) -> dict[str, list[int | None]]:
    ranks: dict[str, list[int | None]] = {}
    for player_id, _, rep, _ in cases:
        ranks.setdefault(player_id, []).append(ranked[(player_id, rep)][0])
    return ranks


def baselines_and_graph(setup: Setup, cases: Sequence[Case]) -> tuple[Any, list[PlayRecord], dict[str, Any]]:
    reader = setup.reader_factory()
    try:
        play_records = load_play_records(reader, setup.split)
        extra = {
            "tag_ids_by_game": reader.tag_ids_by_game(),
            "studio_games": studio_games(reader, setup.plan["descriptive"]["studio_id"]),
        }
    finally:
        reader.close()
    return baseline_rankings(play_records, cases), play_records, extra


def run_tune(setup: Setup, stage: str, output: Path, schemes: Sequence[str] | None = None) -> dict[str, Any]:
    timer, plan, split = Timer(), setup.plan, setup.split
    schemes = list(plan["grid"]["schemes"] if schemes is None else schemes)
    grid, reference, checks = grid_configs(plan, schemes), reference_config(plan), c1_configs(plan)
    configs = {config_id(config): config for config in (*grid, reference, *checks.values())}
    needed = list(dict.fromkeys([*schemes, *(config["scheme"] for config in configs.values())]))
    cache = open_cache(setup, stage, needed)
    players = stage_players(split, stage)
    cases = stage_cases(split, players)
    ranks: dict[str, dict[str, list[int]]] = {}
    for scheme in needed:
        specs = [spec(name, config) for name, config in configs.items() if config["scheme"] == scheme]
        ranks.update(rank_hidden(cache, scheme, cases, specs, setup.workers))
    timer.mark("rank_configs")
    baselines, _, _ = baselines_and_graph(setup, cases)
    timer.mark("baselines")

    objective, selection = plan["objective"], plan["selection"]["bootstrap"]
    metric, k, group = objective["metric"], objective["k"], objective["group"]
    names = [config_id(config) for config in (*grid, reference)]
    bootstrap = paired_bootstrap(
        {name: ranks[name] for name in names},
        players,
        split["counts"]["test_pool"],
        metrics=sorted({metric, "HR"}),
        ks=(k,),
        replicates=selection["B"],
        seed=selection["seed"],
    )
    values = {name: bootstrap.estimate((name, metric, k), group) for name in names}

    def to(config: Config, other: Config) -> dict[str, Any]:
        return bootstrap.contrast((config_id(config), metric, k), (config_id(other), metric, k), group)

    best, chosen = select(grid, values, lambda config, other: to(config, other)["se"], plan_schemes(plan))
    rows = []
    for config in (*grid, reference):
        to_best = to(config, best)
        rows.append(
            {
                "id": config_id(config),
                **config,
                f"{metric}@{k}": values[config_id(config)],
                "se": bootstrap.summary((config_id(config), metric, k), group)["se"],
                f"HR@{k}": bootstrap.estimate((config_id(config), "HR", k), group),
                "to_best": {"delta": to_best["delta"], "se": to_best["se"]},
                "qualifies": config is not reference and to_best["delta"] >= -to_best["se"],
                "vs_reference": to(config, reference),
            }
        )
    timer.mark("selection")
    graph_ranks = {
        name: {(player_id, rep): ranks[config_id(config)][player_id][rep] for player_id, _, rep, _ in cases}
        for name, config in checks.items()
    }
    baseline_ranks = {name: {key: value[0] for key, value in ranked.items()} for name, ranked in baselines.items()}
    result = {
        "name": "static_tuning_v1",
        "stage": stage,
        "plan": committed_file(setup.plan_path),
        "split": {"path": relative(setup.split_path), "sha256": setup.split_sha256},
        "git": git_state(),
        "environment": environment(),
        "users": {name: len(players[name]) for name in STRATA},
        "evaluations": len(cases),
        "objective": dict(objective),
        "bootstrap": dict(selection, level=0.95),
        "grid": {"schemes": schemes, "weights": plan["grid"]["weights"], "configs": len(grid)},
        "schemes": {name: plan["schemes"][name] for name in needed},
        "reference": config_id(reference),
        "best": {"id": config_id(best), **best},
        "chosen": {"id": config_id(chosen), **chosen},
        "configs": rows,
        "C1": c1_claim(plan["claims"]["C1"], graph_ranks, baseline_ranks, max(plan["metrics"]["k"])),
        "collection": cache.runs(),
        "wall_seconds": timer.summary(),
    }
    write_json(output, result)
    log(f"tune {stage}: chose {config_id(chosen)}, arg-max {config_id(best)}, C1 {result['C1']['status']}")
    return result


# ---- test -------------------------------------------------------------------------------------------


def run_test(setup: Setup, stage: str, tuning_path: Path, output_dir: Path, explanations_path: Path) -> dict[str, Any]:
    timer, plan, split = Timer(), setup.plan, setup.split
    tuning = check_frozen(setup, tuning_path, stage)
    frozen = tuning["chosen"]
    chosen = make_config(frozen["scheme"], frozen["weights"], frozen["studio_normalization"])
    graph_rows = row_configs(chosen, reference_config(plan))
    schemes = list(dict.fromkeys(config["scheme"] for config in graph_rows.values()))
    players = stage_players(split, stage)
    cases = stage_cases(split, players)
    cache = open_cache(setup, stage, schemes)
    for scheme in schemes:
        check_complete(cache, scheme, cases)

    summary = run_collect(setup, stage, [V0])
    if summary["failures"]:
        raise SystemExit(f"v0 failed for {len(summary['failures'])} user x reps; run test again to retry them")
    v0: dict[Key, tuple[int | None, list[str]]] = {}
    for key, entry in check_complete(cache, V0, stage_cases(split, v0_players(split, stage), reps=(0,))).items():
        top = read_record(entry)["top"]
        v0[key] = (top.index(entry.hidden) + 1 if entry.hidden in top else None, top[:TOP])
    timer.mark("v0")
    results: dict[str, Ranked] = {}
    for scheme in schemes:
        specs = [spec(name, config) for name, config in graph_rows.items() if config["scheme"] == scheme]
        results.update(rank_with_combine(cache, scheme, cases, specs, setup.workers, TOP))
    timer.mark("graph_rows")
    baselines, play_records, extra = baselines_and_graph(setup, cases)
    results = {"popularity": baselines["popularity"], "item_knn": baselines["item_knn"], "graph_v0": v0, **results}
    timer.mark("baselines")

    statistics, metrics, ks = plan["statistics"], plan["metrics"]["names"], plan["metrics"]["k"]
    groups, pool_counts = statistics["groups"], split["counts"]["test_pool"]
    resampling = (statistics["bootstrap"]["B"], statistics["bootstrap"]["seed"])
    rows = [name for name in ROW_NAMES if name != "graph_v0"]
    ranks = {name: by_player(results[name], cases) for name in rows}
    bootstrap = paired_bootstrap(ranks, players, pool_counts, metrics, ks, *resampling)
    v0_subset = subset_players(split, stage, "v0")
    v0_cases = stage_cases(split, v0_subset, reps=(0,))
    v0_ranks = {name: by_player(results[name], v0_cases) for name in ROW_NAMES}
    v0_bootstrap = paired_bootstrap(v0_ranks, v0_subset, pool_counts, metrics, ks, *resampling)
    hidden = {(player_id, appid) for player_id, _, _, appid in cases}
    hours = {(player_id, appid): played for player_id, appid, played in play_records if (player_id, appid) in hidden}
    counts = Counter(appid for _, appid, _ in play_records)
    player_total = len({player_id for player_id, _, _ in play_records})
    beyond_cases = stage_cases(split, subset_players(split, stage, "beyond_accuracy"), reps=(0,))
    cutoff = max(ks)
    result = {
        "name": "static_test_v1",
        "stage": stage,
        "plan": committed_file(setup.plan_path),
        "split": {"path": relative(setup.split_path), "sha256": setup.split_sha256},
        "tuning": {"path": relative(tuning_path), "sha256": file_sha256(tuning_path), "chosen": tuning["chosen"]},
        "code_defaults": code_defaults(),
        "git": git_state(),
        "environment": environment(),
        "graph": dict(split["graph"], all_games=len(extra["tag_ids_by_game"])),
        "rows": {
            row["name"]: {"definition": row["definition"], **with_id(graph_rows.get(row["name"]))}
            for row in plan["test_rows"]
        },
        "users": {name: len(players[name]) for name in STRATA},
        "evaluations": len(cases),
        "estimates": estimate_rows(bootstrap, rows, metrics, ks, groups),
        "contrasts": contrast_rows(bootstrap, pairs(plan["contrasts"]["test_sample"]), metrics, ks, groups),
        "v0_subset": {
            "users": {name: len(v0_subset[name]) for name in STRATA},
            "estimates": estimate_rows(v0_bootstrap, ROW_NAMES, metrics, ks, groups),
            "contrasts": contrast_rows(v0_bootstrap, pairs(plan["contrasts"]["v0_subset"]), metrics, ks, groups),
        },
        "claims": {
            "C1": dict(tuning["C1"], stage=tuning["stage"]),
            **contrast_claims(plan["claims"], bootstrap),
            "C5": c5_claim(explanations_path, relative(explanations_path)),
        },
        "pop_only_agreement": agreement(
            {key: value[0] for key, value in results["pop_only"].items()},
            {key: value[0] for key, value in results["popularity"].items()},
            cutoff,
        ),
        "descriptive": descriptive_groups(
            plan["descriptive"],
            statistics["bootstrap"],
            ranks,
            players,
            cases,
            hours,
            extra["studio_games"],
            pool_counts,
        ),
        "beyond_accuracy": beyond_accuracy(
            plan["beyond_accuracy"], results, beyond_cases, extra["tag_ids_by_game"], counts, player_total
        ),
        "collection": StageCache(setup.cache_root, stage).runs(),
    }
    timer.mark("statistics")
    result["wall_seconds"] = timer.summary()
    write_json(Path(output_dir) / "static_test_v1.json", result)
    write_ranks(Path(output_dir) / "static_test_v1_ranks.jsonl.gz", results, cases)
    log(f"test {stage}: wrote static_test_v1.json and static_test_v1_ranks.jsonl.gz to {output_dir}")
    return result


def with_id(config: Config | None) -> dict[str, Any]:
    return {} if config is None else {"id": config_id(config), **config}


def pairs(contrasts: Sequence[Sequence[str]]) -> list[tuple[str, str]]:
    return [(first, second) for first, second in contrasts]


def write_ranks(path: Path, results: Mapping[str, Ranked], cases: Sequence[Case]) -> None:
    """One line per user x rep: every row's hidden rank, and at rep 0 every row's top 10."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
        for player_id, stratum, rep, hidden in cases:
            key = (player_id, rep)
            rows = [name for name in ROW_NAMES if key in results[name]]
            line: dict[str, Any] = {"player_id": player_id, "stratum": stratum, "rep": rep, "hidden": hidden}
            line["ranks"] = {name: results[name][key][0] for name in rows}
            if rep == 0:
                line["top10"] = {name: results[name][key][1] for name in rows}
            handle.write((json.dumps(line, separators=(",", ":")) + "\n").encode("ascii"))


# ---- command line -----------------------------------------------------------------------------------


def require_hash_seed(argv: Sequence[str]) -> None:
    if os.environ.get("PYTHONHASHSEED") != "0":
        environment = {**os.environ, "PYTHONHASHSEED": "0"}
        os.execve(sys.executable, [sys.executable, "-m", "eval.static_eval", *argv], environment)


def require_committed(plan_path: Path) -> None:
    if committed_file(plan_path)["commit"] is None:
        raise SystemExit(f"commit {plan_path} before tune or test on the real stages")


def output_path(setup: Setup, stage: str, output: str) -> Path:
    """A result file of the plan; smoke results go to the cache instead."""
    path = ROOT / setup.plan["outputs"][output]
    return path if stage != "smoke" else setup.cache_root / "smoke" / "results" / path.name


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The static recommendation evaluation (README §8)")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help: str, stages: Sequence[str], default: str | None) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help)
        sub.add_argument("--stage", choices=stages, default=default, required=default is None)
        sub.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
        sub.add_argument("--graph", default="ucsd")
        sub.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
        sub.add_argument("--workers", type=int, default=1)
        return sub

    every_stage = ("validation", "test", "smoke")
    collect_command = command("collect", "collect the path scores of a stage", every_stage, None)
    collect_command.add_argument("--schemes", required=True, help="plan schemes, all (the grid's), v0; comma-separated")
    tune_command = command("tune", "rank the grid on the validation cache", ("validation", "smoke"), "validation")
    tune_command.add_argument("--schemes", help="comma-separated; the grid's schemes by default")
    tune_command.add_argument("--output", type=Path)
    test_command = command("test", "rank every test row and write the results", ("test", "smoke"), "test")
    test_command.add_argument("--tuning", type=Path)
    test_command.add_argument("--output-dir", type=Path)
    verify_command = command("verify", "check combine() on the cache against recommend_games()", every_stage, "smoke")
    verify_command.add_argument("--schemes", required=True)
    verify_command.add_argument("--sample", type=int, help="check only this many user x reps, spread over the stage")
    report_command = commands.add_parser("report", help="README §8's tables from the result files")
    report_command.add_argument("--lang", choices=static_report.LANGUAGES, required=True)
    report_command.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    report_command.add_argument("--tuning", type=Path)
    report_command.add_argument("--test", type=Path)
    report_command.add_argument("--explanations", type=Path)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    if args.command == "report":
        plan = load_plan(args.plan)
        paths = {name: getattr(args, name) or ROOT / plan["outputs"][name] for name in RESULT_FILES}
        tuning, test = read_result(paths["tuning"]), read_result(paths["test"])
        if tuning is None and test is None:
            raise SystemExit(f"neither {paths['tuning']} nor {paths['test']} exists yet")
        c5 = c5_claim(paths["explanations"], relative(paths["explanations"]))
        sys.stdout.write(static_report.render(args.lang, tuning, test, c5))
        return

    if args.command in ("tune", "test"):
        require_hash_seed(arguments)
        if args.stage != "smoke":
            require_committed(args.plan)
    setup = Setup.load(args.plan, args.cache_dir, args.workers, functools.partial(GraphReader, args.graph))
    names = args.schemes.split(",") if getattr(args, "schemes", None) else None
    if args.command == "collect":
        if run_collect(setup, args.stage, names)["failures"]:
            raise SystemExit("some keys failed; run collect again to retry them")
    elif args.command == "verify":
        report = run_verify(setup, args.stage, names, args.sample)
        for item in report.values():
            if item["top50_equal"] != item["records"] or item["rank_mismatches"] or item["errors"]:
                raise SystemExit("verify found differences")
    elif args.command == "tune":
        run_tune(setup, args.stage, args.output or output_path(setup, args.stage, "tuning"), names)
    else:
        tuning = args.tuning or output_path(setup, args.stage, "tuning")
        output_dir = args.output_dir or output_path(setup, args.stage, "test").parent
        run_test(setup, args.stage, tuning, output_dir, ROOT / setup.plan["outputs"]["explanations"])


if __name__ == "__main__":
    main()
