"""The fixed validation/test split of the static recommendation evaluation.

Eligible players have played at least 2 games. A player's stratum is the number of games left visible
when one is hidden: 1, 2-3, 4-10 or 11+. Players are ordered by user key
sha256(salt + ":user:" + player_id), and every list below keeps that order:

- validation: the 1,000 smallest keys; only for tuning λ and the seed scheme
- test pool: every other eligible player; its stratum sizes N_s give the post-stratification
  weights W_s = N_s / N
- test sample: every pool player with 1 or 2-3 visible games, the first 1,500 with 4-10 and the
  first 2,500 with 11+
- subsets of the test sample, evaluated on rep 0 only: v0 (first 500 per stratum), explanations
  (first 75 per stratum) and beyond_accuracy (1,000 players split over the strata in proportion
  to W_s, first within each stratum)

A player's games are ordered by sha256(salt + ":hide:" + player_id + ":" + appid); rep r hides the
r-th of them, with min(3, #games) reps, and every evaluation hides one game. Nothing is random and
the order of the play records does not matter, so the same graph always gives the same file.

    python -m eval.static_split --output eval/results/static_split_v1.json
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from utils.graph_reader import GraphReader

from .rankers import PlayRecord, group_appids_by_player

NAME = "static_split_v1"
SALT = "static-rec-eval-v1"
STRATA = ("1", "2-3", "4-10", "11+")
VALIDATION_SIZE = 1000
# The first N test-pool players per stratum; None takes all of them.
TEST_SAMPLE_SIZES: dict[str, int | None] = {"1": None, "2-3": None, "4-10": 1500, "11+": 2500}
MAX_REPS = 3
# Subsets of the test sample: the first N per stratum, or N players split over the strata in
# proportion to the test pool.
SUBSETS: dict[str, tuple[str, int]] = {
    "v0": ("per_stratum", 500),
    "explanations": ("per_stratum", 75),
    "beyond_accuracy": ("proportional", 1000),
}
RULES = {
    "eligible": "players with at least 2 played games",
    "stratum": "visible games (games - 1): 1, 2-3, 4-10 or 11+",
    "user_key": "sha256(salt + ':user:' + player_id), smallest first; every player list is in this order",
    "validation": "the first sizes.validation eligible players",
    "test_pool": "every other eligible player; W_s = counts.test_pool[s] / counts.test_pool.all",
    "test_sample": "the first sizes.test_sample[s] test-pool players of each stratum (null: all of them)",
    "hide_key": "sha256(salt + ':hide:' + player_id + ':' + appid), smallest first",
    "reps": "min(sizes.reps, games); rep r hides the game with the r-th smallest hide key; one game per evaluation",
    "subsets": "test_sample players, rep 0 only: per_stratum takes the first n of each stratum; proportional "
    "splits n over the strata by the largest remainder of n * W_s and takes the first of each",
}
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results" / f"{NAME}.json"


def stratum(visible_games: int) -> str:
    if visible_games < 1:
        raise ValueError(f"a player needs at least one visible game, got {visible_games}")
    if visible_games == 1:
        return "1"
    if visible_games <= 3:
        return "2-3"
    if visible_games <= 10:
        return "4-10"
    return "11+"


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def user_key(player_id: str) -> str:
    return digest(f"{SALT}:user:{player_id}")


def hidden_appids(player_id: str, appids: Iterable[str]) -> list[str]:
    """The game each rep hides: the player's games by hide key, min(MAX_REPS, #games) of them."""
    keyed = sorted((digest(f"{SALT}:hide:{player_id}:{appid}"), appid) for appid in set(appids))
    return [appid for _, appid in keyed[:MAX_REPS]]


def proportional_allocation(total: int, sizes: Mapping[str, int]) -> dict[str, int]:
    """round(total · N_s / N) per stratum, by largest remainder so that the parts add up to total."""
    population = sum(sizes[name] for name in STRATA)
    if population == 0:
        return {name: 0 for name in STRATA}
    quotients = {name: divmod(total * sizes[name], population) for name in STRATA}
    allocation = {name: quotients[name][0] for name in STRATA}
    by_remainder = sorted(STRATA, key=lambda name: -quotients[name][1])
    for name in by_remainder[: total - sum(allocation.values())]:
        allocation[name] += 1
    return allocation


def assign_players(
    appids_by_player: Mapping[str, set[str]],
    validation_size: int = VALIDATION_SIZE,
    test_sample_sizes: Mapping[str, int | None] = TEST_SAMPLE_SIZES,
    subsets: Mapping[str, tuple[str, int]] = SUBSETS,
) -> dict[str, Any]:
    """Player ids per stratum, in user-key order, of validation, test_pool, test_sample and each subset."""
    eligible = [player_id for player_id, appids in appids_by_player.items() if len(appids) >= 2]
    ordered = sorted(eligible, key=lambda player_id: (user_key(player_id), player_id))
    validation, pool = ordered[:validation_size], ordered[validation_size:]
    assert not set(validation) & set(pool), "validation and test players overlap"

    def by_stratum(players: list[str]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {name: [] for name in STRATA}
        for player_id in players:
            grouped[stratum(len(appids_by_player[player_id]) - 1)].append(player_id)
        return grouped

    test_pool = by_stratum(pool)
    test_sample = {name: test_pool[name][: test_sample_sizes[name]] for name in STRATA}
    pool_counts = {name: len(test_pool[name]) for name in STRATA}
    chosen: dict[str, dict[str, list[str]]] = {}
    for subset, (kind, size) in subsets.items():
        if kind == "per_stratum":
            sizes = {name: size for name in STRATA}
        elif kind == "proportional":
            sizes = proportional_allocation(size, pool_counts)
        else:
            raise ValueError(f"unknown subset kind {kind!r}")
        chosen[subset] = {name: test_sample[name][: sizes[name]] for name in STRATA}
        for name in STRATA:
            assert set(chosen[subset][name]) <= set(test_sample[name]), f"{subset} is not in the test sample"
    return {"validation": by_stratum(validation), "test_pool": test_pool, "test_sample": test_sample, "subsets": chosen}


def stratum_counts(players: Mapping[str, list[Any]]) -> dict[str, int]:
    counts = {name: len(players[name]) for name in STRATA}
    counts["all"] = sum(counts.values())
    return counts


def played_digest(appids_by_player: Mapping[str, set[str]]) -> str:
    """sha256 of every (player, game) pair, so that a later run can tell whether the graph changed."""
    lines = "".join(
        f"{player_id}:{','.join(sorted(appids_by_player[player_id]))}\n" for player_id in sorted(appids_by_player)
    )
    return digest(lines)


def build_split(
    play_records: Iterable[PlayRecord],
    graph: str = "ucsd",
    validation_size: int = VALIDATION_SIZE,
    test_sample_sizes: Mapping[str, int | None] = TEST_SAMPLE_SIZES,
    subsets: Mapping[str, tuple[str, int]] = SUBSETS,
) -> dict[str, Any]:
    appids_by_player = group_appids_by_player(play_records)
    parts = assign_players(appids_by_player, validation_size, test_sample_sizes, subsets)
    test_pool = parts["test_pool"]
    population = {name: parts["validation"][name] + test_pool[name] for name in STRATA}

    def with_hidden(players: Mapping[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
        return {
            name: [
                {"player_id": player_id, "hidden": hidden_appids(player_id, appids_by_player[player_id])}
                for player_id in players[name]
            ]
            for name in STRATA
        }

    validation, test_sample = with_hidden(parts["validation"]), with_hidden(parts["test_sample"])
    return {
        "name": NAME,
        "salt": SALT,
        "graph": {
            "name": graph,
            "players": len(appids_by_player),
            "games": len({appid for appids in appids_by_player.values() for appid in appids}),
            "played": sum(len(appids) for appids in appids_by_player.values()),
            "played_sha256": played_digest(appids_by_player),
        },
        "rules": RULES,
        "sizes": {
            "validation": validation_size,
            "test_sample": dict(test_sample_sizes),
            "reps": MAX_REPS,
            "subsets": {subset: {kind: size} for subset, (kind, size) in subsets.items()},
        },
        "counts": {
            "population": stratum_counts(population),
            "validation": stratum_counts(parts["validation"]),
            "test_pool": stratum_counts(test_pool),
            "test_sample": stratum_counts(parts["test_sample"]),
            "evaluations": {
                part: sum(len(record["hidden"]) for name in STRATA for record in records[name])
                for part, records in (("validation", validation), ("test_sample", test_sample))
            },
            "subsets": {subset: stratum_counts(players) for subset, players in parts["subsets"].items()},
        },
        "test_pool_sha256": digest("".join(f"{player_id}\n" for name in STRATA for player_id in test_pool[name])),
        "validation": validation,
        "test_sample": test_sample,
        "subsets": parts["subsets"],
    }


def dumps(split: Mapping[str, Any]) -> str:
    return json.dumps(split, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def load_split(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cases(split: Mapping[str, Any], part: str) -> list[tuple[str, str, int, str]]:
    """(player_id, stratum, rep, hidden appid) of every evaluation of a part: validation and test_sample
    use every rep, the subsets rep 0 only. Strata in order, players in user-key order."""
    if part in ("validation", "test_sample"):
        return [
            (record["player_id"], name, rep, appid)
            for name in STRATA
            for record in split[part][name]
            for rep, appid in enumerate(record["hidden"])
        ]
    first_hidden = {
        record["player_id"]: record["hidden"][0] for name in STRATA for record in split["test_sample"][name]
    }
    subset = split["subsets"][part]
    return [(player_id, name, 0, first_hidden[player_id]) for name in STRATA for player_id in subset[name]]


def players_by_stratum(split: Mapping[str, Any], part: str) -> dict[str, list[str]]:
    """The players of a part per stratum, in user-key order (the user order of eval/static_stats.py)."""
    if part in ("validation", "test_sample"):
        return {name: [record["player_id"] for record in split[part][name]] for name in STRATA}
    return {name: list(split["subsets"][part][name]) for name in STRATA}


def print_counts(split: Mapping[str, Any]) -> None:
    counts = split["counts"]
    rows = [(part, counts[part]) for part in ("population", "validation", "test_pool", "test_sample")]
    rows += [(f"subset {subset}", values) for subset, values in sorted(counts["subsets"].items())]
    print(f"{'':24}" + "".join(f"{name:>8}" for name in (*STRATA, "all")))
    for label, values in rows:
        print(f"{label:24}" + "".join(f"{values[name]:>8}" for name in (*STRATA, "all")))
    print("evaluations (user x rep): " + ", ".join(f"{part} {n}" for part, n in counts["evaluations"].items()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the validation/test split of the static evaluation")
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    reader = GraphReader(args.graph)
    try:
        play_records = reader.all_play_records()
    finally:
        reader.close()
    split = build_split(play_records, args.graph)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps(split), encoding="utf-8")
    print_counts(split)
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
