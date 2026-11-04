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
combine() divides peer, tag and popular by their maximum and studio by the player's total seed weight
Σ_s w_s, multiplies by the path weight λ, adds the paths up and ranks by (-score, appid), filling up
with popular games; the API and offline tuning share it. The reason text states nothing but the
evidence: the path instances that produced the score.
"""

import math
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Literal, Mapping, TypedDict

from utils.graph_reader import GraphReader, SeedGame, TagConcept, tag_term

from .models import Evidence, EvidencePath, Lang, Reason, Recommendation
from .presenters import build_recommendation, pick_language

TAG_PREFERENCE_LIMIT = 30
REASON_TAG_LIMIT = 3
PEER_SEED_GAMES = 10
EVIDENCE_PER_PATH = REASON_TAG_LIMIT
# Tags that at least GENERIC_TAG_GAMES of every GENERIC_TAG_GRAPH_GAMES games have (Multiplayer, Singleplayer,
# Action, ...) say little in a reason. The cut was set on the UCSD graph, which has 9,192 games, so there it
# stays at 1,000 games; other graphs cut at the same share of their games (a 100-game graph at 11).
GENERIC_TAG_GAMES = 1000
GENERIC_TAG_GRAPH_GAMES = 9192
# The order paths are added up in, and the tie order when choosing the headline evidence.
PATHS: tuple[EvidencePath, ...] = ("peer", "tag", "studio", "popular")
# λ per path, chosen on the validation users (eval/results/static_tuning_v1.json, README §8.5): tags add a
# little to co-play; studio and popular are off (popular still fills up short lists). λ_studio scales a
# share of the player's seed weight, S_studio / Σ_s w_s; the other λ scale a path score divided by its
# highest score.
DEFAULT_PATH_WEIGHTS: dict[str, float] = {"peer": 1.0, "tag": 0.1, "studio": 0.0, "popular": 0.0}
STUDIO_ROLE_WEIGHTS = {"developer": 1.0, "publisher": 0.5}
# The studio divisor: the player's total seed weight, or the highest studio score (the previous rule,
# kept for comparison).
StudioNormalization = Literal["seed_weight", "max"]
STUDIO_NORMALIZATIONS: tuple[StudioNormalization, ...] = ("seed_weight", "max")
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


# Chosen on the validation users with the λ above: every played game counts the same and seeds co-play,
# which makes the peer path Item-KNN without the player.
DEFAULT_SEED_SCHEME = SeedScheme(weighting="uniform", peer_limit=None)


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
    # The tag path's concept table by concept, with the seeds that hold each concept; a tag row's "matched"
    # lists the concepts of the table that its game has.
    tag_concepts: dict[str, TagConcept]


class RankedGame(TypedDict):
    appid: str
    score: float
    contributions: dict[str, float]  # path -> λ · score / highest score of the path (studio: Σ_s w_s); adds up to score
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
    if not player_id:
        return [
            build_recommendation(game, lang, float(game["played_count"]), [], POPULAR_EXPLANATION[lang])
            for game in reader.popular_games(limit)
        ]

    path_scores = collect_path_scores(reader, player_id, hidden_appids, scheme)
    ranked = combine(path_scores, limit, weights)
    games = {game["appid"]: game for game in reader.games_by_appids([item["appid"] for item in ranked])}
    generic = generic_tags(path_scores)
    recommendations = []
    for item in ranked:
        anchor = tag_anchor(path_scores, item)
        evidence = build_evidence(path_scores, item, lang, anchor)
        reasons, explanation = describe_evidence(evidence, lang, anchor, generic)
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
) -> PathScores:
    """Run the path queries for one player. Without visible history only popularity is scored (still
    without the player, so hiding all of a player's games leaks nothing either)."""
    played = sorted(
        reader.played_games(player_id, sorted(hidden_appids)), key=lambda game: (-game["hours"], game["appid"])
    )
    seeds = [
        Seed(appid=game["appid"], name=game["name"], hours=game["hours"], weight=scheme.weight(game["hours"]))
        for game in played
    ]
    played_appids = [seed["appid"] for seed in seeds]
    seed_games = [SeedGame(appid=seed["appid"], weight=seed["weight"]) for seed in seeds]
    peer_seed_games = seed_games if scheme.peer_limit is None else seed_games[: scheme.peer_limit]
    rows_by_path: dict[str, list[Any]] = {"peer": [], "tag": [], "studio": []}
    concepts: list[TagConcept] = []
    if seeds:
        concepts = reader.tag_concepts(seed_games, TAG_PREFERENCE_LIMIT)
        rows_by_path = {
            "peer": reader.co_play_scores(player_id, peer_seed_games, played_appids, EVIDENCE_PER_PATH),
            "tag": reader.tag_concept_scores(
                seed_games, played_appids, TAG_PREFERENCE_LIMIT, EVIDENCE_PER_PATH, concepts
            ),
            "studio": reader.studio_scores(
                seed_games,
                played_appids,
                STUDIO_ROLE_WEIGHTS["developer"],
                STUDIO_ROLE_WEIGHTS["publisher"],
                EVIDENCE_PER_PATH,
            ),
        }
    rows_by_path["popular"] = [
        dict(row, score=math.log1p(row["players"])) for row in reader.game_popularity(player_id, played_appids)
    ]
    return PathScores(
        player_id=player_id,
        seeds=seeds,
        paths={path: {row["appid"]: dict(row) for row in rows} for path, rows in rows_by_path.items()},
        tag_concepts={concept["concept"]: concept for concept in concepts},
    )


def ordered_sum(values: Iterable[float]) -> float:
    """Add from left to right. From Python 3.12 on, sum() of floats compensates rounding and can differ in the
    last bit from 3.11 (the API image), so totals are added in a plain loop to be the same on every version."""
    total = 0.0
    for value in values:
        total += value
    return total


def combine(
    path_scores: PathScores,
    limit: int,
    weights: Mapping[str, float] | None = None,
    studio_normalization: StudioNormalization = "seed_weight",
) -> list[RankedGame]:
    """Rank by Σ_path λ · score / divisor: the highest score of the path, but for studio the total weight
    Σ_s w_s of all visible seeds unless studio_normalization is "max". A path with λ = 0 or a divisor of 0
    is switched off. Games no path scored fill the list in order of popularity."""
    weights = DEFAULT_PATH_WEIGHTS if weights is None else weights
    unknown_paths = sorted(set(weights) - set(PATHS))
    if unknown_paths:
        raise ValueError(f"unknown paths: {unknown_paths}")
    if any(weight < 0 for weight in weights.values()):
        raise ValueError("path weights must not be negative")
    if studio_normalization not in STUDIO_NORMALIZATIONS:
        raise ValueError(f"unknown studio normalization: {studio_normalization!r}")

    contributions: dict[str, dict[str, float]] = {}
    for path in PATHS:
        weight = weights.get(path, 0.0)
        rows = path_scores["paths"].get(path, {})
        if path == "studio" and studio_normalization == "seed_weight":
            # All titles of one studio share one score, so dividing by the highest score would give every
            # title of the player's strongest studio the full λ.
            divisor = ordered_sum(seed["weight"] for seed in path_scores["seeds"])
        else:
            divisor = max((row["score"] for row in rows.values()), default=0.0)
        if weight == 0 or divisor <= 0:
            continue
        for appid, row in rows.items():
            if row["score"] > 0:
                contributions.setdefault(appid, {})[path] = weight * row["score"] / divisor

    totals = {appid: ordered_sum(parts.values()) for appid, parts in contributions.items()}
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


def tag_anchor(path_scores: PathScores, ranked: RankedGame) -> str | None:
    """The seed a tag reason is about: the one with the largest part of the game's tag score. Each matched
    concept's term is split among the seeds that have it by their weight, term · w_s / Σ_{seeds with c} w
    = w_s · ln(N / df(c)) / sqrt(n(g)), so a seed is credited with its own part only. Ties (totals that agree
    to SCORE_DECIMALS decimals) go to the larger w_s, then to the smaller appid as a string ("102700" before
    "20"). None unless the tag path contributed."""
    if "tag" not in ranked["contributions"]:
        return None
    row = path_scores["paths"]["tag"][ranked["appid"]]
    totals: dict[str, float] = {}
    weights: dict[str, float] = {}
    for concept in row["matched"]:
        entry = path_scores["tag_concepts"][concept]
        term = tag_term(entry["weight"], row["concept_count"])
        held = ordered_sum(holder["weight"] for holder in entry["holders"])
        for holder in entry["holders"]:
            totals[holder["appid"]] = totals.get(holder["appid"], 0.0) + term * holder["weight"] / held
            weights[holder["appid"]] = holder["weight"]
    return min(totals, key=lambda appid: (-round(totals[appid], SCORE_DECIMALS), -weights[appid], appid))


def generic_tags(path_scores: PathScores) -> set[str]:
    """The concepts of the tag table that at least GENERIC_TAG_GAMES of every GENERIC_TAG_GRAPH_GAMES games
    have: df(c) · GENERIC_TAG_GRAPH_GAMES >= GENERIC_TAG_GAMES · N, compared as integers. Every row of the
    table carries N, the number of games of the graph, as `games`."""
    return {
        concept
        for concept, entry in path_scores["tag_concepts"].items()
        if entry["document_frequency"] * GENERIC_TAG_GRAPH_GAMES >= GENERIC_TAG_GAMES * entry["games"]
    }


def build_evidence(
    path_scores: PathScores, ranked: RankedGame, lang: Lang, anchor: str | None = None
) -> list[Evidence]:
    """The path instances behind one ranked game. The headline path comes first: the path other than
    popularity with the largest contribution, or popularity when nothing else contributed. A tag
    instance names the anchor seed (tag_anchor) if the anchor has the tag, else the seed of the largest
    w_s that has it (ties by appid)."""
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
    if anchor is None:
        return evidence
    return [with_anchor(item, path_scores["tag_concepts"], anchor) for item in evidence]


def with_anchor(item: Evidence, concepts: Mapping[str, TagConcept], anchor: str) -> Evidence:
    """A tag instance with the anchor as its seed if the anchor has the tag; any other instance as it is."""
    if item.path != "tag":
        return item
    for holder in concepts[item.bridge_id]["holders"]:
        if holder["appid"] == anchor:
            return item.model_copy(update={"seed_appid": holder["appid"], "seed_name": holder["name"]})
    return item


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


def describe_evidence(
    evidence: list[Evidence], lang: Lang, anchor: str | None = None, generic: Collection[str] = ()
) -> tuple[list[Reason], str]:
    """Reason chips and sentence written from the headline evidence only. Tags are stated with one seed:
    the anchor (tag_anchor) if a tag instance names it, else the seed of the first tag instance. Of that
    seed's instances, tags in `generic` are left out unless all of them are generic (other tags the seed
    shares with the game but that are not among the instances do not count). Studios are stated with
    their role for both games. Player counts have thousands separators."""
    if not evidence:
        return [], POPULAR_EXPLANATION[lang]
    headline = [item for item in evidence if item.path == evidence[0].path]
    first = headline[0]
    japanese = lang == "ja"
    if first.path == "peer":
        explanation = (
            f"『{first.seed_name}』を遊んだ他のプレイヤーのうち {first.count:,}人が、このゲームも遊んでいます"
            if japanese
            else f"{other_players(first.count)} who played {first.seed_name} also played this game"
        )
        return [Reason(kind="game", label=first.seed_name)], explanation
    if first.path == "tag":
        tags_by_seed: dict[str | None, list[Evidence]] = {}
        for item in headline:
            tags_by_seed.setdefault(item.seed_appid, []).append(item)
        stated = tags_by_seed.get(anchor) or tags_by_seed[first.seed_appid]
        stated = [item for item in stated if item.bridge_id not in generic] or stated
        seed, tags = stated[0].seed_name, [item.bridge_name for item in stated]
        explanation = (
            f"『{seed}』と共通するタグ: {'、'.join(tags)}" if japanese else f"Shares {', '.join(tags)} with {seed}"
        )
        return [Reason(kind="game", label=seed)] + [Reason(kind="tag", label=tag) for tag in tags], explanation
    if first.path == "studio":
        template = STUDIO_EXPLANATIONS[lang][(first.seed_role, first.candidate_role)]
        explanation = template.format(studio=first.bridge_name, seed=first.seed_name)
        return [Reason(kind="game", label=first.seed_name), Reason(kind="studio", label=first.bridge_name)], explanation
    if japanese:
        explanation = f"他のプレイヤー {first.count:,}人が遊んでいるゲーム" if first.count else "まだ他のプレイヤーが遊んでいないゲーム"
    else:
        explanation = f"Played by {other_players(first.count)}" if first.count else "No other player has played this game yet"
    return [], explanation


def other_players(count: int | None) -> str:
    return "1 other player" if count == 1 else f"{count:,} other players"
