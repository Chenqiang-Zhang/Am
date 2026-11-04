"""Wall time of recommend_games() top 10 on the real graph, for README §7.5: the shipped defaults
(DEFAULT_PATH_WEIGHTS, DEFAULT_SEED_SCHEME: w_s = 1, every seed co-plays) against the previous ones (λ 1 / 1 /
0.5 / 0.1, w_s = ln(1 + hours), the 10 longest-played seeds co-play). Users: the first 5 validation users of
each stratum with their rep-0 hidden game, then the heavy players given with --players (each hides its first
game in the split's hide order). The two configurations alternate per user. Read-only; the times depend on
the machine and on what else the database is doing, so the load average is recorded with each call.

    python -m eval.static_latency
"""

import argparse
import json
import logging
import os
import statistics
import time
from pathlib import Path
from typing import Any, Mapping

from app.api.static_recommend import DEFAULT_PATH_WEIGHTS, DEFAULT_SEED_SCHEME, SeedScheme, recommend_games
from utils.graph_reader import GraphReader

from .static_eval import ROOT, environment, git_state, write_json
from .static_plan import stage_players
from .static_split import STRATA, hidden_appids, load_split

RESULTS = ROOT / "eval" / "results"
PER_STRATUM = 5
HEAVY_PLAYERS = ("10a1b4f57f0f6d305685143fafc79fdc", "2a957d3d973dc2e2a65efd68cc7f8ef9")
CONFIGS: dict[str, tuple[Mapping[str, float], SeedScheme]] = {
    "shipped": (DEFAULT_PATH_WEIGHTS, DEFAULT_SEED_SCHEME),
    "previous": ({"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}, SeedScheme("log_hours", 10)),
}


class TimingReader(GraphReader):
    """GraphReader that adds up the seconds spent in query() and counts the statements."""

    def __init__(self, graph_name: str) -> None:
        super().__init__(graph_name)
        self.query_seconds, self.queries = 0.0, 0

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            return super().query(cypher, **params)
        finally:
            self.query_seconds += time.perf_counter() - started
            self.queries += 1


def timed_calls(reader: TimingReader, player_id: str, hidden: str, first: str) -> dict[str, Any]:
    order = list(CONFIGS) if first == "shipped" else list(reversed(CONFIGS))
    calls = {}
    for name in order:
        weights, scheme = CONFIGS[name]
        reader.query_seconds, reader.queries = 0.0, 0
        load = [round(value, 1) for value in os.getloadavg()]
        started = time.perf_counter()
        recommend_games(reader, player_id, 10, "ja", frozenset({hidden}), weights=weights, scheme=scheme)
        seconds = time.perf_counter() - started
        calls[name] = {
            "seconds": round(seconds, 3),
            "query_seconds": round(reader.query_seconds, 3),
            "queries": reader.queries,
            "load_average": load,
        }
    return calls


def times(calls: Mapping[str, Mapping[str, Any]]) -> str:
    return "  ".join(f"{name} {call['seconds']:6.2f} s" for name, call in calls.items())


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for name in STRATA:
        stratum_rows = [row for row in rows if row["stratum"] == name]
        result[name] = {
            config: {
                "median": round(statistics.median(row["calls"][config]["seconds"] for row in stratum_rows), 2),
                "max": round(max(row["calls"][config]["seconds"] for row in stratum_rows), 2),
            }
            for config in CONFIGS
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--split", type=Path, default=RESULTS / "static_split_v1.json")
    parser.add_argument(
        "--players", nargs="*", default=list(HEAVY_PLAYERS), help="heavy players timed after the strata"
    )
    parser.add_argument("--output", type=Path, default=RESULTS / "static_latency_v1.json")
    args = parser.parse_args()
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    git = git_state()
    split = load_split(args.split)
    players = stage_players(split, "validation")
    hidden = {record["player_id"]: record["hidden"][0] for name in STRATA for record in split["validation"][name]}
    reader = TimingReader(args.graph)
    rows, heavy = [], []
    try:
        reader.query("RETURN 1 AS one")
        index = 0
        for name in STRATA:
            for player_id in players[name][:PER_STRATUM]:
                visible = len(reader.played_games(player_id, [hidden[player_id]]))
                calls = timed_calls(reader, player_id, hidden[player_id], "shipped" if index % 2 == 0 else "previous")
                rows.append({"player_id": player_id, "stratum": name, "visible": visible, "calls": calls})
                print(f"{name:5} {player_id[:8]} visible {visible:5d}  {times(calls)}", flush=True)
                index += 1
        for number, player_id in enumerate(args.players):
            appids = [game["appid"] for game in reader.played_games(player_id, [])]
            first = hidden_appids(player_id, appids)[0]
            calls = timed_calls(reader, player_id, first, "shipped" if number % 2 == 0 else "previous")
            heavy.append({"player_id": player_id, "visible": len(appids) - 1, "calls": calls})
            print(f"heavy {player_id[:8]} visible {len(appids) - 1:5d}  {times(calls)}", flush=True)
    finally:
        reader.close()
    result = {
        "name": "static_latency_v1",
        "configs": {
            name: {"weights": dict(weights), "weighting": scheme.weighting, "peer_limit": scheme.peer_limit}
            for name, (weights, scheme) in CONFIGS.items()
        },
        "git": git,
        "environment": environment(),
        "users": rows,
        "heavy": heavy,
        "summary": summary(rows),
    }
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
