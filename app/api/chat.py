from utils import settings
from utils.graph_reader import GraphReader
from utils.llm_client import LlmClient

from .chat_prompts import build_analysis_prompt, build_answer_prompt
from .models import ChatMessage, ChatResponse, Lang, Reason
from .player_profile import build_player_profile
from .presenters import build_recommendation
from .static_recommend import POPULAR_EXPLANATION

TAG_CHOICE_COUNT = 80
CANDIDATE_COUNT = 10
REVIEW_EXCERPT_COUNT = 2
REVIEW_EXCERPT_CHARS = 200

NO_MATCH_REPLY: dict[Lang, str] = {
    "ja": "条件に合うゲームが見つからなかったので、人気のゲームを表示します。",
    "en": "No game matched, so here are popular games.",
}


def chat(
    reader: GraphReader,
    llm_client: LlmClient,
    messages: list[ChatMessage],
    player_id: str | None,
    limit: int,
    lang: Lang,
) -> ChatResponse:
    profile = build_player_profile(reader, player_id, set()) if player_id else ""
    conversation = [message.model_dump() for message in messages]

    tag_choices = reader.top_tags(TAG_CHOICE_COUNT)
    aspect_choices = reader.aspect_names()
    analysis = llm_client.complete_json(
        [
            {
                "role": "system",
                "content": build_analysis_prompt(tag_choices, aspect_choices, settings.CURRENCY, profile),
            },
            *conversation,
        ]
    )
    max_price = analysis.get("max_price")
    candidates = reader.search_games(
        [tag for tag in analysis.get("tags", []) if tag in tag_choices],
        [aspect for aspect in analysis.get("aspects", []) if aspect in aspect_choices],
        analysis.get("keywords", []),
        max_price if isinstance(max_price, (int, float)) else None,
        CANDIDATE_COUNT,
    )
    if not candidates:
        return ChatResponse(
            reply=NO_MATCH_REPLY[lang],
            recommendations=[
                build_recommendation(game, lang, float(game["played_count"]), [], POPULAR_EXPLANATION[lang])
                for game in reader.popular_games(limit)
            ],
        )

    candidate_summaries = []
    for candidate in candidates:
        lines = [
            f"appid: {candidate['appid']}",
            f"name: {candidate['name']}",
            f"tags: {', '.join(tag['name'] for tag in candidate['tags'])}",
            f"matched_tags: {', '.join(sorted(set(candidate['matched_tags'])))}",
            f"matched_aspects: {', '.join(sorted(set(candidate['matched_aspects'])))}",
            f"review_count: {candidate['review_count']}, recommend_rate: {candidate['recommend_rate']}",
        ]
        for review in reader.top_reviews(candidate["appid"], REVIEW_EXCERPT_COUNT):
            lines.append(f"review: {review['text'][:REVIEW_EXCERPT_CHARS]}")
        candidate_summaries.append("\n".join(lines))

    answer = llm_client.complete_json(
        [
            {"role": "system", "content": build_answer_prompt("\n\n".join(candidate_summaries), profile, lang)},
            *conversation,
        ],
        temperature=0.35,
    )
    candidate_by_appid = {candidate["appid"]: candidate for candidate in candidates}
    recommendations = []
    for pick in answer.get("picks", []):
        candidate = candidate_by_appid.get(str(pick.get("appid")))
        if candidate is None or len(recommendations) >= limit:
            continue
        reasons = [Reason(kind="tag", label=tag) for tag in sorted(set(candidate["matched_tags"]))]
        reasons += [
            Reason(kind="aspect", label=aspect.replace("_", " "))
            for aspect in sorted(set(candidate["matched_aspects"]))
        ]
        recommendations.append(
            build_recommendation(candidate, lang, candidate["score"], reasons, str(pick.get("reason", "")))
        )
    return ChatResponse(
        reply=str(answer.get("reply", "")),
        options=[str(option) for option in answer.get("options", [])],
        recommendations=recommendations,
    )
