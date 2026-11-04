"""Check on the real graph (read-only) that the path queries of GraphReader return exactly what the
statements they replaced returned. ReferenceReader keeps those statements: the single co-play
statement of commit aa90eb9, tag_concept_scores, game_popularity and games_by_appids of commit
ffa5fa3, and tag_concepts of commit ccb366c. What only the tag reason uses is read apart (see
ReferenceReader).

Players are taken per stratum of visible history (1, 2-3, 4-10, 11+ games once one is hidden) in the
order of sha256('reader-equivalence:' + player_id), and each hides the game with the smallest
sha256('reader-equivalence:' + player_id + ':' + appid). For each player these are compared with
Python == between GraphReader and ReferenceReader: the rows of each path query and of tag_concepts
(every field, in order), the whole collect_path_scores(), the combine() top 50, games_by_appids() of
those 50 and the model_dump() of recommend_games() in en and ja. Then the edge cases of the same player
(all games hidden, seeds outside played_appids, no played_appids, no seeds, an unknown seed), and once
those of an unknown player. The median seconds of each path query, games_by_appids(),
collect_path_scores() and recommend_games() (en) are printed for both readers. Exit code 1 on any
mismatch or broken precondition. A player whose reference statements fail with a transient error (the
database is shared) is reported as "reference failed" and not compared; exit code 2 if a reference
failed and no player was compared. With --all-seeds the reference only runs for players with at most
REFERENCE_SEED_LIMIT visible games: above that its co-play statement runs out of transaction memory.
--uniform --all-seeds is the shipped DEFAULT_SEED_SCHEME; without them the scheme is SeedScheme()
(w_s = ln(1 + hours), the 10 longest-played seeds for co-play), which exercises the seed weights.

    python -m eval.check_reader_equivalence --users-per-stratum 5
    python -m eval.check_reader_equivalence --users-per-stratum 5 --uniform --all-seeds
    python -m eval.check_reader_equivalence --players 0ebcdac87473bdce47327aef5ec69eeb --all-seeds
"""

import argparse
import hashlib
import logging
import math
import statistics
import sys
import time
from collections import defaultdict
from typing import Any, Callable

from neo4j.exceptions import TransientError

from app.api.static_recommend import (
    EVIDENCE_PER_PATH,
    PEER_SEED_GAMES,
    TAG_PREFERENCE_LIMIT,
    SeedScheme,
    collect_path_scores,
    combine,
    recommend_games,
)
from utils.graph_reader import GAME_COLUMNS, TAG_MAX_RANK, GraphReader, SeedGame, with_recommend_rate

# Visible games per stratum: (name, fewest, most).
STRATA = [("1", 1, 1), ("2-3", 2, 3), ("4-10", 4, 10), ("11+", 11, None)]
REFERENCE_SEED_LIMIT = 60
TOP_K = 50
PATH_QUERIES = ("co_play_scores", "tag_concepts", "tag_concept_scores", "studio_scores", "game_popularity")
TIMED = PATH_QUERIES + ("games_by_appids",)
UNKNOWN_PLAYER = "nobody"
UNKNOWN_APPID = "999999999"

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

# GraphReader.tag_concept_scores up to commit ffa5fa3: the concept table and the candidates in one
# statement, the evidence as nested maps.
REFERENCE_TAG_CONCEPT_SCORES = (
    "MATCH (game:Game) WITH count(game) AS total "
    "UNWIND $seeds AS seed_row "
    "MATCH (seed:Game {appid: seed_row.appid})-[seed_tag:HAS_TAG]->(t:Tag) "
    "WHERE NOT t:Feature AND (seed_tag.rank IS NULL OR seed_tag.rank <= $max_rank) "
    "WITH DISTINCT total, seed, seed_row.weight AS seed_weight, split(t.tag_id, ':')[1] AS concept "
    "ORDER BY seed_weight DESC, seed.appid ASC "
    "WITH total, concept, sum(seed_weight) AS preference, head(collect(seed)) AS via "
    "MATCH (tag:Tag) WHERE NOT tag:Feature AND split(tag.tag_id, ':')[1] = concept "
    "WITH total, concept, preference, via, tag ORDER BY tag.tag_id ASC "
    "WITH total, concept, preference, via, collect(tag) AS tags "
    "UNWIND tags AS tag "
    "MATCH (tag)<-[owner_tag:HAS_TAG]-(owner:Game) "
    "WHERE owner_tag.rank IS NULL OR owner_tag.rank <= $max_rank "
    "WITH total, concept, preference, via, tags, count(DISTINCT owner) AS document_frequency "
    "WITH concept, via, tags, preference * log(toFloat(total) / document_frequency) AS weight "
    "WHERE weight > 0 "
    "WITH concept, via, tags, weight ORDER BY weight DESC, concept ASC LIMIT $concept_limit "
    "UNWIND tags AS tag "
    "MATCH (tag)<-[game_tag:HAS_TAG]-(g:Game) "
    "WHERE (game_tag.rank IS NULL OR game_tag.rank <= $max_rank) AND NOT g.appid IN $played_appids "
    "WITH DISTINCT g, concept, weight, via, head(tags) AS label "
    "WITH g, collect({concept: concept, weight: weight, via: via, label: label}) AS matched "
    "WITH g, matched, size(reduce(seen = [], key IN "
    "[(g)-[own:HAS_TAG]->(own_tag:Tag) WHERE NOT own_tag:Feature "
    "AND (own.rank IS NULL OR own.rank <= $max_rank) | split(own_tag.tag_id, ':')[1]] "
    "| CASE WHEN key IN seen THEN seen ELSE seen + key END)) AS concept_count "
    "UNWIND matched AS match "
    "WITH g, concept_count, match, match.weight / sqrt(toFloat(concept_count)) AS term "
    "ORDER BY term DESC, match.concept ASC "
    "WITH g, concept_count, sum(term) AS score, count(match) AS match_count, "
    "collect({concept: match.concept, name: match.label.name, name_ja: match.label.name_ja, "
    "weight: match.weight, seed_appid: match.via.appid, seed_name: match.via.name, term: term})"
    "[..$evidence_limit] AS matches "
    "RETURN g.appid AS appid, score, concept_count, match_count, matches "
    "ORDER BY score DESC, appid ASC"
)

# GraphReader.tag_concepts up to commit ccb366c, before the holders and the document frequency.
REFERENCE_TAG_CONCEPTS = (
    "MATCH (game:Game) WITH count(game) AS total "
    "UNWIND $seeds AS seed_row "
    "MATCH (seed:Game {appid: seed_row.appid})-[seed_tag:HAS_TAG]->(t:Tag) "
    "WHERE NOT t:Feature AND (seed_tag.rank IS NULL OR seed_tag.rank <= $max_rank) "
    "WITH DISTINCT total, seed, seed_row.weight AS seed_weight, split(t.tag_id, ':')[1] AS concept "
    "ORDER BY seed_weight DESC, seed.appid ASC "
    "WITH total, concept, sum(seed_weight) AS preference, head(collect(seed)) AS via "
    "MATCH (tag:Tag) WHERE NOT tag:Feature AND split(tag.tag_id, ':')[1] = concept "
    "WITH total, concept, preference, via, tag ORDER BY tag.tag_id ASC "
    "WITH total, concept, preference, via, collect(tag) AS tags "
    "UNWIND tags AS tag "
    "MATCH (tag)<-[owner_tag:HAS_TAG]-(owner:Game) "
    "WHERE owner_tag.rank IS NULL OR owner_tag.rank <= $max_rank "
    "WITH total, concept, preference, via, tags, count(DISTINCT owner) AS document_frequency "
    "WITH concept, via, tags, preference * log(toFloat(total) / document_frequency) AS weight "
    "WHERE weight > 0 "
    "WITH concept, via, tags, weight ORDER BY weight DESC, concept ASC LIMIT $concept_limit "
    "RETURN concept, weight, via.appid AS seed_appid, via.name AS seed_name, head(tags).name AS name, "
    "head(tags).name_ja AS name_ja, [tag IN tags | tag.tag_id] AS tag_ids"
)

# The holders and document frequency of concepts, written apart from TAG_CONCEPTS: the seeds with an edge
# to a tag of the concept, by (w_s DESC, appid ASC), and the games with one.
REFERENCE_TAG_HOLDERS = (
    "UNWIND $concepts AS concept "
    "MATCH (tag:Tag) WHERE NOT tag:Feature AND tag.tag_id ENDS WITH ':' + concept "
    "OPTIONAL MATCH (tag)<-[edge:HAS_TAG]-(game:Game) WHERE edge.rank IS NULL OR edge.rank <= $max_rank "
    "WITH concept, count(DISTINCT game) AS document_frequency "
    "UNWIND $seeds AS seed_row "
    "OPTIONAL MATCH (seed:Game {appid: seed_row.appid}) "
    "WHERE EXISTS { (seed)-[edge:HAS_TAG]->(tag:Tag) WHERE NOT tag:Feature "
    "AND (edge.rank IS NULL OR edge.rank <= $max_rank) AND tag.tag_id ENDS WITH ':' + concept } "
    "WITH DISTINCT concept, document_frequency, seed, seed_row.weight AS weight "
    "ORDER BY weight DESC, seed.appid ASC "
    "RETURN concept, document_frequency, "
    "collect(CASE WHEN seed IS NOT NULL THEN {appid: seed.appid, name: seed.name, weight: weight} END) AS holders"
)

# GraphReader.game_popularity up to commit ffa5fa3: the player's edge looked up for every game.
REFERENCE_GAME_POPULARITY = (
    "MATCH (g:Game) WHERE NOT g.appid IN $played_appids "
    "RETURN g.appid AS appid, COUNT { (g)<-[:PLAYED]-() } "
    "- CASE WHEN EXISTS { (:Player {player_id: $player_id})-[:PLAYED]->(g) } THEN 1 ELSE 0 END AS players "
    "ORDER BY players DESC, appid ASC"
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


class ReferenceReader(GraphReader):
    """GraphReader with the statements its path queries replaced; games_by_appids reads played_count
    through the shared GAME_COLUMNS, which walks every PLAYED edge of the game.

    Those statements do not return what only the tag reason uses: the holders and document frequency of
    each concept and the `matched` concepts of each candidate. The holders and document frequency are read
    with REFERENCE_TAG_HOLDERS, `matched` is every match of the single tag statement, so that these fields
    are compared with == as well."""

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        return self.query(
            REFERENCE_CO_PLAY,
            player_id=player_id,
            seeds=seeds,
            played_appids=played_appids,
            evidence_limit=evidence_limit,
        )

    def tag_concepts(self, seeds, concept_limit):
        rows = self.query(REFERENCE_TAG_CONCEPTS, seeds=seeds, max_rank=TAG_MAX_RANK, concept_limit=concept_limit)
        facts = self.query(
            REFERENCE_TAG_HOLDERS, concepts=[row["concept"] for row in rows], seeds=seeds, max_rank=TAG_MAX_RANK
        )
        by_concept = {fact["concept"]: fact for fact in facts}
        return [
            dict(
                row,
                holders=by_concept[row["concept"]]["holders"],
                document_frequency=by_concept[row["concept"]]["document_frequency"],
            )
            for row in rows
        ]

    def tag_concept_scores(self, seeds, played_appids, concept_limit, evidence_limit, concepts=None):
        """The single statement reads its own concept table, so `concepts` is not used."""
        rows = self.query(
            REFERENCE_TAG_CONCEPT_SCORES,
            seeds=seeds,
            played_appids=played_appids,
            max_rank=TAG_MAX_RANK,
            concept_limit=concept_limit,
            evidence_limit=concept_limit,
        )
        return [
            dict(row, matches=row["matches"][:evidence_limit], matched=[match["concept"] for match in row["matches"]])
            for row in rows
        ]

    def game_popularity(self, player_id, played_appids):
        return self.query(REFERENCE_GAME_POPULARITY, player_id=player_id, played_appids=played_appids)

    def games_by_appids(self, appids):
        rows = self.query(
            f"UNWIND $appids AS wanted MATCH (g:Game {{appid: wanted}}) RETURN {GAME_COLUMNS}", appids=appids
        )
        position = {appid: index for index, appid in enumerate(appids)}
        return sorted((with_recommend_rate(row) for row in rows), key=lambda row: position[row["appid"]])


class Timed:
    """Forwards to a reader and keeps the rows and the seconds of the last call of each method in TIMED."""

    def __init__(self, reader: GraphReader) -> None:
        self.reader = reader
        self.rows: dict[str, Any] = {}
        self.seconds: dict[str, float] = {}

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.reader, name)
        if name not in TIMED:
            return method

        def timed(*args: Any) -> Any:
            start = time.perf_counter()
            self.rows[name] = method(*args)
            self.seconds[name] = time.perf_counter() - start
            return self.rows[name]

        return timed


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


def dump(recommendations: list[Any]) -> list[dict[str, Any]]:
    return [item.model_dump() for item in recommendations]


def player_results(
    reader: Timed, player_id: str, hidden: frozenset[str], scheme: SeedScheme
) -> tuple[dict[str, Any], dict[str, float]]:
    """Everything compared for one player, and the seconds of the methods in TIMED,
    collect_path_scores() and recommend_games() in en."""
    reader.rows, reader.seconds = {}, {}
    start = time.perf_counter()
    path_scores = collect_path_scores(reader, player_id, hidden, scheme)
    collect_seconds = time.perf_counter() - start
    ranked = combine(path_scores, TOP_K)
    reader.games_by_appids([item["appid"] for item in ranked])
    results = dict(reader.rows, collect_path_scores=path_scores)
    results[f"combine top {TOP_K}"] = ranked
    seconds = dict(reader.seconds, collect_path_scores=collect_seconds)
    start = time.perf_counter()
    results["recommend_games en"] = dump(recommend_games(reader, player_id, TOP_K, "en", hidden, scheme=scheme))
    seconds["recommend_games"] = time.perf_counter() - start
    results["recommend_games ja"] = dump(recommend_games(reader, player_id, TOP_K, "ja", hidden, scheme=scheme))
    return results, seconds


def edge_cases(player_id: str, played: list[dict[str, Any]]) -> dict[str, Callable[[GraphReader], Any]]:
    """Calls with inputs collect_path_scores() does not make for this player, by name. All of the
    player's games are seeds (the first PEER_SEED_GAMES for co-play)."""
    seeds = [SeedGame(appid=game["appid"], weight=math.log1p(game["hours"])) for game in played]
    with_unknown = [SeedGame(appid=UNKNOWN_APPID, weight=1.0)] + seeds
    appids = [game["appid"] for game in played]
    every_other = appids[::2]
    everything = frozenset(appids)

    def co_play(seeds: list[SeedGame], played_appids: list[str]) -> Callable[[GraphReader], Any]:
        return lambda reader: reader.co_play_scores(
            player_id, seeds[:PEER_SEED_GAMES], played_appids, EVIDENCE_PER_PATH
        )

    def tags(seeds: list[SeedGame], played_appids: list[str]) -> Callable[[GraphReader], Any]:
        return lambda reader: reader.tag_concept_scores(seeds, played_appids, TAG_PREFERENCE_LIMIT, EVIDENCE_PER_PATH)

    def popularity(played_appids: list[str]) -> Callable[[GraphReader], Any]:
        return lambda reader: reader.game_popularity(player_id, played_appids)

    return {
        "all games hidden: collect_path_scores": lambda reader: collect_path_scores(reader, player_id, everything),
        "all games hidden: recommend_games en": lambda reader: dump(
            recommend_games(reader, player_id, TOP_K, "en", everything)
        ),
        "all games hidden: recommend_games ja": lambda reader: dump(
            recommend_games(reader, player_id, TOP_K, "ja", everything)
        ),
        "seeds outside played_appids: co_play_scores": co_play(seeds, every_other),
        "seeds outside played_appids: tag_concept_scores": tags(seeds, every_other),
        "seeds outside played_appids: game_popularity": popularity(every_other),
        "no played_appids: co_play_scores": co_play(seeds, []),
        "no played_appids: tag_concept_scores": tags(seeds, []),
        "no played_appids: game_popularity": popularity([]),
        "no seeds: co_play_scores": co_play([], appids),
        "no seeds: tag_concepts": lambda reader: reader.tag_concepts([], TAG_PREFERENCE_LIMIT),
        "no seeds: tag_concept_scores": tags([], appids),
        "unknown seed: co_play_scores": co_play(with_unknown, appids),
        "unknown seed: tag_concepts": lambda reader: reader.tag_concepts(with_unknown, TAG_PREFERENCE_LIMIT),
        "unknown seed: tag_concept_scores": tags(with_unknown, appids),
    }


def unknown_player_cases(played: list[dict[str, Any]]) -> dict[str, Callable[[GraphReader], Any]]:
    """An unknown player, with the seeds and played_appids of a real one where they are needed."""
    seeds = [SeedGame(appid=game["appid"], weight=math.log1p(game["hours"])) for game in played]
    appids = [game["appid"] for game in played]
    return {
        "unknown player: collect_path_scores": lambda reader: collect_path_scores(reader, UNKNOWN_PLAYER),
        "unknown player: recommend_games en": lambda reader: dump(recommend_games(reader, UNKNOWN_PLAYER, TOP_K, "en")),
        "unknown player: recommend_games ja": lambda reader: dump(recommend_games(reader, UNKNOWN_PLAYER, TOP_K, "ja")),
        "unknown player: co_play_scores": lambda reader: reader.co_play_scores(
            UNKNOWN_PLAYER, seeds[:PEER_SEED_GAMES], appids, EVIDENCE_PER_PATH
        ),
        "unknown player: game_popularity": lambda reader: reader.game_popularity(UNKNOWN_PLAYER, appids),
    }


def different_results(new: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    names = dict.fromkeys([*new, *reference])
    return [name for name in names if new.get(name) != reference.get(name)]


def case_results(reader: GraphReader, cases: dict[str, Callable[[GraphReader], Any]]) -> dict[str, Any]:
    return {name: case(reader) for name, case in cases.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the path queries with the statements they replaced")
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--users-per-stratum", type=int, default=5)
    parser.add_argument("--all-seeds", action="store_true", help="every seed for co-play (peer_limit=None)")
    parser.add_argument("--uniform", action="store_true", help="w_s = 1 instead of ln(1 + hours)")
    parser.add_argument("--players", nargs="+", help="these players instead of the strata")
    args = parser.parse_args()
    logging.getLogger("neo4j").setLevel(logging.ERROR)  # UCSD games have no description properties

    scheme = SeedScheme(
        weighting="uniform" if args.uniform else SeedScheme().weighting,
        peer_limit=None if args.all_seeds else SeedScheme().peer_limit,
    )
    new_reader, reference_reader = GraphReader(args.graph), ReferenceReader(args.graph)
    new, reference = Timed(new_reader), Timed(reference_reader)
    broken = 0
    compared = rows_compared = cases_compared = mismatches = reference_failed = 0
    seconds: dict[str, dict[str, list[float]]] = {"reference": defaultdict(list), "new": defaultdict(list)}
    try:
        for name, cypher in PRECONDITIONS.items():
            count = new_reader.query(cypher)[0]["n"]
            broken += count
            print(f"{name}: {count}")
        rows = new_reader.query(
            "MATCH (p:Player) RETURN p.player_id AS player_id, COUNT { (p)-[:PLAYED]->() } AS games"
        )
        players = args.players or pick_players({row["player_id"]: row["games"] for row in rows}, args.users_per_stratum)
        print(f"graph={args.graph} scheme={scheme} players={len(players)}")
        for player_id in players:
            played = new_reader.played_games(player_id, [])
            hidden = hidden_game(player_id, [game["appid"] for game in played])
            visible = len(played) - len(hidden)
            line = f"{stratum_of(visible) or '-':5} {player_id} visible={visible:<5}"
            if args.all_seeds and visible > REFERENCE_SEED_LIMIT:
                _, new_seconds = player_results(new, player_id, hidden, scheme)
                print(f"{line} new {new_seconds['collect_path_scores']:6.2f} s  (no reference)")
                continue
            new_results, new_seconds = player_results(new, player_id, hidden, scheme)
            cases = edge_cases(player_id, played)
            new_cases = case_results(new_reader, cases)
            try:
                reference_results, reference_seconds = player_results(reference, player_id, hidden, scheme)
                reference_cases = case_results(reference_reader, cases)
            except TransientError as error:
                reference_failed += 1
                print(f"{line} reference failed: {error.code}", flush=True)
                continue
            different = different_results(new_results, reference_results)
            different += different_results(new_cases, reference_cases)
            path_rows = sum(len(new_results.get(name, [])) for name in PATH_QUERIES)
            compared += 1
            rows_compared += path_rows
            cases_compared += len(cases)
            mismatches += bool(different)
            for name, value in reference_seconds.items():
                seconds["reference"][name].append(value)
            for name, value in new_seconds.items():
                seconds["new"][name].append(value)
            verdict = "DIFFERENT: " + ", ".join(different) if different else "same"
            print(
                f"{line} rows={path_rows:<6} reference {reference_seconds['collect_path_scores']:6.2f} s  "
                f"new {new_seconds['collect_path_scores']:6.2f} s  {verdict}",
                flush=True,
            )
        if players:
            cases = unknown_player_cases(new_reader.played_games(players[0], []))
            new_cases = case_results(new_reader, cases)
            try:
                different = different_results(new_cases, case_results(reference_reader, cases))
            except TransientError as error:
                reference_failed += 1
                print(f"unknown player: reference failed: {error.code}")
            else:
                cases_compared += len(cases)
                mismatches += bool(different)
                print(f"unknown player: {'DIFFERENT: ' + ', '.join(different) if different else 'same'}")
    finally:
        new_reader.close()
        reference_reader.close()
    print(
        f"compared {compared} players ({rows_compared} path query rows) and {cases_compared} edge cases: "
        f"{mismatches} with a mismatch, {reference_failed} where the reference failed"
    )
    if compared:
        print(f"median seconds over {compared} players: reference -> new")
        for name in TIMED + ("collect_path_scores", "recommend_games"):
            reference_median = statistics.median(seconds["reference"][name])
            new_median = statistics.median(seconds["new"][name])
            print(f"  {name:20} {reference_median:6.3f} -> {new_median:6.3f}")
    if broken or mismatches:
        sys.exit(1)
    sys.exit(2 if reference_failed and not compared else 0)


if __name__ == "__main__":
    main()
