"""A check of the post-stratified estimates of eval/static_eval.py: popularity and Item-KNN are cheap, so
their value over the whole test pool (every pool player, every rep) is computed directly and compared with
the test sample's estimate and 95% CI in eval/results/static_test_v1.json. Each CI should cover the pool
value; about 1 in 20 may miss by chance.

    python -m eval.static_pool_check
"""

import argparse
import json
import math
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from utils.graph_reader import GraphReader

from .rankers import PlayRecord, group_appids_by_player
from .static_eval import baseline_rankings, load_play_records
from .static_split import STRATA, assign_players, digest, hidden_appids
from .static_stats import KS, METRICS, user_value

RESULTS = Path(__file__).resolve().parent / "results"
METHODS = ("popularity", "item_knn")
Case = tuple[str, str, int, str]


def pool_cases(play_records: Sequence[PlayRecord], split: Mapping[str, Any]) -> tuple[dict[str, list[str]], list[Case]]:
    """The test pool per stratum and every (player, stratum, rep, hidden appid) of it, by the split's rules."""
    appids_by_player = group_appids_by_player(play_records)
    sizes = split["sizes"]
    subsets = {name: next(iter(kind.items())) for name, kind in sizes["subsets"].items()}
    pool = assign_players(appids_by_player, sizes["validation"], sizes["test_sample"], subsets)["test_pool"]
    if digest("".join(f"{player_id}\n" for name in STRATA for player_id in pool[name])) != split["test_pool_sha256"]:
        raise SystemExit("the test pool is not the split's")
    cases = [
        (player_id, name, rep, appid)
        for name in STRATA
        for player_id in pool[name]
        for rep, appid in enumerate(hidden_appids(player_id, appids_by_player[player_id]))
    ]
    return pool, cases


def pool_values(
    ranked: Mapping[str, Mapping[tuple[str, int], tuple[int, Any]]],
    pool: Mapping[str, list[str]],
    cases: Sequence[Case],
    metrics: Sequence[str] = METRICS,
    ks: Sequence[int] = KS,
) -> dict[tuple[str, str, int], float]:
    """Σ_s W_s · mean_s over the whole pool, W_s = N_s / N: the quantity the test sample estimates."""
    ranks: dict[str, dict[str, list[int]]] = {method: {} for method in ranked}
    for player_id, _, rep, _ in cases:
        for method, by_key in ranked.items():
            ranks[method].setdefault(player_id, []).append(by_key[(player_id, rep)][0])
    total = sum(len(pool[name]) for name in STRATA)
    values = {}
    for method in ranked:
        for metric in metrics:
            for k in ks:
                parts = []
                for name in STRATA:
                    if pool[name]:
                        mean = math.fsum(user_value(ranks[method][p], metric, k) for p in pool[name]) / len(pool[name])
                        parts.append(len(pool[name]) / total * mean)
                values[(method, metric, k)] = math.fsum(parts)
    return values


def compare(values: Mapping[tuple[str, str, int], float], test: Mapping[str, Any]) -> list[dict[str, Any]]:
    estimates = {
        (row["method"], row["metric"], row["k"]): row
        for row in test["estimates"]
        if row["group"] == "all" and row["method"] in METHODS
    }
    rows = []
    for key, value in values.items():
        row = estimates[key]
        rows.append(
            {
                "method": key[0],
                "metric": key[1],
                "k": key[2],
                "pool_value": value,
                "estimate": row["estimate"],
                "lo": row["lo"],
                "hi": row["hi"],
                "covered": row["lo"] <= value <= row["hi"],
            }
        )
    return rows


def file_sha256(path: Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--split", type=Path, default=RESULTS / "static_split_v1.json")
    parser.add_argument("--test", type=Path, default=RESULTS / "static_test_v1.json")
    parser.add_argument("--output", type=Path, default=RESULTS / "static_pool_check_v1.json")
    args = parser.parse_args()

    split = json.loads(args.split.read_text(encoding="utf-8"))
    test = json.loads(args.test.read_text(encoding="utf-8"))
    if test["split"]["sha256"] != file_sha256(args.split):
        raise SystemExit("the test result was made from another split")
    reader = GraphReader(args.graph)
    try:
        play_records = load_play_records(reader, split)
    finally:
        reader.close()
    pool, cases = pool_cases(play_records, split)
    ranked = baseline_rankings(play_records, cases)
    rows = compare(pool_values(ranked, pool, cases), test)
    result = {
        "name": "static_pool_check_v1",
        "split": {"path": str(args.split.name), "sha256": file_sha256(args.split)},
        "test": {"path": str(args.test.name), "sha256": file_sha256(args.test)},
        "pool": {name: len(pool[name]) for name in STRATA},
        "evaluations": len(cases),
        "rows": rows,
        "covered": sum(row["covered"] for row in rows),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row in rows:
        mark = "ok" if row["covered"] else "MISS"
        print(
            f"{row['method']:10s} {row['metric']}@{row['k']:<2d} pool {row['pool_value']:.4f}  "
            f"sample {row['estimate']:.4f} [{row['lo']:.4f}, {row['hi']:.4f}]  {mark}"
        )
    print(f"covered {result['covered']}/{len(rows)}")


if __name__ == "__main__":
    main()
