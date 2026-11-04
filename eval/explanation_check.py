"""Re-check the evidence behind static recommendations with independent read-only Cypher.

Users: with --split, the explanations subset of the split (75 test users per stratum, eval/static_split.py),
in the split's order, each with the game rep 0 hides; otherwise players sampled as in run 0 of
eval/evaluate.py (same --seed and --users give the same players and hidden games). Every evidence item
of the current recommender, and every claim of the v0 reason text, becomes assertions that are tested
against the graph with queries written apart from the recommender's own:

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

A recommendation is valid when every assertion of every evidence item holds (one without evidence asserts
nothing, so it is valid; their number is reported as without_evidence). Per version:

- recommendation validity (claim C5 of the plan): valid recommendations / recommendations, overall
  and per stratum, with 95% percentile CIs from a bootstrap that resamples users within each stratum:
  numpy default_rng(0) draws one B × n_s index matrix per stratum, strata in order (the same draws for
  every version), B = 10,000, and a replicate's share is its valid recommendations / recommendations
- headline validity per headline path (the path of the first evidence item): every assertion of the
  evidence items of that path holds
- per assertion, secondary: verified assertions / assertions. The versions make different numbers
  and kinds of assertions per recommendation, so these shares do not compare across versions.

    python -m eval.explanation_check --split eval/results/static_split_v1.json --k 10 \\
        --output eval/results/static_explanations_v1.json
"""

import argparse
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import numpy as np

from app.api.models import Evidence, Recommendation
from app.api.static_recommend import recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
from kg.ids import to_snake_case
from utils.graph_reader import TAG_MAX_RANK, GraphReader

from .rankers import PlayRecord, group_appids_by_player
from .static_cache import log
from .static_eval import Timer, file_sha256, git_state, graph_counts, relative, write_json
from .static_split import STRATA, cases, load_split, stratum
from .static_stats import LEVEL, REPLICATES, SEED, interval

RECOMMENDERS: dict[str, Callable[..., list[Recommendation]]] = {"current": recommend_games, "v0": recommend_games_v0}
FAILURES_KEPT = 200
SUBSET = "explanations"
PROGRESS_EVERY = 25
BOOTSTRAP = {
    "B": REPLICATES,
    "seed": SEED,
    "rng": "numpy.random.default_rng",
    "level": LEVEL,
    "interval": "percentile",
    "resampling": "users within each stratum; one B x n_s index matrix per stratum, strata in order, the same "
    "for every version",
    "statistic": "valid recommendations / recommendations over the resampled users",
}

User = tuple[str, str, str]  # (player_id, stratum, hidden appid)
Checked = tuple[Evidence, str, bool, Any]  # (evidence item, assertion, holds, what the graph says)


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


def check_recommendation(
    facts: GraphFacts, player_id: str, visible: dict[str, str], recommendation: Recommendation
) -> list[Checked]:
    """Every assertion of every evidence item of one recommendation."""
    return [
        (item, assertion, holds, actual)
        for item in recommendation.evidence or []
        for assertion, holds, actual in check_evidence(facts, player_id, visible, recommendation.appid, item)
    ]


@dataclass
class VersionReport:
    recommendations: int = 0
    valid: int = 0  # recommendations whose every assertion holds
    without_evidence: int = 0
    explained: int = 0  # recommendations whose headline evidence is not popularity
    headline_paths: Counter = field(default_factory=Counter)
    headline_valid: Counter = field(default_factory=Counter)  # every assertion of the headline path holds
    assertions: Counter = field(default_factory=Counter)
    verified: Counter = field(default_factory=Counter)
    by_user: dict[str, list[int]] = field(default_factory=dict)  # player_id -> [recommendations, valid]
    seconds: Counter = field(default_factory=Counter)  # wall time spent recommending and checking
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def validity(self) -> float | None:
        """Per assertion."""
        total = sum(self.assertions.values())
        return sum(self.verified.values()) / total if total else None

    @property
    def recommendation_validity(self) -> float | None:
        return self.valid / self.recommendations if self.recommendations else None

    def add_user(self, player_id: str, checked: Sequence[tuple[Recommendation, list[Checked]]]) -> None:
        """One user's recommendations, each with its checked assertions."""
        valid = sum(self.add(player_id, recommendation, results) for recommendation, results in checked)
        self.by_user[player_id] = [len(checked), valid]

    def add(self, player_id: str, recommendation: Recommendation, checked: list[Checked]) -> bool:
        """Count one recommendation and its checked assertions; True when it is valid."""
        evidence = recommendation.evidence or []
        valid = all(holds for _, _, holds, _ in checked)
        self.recommendations += 1
        self.valid += valid
        if evidence:
            headline = evidence[0].path
            self.headline_paths[headline] += 1
            self.headline_valid[headline] += all(holds for item, _, holds, _ in checked if item.path == headline)
            self.explained += headline != "popular"
        else:
            self.without_evidence += 1
        for item, assertion, holds, actual in checked:
            self.assertions[assertion] += 1
            self.verified[assertion] += holds
            if not holds and len(self.failures) < FAILURES_KEPT:
                self.failures.append(
                    {
                        "player_id": player_id,
                        "appid": recommendation.appid,
                        "assertion": assertion,
                        "evidence": item.model_dump(),
                        "graph": actual,
                        "explanation": recommendation.explanation,
                    }
                )
        return valid

    def summary(self) -> dict[str, Any]:
        assertions = sum(self.assertions.values())
        return {
            "recommendations": self.recommendations,
            "valid": self.valid,
            "recommendation_validity": self.recommendation_validity,
            "without_evidence": self.without_evidence,
            "headline_validity": {
                path: {
                    "recommendations": self.headline_paths[path],
                    "valid": self.headline_valid[path],
                    "validity": self.headline_valid[path] / self.headline_paths[path],
                }
                for path in sorted(self.headline_paths)
            },
            "explanation_coverage": self.explained / self.recommendations if self.recommendations else None,
            "per_assertion": {
                "validity": self.validity,
                "assertions": assertions,
                "verified": sum(self.verified.values()),
                "per_recommendation": assertions / self.recommendations if self.recommendations else None,
                "by_assertion": {
                    kind: {"assertions": self.assertions[kind], "verified": self.verified[kind]}
                    for kind in sorted(self.assertions)
                },
            },
        }


def share_interval(drawn: np.ndarray, level: float) -> list[float] | None:
    """Percentile interval of valid / recommendations over the replicates (rows [recommendations, valid]);
    None when a replicate has no recommendation."""
    if not np.all(drawn[:, 0] > 0):
        return None
    return list(interval(drawn[:, 1] / drawn[:, 0], level))


def validity_statistics(
    report: VersionReport,
    strata: Mapping[str, str],
    replicates: int = REPLICATES,
    seed: int = SEED,
    level: float = LEVEL,
) -> dict[str, Any]:
    """The CI of the recommendation validity and the validity per stratum with its CI, from the stratified
    bootstrap of the module docstring. strata: player_id -> stratum of every user, in the users' order."""
    rng = np.random.default_rng(seed)
    total = np.zeros((replicates, 2), dtype=np.int64)
    by_stratum = {}
    for name in STRATA:
        tallies = [report.by_user[player_id] for player_id, group in strata.items() if group == name]
        counts = np.array(tallies, dtype=np.int64).reshape(len(tallies), 2)
        drawn = np.zeros((replicates, 2), dtype=np.int64)
        if tallies:
            drawn = counts[rng.integers(0, len(tallies), size=(replicates, len(tallies)))].sum(axis=1)
        total += drawn
        recommendations, valid = (int(value) for value in counts.sum(axis=0))
        by_stratum[name] = {
            "users": len(tallies),
            "recommendations": recommendations,
            "valid": valid,
            "recommendation_validity": valid / recommendations if recommendations else None,
            "recommendation_validity_ci": share_interval(drawn, level),
        }
    return {"recommendation_validity_ci": share_interval(total, level), "by_stratum": by_stratum}


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


def sampled_users(play_records: list[PlayRecord], users: int, seed: int) -> list[User]:
    """sample_players' players and hidden games, with the stratum of the split's rule."""
    players, hidden = sample_players(play_records, users, seed)
    appids_by_player = group_appids_by_player(play_records)
    return [(player_id, stratum(len(appids_by_player[player_id]) - 1), hidden[player_id]) for player_id in players]


def split_users(split: Mapping[str, Any], limit_per_stratum: int | None = None) -> list[User]:
    """The split's explanations subset at rep 0, in the split's order; the first `limit_per_stratum` of
    each stratum when given."""
    taken: Counter = Counter()
    users = []
    for player_id, name, _, appid in cases(split, SUBSET):
        if limit_per_stratum is None or taken[name] < limit_per_stratum:
            taken[name] += 1
            users.append((player_id, name, appid))
    return users


def check_recommendations(
    reader: GraphReader,
    facts: GraphFacts,
    players: list[str],
    hidden: dict[str, str],
    k: int,
    versions: list[str],
    lang: str = "en",
    progress: Callable[[int], None] | None = None,
) -> dict[str, VersionReport]:
    """Each version's top k for every player, with hidden[player_id] hidden, checked; progress(done) after
    every player."""
    reports = {version: VersionReport() for version in versions}
    for done, player_id in enumerate(players, start=1):
        hidden_appids = frozenset({hidden[player_id]}) if player_id in hidden else frozenset()
        visible = {appid: name for appid, name in facts.played(player_id).items() if appid not in hidden_appids}
        for version in versions:
            report = reports[version]
            started = time.perf_counter()
            recommendations = RECOMMENDERS[version](reader, player_id, k, lang, hidden_appids)
            recommended = time.perf_counter()
            checked = [(item, check_recommendation(facts, player_id, visible, item)) for item in recommendations]
            report.seconds["recommend"] += recommended - started
            report.seconds["check"] += time.perf_counter() - recommended
            report.add_user(player_id, checked)
        if progress:
            progress(done)
    return reports


def ratio(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f" = {part / whole:.4f}" if whole else "")


def share_text(row: Mapping[str, Any]) -> str:
    ci = row.get("recommendation_validity_ci")
    return ratio(row["valid"], row["recommendations"]) + (f" [{ci[0]:.4f}, {ci[1]:.4f}]" if ci else "")


def print_results(versions: Mapping[str, Mapping[str, Any]]) -> None:
    for version, result in versions.items():
        print(f"{version}: {result['recommendations']} recommendations, {result['without_evidence']} without evidence")
        print(f"  per recommendation {share_text(result)}")
        for name, row in result["by_stratum"].items():
            print(f"    {name}: {row['users']} users, {share_text(row)}")
        headline = result["headline_validity"]
        print("  headline " + ", ".join(f"{path} {share_text(row)}" for path, row in headline.items()))
        per_assertion = result["per_assertion"]
        print(f"  per assertion (secondary) {ratio(per_assertion['verified'], per_assertion['assertions'])}")
        for kind, counts in per_assertion["by_assertion"].items():
            print(f"    {kind}: {counts['verified']}/{counts['assertions']}")
        if result["explanation_coverage"] is not None:
            print(f"  explanation coverage (headline is not popularity) {result['explanation_coverage']:.4f}")


def select_users(
    reader: GraphReader, graph: Mapping[str, Any], args: argparse.Namespace
) -> tuple[list[User], dict[str, Any]]:
    """The users to check, and where they come from."""
    if args.split is None:
        users = sampled_users(reader.all_play_records(), args.users, args.seed)
        return users, {"sample": "run 0 of eval/evaluate.py", "users": args.users, "seed": args.seed}
    split = load_split(args.split)
    expected = {name: split["graph"][name] for name in ("players", "games", "played")}
    if {name: graph[name] for name in expected} != expected:
        raise SystemExit(f"the graph {graph} is not the graph of the split {expected}")
    source = {
        "split": relative(args.split),
        "split_sha256": file_sha256(args.split),
        "subset": SUBSET,
        "rep": 0,
        "limit_per_stratum": args.limit_per_stratum,
    }
    return split_users(split, args.limit_per_stratum), source


def user_rows(users: Sequence[User], reports: Mapping[str, VersionReport]) -> list[dict[str, Any]]:
    """Each user's stratum and hidden game, and per version the recommendations and the valid ones."""
    return [
        {
            "player_id": player_id,
            "stratum": name,
            "hidden": appid,
            "recommendations": {version: report.by_user[player_id][0] for version, report in reports.items()},
            "valid": {version: report.by_user[player_id][1] for version, report in reports.items()},
        }
        for player_id, name, appid in users
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-check static recommendation evidence against the graph")
    parser.add_argument("--graph", default="ucsd")
    parser.add_argument("--split", type=Path, help="check the users of the split's explanations subset, rep 0")
    parser.add_argument("--limit-per-stratum", type=int, help="with --split: only the first N users of each stratum")
    parser.add_argument("--users", type=int, default=300, help="without --split: sampled as in eval/evaluate.py")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--lang", choices=("en", "ja"), default="en")
    parser.add_argument("--versions", nargs="+", choices=sorted(RECOMMENDERS), default=["current", "v0"])
    parser.add_argument("--output", type=Path, help="write the results and the first failures as JSON")
    args = parser.parse_args()
    if args.limit_per_stratum is not None and args.split is None:
        parser.error("--limit-per-stratum needs --split")
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    timer, git = Timer(), git_state()
    reader = GraphReader(args.graph)
    try:
        graph = {"name": args.graph, **graph_counts(reader)}
        users, source = select_users(reader, graph, args)
        if not users:
            raise SystemExit("no users to check")
        timer.mark("users")
        players = [player_id for player_id, _, _ in users]

        def progress(done: int) -> None:
            if done % PROGRESS_EVERY == 0 or done == len(players):
                log(f"checked {done}/{len(players)} users")

        hidden = {player_id: appid for player_id, _, appid in users}
        facts = CypherFacts(reader)
        reports = check_recommendations(reader, facts, players, hidden, args.k, args.versions, args.lang, progress)
        timer.mark("check")
    finally:
        reader.close()
    strata = {player_id: name for player_id, name, _ in users}
    versions = {
        version: report.summary() | validity_statistics(report, strata) | {"failures": report.failures}
        for version, report in reports.items()
    }
    timer.mark("statistics")
    wall = timer.summary() | {
        "per_user": round(timer.seconds["check"] / len(users), 2),
        "versions": {
            version: {part: round(seconds, 1) for part, seconds in sorted(report.seconds.items())}
            for version, report in reports.items()
        },
    }
    sizes = Counter(strata.values())
    per_stratum = ", ".join(f"{name}: {sizes[name]}" for name in STRATA)
    print(f"graph={args.graph} users={len(users)} ({per_stratum}) k={args.k} lang={args.lang}")
    print_results(versions)
    print(f"{wall['check']} s checking, {wall['per_user']} s per user")
    if args.output:
        result = {
            "graph": graph,
            "users_from": source,
            "k": args.k,
            "lang": args.lang,
            "bootstrap": BOOTSTRAP,
            "git": git,
            "versions": versions,
            "users": user_rows(users, reports),
            "wall_seconds": wall,
        }
        write_json(args.output, result)


if __name__ == "__main__":
    main()
