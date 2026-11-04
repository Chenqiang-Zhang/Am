"""Check on the real graph (read-only) that GraphReader.co_play_scores returns exactly what the single
co-play statement it replaced returned. That statement is kept here as REFERENCE_CO_PLAY.

Players are taken per stratum of visible history (1, 2-3, 4-10, 11+ games once one is hidden) in the
order of sha256('reader-equivalence:' + player_id), and each hides the game with the smallest
sha256('reader-equivalence:' + player_id + ':' + appid). For each player these are compared with
Python == against the same reader running the reference: the co_play_scores rows (every field, in
order), the whole collect_path_scores() and the combine() top 50. The co-play times of both are
printed. Exit code 1 on any mismatch. With --all-seeds the reference only runs for players with at
most REFERENCE_SEED_LIMIT visible games: above that it runs out of transaction memory.

    python -m eval.check_reader_equivalence --users-per-stratum 5
    python -m eval.check_reader_equivalence --users-per-stratum 5 --all-seeds
    python -m eval.check_reader_equivalence --players 0ebcdac87473bdce47327aef5ec69eeb --all-seeds
"""

import argparse
import hashlib
import logging
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

from app.api.static_recommend import SeedScheme, collect_path_scores, combine
from utils.graph_reader import GraphReader

# Visible games per stratum: (name, fewest, most).
STRATA = [("1", 1, 1), ("2-3", 2, 3), ("4-10", 4, 10), ("11+", 11, None)]
REFERENCE_SEED_LIMIT = 60
TOP_K = 50

# GraphReader.co_play_scores up to commit aa90eb9: one row per (seed, other player, game).
REFERENCE_CO_PLAY = (
    "MATCH (me:Player {player_id: $player_id}) "
    "UNWIND $seeds AS seed_row "
    "MATCH (seed:Game {appid: seed_row.appid})<-[:PLAYED]-(peer:Player)-[:PLAYED]->(g:Game) "
    "WHERE peer <> me "
    "WITH me, seed, seed_row.weight AS seed_weight, g, count(peer) AS co_players "
    "WHERE NOT g.appid IN $played_appids "
    "WITH g, seed, seed_weight, co_players, "
    "COUNT { (seed)<-[:PLAYED]-() } - CASE WHEN EXISTS { (me)-[:PLAYED]->(seed) } THEN 1 ELSE 0 END "
    "AS seed_players, "
    "COUNT { (g)<-[:PLAYED]-() } - CASE WHEN EXISTS { (me)-[:PLAYED]->(g) } THEN 1 ELSE 0 END "
    "AS game_players "
    "WITH g, game_players, seed, seed_weight, co_players, seed_players, "
    "seed_weight * co_players / sqrt(toFloat(seed_players) * game_players) AS term "
    "ORDER BY term DESC, seed.appid ASC "
    "WITH g, game_players, sum(term) AS score, count(seed) AS seed_count, "
    "collect({appid: seed.appid, name: seed.name, weight: seed_weight, co_players: co_players, "
    "seed_players: seed_players, term: term})[..$evidence_limit] AS seeds "
    "RETURN g.appid AS appid, score, game_players, seed_count, seeds "
    "ORDER BY score DESC, appid ASC"
)

# The count fields of co_play_scores cannot carry only if both are 0.
PRECONDITIONS = {
    "PLAYED edges not from a Player to a Game": (
        "MATCH (a)-[r:PLAYED]->(b) WHERE NOT (a:Player AND b:Game) RETURN count(r) AS n"
    ),
    "players with two PLAYED edges to one game": (
        "MATCH (p:Player)-[r:PLAYED]->(g) WITH p, g, count(r) AS edges WHERE edges > 1 RETURN count(*) AS n"
    ),
}


class TimedReader(GraphReader):
    """Keeps the rows and the seconds of its last co_play_scores call."""

    co_play_rows: list[Any] = []
    co_play_seconds = 0.0

    def reset(self) -> None:
        self.co_play_rows, self.co_play_seconds = [], 0.0

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        start = time.perf_counter()
        self.co_play_rows = self.compute_co_play(player_id, seeds, played_appids, evidence_limit)
        self.co_play_seconds = time.perf_counter() - start
        return self.co_play_rows

    def compute_co_play(self, player_id, seeds, played_appids, evidence_limit):
        return super().co_play_scores(player_id, seeds, played_appids, evidence_limit)


class ReferenceReader(TimedReader):
    def compute_co_play(self, player_id, seeds, played_appids, evidence_limit):
        return self.query(
            REFERENCE_CO_PLAY,
            player_id=player_id,
            seeds=seeds,
            played_appids=played_appids,
            evidence_limit=evidence_limit,
        )


def order_key(*parts: str) -> str:
    return hashlib.sha256(("reader-equivalence:" + ":".join(parts)).encode()).hexdigest()


def stratum_of(visible: int) -> str | None:
    for name, fewest, most in STRATA:
        if visible >= fewest and (most is None or visible <= most):
            return name
    return None


def pick_players(game_counts: dict[str, int], per_stratum: int) -> list[str]:
    players_by_stratum: dict[str | None, list[str]] = defaultdict(list)
    for player_id, games in game_counts.items():
        players_by_stratum[stratum_of(games - 1)].append(player_id)
    return [
        player_id
        for name, _, _ in STRATA
        for player_id in sorted(players_by_stratum[name], key=order_key)[:per_stratum]
    ]


def hidden_game(player_id: str, appids: list[str]) -> frozenset[str]:
    """The game with the smallest key, unless it is the player's only one."""
    if len(appids) < 2:
        return frozenset()
    return frozenset([min(appids, key=lambda appid: order_key(player_id, appid))])


def differences(
    new: TimedReader, reference: ReferenceReader, player_id: str, hidden: frozenset[str], scheme: SeedScheme
) -> list[str]:
    new_scores = collect_path_scores(new, player_id, hidden, scheme)
    reference_scores = collect_path_scores(reference, player_id, hidden, scheme)
    checks = {
        "co_play_scores": new.co_play_rows == reference.co_play_rows,
        "collect_path_scores": new_scores == reference_scores,
        f"combine top {TOP_K}": combine(new_scores, TOP_K) == combine(reference_scores, TOP_K),
    }
    return [name for name, same in checks.items() if not same]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare co_play_scores with the statement it replaced")
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--users-per-stratum", type=int, default=5)
    parser.add_argument("--all-seeds", action="store_true", help="SeedScheme(peer_limit=None) instead of the default")
    parser.add_argument("--players", nargs="+", help="these players instead of the strata")
    args = parser.parse_args()
    logging.getLogger("neo4j").setLevel(logging.ERROR)  # UCSD games have no description properties

    scheme = SeedScheme(peer_limit=None) if args.all_seeds else SeedScheme()
    new, reference = TimedReader(args.graph), ReferenceReader(args.graph)
    broken = 0
    compared = rows_compared = mismatches = 0
    seconds: dict[str, list[float]] = {"reference": [], "new": []}
    try:
        for name, cypher in PRECONDITIONS.items():
            count = new.query(cypher)[0]["n"]
            broken += count
            print(f"{name}: {count}")
        rows = new.query("MATCH (p:Player) RETURN p.player_id AS player_id, COUNT { (p)-[:PLAYED]->() } AS games")
        players = args.players or pick_players({row["player_id"]: row["games"] for row in rows}, args.users_per_stratum)
        print(f"graph={args.graph} scheme={scheme} players={len(players)}")
        for player_id in players:
            appids = [game["appid"] for game in new.played_games(player_id, [])]
            hidden = hidden_game(player_id, appids)
            visible = len(appids) - len(hidden)
            new.reset()
            reference.reset()
            line = f"{stratum_of(visible) or '-':5} {player_id} visible={visible:<5}"
            if args.all_seeds and visible > REFERENCE_SEED_LIMIT:
                collect_path_scores(new, player_id, hidden, scheme)
                print(f"{line} rows={len(new.co_play_rows):<6} new {new.co_play_seconds:6.2f} s  (no reference)")
                continue
            different = differences(new, reference, player_id, hidden, scheme)
            compared += 1
            rows_compared += len(new.co_play_rows)
            mismatches += bool(different)
            seconds["reference"].append(reference.co_play_seconds)
            seconds["new"].append(new.co_play_seconds)
            verdict = "DIFFERENT: " + ", ".join(different) if different else "same"
            print(
                f"{line} rows={len(new.co_play_rows):<6} reference {reference.co_play_seconds:6.2f} s  "
                f"new {new.co_play_seconds:6.2f} s  {verdict}"
            )
    finally:
        new.close()
        reference.close()
    print(f"compared {compared} players and {rows_compared} co-play rows: {mismatches} players with a mismatch")
    for name, values in seconds.items():
        if values:
            print(f"co-play {name}: median {statistics.median(values):.2f} s, max {max(values):.2f} s")
    sys.exit(1 if broken or mismatches else 0)


if __name__ == "__main__":
    main()
