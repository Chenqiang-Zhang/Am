"""Re-check the evidence behind static recommendations with independent read-only Cypher.

Players are sampled as in run 0 of eval/evaluate.py (same --seed and --users give the same players and
hidden games). Every evidence item of the current recommender, and every claim of the v0 reason
text, becomes assertions that are tested against the graph with queries written apart from the
recommender's own:

- seed_played: the player played the seed, it is not hidden, and it has the stated name
- co_players: exactly `count` other players played both the seed and the game (peer)
- seed_has_tag, game_has_tag: both games carry the tag (tag). For the current recommender a genre
  and a user tag with the same name are one tag (names compared as in tag_id, so "Free To Play"
  and "Free to Play" are one name), Feature tags do not count and only edges with
  rank <= TAG_MAX_RANK or no rank count. For v0 it is a tag of that exact name, with v0's edges.
- seed_studio, game_studio: both games are linked to the studio in the stated role (studio).
  developer: DEVELOPED_BY; publisher: PUBLISHED_BY without DEVELOPED_BY; v0 states no role.
- studio_titles: the studio has exactly `count` games (studio)
- players: exactly `count` other players played the game (popular)

Evidence validity = verified assertions / all assertions.

    python -m eval.explanation_check --users 300 --seed 0 --output eval/results/explanations.json
"""

import argparse
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from app.api.models import Evidence, Recommendation
from app.api.static_recommend import recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
from kg.ids import to_snake_case
from utils.graph_reader import TAG_MAX_RANK, GraphReader

from .rankers import PlayRecord, group_appids_by_player

RECOMMENDERS: dict[str, Callable[..., list[Recommendation]]] = {"current": recommend_games, "v0": recommend_games_v0}
FAILURES_KEPT = 200


class GraphFacts(Protocol):
    def played(self, player_id: str) -> dict[str, str]: ...
    def co_players(self, player_id: str, first_appid: str, second_appid: str) -> int: ...
    def players(self, player_id: str, appid: str) -> int: ...
    def merged_tag_names(self, appid: str, tag_key: str) -> set[str]: ...
    def has_tag_named(self, appid: str, name: str) -> bool: ...
    def studio_links(self, appid: str, studio_id: str) -> tuple[str | None, set[str]]: ...
    def studio_relations_named(self, appid: str, name: str) -> set[str]: ...
    def studio_titles(self, studio_id: str) -> int: ...


class CypherFacts:
    """One small read-only query per fact, independent of the recommender's queries."""

    def __init__(self, reader: GraphReader) -> None:
        self.reader = reader

    def played(self, player_id: str) -> dict[str, str]:
        rows = self.reader.query(
            "MATCH (p:Player)-[:PLAYED]->(g:Game) WHERE p.player_id = $player_id "
            "RETURN g.appid AS appid, g.name AS name",
            player_id=player_id,
        )
        return {row["appid"]: row["name"] for row in rows}

    def co_players(self, player_id: str, first_appid: str, second_appid: str) -> int:
        rows = self.reader.query(
            "MATCH (first:Game {appid: $first}), (second:Game {appid: $second}) "
            "MATCH (first)<-[:PLAYED]-(other:Player)-[:PLAYED]->(second) WHERE other.player_id <> $player_id "
            "RETURN count(DISTINCT other) AS n",
            player_id=player_id,
            first=first_appid,
            second=second_appid,
        )
        return rows[0]["n"]

    def players(self, player_id: str, appid: str) -> int:
        rows = self.reader.query(
            "MATCH (other:Player)-[:PLAYED]->(g:Game {appid: $appid}) WHERE other.player_id <> $player_id "
            "RETURN count(DISTINCT other) AS n",
            player_id=player_id,
            appid=appid,
        )
        return rows[0]["n"]

    def merged_tag_names(self, appid: str, tag_key: str) -> set[str]:
        rows = self.reader.query(
            "MATCH (g:Game {appid: $appid})-[edge:HAS_TAG]->(t:Tag) "
            "WHERE t.tag_id ENDS WITH $suffix AND NOT t:Feature AND (edge.rank IS NULL OR edge.rank <= $max_rank) "
            "RETURN t.name AS name, t.name_ja AS name_ja",
            appid=appid,
            suffix=":" + tag_key,
            max_rank=TAG_MAX_RANK,
        )
        return {name for row in rows for name in (row["name"], row["name_ja"]) if name}

    def has_tag_named(self, appid: str, name: str) -> bool:
        rows = self.reader.query(
            "MATCH (g:Game {appid: $appid})-[edge:HAS_TAG]->(t:Tag {name: $name}) "
            "WHERE edge.rank IS NULL OR edge.rank <= $max_rank RETURN count(*) AS n",
            appid=appid,
            name=name,
            max_rank=TAG_MAX_RANK,
        )
        return rows[0]["n"] > 0

    def studio_links(self, appid: str, studio_id: str) -> tuple[str | None, set[str]]:
        rows = self.reader.query(
            "MATCH (g:Game {appid: $appid})-[link]->(s:Studio {studio_id: $studio_id}) "
            "RETURN s.name AS name, type(link) AS relation",
            appid=appid,
            studio_id=studio_id,
        )
        return (rows[0]["name"] if rows else None), {row["relation"] for row in rows}

    def studio_relations_named(self, appid: str, name: str) -> set[str]:
        rows = self.reader.query(
            "MATCH (g:Game {appid: $appid})-[link]->(s:Studio) WHERE s.name = $name RETURN type(link) AS relation",
            appid=appid,
            name=name,
        )
        return {row["relation"] for row in rows}

    def studio_titles(self, studio_id: str) -> int:
        rows = self.reader.query(
            "MATCH (s:Studio {studio_id: $studio_id})<-[link]-(g:Game) "
            "WHERE type(link) IN ['DEVELOPED_BY', 'PUBLISHED_BY'] RETURN count(DISTINCT g) AS n",
            studio_id=studio_id,
        )
        return rows[0]["n"]


def has_role(relations: set[str], role: str | None) -> bool:
    if role is None:
        return bool(relations)
    if role == "developer":
        return "DEVELOPED_BY" in relations
    return "PUBLISHED_BY" in relations and "DEVELOPED_BY" not in relations


def check_evidence(
    facts: GraphFacts, player_id: str, visible: dict[str, str], appid: str, item: Evidence
) -> list[tuple[str, bool, Any]]:
    """(assertion, holds, what the graph says) for one evidence item of the game `appid`."""
    results: list[tuple[str, bool, Any]] = []
    if item.path != "popular":
        actual_name = visible.get(item.seed_appid or "")
        results.append(("seed_played", actual_name is not None and actual_name == item.seed_name, actual_name))
    if item.path == "peer":
        actual = facts.co_players(player_id, item.seed_appid, appid) if item.seed_appid else None
        results.append(("co_players", actual == item.count, actual))
    elif item.path == "tag":
        for assertion, game in (("seed_has_tag", item.seed_appid), ("game_has_tag", appid)):
            if not game:
                results.append((assertion, False, None))
            elif item.bridge_id:
                # The game carries the merged tag, under the stated name or another spelling of it
                # (Steam's genre "Free To Play" and user tag "Free to Play" are one tag).
                names = facts.merged_tag_names(game, item.bridge_id)
                stated = item.bridge_name or ""
                holds = bool(names) and (stated in names or to_snake_case(stated) == item.bridge_id)
                results.append((assertion, holds, sorted(names)))
            else:
                found = facts.has_tag_named(game, item.bridge_name or "")
                results.append((assertion, found, found))
    elif item.path == "studio":
        for assertion, game, role in (
            ("seed_studio", item.seed_appid, item.seed_role),
            ("game_studio", appid, item.candidate_role),
        ):
            if not game:
                results.append((assertion, False, None))
            elif item.bridge_id:
                name, relations = facts.studio_links(game, item.bridge_id)
                holds = name == item.bridge_name and has_role(relations, role)
                results.append((assertion, holds, {"name": name, "relations": sorted(relations)}))
            else:
                relations = facts.studio_relations_named(game, item.bridge_name or "")
                results.append((assertion, has_role(relations, role), sorted(relations)))
        if item.bridge_id and item.count is not None:
            actual = facts.studio_titles(item.bridge_id)
            results.append(("studio_titles", actual == item.count, actual))
    else:
        actual = facts.players(player_id, appid)
        results.append(("players", actual == item.count, actual))
    return results


@dataclass
class VersionReport:
    recommendations: int = 0
    explained: int = 0  # recommendations whose headline evidence is not popularity
    headline_paths: Counter = field(default_factory=Counter)
    assertions: Counter = field(default_factory=Counter)
    verified: Counter = field(default_factory=Counter)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def validity(self) -> float | None:
        total = sum(self.assertions.values())
        return sum(self.verified.values()) / total if total else None

    def summary(self) -> dict[str, Any]:
        return {
            "recommendations": self.recommendations,
            "evidence_validity": self.validity,
            "assertions": sum(self.assertions.values()),
            "verified": sum(self.verified.values()),
            "by_assertion": {
                kind: {"assertions": self.assertions[kind], "verified": self.verified[kind]}
                for kind in sorted(self.assertions)
            },
            "explanation_coverage": self.explained / self.recommendations if self.recommendations else None,
            "headline_paths": dict(sorted(self.headline_paths.items())),
        }


def sample_players(play_records: list[PlayRecord], users: int, seed: int) -> tuple[list[str], dict[str, str]]:
    """The players and hidden games of run 0 of eval/evaluate.py with the same --users and --seed."""
    appids_by_player = group_appids_by_player(play_records)
    eligible = sorted(player_id for player_id, appids in appids_by_player.items() if len(appids) >= 2)
    if not eligible:
        raise SystemExit("no player has played two or more games in this graph")
    random_generator = random.Random(seed)
    players = random_generator.sample(eligible, min(users, len(eligible)))
    hidden = {player_id: random_generator.choice(sorted(appids_by_player[player_id])) for player_id in players}
    return players, hidden


def check_recommendations(
    reader: GraphReader,
    facts: GraphFacts,
    players: list[str],
    hidden: dict[str, str],
    k: int,
    versions: list[str],
    lang: str = "en",
) -> dict[str, VersionReport]:
    reports = {version: VersionReport() for version in versions}
    for player_id in players:
        hidden_appids = frozenset({hidden[player_id]}) if player_id in hidden else frozenset()
        visible = {appid: name for appid, name in facts.played(player_id).items() if appid not in hidden_appids}
        for version in versions:
            report = reports[version]
            for recommendation in RECOMMENDERS[version](reader, player_id, k, lang, hidden_appids):
                evidence = recommendation.evidence or []
                headline = evidence[0].path if evidence else "none"
                report.recommendations += 1
                report.headline_paths[headline] += 1
                report.explained += headline not in ("popular", "none")
                for item in evidence:
                    for assertion, holds, actual in check_evidence(facts, player_id, visible, recommendation.appid, item):
                        report.assertions[assertion] += 1
                        report.verified[assertion] += holds
                        if not holds and len(report.failures) < FAILURES_KEPT:
                            report.failures.append(
                                {
                                    "player_id": player_id,
                                    "appid": recommendation.appid,
                                    "assertion": assertion,
                                    "evidence": item.model_dump(),
                                    "graph": actual,
                                    "explanation": recommendation.explanation,
                                }
                            )
    return reports


def print_reports(reports: dict[str, VersionReport]) -> None:
    for version, report in reports.items():
        summary = report.summary()
        validity = summary["evidence_validity"]
        coverage = summary["explanation_coverage"]
        print(f"{version}: {summary['recommendations']} recommendations")
        print(
            f"  evidence validity {summary['verified']}/{summary['assertions']}"
            + (f" = {validity:.4f}" if validity is not None else "")
        )
        for kind, counts in summary["by_assertion"].items():
            print(f"    {kind}: {counts['verified']}/{counts['assertions']}")
        if coverage is not None:
            print(f"  explanation coverage (headline is not popularity) {coverage:.4f}")
        print("  headline paths " + ", ".join(f"{path}={count}" for path, count in summary["headline_paths"].items()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-check static recommendation evidence against the graph")
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--versions", nargs="+", choices=sorted(RECOMMENDERS), default=["current", "v0"])
    parser.add_argument("--output", type=Path, help="write the summary and the first failures as JSON")
    args = parser.parse_args()

    reader = GraphReader(args.graph)
    try:
        players, hidden = sample_players(reader.all_play_records(), args.users, args.seed)
        reports = check_recommendations(reader, CypherFacts(reader), players, hidden, args.k, args.versions)
    finally:
        reader.close()
    print(f"graph={args.graph} users={len(players)} seed={args.seed} k={args.k}")
    print_reports(reports)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "graph": args.graph,
            "users": len(players),
            "seed": args.seed,
            "k": args.k,
            "versions": {
                version: dict(report.summary(), failures=report.failures) for version, report in reports.items()
            },
        }
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
