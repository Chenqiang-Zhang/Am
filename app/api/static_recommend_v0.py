"""The static recommender before M3 (v0), changed only by the M2 tie-breaks.

Kept so that the evaluation can report v0 next to the current scoring under the same protocol, and so
that eval/explanation_check.py can measure how often its reason text holds. Delete this module, the
graph_v0 ranker and GraphReader.games_sharing_tags / games_played_by_peers /
games_from_played_studios once the v0 numbers are saved.
"""

from typing import Any

from utils.graph_reader import GameRow, GraphReader, PlayedGame

from .models import Evidence, Lang, Reason, Recommendation
from .presenters import build_recommendation, pick_language
from .static_recommend import PEER_SEED_GAMES, POPULAR_EXPLANATION, REASON_TAG_LIMIT, TAG_PREFERENCE_LIMIT

CANDIDATES_PER_PATH = 100
PATH_WEIGHTS = {"tag": 1.0, "peer": 1.0, "studio": 0.5}
PATH_SCORE_FIELDS = {"tag": "score", "peer": "peer_count", "studio": "score"}


def recommend_games_v0(
    reader: GraphReader,
    player_id: str | None,
    limit: int,
    lang: Lang,
    hidden_appids: frozenset[str] = frozenset(),
) -> list[Recommendation]:
    played_games = reader.played_games(player_id, sorted(hidden_appids)) if player_id else []
    if not played_games:
        return [
            build_recommendation(game, lang, float(game["played_count"]), [], POPULAR_EXPLANATION[lang])
            for game in reader.popular_games(limit)
        ]

    played_appids = [game["appid"] for game in played_games]
    tag_preferences = reader.tag_preferences(played_games, TAG_PREFERENCE_LIMIT)
    candidates_by_path: dict[str, list[Any]] = {
        "tag": reader.games_sharing_tags(tag_preferences, played_appids, CANDIDATES_PER_PATH)
        if tag_preferences
        else [],
        "peer": reader.games_played_by_peers(
            player_id, played_games[:PEER_SEED_GAMES], played_appids, CANDIDATES_PER_PATH
        ),
        "studio": reader.games_from_played_studios(played_games, played_appids, CANDIDATES_PER_PATH),
    }

    games: dict[str, GameRow] = {}
    weighted_scores: dict[str, dict[str, float]] = {}
    candidate_by_path: dict[str, dict[str, Any]] = {}
    for path, candidates in candidates_by_path.items():
        score_field = PATH_SCORE_FIELDS[path]
        highest_score = max((candidate[score_field] for candidate in candidates), default=0) or 1
        for candidate in candidates:
            appid = candidate["appid"]
            games.setdefault(appid, candidate)
            weighted_scores.setdefault(appid, {})[path] = PATH_WEIGHTS[path] * candidate[score_field] / highest_score
            candidate_by_path.setdefault(appid, {})[path] = candidate

    ranked_appids = sorted(weighted_scores, key=lambda appid: (-sum(weighted_scores[appid].values()), appid))
    recommendations = []
    for appid in ranked_appids[:limit]:
        strongest_path = max(weighted_scores[appid], key=lambda path: weighted_scores[appid][path])
        candidate = candidate_by_path[appid][strongest_path]
        reasons, explanation = describe_reason(strongest_path, candidate, lang)
        total_score = sum(weighted_scores[appid].values())
        claims = legacy_claims(strongest_path, candidate, played_games)
        recommendations.append(
            build_recommendation(games[appid], lang, total_score, reasons, explanation, evidence=claims)
        )
    return recommendations


def top_tag_matches(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(candidate["hits"], key=lambda match: (-match["weight"], match["tag"], match["via"]))[
        :REASON_TAG_LIMIT
    ]


def describe_reason(path: str, candidate: dict[str, Any], lang: Lang) -> tuple[list[Reason], str]:
    is_japanese = lang == "ja"
    if path == "tag":
        top_matches = top_tag_matches(candidate)
        tag_labels = [pick_language(match["tag"], match["tag_ja"], lang) or match["tag"] for match in top_matches]
        joined_labels = "、".join(tag_labels) if is_japanese else ", ".join(tag_labels)
        source_game = top_matches[0]["via"]
        reasons = [Reason(kind="game", label=source_game)] + [Reason(kind="tag", label=label) for label in tag_labels]
        explanation = (
            f"『{source_game}』を遊んだあなたに。共通するタグ: {joined_labels}"
            if is_japanese
            else f"Because you played {source_game}. Shared tags: {joined_labels}"
        )
        return reasons, explanation
    if path == "peer":
        explanation = (
            f"『{candidate['via']}』を遊んだ他のプレイヤー {candidate['peer_count']}人が、このゲームも遊んでいます"
            if is_japanese
            else f"{candidate['peer_count']} players who also played {candidate['via']} play this game"
        )
        return [Reason(kind="game", label=candidate["via"])], explanation
    explanation = (
        f"『{candidate['via']}』と同じ {candidate['studio']} のゲーム"
        if is_japanese
        else f"From {candidate['studio']}, the studio behind {candidate['via']}"
    )
    return [Reason(kind="game", label=candidate["via"]), Reason(kind="studio", label=candidate["studio"])], explanation


def legacy_claims(path: str, candidate: dict[str, Any], played_games: list[PlayedGame]) -> list[Evidence]:
    """What the v0 reason text asserts, as Evidence, so that eval/explanation_check.py can test it.
    v0 names seeds, tags (in English here) and studios but not their ids or roles."""
    appid_by_name: dict[str, str] = {}
    for game in played_games:
        appid_by_name.setdefault(game["name"], game["appid"])
    if path == "tag":
        matches = top_tag_matches(candidate)
        via = matches[0]["via"]
        return [
            Evidence(path="tag", seed_appid=appid_by_name.get(via), seed_name=via, bridge_name=match["tag"])
            for match in matches
        ]
    via = candidate["via"]
    if path == "peer":
        return [Evidence(path="peer", seed_appid=appid_by_name.get(via), seed_name=via, count=candidate["peer_count"])]
    return [Evidence(path="studio", seed_appid=appid_by_name.get(via), seed_name=via, bridge_name=candidate["studio"])]
