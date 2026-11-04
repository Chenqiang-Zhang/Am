"""Analyses that are not in the frozen plan (eval/results/static_plan_v1.json), run after the test results
for README §8.5 and §8.6. They are descriptive: none of them changes a claim.

- exploration: how many test users and test cases (user, hidden game) the exploratory runs made before the
  split and the plan used (their pairs are in eval/results/static_exploration_v1.json).
- without_explored: C2-C4 on the test sample without those users, from static_test_v1_ranks.jsonl.gz, with
  the plan's bootstrap (B, seed) and the test pool's weights W_s.
- hours: on the validation users (every rep), the peer-only NDCG@10 of the seed schemes log1p_10, uniform_10
  and uniform_all by the hidden game's hours (< 1 h, 1-10 h, >= 10 h), as ratios of post-stratified means
  like the plan's descriptive groups, with a paired bootstrap (B 10,000, seed 0). This one reads the
  validation path scores (python -m eval.static_eval collect --stage validation) and the graph.

    python -m eval.static_posthoc --cache-dir data/static_eval_cache
"""

import argparse
import functools
import gzip
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from utils.graph_reader import GraphReader

from .static_analysis import contrast_claims
from .static_cache import rank_hidden
from .static_eval import (
    DEFAULT_CACHE,
    DEFAULT_PLAN,
    ROOT,
    Setup,
    environment,
    file_sha256,
    git_state,
    load_play_records,
    open_cache,
    relative,
    write_json,
)
from .static_plan import make_config, spec, stage_cases, stage_players
from .static_split import STRATA
from .static_stats import LEVEL, PairedBootstrap, estimate_rows, metric_value, paired_bootstrap

RESULTS = ROOT / "eval" / "results"
EXPLORATION = RESULTS / "static_exploration_v1.json"
TEST_RANKS = RESULTS / "static_test_v1_ranks.jsonl.gz"
OUTPUT = RESULTS / "static_posthoc_v1.json"
HOURS_GROUPS: dict[str, Callable[[float], bool]] = {
    "under_1h": lambda hours: hours < 1,
    "1h_to_10h": lambda hours: 1 <= hours < 10,
    "10h_or_more": lambda hours: hours >= 10,
}
PEER_ONLY = {"peer_log1p_10": "log1p_10", "peer_uniform_10": "uniform_10", "peer_uniform_all": "uniform_all"}
HOURS_CONTRASTS = (
    ("peer_uniform_10", "peer_log1p_10"),  # w_s = 1 instead of ln(1 + hours), 10 co-play seeds
    ("peer_uniform_all", "peer_uniform_10"),  # every co-play seed instead of 10, w_s = 1
    ("peer_uniform_all", "peer_log1p_10"),  # both
)
HOURS_BOOTSTRAP = {"B": 10_000, "seed": 0}
Case = tuple[str, str, int, str]


def exploration(split_cases: Mapping[str, Sequence[Case]], sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per source and over all of them: pairs, users and pairs that are validation or test-sample cases."""
    users = {part: {player_id for player_id, _, _, _ in cases} for part, cases in split_cases.items()}
    pairs = {part: {(player_id, appid) for player_id, _, _, appid in cases} for part, cases in split_cases.items()}
    strata = strata_of(split_cases)

    def counts(explored: Sequence[tuple[str, str]]) -> dict[str, Any]:
        players = {player_id for player_id, _ in explored}
        test_pairs = [pair for pair in explored if pair in pairs["test_sample"]]
        return {
            "pairs": len(explored),
            "users": len(players),
            "validation_users": len(players & users["validation"]),
            "test_users": len(players & users["test_sample"]),
            "validation_cases": sum(pair in pairs["validation"] for pair in explored),
            "test_cases": len(test_pairs),
            "test_users_by_stratum": {
                name: sum(strata[player_id] == name for player_id in players & users["test_sample"]) for name in STRATA
            },
        }

    explored_all = sorted({tuple(pair) for source in sources for pair in source["pairs"]})
    return {
        "sources": {source["name"]: counts([tuple(pair) for pair in source["pairs"]]) for source in sources},
        "all": counts(explored_all),
    }


def strata_of(split_cases: Mapping[str, Sequence[Case]]) -> dict[str, str]:
    return {player_id: name for cases in split_cases.values() for player_id, name, _, _ in cases}


def read_test_ranks(path: Path) -> dict[str, dict[tuple[str, int], int | None]]:
    ranks: dict[str, dict[tuple[str, int], int | None]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            for row, rank in record["ranks"].items():
                ranks.setdefault(row, {})[(record["player_id"], record["rep"])] = rank
    return ranks


def without_explored(
    plan: Mapping[str, Any],
    split: Mapping[str, Any],
    ranks: Mapping[str, Mapping[tuple[str, int], int | None]],
    left_out: set[str],
) -> dict[str, Any]:
    """C2-C4 and the overall NDCG@10 of every test row on the test sample without the left-out users."""
    players = stage_players(split, "test")
    kept = {name: [player_id for player_id in players[name] if player_id not in left_out] for name in STRATA}
    cases = stage_cases(split, kept)
    rows = sorted(row for row in ranks if row != "graph_v0")
    by_player = {row: {} for row in rows}
    for player_id, _, rep, _ in cases:
        for row in rows:
            by_player[row].setdefault(player_id, []).append(ranks[row][(player_id, rep)])
    bootstrap_plan = plan["statistics"]["bootstrap"]
    bootstrap = paired_bootstrap(
        by_player, kept, split["counts"]["test_pool"], ("NDCG",), (10,), bootstrap_plan["B"], bootstrap_plan["seed"]
    )
    claims = contrast_claims(plan["claims"], bootstrap)
    return {
        "left_out_users": {name: len(players[name]) - len(kept[name]) for name in STRATA},
        "users": {name: len(kept[name]) for name in STRATA},
        "evaluations": len(cases),
        "claims": claims,
        "estimates": estimate_rows(bootstrap, rows, ("NDCG",), (10,), ("all",)),
    }


def hours_groups(
    ranks: Mapping[str, Mapping[str, Sequence[int]]],
    players: Mapping[str, Sequence[str]],
    cases: Sequence[Case],
    hours: Mapping[tuple[str, str], float],
    pool_counts: Mapping[str, int],
    replicates: int,
    seed: int,
    level: float = LEVEL,
) -> dict[str, Any]:
    """Per hours group: its post-stratified share of the user x reps, every row's NDCG@10 on it and the
    contrasts, each the ratio of post-stratified means (the metric summed over the user's reps in the group,
    and the number of them, each divided by the user's reps) as eval/static_analysis.descriptive_groups."""
    hidden: dict[str, list[str]] = {}
    for player_id, _, _, appid in cases:
        hidden.setdefault(player_id, []).append(appid)
    rows = list(ranks)
    specs = [(group, None) for group in HOURS_GROUPS] + [(group, row) for group in HOURS_GROUPS for row in rows]
    columns = [(group if row is None else f"{row}|{group}", "NDCG", 10) for group, row in specs]
    values = {}
    for name in STRATA:
        matrix = []
        for group, row in specs:
            line = []
            for player_id in players[name]:
                inside = [
                    rep for rep, appid in enumerate(hidden[player_id]) if HOURS_GROUPS[group](hours[(player_id, appid)])
                ]
                if row is None:
                    total = float(len(inside))
                else:
                    total = math.fsum(metric_value("NDCG", ranks[row][player_id][rep], 10) for rep in inside)
                line.append(total / len(hidden[player_id]))
            matrix.append(line)
        values[name] = np.array(matrix, dtype=float).reshape(len(specs), len(players[name]))
    bootstrap = PairedBootstrap(columns, values, pool_counts, replicates, seed)
    low, high = (1 - level) / 2, (1 + level) / 2
    result = {}
    for group in HOURS_GROUPS:
        share = bootstrap.estimate((group, "NDCG", 10), "all")
        shares = bootstrap.distribution((group, "NDCG", 10), "all")
        evaluations = sum(HOURS_GROUPS[group](hours[(player_id, appid)]) for player_id, _, _, appid in cases)
        item: dict[str, Any] = {"evaluations": evaluations, "share": share, "rows": {}, "contrasts": {}}
        for row in rows:
            item["rows"][row] = bootstrap.estimate((f"{row}|{group}", "NDCG", 10), "all") / share
        for first, second in HOURS_CONTRASTS:
            point = (
                bootstrap.estimate((f"{first}|{group}", "NDCG", 10), "all")
                - bootstrap.estimate((f"{second}|{group}", "NDCG", 10), "all")
            ) / share
            replicate = (
                bootstrap.distribution((f"{first}|{group}", "NDCG", 10), "all")
                - bootstrap.distribution((f"{second}|{group}", "NDCG", 10), "all")
            ) / shares
            lo, hi = np.quantile(replicate, [low, high])
            item["contrasts"][f"{first} - {second}"] = {"delta": point, "lo": float(lo), "hi": float(hi)}
        result[group] = item
    return result


def validation_hours(setup: Setup, workers: int) -> dict[str, Any]:
    split = setup.split
    players = stage_players(split, "validation")
    cases = stage_cases(split, players)
    schemes = sorted(set(PEER_ONLY.values()))
    cache = open_cache(setup, "validation", schemes)
    ranks: dict[str, dict[str, list[int]]] = {}
    for row, scheme in PEER_ONLY.items():
        config = make_config(scheme, {"peer": 1.0}, "seed_weight")
        ranks.update(rank_hidden(cache, scheme, cases, [spec(row, config)], workers))
    reader = setup.reader_factory()
    try:
        play_records = load_play_records(reader, split)
    finally:
        reader.close()
    wanted = {(player_id, appid) for player_id, _, _, appid in cases}
    hours = {(player_id, appid): played for player_id, appid, played in play_records if (player_id, appid) in wanted}
    groups = hours_groups(
        ranks, players, cases, hours, split["counts"]["test_pool"], HOURS_BOOTSTRAP["B"], HOURS_BOOTSTRAP["seed"]
    )
    return {"users": {name: len(players[name]) for name in STRATA}, "evaluations": len(cases), "groups": groups}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    git = git_state()
    setup = Setup.load(args.plan, args.cache_dir, args.workers, functools.partial(GraphReader, args.graph))
    split, plan = setup.split, setup.plan
    split_cases = {
        "validation": stage_cases(split, stage_players(split, "validation")),
        "test_sample": stage_cases(split, stage_players(split, "test")),
    }
    explored = json.loads(EXPLORATION.read_text(encoding="utf-8"))
    overlap = exploration(split_cases, explored["sources"])
    explored_players = {pair[0] for source in explored["sources"] for pair in source["pairs"]}
    test_users = {player_id for player_id, _, _, _ in split_cases["test_sample"]}
    result = {
        "name": "static_posthoc_v1",
        "post_hoc": "not in the frozen plan; descriptive only",
        "plan": {"path": relative(setup.plan_path), "sha256": file_sha256(setup.plan_path)},
        "split": {"path": relative(setup.split_path), "sha256": setup.split_sha256},
        "inputs": {
            relative(path): file_sha256(path) for path in (EXPLORATION, TEST_RANKS, RESULTS / "static_test_v1.json")
        },
        "git": git,
        "environment": environment(),
        "exploration": overlap,
        "without_explored": without_explored(plan, split, read_test_ranks(TEST_RANKS), explored_players & test_users),
        "hours": dict(
            validation_hours(setup, args.workers), bootstrap=HOURS_BOOTSTRAP, links_sha256=setup.graph["links_sha256"]
        ),
    }
    write_json(args.output, result)
    every = overlap["all"]
    users, by_stratum, cases = every["test_users"], every["test_users_by_stratum"], every["test_cases"]
    print(f"exploration: {users} test users {by_stratum}, {cases} test cases")
    claims = result["without_explored"]["claims"]
    for role, test in claims["C3"]["tests"].items():
        ci = test["ci_975"]
        interval = f"{ci['delta']:+.4f} [{ci['lo']:+.4f}, {ci['hi']:+.4f}]"
        print(f"without them, C3 {role}: {interval} Holm p {test['p_holm']:.4f} {test['status']}")
    print(f"without them, C2 {claims['C2']['status']}, C4 {claims['C4']['status']}")
    for group, item in result["hours"]["groups"].items():
        contrasts = ", ".join(
            f"{name} {row['delta']:+.4f} [{row['lo']:+.4f}, {row['hi']:+.4f}]"
            for name, row in item["contrasts"].items()
        )
        print(f"{group}: share {item['share']:.3f}; {contrasts}")


if __name__ == "__main__":
    main()
