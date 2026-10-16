import html
import re

from utils.graph_reader import GameRow, TagLabel

from .models import Lang, Reason, Recommendation

DISPLAY_TAG_LIMIT = 6


def build_image_url(appid: str) -> str:
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


def build_store_url(appid: str) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def pick_language(english: str | None, japanese: str | None, lang: Lang) -> str | None:
    return japanese if lang == "ja" and japanese else english


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip() or None


def display_tag_labels(tags: list[TagLabel], lang: Lang) -> list[str]:
    ordered = sorted(tags, key=lambda tag: (tag["rank"] is not None, tag["rank"] or 0))
    labels = [pick_language(tag["name"], tag["name_ja"], lang) or tag["name"] for tag in ordered]
    return list(dict.fromkeys(labels))[:DISPLAY_TAG_LIMIT]


def build_recommendation(
    game: GameRow, lang: Lang, score: float, reasons: list[Reason], explanation: str
) -> Recommendation:
    return Recommendation(
        appid=game["appid"],
        name=pick_language(game["name"], game["name_ja"], lang) or game["name"],
        description=strip_html(pick_language(game["description"], game["description_ja"], lang)),
        image_url=build_image_url(game["appid"]),
        store_url=build_store_url(game["appid"]),
        price=game["price"],
        currency=game["currency"],
        tags=display_tag_labels(game["tags"], lang),
        review_count=game["review_count"],
        recommend_rate=game["recommend_rate"],
        score=score,
        reasons=reasons,
        explanation=explanation,
    )
