"""Static (history-based) recommendation over fixed, parameterized, read-only Cypher.

The games a player has played (minus the hidden ones during evaluation) are the seeds, weighted by a
SeedScheme. Every game the player has not played is scored along three typed graph paths plus a
popularity prior, and every count leaves the player out:

- peer:    Σ_s w_s · co(s, g) / sqrt(deg(s) · deg(g))      co(s, g): other players who played both
- tag:     Σ_c pref(c) · ln(N / df(c)) / sqrt(#tags of g)   a genre and a user tag with the same name are
                                                            one tag; Feature tags are left out
- studio:  Σ_s w_s · role / ln(2 + titles of the studio)    each seed once; developer 1.0, publisher 0.5
- popular: ln(1 + deg(g))

collect_path_scores() runs the queries once per player and returns plain data that can be cached.
combine() divides each path by its maximum, multiplies by the path weight λ, adds the paths up and
ranks by (-score, appid), filling up with popular games; the API and offline tuning share it. The
reason text states nothing but the evidence: the path instances that produced the score.
"""

import math
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, TypedDict

from utils.graph_reader import GraphReader, SeedGame

from .models import Evidence, EvidencePath, Lang, Reason, Recommendation
from .presenters import build_recommendation, pick_language

TAG_PREFERENCE_LIMIT = 30
REASON_TAG_LIMIT = 3
PEER_SEED_GAMES = 10
EVIDENCE_PER_PATH = REASON_TAG_LIMIT
# The order paths are added up in, and the tie order when choosing the headline evidence.
PATHS: tuple[EvidencePath, ...] = ("peer", "tag", "studio", "popular")
# λ per path until they are tuned on the validation users (M5).
DEFAULT_PATH_WEIGHTS: dict[str, float] = {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}
STUDIO_ROLE_WEIGHTS = {"developer": 1.0, "publisher": 0.5}
# Totals that agree to this many decimals are ties, ordered by appid.
SCORE_DECIMALS = 12

POPULAR_EXPLANATION: dict[Lang, str] = {"ja": "遊んだプレイヤーが多いゲーム", "en": "Played by many players"}

STUDIO_EXPLANATIONS: dict[Lang, dict[tuple[str | None, str | None], str]] = {
    "en": {
        ("developer", "developer"): "Developed by {studio}, the developer of {seed}",
        ("publisher", "publisher"): "Published by {studio}, the publisher of {seed}",
        ("developer", "publisher"): "Published by {studio}, the developer of {seed}",
        ("publisher", "developer"): "Developed by {studio}, the publisher of {seed}",
    },
    "ja": {
        ("developer", "developer"): "『{seed}』の開発元 {studio} が開発したゲーム",
        ("publisher", "publisher"): "『{seed}』の販売元 {studio} が販売したゲーム",
        ("developer", "publisher"): "『{seed}』の開発元 {studio} が販売したゲーム",
        ("publisher", "developer"): "『{seed}』の販売元 {studio} が開発したゲーム",
    },
}


@dataclass(frozen=True)
class SeedScheme:
    """Which played games seed the paths, and how much each one counts."""

    weighting: Literal["log_hours", "uniform"] = "log_hours"
    # Co-play seeds: the N most played games, or all of them with None. Tags and studios use all.
    peer_limit: int | None = PEER_SEED_GAMES

    def weight(self, hours: float) -> float:
        return math.log1p(hours) if self.weighting == "log_hours" else 1.0


DEFAULT_SEED_SCHEME = SeedScheme()


class Seed(TypedDict):
    appid: str
    name: str
    hours: float
    weight: float


class PathScores(TypedDict):
    player_id: str
    seeds: list[Seed]
    # path -> appid -> query row: its "score" (before normalisation and λ) and its path instances
    paths: dict[str, dict[str, dict[str, Any]]]


class RankedGame(TypedDict):
    appid: str
    score: float
    contributions: dict[str, float]  # path -> λ · score / highest score of the path; adds up to score
    backfill: bool  # no path scored it; it fills the list by popularity


def recommend_games(
    reader: GraphReader,
    player_id: str | None,
    limit: int,
    lang: Lang,
    hidden_appids: frozenset[str] = frozenset(),
    weights: Mapping[str, float] | None = None,
    scheme: SeedScheme = DEFAULT_SEED_SCHEME,
) -> list[Recommendation]:
    path_scores = collect_path_scores(reader, player_id, hidden_appids, scheme) if player_id else None
    if path_scores is None:
        return [
            build_recommendation(game, lang, float(game["played_count"]), [], POPULAR_EXPLANATION[lang])
            for game in reader.popular_games(limit)
        ]

    ranked = combine(path_scores, limit, weights)
    games = {game["appid"]: game for game in reader.games_by_appids([item["appid"] for item in ranked])}
    recommendations = []
    for item in ranked:
        evidence = build_evidence(path_scores, item, lang)
        reasons, explanation = describe_evidence(evidence, lang)
        recommendations.append(
            build_recommendation(
                games[item["appid"]],
                lang,
                item["score"],
                reasons,
                explanation,
                evidence=evidence,
                contributions=item["contributions"],
            )
        )
    return recommendations


def collect_path_scores(
    reader: GraphReader,
    player_id: str,
    hidden_appids: frozenset[str] = frozenset(),
    scheme: SeedScheme = DEFAULT_SEED_SCHEME,
) -> PathScores | None:
    """Run the path queries for one player; None when the player has no visible history."""
    played = sorted(
        reader.played_games(player_id, sorted(hidden_appids)), key=lambda game: (-game["hours"], game["appid"])
    )
    if not played:
        return None
    seeds = [
        Seed(appid=game["appid"], name=game["name"], hours=game["hours"], weight=scheme.weight(game["hours"]))
        for game in played
    ]
    played_appids = [seed["appid"] for seed in seeds]
    seed_games = [SeedGame(appid=seed["appid"], weight=seed["weight"]) for seed in seeds]
    peer_seed_games = seed_games if scheme.peer_limit is None else seed_games[: scheme.peer_limit]
    rows_by_path: dict[str, list[Any]] = {
        "peer": reader.co_play_scores(player_id, peer_seed_games, played_appids, EVIDENCE_PER_PATH),
        "tag": reader.tag_concept_scores(seed_games, played_appids, TAG_PREFERENCE_LIMIT, EVIDENCE_PER_PATH),
        "studio": reader.studio_scores(
            seed_games,
            played_appids,
            STUDIO_ROLE_WEIGHTS["developer"],
            STUDIO_ROLE_WEIGHTS["publisher"],
            EVIDENCE_PER_PATH,
        ),
        "popular": [
            dict(row, score=math.log1p(row["players"])) for row in reader.game_popularity(player_id, played_appids)
        ],
    }
    return PathScores(
        player_id=player_id,
        seeds=seeds,
        paths={path: {row["appid"]: dict(row) for row in rows} for path, rows in rows_by_path.items()},
    )


def combine(path_scores: PathScores, limit: int, weights: Mapping[str, float] | None = None) -> list[RankedGame]:
    """Rank by Σ_path λ · score / highest score of the path. A path with λ = 0 is switched off.
    Games no path scored fill the list in order of popularity."""
    weights = DEFAULT_PATH_WEIGHTS if weights is None else weights
    unknown_paths = sorted(set(weights) - set(PATHS))
    if unknown_paths:
        raise ValueError(f"unknown paths: {unknown_paths}")
    if any(weight < 0 for weight in weights.values()):
        raise ValueError("path weights must not be negative")

    contributions: dict[str, dict[str, float]] = {}
    for path in PATHS:
        weight = weights.get(path, 0.0)
        rows = path_scores["paths"].get(path, {})
        highest = max((row["score"] for row in rows.values()), default=0.0)
        if weight == 0 or highest <= 0:
            continue
        for appid, row in rows.items():
            if row["score"] > 0:
                contributions.setdefault(appid, {})[path] = weight * row["score"] / highest

    totals = {appid: sum(parts.values()) for appid, parts in contributions.items()}
    ranked_appids = sorted(totals, key=lambda appid: (-round(totals[appid], SCORE_DECIMALS), appid))[:limit]
    ranked = [
        RankedGame(appid=appid, score=totals[appid], contributions=contributions[appid], backfill=False)
        for appid in ranked_appids
    ]
    if len(ranked) < limit:
        taken = set(ranked_appids)
        popularity = path_scores["paths"].get("popular", {})
        fill = sorted(
            (appid for appid in popularity if appid not in taken),
            key=lambda appid: (-popularity[appid]["players"], appid),
        )
        ranked += [
            RankedGame(appid=appid, score=0.0, contributions={}, backfill=True) for appid in fill[: limit - len(ranked)]
        ]
    return ranked


def build_evidence(path_scores: PathScores, ranked: RankedGame, lang: Lang) -> list[Evidence]:
    """The path instances behind one ranked game. The headline path comes first: the path other than
    popularity with the largest contribution, or popularity when nothing else contributed."""
    appid = ranked["appid"]
    if ranked["backfill"]:
        return [Evidence(path="popular", count=path_scores["paths"]["popular"][appid]["players"])]
    contributions = ranked["contributions"]
    headline_order = sorted(
        contributions, key=lambda path: (path == "popular", -contributions[path], PATHS.index(path))
    )
    evidence: list[Evidence] = []
    for path in headline_order:
        row = path_scores["paths"][path][appid]
        evidence += EVIDENCE_BUILDERS[path](row, contributions[path] / ranked["score"], lang)
    return evidence


def peer_evidence(row: dict[str, Any], path_share: float, lang: Lang) -> list[Evidence]:
    return [
        Evidence(
            path="peer",
            seed_appid=seed["appid"],
            seed_name=seed["name"],
            count=seed["co_players"],
            share=path_share * seed["term"] / row["score"],
        )
        for seed in row["seeds"]
    ]


def tag_evidence(row: dict[str, Any], path_share: float, lang: Lang) -> list[Evidence]:
    return [
        Evidence(
            path="tag",
            seed_appid=match["seed_appid"],
            seed_name=match["seed_name"],
            bridge_id=match["concept"],
            bridge_name=pick_language(match["name"], match["name_ja"], lang) or match["name"],
            share=path_share * match["term"] / row["score"],
        )
        for match in row["matches"]
    ]


def studio_evidence(row: dict[str, Any], path_share: float, lang: Lang) -> list[Evidence]:
    return [
        Evidence(
            path="studio",
            seed_appid=link["seed_appid"],
            seed_name=link["seed_name"],
            bridge_id=link["studio_id"],
            bridge_name=link["studio"],
            count=link["titles"],
            seed_role=link["seed_role"],
            candidate_role=link["game_role"],
            share=path_share * link["term"] / row["score"],
        )
        for link in row["links"]
    ]


def popular_evidence(row: dict[str, Any], path_share: float, lang: Lang) -> list[Evidence]:
    return [Evidence(path="popular", count=row["players"], share=path_share)]


EVIDENCE_BUILDERS: dict[str, Callable[[dict[str, Any], float, Lang], list[Evidence]]] = {
    "peer": peer_evidence,
    "tag": tag_evidence,
    "studio": studio_evidence,
    "popular": popular_evidence,
}


def describe_evidence(evidence: list[Evidence], lang: Lang) -> tuple[list[Reason], str]:
    """Reason chips and sentence written from the headline evidence only: each tag with the game it
    came from, and each studio with its role for both games."""
    if not evidence:
        return [], POPULAR_EXPLANATION[lang]
    headline = [item for item in evidence if item.path == evidence[0].path]
    first = headline[0]
    japanese = lang == "ja"
    if first.path == "peer":
        explanation = (
            f"『{first.seed_name}』を遊んだ他のプレイヤーのうち {first.count}人が、このゲームも遊んでいます"
            if japanese
            else f"{other_players(first.count)} who played {first.seed_name} also played this game"
        )
        return [Reason(kind="game", label=first.seed_name)], explanation
    if first.path == "tag":
        tags_by_seed: dict[str, list[str]] = {}
        for item in headline:
            tags_by_seed.setdefault(item.seed_name, []).append(item.bridge_name)
        explanation = (
            "／".join(f"『{seed}』と共通するタグ: {'、'.join(tags)}" for seed, tags in tags_by_seed.items())
            if japanese
            else "Shares " + "; ".join(f"{', '.join(tags)} with {seed}" for seed, tags in tags_by_seed.items())
        )
        reasons = [Reason(kind="game", label=seed) for seed in tags_by_seed]
        reasons += [Reason(kind="tag", label=item.bridge_name) for item in headline]
        return reasons, explanation
    if first.path == "studio":
        template = STUDIO_EXPLANATIONS[lang][(first.seed_role, first.candidate_role)]
        explanation = template.format(studio=first.bridge_name, seed=first.seed_name)
        return [Reason(kind="game", label=first.seed_name), Reason(kind="studio", label=first.bridge_name)], explanation
    if japanese:
        explanation = f"他のプレイヤー {first.count}人が遊んでいるゲーム" if first.count else "まだ他のプレイヤーが遊んでいないゲーム"
    else:
        explanation = f"Played by {other_players(first.count)}" if first.count else "No other player has played this game yet"
    return [], explanation


def other_players(count: int | None) -> str:
    return "1 other player" if count == 1 else f"{count} other players"
