"""
LLM-driven autonomous Text2Cypher recommender.

Endpoints served:
  POST /recommend                        — keyword search with optional personalization
  POST /recommend/home                   — behavior-based recommendations (no query text)
  POST /recommend/home/warm              — fire-and-forget cache warm-up (call on tab close/hide)
  POST /behavior/view                    — log product view to Neo4j
  POST /chat                             — multi-turn conversational recommendation (CRS)
  GET  /products/{product_id}/reviews    — top reviews for a product

Flow (search):
  1. Build user context from Neo4j (rated/viewed products, inferred attributes)
  2. LLM chooses query strategy and generates Cypher
  3. Execute Cypher; retry (feeding back the error, or "0 results — broaden the
     filters" if it ran but matched nothing) up to max_cypher_attempts
  4. If every attempt still yields 0 rows, fall back entirely to popular
     products (fallback=True). Otherwise return whatever the query matched,
     even if that's fewer than `limit` — no padding.

Flow (home):
  1. Build user context
  2. If the user has no RATED/attribute history, skip the LLM entirely and return
     popular products directly (fast path, no personalization)
  3. Otherwise, LLM generates personalized Cypher (collaborative filtering or
     attribute similarity); falls back to popular products if that fails or is
     empty after retries (same rule as search)

Flow (chat):
  1. The attr_type vocabulary actually present in the graph (queried once from
     Neo4j and cached) plus config.yaml's genre are injected into the chat
     prompt — no hardcoded categories/slots, so the flow adapts to whatever
     catalog is loaded
  2. The LLM decides itself (via "action"/"filled_slots" in its structured
     response) whether to ask another clarifying question or move to search.
     Python requires at least one clarification question before search,
     enforces MAX_QUESTIONS as a hard cap, and has a safe first-turn question
     if the LLM call itself fails
  3. Once search is triggered, delegate to the same Text2Cypher search used
     by /recommend
"""
from __future__ import annotations

import html as _html_mod
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase

from .models import MatchedAttr, Recommendation, SearchIntent

# ── graph schema ───────────────────────────────────────────────────────────────

_SCHEMA = """\
Nodes:
  Product   { product_id, title, title_ja (string|null, Japanese translation of title),
               price (float|null), avg_rating (float|null),
               rating_count (int|null), description, image_url (string|null) }
  User      { user_id }
  Review    { review_id, rating (float 1-5), timestamp (int unix-ms),
               helpful_vote (int), verified (bool), title, text }
  Category  { category_id, name, level (int, 0=root) }
  Brand     { brand_id, name }
  Attribute { attribute_id, attr_type, value,
               value_ja (string|null, Japanese translation of value),
               canonical_type (string|null), canonical_value (string|null) }
    attr_type is genre-dependent and open-ended (see "Attribute Types Currently
    in the Graph" below for what's actually available in this catalog).

Relationships:
  (User)-[:RATED {rating (float), timestamp}]->(Product)
  (User)-[:WROTE]->(Review)
  (User)-[:VIEWED {timestamp}]->(Product)
  (Review)-[:ABOUT]->(Product)
  (Product)-[:MADE_BY]->(Brand)
  (Product)-[:BELONGS_TO]->(Category)
  (Category)-[:SUBCATEGORY_OF]->(Category)
  (Product)-[:HAS_ATTRIBUTE]->(Attribute)
"""

_FEW_SHOT_EXAMPLES = """\
## Examples (structural patterns only — invent your own approach as needed)

The attr_type/value names below (e.g. "feature", "durable") are placeholders that illustrate
QUERY STRUCTURE only, not a real catalog. They are intentionally generic and not tied to any
specific product category — never use them literally. Always substitute the actual attr_type
names from "Attribute Types Currently in the Graph" above, matched to what the catalog and the
user's query are actually about.

User: "something durable and easy to carry"
{"cypher": "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute) WHERE toLower(a.value) CONTAINS 'durable' OR toLower(a.value) CONTAINS 'lightweight' OR toLower(a.value) CONTAINS 'portable' WITH p, collect({attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(a) AS matches RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(matches)*2+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Durable, easy-to-carry match' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Keyword match across attribute values regardless of attr_type"}

User: "recommend something popular among people whose taste overlaps with mine" (user_id provided as $uid)
{"cypher": "MATCH (u:User {user_id:$uid})-[:RATED]->(seen:Product)<-[:RATED]-(peer:User)-[:RATED]->(rec:Product) WHERE peer<>u AND NOT (u)-[:RATED]->(rec) WITH rec AS p, count(DISTINCT peer) AS support RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(support)*1.2+coalesce(p.avg_rating,3.5)*0.4 AS score, 'Liked by other users with overlapping taste' AS explanation, [] AS matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Peer collaborative filtering — no attributes involved, purely shared-rating overlap"}

User: "recommend something similar to what I've rated highly" (user_id provided as $uid)
{"cypher": "MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(rec:Product) WHERE r.rating>=4 AND NOT (u)-[:RATED]->(rec) WITH rec AS p, collect(DISTINCT {attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(DISTINCT a) AS shared RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(shared)*1.5+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Similar to products you rated highly' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Attribute similarity from user's high-rated products"}

User: "a well-reviewed option with a specific feature the user names"
{"cypher": "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:MENTIONS {sentiment:'positive'}]-(rev:Review)-[:ABOUT]->(p) WHERE a.attr_type='feature' AND toLower(a.value) CONTAINS 'compact' WITH p, collect(DISTINCT {attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(DISTINCT a) AS attr_matches, count(DISTINCT rev) AS confirmations RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(attr_matches)*1.5+toFloat(confirmations)*0.8+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Feature independently confirmed by other users reviews' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Cross-verifies a product-description attribute against positive MENTIONS from separate reviews — not just a description claim"}
"""

_RULES = """\
## Rules
- Named products, franchises, characters, brands, and series are hard constraints.
  Search both `toLower(p.title)` and `toLower(coalesce(p.title_ja, ''))` for them.
  When the user writes a name in Japanese or another language, also use its likely
  English catalog spelling (e.g. a transliterated franchise name). Never discard a
  named-entity constraint merely to broaden a zero-result query.
- Use $uid when referencing the user; NEVER hardcode a user_id string
- End every query with: ORDER BY score DESC LIMIT $limit
- Case-insensitive match: toLower(a.value) CONTAINS toLower("keyword")
- Price filter: toFloat(p.price) <= X  AND p.price IS NOT NULL
- NEVER use CREATE, MERGE, DELETE, SET, or any write clause
- Required RETURN aliases (exact names):
    product_id, title, title_ja, price, avg_rating, rating_count, score, explanation, matched_attrs, image_url
- matched_attrs: collect({attr_type: coalesce(a.canonical_type, a.attr_type),
                          value: coalesce(a.canonical_value, a.value),
                          value_ja: a.value_ja})  — use [] when no attrs
  (always include value_ja alongside value — Python picks whichever fits the requested language)
- When canonical_type/canonical_value exist, prefer them for normalized facet matching.
- Platform names (Switch, PS4, Xbox...) usually appear inside p.title (e.g.
  "Super Mario Odyssey - Nintendo Switch") — filter platform via
  toLower(p.title) CONTAINS 'switch' in addition to (or instead of) Attribute matching.
- Genre-like conditions (adventure, RPG, ...) are SPARSE as Attributes. Prefer matching
  genre keywords against review MENTIONS values or toLower(p.description), and fold them
  into `score` as a boost rather than a hard WHERE, so a missing tag doesn't zero the results.
- Product type (game vs console vs accessory) is best filtered via Category:
  MATCH (p)-[:BELONGS_TO]->(c:Category) WHERE toLower(c.name) CONTAINS 'games'
- `MENTIONS` from reviews are soft evidence only; do not use them as the sole hard filter
  unless the user explicitly asks for review-based evidence.

## Excluding already-rated/viewed products (CRITICAL)
Bind the user node in MATCH first, then filter in WHERE:
  CORRECT:   MATCH (u:User {user_id:$uid}) ... WHERE NOT (u)-[:RATED]->(p) AND NOT (u)-[:VIEWED]->(p)
  CORRECT:   MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product) ... WHERE NOT (u)-[:RATED]->(rec)
  WRONG:     WHERE NOT (p)<-[:RATED]-(u:User {user_id:$uid}) AND NOT (p)<-[:VIEWED]-(u)
  — in the WRONG pattern, 'u' in the second NOT is unbound and excludes ALL viewed products

Output — JSON only, no markdown fences:
{"cypher": "<valid Cypher>", "explanation": "<one sentence>"}
"""

# $explanationはパラメータ化されており、LLMを介さずに言語ごとの文言をPython側で決めて
# 渡せる（フォールバック時・パーソナライズ不要時の両方で共有する）。
_FALLBACK_CYPHER = (
    "MATCH (p:Product) "
    "WHERE p.avg_rating IS NOT NULL AND p.rating_count >= 50 "
    "WITH p, "
    "  coalesce(p.avg_rating, 3.5) * 0.5 "
    "  + log(toFloat(p.rating_count) + 1) * 0.2 AS score, "
    "  [] AS matched_attrs "
    "RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, "
    "  p.avg_rating AS avg_rating, p.rating_count AS rating_count, score, "
    "  $explanation AS explanation, matched_attrs "
    "ORDER BY score DESC LIMIT $limit"
)


def _popular_explanation(lang: str) -> str:
    return "評価の高い人気商品" if lang == "ja" else "Popular highly-rated products"


_TITLE_GENERIC_TERMS = {
    "game", "games", "gaming", "want", "wanted", "looking", "play", "please",
    "for", "the", "a", "an", "to", "of", "my", "with", "something",
    "action", "adventure", "puzzle", "rpg", "other",
    "switch", "nintendo", "playstation", "xbox", "steam", "console", "pc",
    "ゲーム", "ゲーミング", "欲しい", "ほしい", "探して", "お願い", "いる",
    "アクション", "アドベンチャー", "パズル", "その他", "特にこだわりなし",
    "友達", "友だち", "一緒", "みんな", "家族", "子供", "子ども", "プレゼント",
}

_TITLE_ALIASES = {
    "マリオ": "mario",
    "ゼルダ": "zelda",
    "ポケモン": "pokemon",
    "ポケットモンスター": "pokemon",
    "カービィ": "kirby",
    "ソニック": "sonic",
    "スプラトゥーン": "splatoon",
    "どうぶつの森": "animal crossing",
    "モンハン": "monster hunter",
    "ドラクエ": "dragon quest",
    "ファイナルファンタジー": "final fantasy",
}

_FACET_ALIAS_MAPS: dict[str, dict[str, tuple[str, ...]]] = {
    "platform": {
        "switch": ("switch", "nintendo switch", "ニンテンドースイッチ", "任天堂スイッチ", "スイッチ"),
        "playstation_5": ("ps5", "playstation 5", "playstation5", "プレイステーション5", "プレステ5"),
        "playstation_4": ("ps4", "playstation 4", "playstation4", "プレイステーション4", "プレステ4"),
        "xbox_series_x": ("xbox series x", "xbox series s", "xbox series", "series x", "series s"),
        "xbox_one": ("xbox one", "xboxone", "エックスボックスワン"),
        "pc": ("pc", "steam", "windows", "computer", "パソコン"),
        "wii_u": ("wii u", "wiiu", "wiiu版"),
        "nintendo_3ds": ("3ds", "nintendo 3ds", "ニンテンドー3ds", "3ds版"),
        "nintendo_ds": ("ds", "nintendo ds", "ニンテンドーds", "nds"),
    },
    "genre": {
        "action": ("action", "アクション"),
        "adventure": ("adventure", "アドベンチャー"),
        "rpg": ("rpg", "role playing", "ロールプレイング"),
        "jrpg": ("jrpg", "日本製rpg", "和製rpg"),
        "puzzle": ("puzzle", "パズル"),
        "shooter": ("shooter", "シューティング"),
        "racing": ("racing", "レース"),
        "sports": ("sports", "スポーツ"),
        "simulation": ("simulation", "シミュレーション"),
        "fighting": ("fighting", "格闘"),
        "horror": ("horror", "ホラー"),
        "strategy": ("strategy", "ストラテジー"),
        "platformer": ("platformer", "横スクロール", "2dアクション"),
        "party": ("party", "パーティ"),
        "rhythm": ("rhythm", "リズム"),
        "stealth": ("stealth", "ステルス"),
        "open_world": ("open world", "オープンワールド"),
        "sandbox": ("sandbox", "サンドボックス"),
    },
    "multiplayer_type": {
        "single_player": ("single player", "single-player", "1人", "一人", "ソロ"),
        "local_coop": ("local co-op", "local coop", "協力プレイ", "ローカル協力", "ローカルco-op"),
        "online_coop": ("online co-op", "online coop", "オンライン協力"),
        "local_multiplayer": ("local multiplayer", "オフライン対戦", "画面分割", "対戦"),
        "online_multiplayer": ("online multiplayer", "オンライン対戦", "ネット対戦"),
        "competitive": ("competitive", "versus", "対戦"),
    },
    "play_mode": {
        "single_player": ("single player", "single-player", "1人", "一人", "ソロ"),
        "multiplayer": ("multiplayer", "対戦", "協力", "みんなで"),
        "cooperative": ("co-op", "coop", "協力", "協力プレイ"),
    },
    "product_kind": {
        "game": ("game", "games", "ソフト", "ゲーム", "タイトル"),
        "accessory": ("accessory", "周辺機器", "アクセサリ"),
        "console": ("console", "本体", "ハード"),
        "controller": ("controller", "コントローラ", "コントローラー"),
        "bundle": ("bundle", "セット", "同梱"),
        "gift_card": ("gift card", "プリペイド", "カード"),
        "expansion": ("expansion", "dlc", "追加コンテンツ"),
    },
    "difficulty": {
        "easy": ("easy", "やさしい", "簡単"),
        "normal": ("normal", "standard", "普通"),
        "hard": ("hard", "難しい", "むずかしい"),
        "challenging": ("challenging", "やりごたえ", "高難度"),
        "beginner": ("beginner", "初心者向け"),
    },
    "gameplay_style": {
        "turn_based": ("turn based", "turn-based", "ターン制"),
        "real_time": ("real time", "リアルタイム"),
        "open_world": ("open world", "オープンワールド"),
        "side_scrolling": ("side scrolling", "横スクロール", "2dスクロール"),
        "first_person": ("first person", "fps視点", "一人称"),
        "third_person": ("third person", "三人称"),
        "roguelike": ("roguelike", "ローグライク"),
        "metroidvania": ("metroidvania", "メトロイドヴァニア"),
    },
    "graphics": {
        "pixel_art": ("pixel art", "pixel-art", "ドット絵", "レトロ"),
        "retro": ("retro", "レトロ", "昭和", "8bit", "16bit", "8-bit", "16-bit"),
        "3d": ("3d", "3d graphics", "立体"),
        "realistic": ("realistic", "写実"),
        "cartoon": ("cartoon", "カートゥーン"),
        "anime": ("anime", "アニメ"),
        "dark": ("dark", "ダーク", "黒"),
    },
    "story": {
        "story_driven": ("story driven", "story-driven", "ストーリー重視", "物語"),
        "narrative": ("narrative", "物語", "シナリオ"),
        "character_driven": ("character driven", "キャラクター重視"),
    },
    "language": {
        "japanese": ("japanese", "日本語"),
        "english": ("english", "英語"),
        "multilingual": ("multilingual", "multi language", "多言語"),
    },
    "release_date": {
        "new": ("new", "recent", "latest", "新しい", "新作"),
        "classic": ("classic", "old", "レトロ"),
    },
    "price": {
        "cheap": ("cheap", "budget", "安い", "低価格"),
        "expensive": ("expensive", "high end", "高い"),
        "free": ("free", "無料"),
        "discounted": ("discount", "sale", "セール"),
    },
    "franchise": {},
}

_FACET_WEIGHTS: dict[str, float] = {
    "platform": 4.5,
    "product_kind": 4.0,
    "genre": 3.5,
    "franchise": 5.0,
    "play_mode": 3.0,
    "multiplayer_type": 3.2,
    "player_count": 2.5,
    "online_support": 2.0,
    "difficulty": 1.8,
    "gameplay_style": 2.2,
    "graphics": 1.7,
    "story": 1.4,
    "language": 1.2,
    "release_date": 0.8,
    "price": 1.2,
}

_TITLE_MATCH_BOOST = 6.0
_POPULARITY_RATING_WEIGHT = 0.45
_POPULARITY_COUNT_WEIGHT = 0.18

# product_kind属性はグラフ内で453件しか無くハードフィルタに使えないため、
# 商品種別は全商品が持つCategoryノード(BELONGS_TO)から判定する。
# キーは_FACET_ALIAS_MAPS["product_kind"]のcanonical値、値はカテゴリ名に含まれるキーワード。
_CATEGORY_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "game": ("games", "downloadable content"),
    "console": ("consoles", "systems"),
    "controller": ("controllers", "gamepads", "joysticks", "remotes"),
    "accessory": (
        "accessories", "headsets", "cases", "storage", "memory", "kits",
        "chargers", "cables", "batteries", "stands", "skins", "faceplates",
    ),
    "gift_card": ("currency", "gift cards", "subscription"),
}

# タイトル/カテゴリ名から推定したプラットフォームをmatched_attrsに出す際の表示名
# （プラットフォーム名は日本のストアフロントでもローマ字表記が標準なので日英共通）
_PLATFORM_DISPLAY: dict[str, str] = {
    "switch": "Nintendo Switch",
    "playstation_5": "PlayStation 5",
    "playstation_4": "PlayStation 4",
    "xbox_series_x": "Xbox Series X|S",
    "xbox_one": "Xbox One",
    "pc": "PC",
    "wii_u": "Wii U",
    "nintendo_3ds": "Nintendo 3DS",
    "nintendo_ds": "Nintendo DS",
}

# 決定的マッチングが作る擬似属性(matched_attrs)の日本語表示名。
# canonical値("game","adventure"等)がUIタグにそのまま英語で出るのを防ぐ。
# ここに無い値は_facet_value_ja()がエイリアス表の日本語表現にフォールバックする。
_FACET_VALUE_DISPLAY_JA: dict[str, dict[str, str]] = {
    "product_kind": {
        "game": "ゲームソフト", "console": "ゲーム機本体", "controller": "コントローラー",
        "accessory": "アクセサリ", "gift_card": "ギフトカード", "bundle": "セット品",
        "expansion": "DLC・追加コンテンツ",
    },
    "genre": {
        "action": "アクション", "adventure": "アドベンチャー", "rpg": "RPG", "jrpg": "JRPG",
        "puzzle": "パズル", "shooter": "シューティング", "racing": "レース",
        "sports": "スポーツ", "simulation": "シミュレーション", "fighting": "格闘",
        "horror": "ホラー", "strategy": "ストラテジー", "platformer": "プラットフォーマー",
        "party": "パーティ", "rhythm": "リズム", "stealth": "ステルス",
        "open_world": "オープンワールド", "sandbox": "サンドボックス",
    },
    "multiplayer_type": {
        "single_player": "1人プレイ", "local_coop": "ローカル協力プレイ",
        "online_coop": "オンライン協力プレイ", "local_multiplayer": "ローカル対戦",
        "online_multiplayer": "オンライン対戦", "competitive": "対戦",
    },
    "play_mode": {
        "single_player": "1人プレイ", "multiplayer": "マルチプレイ", "cooperative": "協力プレイ",
    },
    "gameplay_style": {
        "turn_based": "ターン制", "real_time": "リアルタイム", "open_world": "オープンワールド",
        "side_scrolling": "横スクロール", "first_person": "一人称視点",
        "third_person": "三人称視点", "roguelike": "ローグライク", "metroidvania": "メトロイドヴァニア",
    },
    "difficulty": {
        "easy": "やさしい", "normal": "ふつう", "hard": "難しい",
        "challenging": "高難度", "beginner": "初心者向け",
    },
    "graphics": {
        "pixel_art": "ドット絵", "retro": "レトロ", "3d": "3Dグラフィック",
        "realistic": "リアル調", "cartoon": "カートゥーン調", "anime": "アニメ調", "dark": "ダーク",
    },
    "story": {
        "story_driven": "ストーリー重視", "narrative": "物語性", "character_driven": "キャラクター重視",
    },
    "language": {"japanese": "日本語対応", "english": "英語", "multilingual": "多言語対応"},
    "release_date": {"new": "新作", "classic": "クラシック"},
    "price": {"cheap": "低価格", "expensive": "ハイエンド", "free": "無料", "discounted": "セール"},
}

# タイトル語の日本語表示（"mario"→"マリオ"）。_TITLE_ALIASESの逆引き（先勝ち）。
_TITLE_ALIAS_JA: dict[str, str] = {}
for _ja_name, _en_name in _TITLE_ALIASES.items():
    _TITLE_ALIAS_JA.setdefault(_en_name, _ja_name)


def _facet_value_ja(facet_type: str, value: str) -> str | None:
    """canonical値の日本語表示名。platformはローマ字表記のまま（日本でも標準のため）。"""
    if facet_type == "platform":
        return _PLATFORM_DISPLAY.get(value, value)
    curated = _FACET_VALUE_DISPLAY_JA.get(facet_type, {}).get(value)
    if curated:
        return curated
    for alias in _FACET_ALIAS_MAPS.get(facet_type, {}).get(value, ()):
        if not re.fullmatch(r"[a-z0-9 .+_|-]+", alias.lower()):
            return alias
    return None

# 属性としては疎(genre実質~150商品)だが、レビューMENTIONS(全商品平均124値)や
# 説明文には豊富に現れるファセット。これらはMENTIONS/descriptionも証拠として使う。
_SOFT_EVIDENCE_FACETS: tuple[str, ...] = (
    "genre", "gameplay_style", "multiplayer_type", "play_mode",
    "story", "graphics", "difficulty",
)


def _normalize_search_text(text: str) -> str:
    return re.sub(r"[\s\-_]+", " ", text.lower()).strip()


def _phrase_hits(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _text_has_token(text: str, phrase: str) -> bool:
    """英数字フレーズは単語境界つきで探す（"ds"が"cards"に誤ヒットするのを防ぐ）。
    日本語などの非ASCIIフレーズは単語境界が定義できないため部分一致で探す。"""
    if re.fullmatch(r"[a-z0-9 .+_|-]+", phrase):
        return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
    return phrase in text


def _soft_facet_aliases() -> list[str]:
    """MENTIONS値の事前フィルタに使うASCIIエイリアス一覧（_SOFT_EVIDENCE_FACETS分）。"""
    aliases: set[str] = set()
    for facet_type in _SOFT_EVIDENCE_FACETS:
        for canonical_value, phrases in _FACET_ALIAS_MAPS.get(facet_type, {}).items():
            aliases.add(_normalize_search_text(canonical_value.replace("_", " ")))
            for phrase in phrases:
                normalized = _normalize_search_text(phrase)
                if re.fullmatch(r"[a-z0-9 .+_|-]+", normalized):
                    aliases.add(normalized)
    return sorted(a for a in aliases if a)


def _kinds_from_categories(categories: list[str]) -> set[str]:
    kinds: set[str] = set()
    for name in categories:
        normalized = _normalize_search_text(str(name))
        for kind, keywords in _CATEGORY_KIND_KEYWORDS.items():
            if any(_text_has_token(normalized, kw) for kw in keywords):
                kinds.add(kind)
    return kinds


def _facet_alias_set(facet_type: str, facet_values: list[str]) -> set[str]:
    """要求されたcanonical値と、そのエイリアス（正規化済み）の集合を返す。"""
    alias_map = _FACET_ALIAS_MAPS.get(facet_type, {})
    aliases: set[str] = set()
    for value in facet_values:
        aliases.add(_normalize_search_text(value.replace("_", " ")))
        aliases.update(_normalize_search_text(a) for a in alias_map.get(value, ()))
    return {a for a in aliases if a}


def _all_facet_alias_terms() -> set[str]:
    """全ファセットの語彙（正規化済み）。ここに載る語はタイトル/フランチャイズ名では
    なくファセット条件なので、タイトル語抽出から除外する（"ホラー"や"rpg"がタイトル
    語扱いされ、どの商品名にも無いため候補ゼロになる事故を防ぐ）。"""
    terms: set[str] = set()
    for alias_map in _FACET_ALIAS_MAPS.values():
        for canonical_value, phrases in alias_map.items():
            terms.add(_normalize_search_text(canonical_value.replace("_", " ")))
            terms.update(_normalize_search_text(p) for p in phrases)
    return {t for t in terms if t}


_FACET_ALIAS_TERMS: set[str] = _all_facet_alias_terms()

# 日本語チャンクは助詞で分割する前にファセット語彙そのものを除去する（"安いパズル"の
# ように助詞なしで連結された場合でも、ジャンル・価格等の語がタイトル語に紛れないように）。
# 長い語から先に除去する（"協力プレイ"を"協力"より先に消す）。
_FACET_ALIAS_TERMS_JA: list[str] = sorted(
    (t for t in _FACET_ALIAS_TERMS if not re.fullmatch(r"[a-z0-9 .+_|-]+", t)),
    key=len,
    reverse=True,
)

# 「〜みたいな」「〜のような」等の類似検索マーカー。これがある場合、名指しされた
# タイトルは「その商品自体」ではなく「類似品を探す手がかり」なので、タイトル一致を
# ハードフィルタにせず加点のみに落とす（ゼルダみたいな→ゼルダ以外も出す）。
_SIMILARITY_MARKERS: tuple[str, ...] = (
    "みたい", "のような", "のように", "っぽい", "similar to", "games like",
)


def _unique_list(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, dict) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _extract_facet_signals(text: str) -> dict[str, set[str]]:
    normalized = _normalize_search_text(text)
    signals: dict[str, set[str]] = {}
    for canonical_type, alias_map in _FACET_ALIAS_MAPS.items():
        for canonical_value, aliases in alias_map.items():
            if aliases and _phrase_hits(normalized, aliases):
                signals.setdefault(canonical_type, set()).add(canonical_value)
    return signals


def _extract_title_terms(query: str) -> list[str]:
    """Extract short catalog-title candidates from a multi-turn query string.

    This is intentionally conservative: generic requests/platforms may support
    ranking, but cannot by themselves trigger the deterministic title path.
    """
    lowered = query.lower()
    terms: list[str] = []
    terms.extend(re.findall(r"[a-z0-9][a-z0-9.+_-]{1,}", lowered))

    # Split Japanese preference phrases at particles and generic request words,
    # leaving franchise/person names such as "マリオ" or "ゼルダ".
    # まずファセット語彙（ジャンル・価格等）を除去し、次に助詞・希望動詞で分割する。
    for chunk in re.findall(r"[぀-ヿ㐀-鿿ー]+", lowered):
        for alias in _FACET_ALIAS_TERMS_JA:
            chunk = chunk.replace(alias, " ")
        for piece in chunk.split():
            parts = re.split(
                r"(?:ゲームが欲しい|ゲーム|が欲しい|がほしい|"
                r"みたいな|みたいの|みたい|のような|のように|っぽい|"
                r"を探して|がしたい|遊びたい|やりたい|プレイしたい|買いたい|遊べる|できる|"
                r"ほしい|欲しい|おすすめ|タイトル|作品|"
                r"の|が|を|は|で|に|と|も)",
                piece,
            )
            terms.extend(part for part in parts if len(part) >= 2)

    terms = [_TITLE_ALIASES.get(term, term) for term in terms]
    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if len(term) < 2 or term in seen:
            continue
        # ファセット語彙（ジャンル・プラットフォーム等）はタイトル語として扱わない
        if _normalize_search_text(term) in _FACET_ALIAS_TERMS:
            continue
        # ひらがなだけの短い語（"して"等の動詞・助詞の断片）はタイトル名ではない
        if re.fullmatch(r"[぀-ゟ]{1,3}", term):
            continue
        seen.add(term)
        unique_terms.append(term)
    return unique_terms

def _build_fix_prompt(lang: str) -> str:
    target = "Japanese" if lang == "ja" else "English"
    return f"""\
You are a Cypher expert. Fix the broken query so it runs correctly in Neo4j 5.
Preserve the original intent and all required RETURN columns.

Graph Schema:
{_SCHEMA}

Required RETURN: product_id, title, title_ja, image_url, price, avg_rating, rating_count, score, explanation, matched_attrs
Always end with: ORDER BY score DESC LIMIT $limit

Write the top-level "explanation" AND the per-product `'...' AS explanation` string literal inside
the Cypher in {target} — do not silently revert either one to English while fixing the query.

Output JSON only: {{"cypher": "<fixed Cypher>", "explanation": "<one sentence>"}}
"""


def _format_attr_vocab(vocab: list[dict]) -> str:
    if not vocab:
        return ""
    lines = ["## Attribute Types Currently in the Graph (attr_type: example values)"]
    for v in vocab:
        examples = ", ".join(str(x) for x in v["examples"][:6])
        lines.append(f"  {v['attr_type']}: {examples}")
    return "\n".join(lines)


def _format_output_language(lang: str) -> str:
    target = "Japanese" if lang == "ja" else "English"
    return (
        "## Output Language (CRITICAL)\n"
        f"Write ALL user-facing text in {target}, one sentence each:\n"
        '- the top-level "explanation" field in the JSON output\n'
        "- the per-product `'...' AS explanation` string literal inside the Cypher's RETURN clause\n"
        f"The examples above show these in English for illustration only — the actual wording you "
        f"write must be in {target} regardless of what language the examples use."
    )


def _build_search_prompt(
    genre: str,
    user_ctx: dict | None,
    dynamic_few_shot: list[dict] | None = None,
    attr_vocab_text: str = "",
    lang: str = "en",
    has_uid: bool = False,
) -> str:
    """has_uid: True only when a user_id was given AND that user has actual rating/
    attribute history — i.e. $uid will actually be bound AND worth personalizing with."""
    parts = [
        f"You are a Cypher query generator for a Neo4j {genre} product knowledge graph.\n"
        "Given a user search query, generate ONE Cypher READ query that best answers it.\n"
        "Think freely — design the query that fits the request. "
        "You may traverse any relationships in the schema, combine multiple MATCH clauses, "
        "or invent a novel scoring expression. Do not limit yourself to a fixed set of patterns.",
        f"## Graph Schema\n{_SCHEMA}",
    ]
    if attr_vocab_text:
        parts.append(attr_vocab_text)
    parts.append(_FEW_SHOT_EXAMPLES)
    if dynamic_few_shot:
        parts.append(_format_dynamic_few_shot(dynamic_few_shot))
    if has_uid:
        parts.append(_format_user_ctx(user_ctx or {}))
        parts.append(
            "## Personalization\n"
            "Incorporate the user context above to personalize results.\n"
            "- Use $uid when referencing this user in Cypher\n"
            "- Exclude products the user already RATED or VIEWED when possible"
        )
    else:
        parts.append(
            "## No User Context (CRITICAL)\n"
            "$uid is NOT bound for this request — either this is an anonymous request, or "
            "the user has no rating/attribute history yet. Do NOT reference $uid anywhere in "
            "the query (ignore the $uid pattern in the examples above), and NEVER hardcode a "
            "literal user_id string as a substitute for $uid.\n"
            "You MAY still use aggregate patterns over OTHER users' RATED data not anchored to "
            "a specific user — e.g. products frequently rated highly by users who also rated a "
            "matching product highly. Just don't bind the query to one specific $uid."
        )
    parts.append(_format_output_language(lang))
    parts.append(_RULES)
    return "\n\n".join(parts)


def _build_home_prompt(
    genre: str,
    user_ctx: dict,
    attr_vocab_text: str = "",
    lang: str = "en",
    dynamic_few_shot: list[dict] | None = None,
) -> str:
    """呼び出し元(recommend_home())は履歴が無いユーザーにはLLMを呼ばず直接人気商品を
    返すため、このプロンプトは常に実履歴のあるuser_ctxが渡される前提で組み立てる。"""
    parts = [
        f"You are a Cypher query generator for a Neo4j {genre} product knowledge graph.\n"
        "TASK: Generate home-page recommendations shown when the user opens the app (no search query).\n"
        "Think freely — invent the query approach that best serves the user based on their history.",
        f"## Graph Schema\n{_SCHEMA}",
    ]
    if attr_vocab_text:
        parts.append(attr_vocab_text)
    parts.append(_FEW_SHOT_EXAMPLES)
    if dynamic_few_shot:
        parts.append(_format_dynamic_few_shot(dynamic_few_shot))
    parts.append(_format_user_ctx(user_ctx))
    parts.append(
        "## Hint\n"
        "User history exists. Generate a personalized query using $uid.\n"
        "Exclude products the user already RATED or VIEWED."
    )
    parts.append(_format_output_language(lang))
    parts.append(_RULES)
    return "\n\n".join(parts)


def _format_user_ctx(ctx: dict) -> str:
    lines = ["## User Context"]
    if ctx.get("rated"):
        lines.append("Rated products (high rating first):")
        for p in ctx["rated"]:
            lines.append(f"  [{p['rating']:.1f}★] {p['title']}")
    if ctx.get("viewed"):
        lines.append("Recently viewed:")
        for p in ctx["viewed"]:
            lines.append(f"  {p['title']}")
    if ctx.get("preferred_attrs"):
        lines.append("Inferred preferred attributes (from 4+ star ratings):")
        for a in ctx["preferred_attrs"]:
            lines.append(f"  {a['attr_type']}: {a['value']}  (×{a['freq']})")
    if ctx.get("recent_queries"):
        lines.append("Recent searches (newest first):")
        for q in ctx["recent_queries"]:
            lines.append(f'  "{q}"')
    return "\n".join(lines)


def _format_dynamic_few_shot(examples: list[dict]) -> str:
    """Format past successful (query, cypher) pairs as user-specific few-shot examples."""
    lines = ["## This User's Past Successful Queries (led to clicks — prioritize similar patterns)"]
    for ex in examples:
        q = ex.get("query", "")
        c = ex.get("cypher", "")
        e = ex.get("explanation", "")
        lines.append(f'User: "{q}"')
        lines.append(json.dumps({"cypher": c, "explanation": e}))
        lines.append("")
    return "\n".join(lines)


# ── env / config helpers ───────────────────────────────────────────────────────

def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_PROVIDER_DEFAULTS: dict[str, tuple[str, str | None, str]] = {
    "gemini": (
        "GEMINI_API_KEY",
        None,
        "gemini-2.0-flash",
    ),
    "groq": (
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com",
        "deepseek-chat",
    ),
    "openai": (
        "OPENAI_API_KEY",
        None,
        "gpt-4o-mini",
    ),
    "ollama": (
        "OLLAMA_API_KEY",
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "llama3.2",
    ),
}


# ── Gemini native SDK wrapper ──────────────────────────────────────────────────

class _GeminiMessage:
    def __init__(self, text: str) -> None:
        self.content = text

class _GeminiChoice:
    def __init__(self, text: str) -> None:
        self.message = _GeminiMessage(text)

class _GeminiUsage:
    def __init__(self, meta: Any) -> None:
        self.input_tokens  = int(getattr(meta, "prompt_token_count",     0) or 0)
        self.output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
        self.total_tokens  = int(getattr(meta, "total_token_count",      0) or 0)

class _GeminiResponse:
    def __init__(self, resp: Any) -> None:
        self.choices = [_GeminiChoice(getattr(resp, "text", "") or "")]
        self.usage   = _GeminiUsage(getattr(resp, "usage_metadata", None))

class _GeminiCompletions:
    def __init__(self, client: Any, types_mod: Any) -> None:
        self._client = client
        self._types  = types_mod

    def create(
        self, model: str, messages: list,
        response_format: dict | None = None,
        temperature: float = 0,
        max_tokens: int | None = None,
        **_: Any,
    ) -> _GeminiResponse:
        types = self._types
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        system_instruction = system_parts[0] if system_parts else None
        contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = "model" if m["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
        mime = "application/json" if (response_format or {}).get("type") == "json_object" else None
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type=mime,
        )
        resp = self._client.models.generate_content(
            model=model, contents=contents, config=config
        )
        return _GeminiResponse(resp)

class _GeminiChat:
    def __init__(self, completions: _GeminiCompletions) -> None:
        self.completions = completions

class _GeminiCompat:
    def __init__(self, api_key: str) -> None:
        try:
            from google import genai as _genai
            from google.genai import types as _types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai not installed. Run: pip install google-genai"
            ) from exc
        self.chat = _GeminiChat(
            _GeminiCompletions(_genai.Client(api_key=api_key), _types)
        )


def _build_llm_client(provider: str, model: str | None, base_url: str | None):
    """Return (client, resolved_model). Client exposes OpenAI-compatible interface."""
    provider = provider.lower().strip()
    if provider not in _PROVIDER_DEFAULTS:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Choose from: {list(_PROVIDER_DEFAULTS)}"
        )
    env_key, default_base_url, default_model = _PROVIDER_DEFAULTS[provider]
    resolved_model = model or default_model

    api_key = os.environ.get(env_key)
    if not api_key:
        if provider == "ollama":
            api_key = "ollama"  # Ollama ignores the key; OpenAI SDK needs a non-empty string
        else:
            raise RuntimeError(
                f"API key not found. Set {env_key} in your .env file.\n"
                f"  cp .env.example .env  # then fill in {env_key}"
            )

    if provider == "gemini":
        return _GeminiCompat(api_key), resolved_model

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai not installed. Run: pip install openai") from exc

    resolved_base_url = base_url or default_base_url
    kwargs: dict[str, Any] = {"api_key": api_key}
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    return OpenAI(**kwargs), resolved_model


# ── JSON / Cypher parsing ──────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


# ── result mapping ─────────────────────────────────────────────────────────────

def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_to_recommendation(record: Any, lang: str = "en") -> Recommendation:
    matched_attrs: list[MatchedAttr] = []
    for m in (record.get("matched_attrs") or []):
        if isinstance(m, dict):
            attr_type = m.get("canonical_type") or m.get("attr_type")
            value = m.get("canonical_value") or m.get("value")
            if not (attr_type and value):
                continue
            value_ja = m.get("value_ja")
            display_value = value_ja if (lang == "ja" and value_ja) else str(value)
            matched_attrs.append(
                MatchedAttr(attr_type=str(attr_type), value=display_value)
            )
    title_ja = record.get("title_ja") or None
    return Recommendation(
        product_id=str(record.get("product_id", "")),
        title=str(record.get("title", "")),
        display_title=title_ja if (lang == "ja" and title_ja) else None,
        image_url=record.get("image_url") or None,
        price=_to_float(record.get("price")),
        avg_rating=_to_float(record.get("avg_rating")),
        rating_count=_to_int(record.get("rating_count")),
        score=float(record.get("score") or 0.0),
        matched_attrs=matched_attrs,
        explanation=str(record.get("explanation", "")),
    )


# ── conversational recommendation (CRS) — chat constants ────────────────────────

# 対話型推薦：聞き返しは最大この回数まで（LLMがaction判断に失敗し続けた場合の安全網）
MAX_QUESTIONS = 5


def _normalize_lang(lang: str | None) -> str:
    return "ja" if (lang or "").lower().startswith("ja") else "en"


def _build_chat_system_prompt(genre: str, attr_vocab_text: str, target_language: str) -> str:
    """カテゴリ非依存のチャットプロンプト。

    どんな属性を聞くべきかはハードコードせず、グラフに実際にある attr_type 語彙
    (attr_vocab_text) から都度組み立てる。ask/search の判断・スロット追跡もすべて
    LLM自身のfilled_slots/actionに委ね、Python側はMAX_QUESTIONSの安全網のみ持つ。
    """
    vocab_section = attr_vocab_text or (
        "(no attribute data available yet in the graph — ask about general product preferences)"
    )
    return f"""You are a friendly shopping assistant for a catalog of {genre} products. \
The catalog itself is in English; the user may write in Japanese or English. \
Write everything you SHOW the user (question, options, preference_summary) in {target_language}.

{vocab_section}

Through conversation, ask about 2-4 clarifying preferences that would help narrow down a
search in the catalog above. Infer the likely product category from the conversation and
prioritize the attribute types most relevant to that category from the list above (plus
price range or minimum rating if it seems relevant) — do not ask about an attribute type
that clearly doesn't apply to what the user is looking for.

Ask ONE question at a time, with 3-5 quick-reply options written in {target_language}.
ALWAYS include one "no preference / skip this" option as the last option.

DECISION RULE:
- filled_slots = number of DISTINCT preferences the user has explicitly confirmed so far
  (do not count the product category itself, and do not count a "no preference" answer).
- On the FIRST assistant turn, ALWAYS set action = "ask", even if the initial
  request is already specific or the user says they have no preference. Ask one
  useful confirmation question before searching.
- After at least one assistant question, action = "ask" while filled_slots < 2
  AND fewer than {MAX_QUESTIONS} questions have been asked so far AND the user
  hasn't said they have no preferences at all.
- After at least one assistant question, action = "search" once filled_slots >= 2,
  OR the user said they have no preference at all, OR {MAX_QUESTIONS} questions
  have already been asked.
- Use the full conversation history (including your own prior questions) to avoid asking
  about something already answered or already skipped.

Always include "preference_summary": ALL of the user's confirmed preferences accumulated
across the ENTIRE conversation so far (not just this latest turn) as short labels for
display, written in {target_language} only (do not mix languages). This MUST include
the product category/type itself if it's known (e.g. what kind of product the user
originally asked for), in addition to every other preference confirmed in any turn.
When the display language is Japanese, translate canonical catalog values into natural
Japanese labels (for example: "mario" -> "マリオ", "narrative" -> "物語性"). Do not
output an internal attribute/slot name by itself (for example, never output "game_mode"
without the user's actual preference value).

CONVERSATION HISTORY NOTE: previous assistant messages in the history contain only the
question text shown to the user. This does NOT mean you should respond in plain text —
you MUST ALWAYS respond with a valid JSON object.

When action = "search", also fill "search_keywords": 3-8 short ENGLISH keywords that a
product-catalog search engine could match, covering EVERY preference confirmed anywhere
in the conversation — franchise/series/character names (in their English catalog spelling,
e.g. "マリオ" -> "mario"), platform (e.g. "switch"), genre (e.g. "adventure"), and any
other confirmed preference. Do not invent preferences the user never stated.

Return ONLY this JSON object (no other text before or after):
{{
  "action": "ask" | "search",
  "question": "(if action=ask) the question text, otherwise null",
  "options": ["(if action=ask) quick-reply options"],
  "slot": "(a short snake_case name for what this question is asking about) | null",
  "filled_slots": <integer>,
  "preference_summary": [],
  "search_keywords": ["(if action=search) English catalog keywords, otherwise []"]
}}"""


def _strip_html(text: str | None) -> str | None:
    if text is None:
        return None
    return _html_mod.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# ── Recommender ────────────────────────────────────────────────────────────────

class Recommender:
    def __init__(self, config_path: Path | None = None) -> None:
        cfg_path = config_path or (Path(__file__).parent.parent.parent / "config.yaml")
        cfg: dict = {}
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

        _load_env(cfg_path.parent / ".env")

        llm_cfg: dict = cfg.get("llm", {})
        t2c_cfg: dict = cfg.get("text2cypher", {})
        neo4j_cfg: dict = cfg.get("neo4j", {})

        # 実行環境ごとに異なる接続先は .env / 環境変数を最優先する。
        # config.yaml は共有可能な既定値として残し、Kubera・LM Studio・クラウド API を
        # 同じコードから切り替えられるようにする。
        provider = os.environ.get("LLM_PROVIDER") or str(llm_cfg.get("provider", "gemini"))
        model = os.environ.get("LLM_MODEL") or llm_cfg.get("model") or None
        base_url = os.environ.get("LLM_BASE_URL") or llm_cfg.get("base_url") or None
        self._llm, self._model = _build_llm_client(provider, model, base_url)
        self._max_attempts: int = int(t2c_cfg.get("max_cypher_attempts", 3))

        neo4j_uri = neo4j_cfg.get("uri") or os.environ.get("NEO4J_URI", "")
        neo4j_user = (
            os.environ.get("NEO4J_USERNAME") or neo4j_cfg.get("username", "neo4j")
        )
        neo4j_password = (
            os.environ.get("NEO4J_PASSWORD") or neo4j_cfg.get("password", "")
        )
        self._neo4j_db = (
            os.environ.get("NEO4J_DATABASE") or neo4j_cfg.get("database", "neo4j")
        )

        if not neo4j_uri:
            raise RuntimeError("neo4j.uri is not set in config.yaml")
        if not neo4j_password:
            raise RuntimeError("NEO4J_PASSWORD is not set in .env")

        self._driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self._genre: str = str(cfg.get("genre", "products"))
        self._attr_vocab_text: str | None = None  # lazily populated, see _get_attr_vocab_text()
        self._home_cache: dict[str, dict[str, Any]] = {}  # see _get_or_generate_home()
        # 同じユーザーのホーム推薦を同時に生成しないためのsingle-flight用ロック。
        # キャッシュが空の初回に、Reactの再実行やwarm endpointが重なってもLLM呼び出しは
        # 1本だけにする。ロックはuser/lang/limitごとに保持する（キー数はデモ規模で小さい）。
        self._home_cache_locks: dict[str, threading.Lock] = {}
        self._home_cache_locks_guard = threading.Lock()
        self._product_catalog: list[dict[str, Any]] | None = None  # lazy cache for canonical reranking

    # ── public API ──────────────────────────────────────────────────────────────

    def _run_title_match(
        self, query: str, limit: int, lang: str,
    ) -> tuple[str, str, list[Recommendation]] | None:
        """Resolve explicit product/franchise names against the real catalog."""
        terms = _extract_title_terms(query)
        if not terms:
            return None

        count_cypher = (
            "UNWIND $terms AS term "
            "MATCH (p:Product) "
            "WHERE toLower(coalesce(p.title, '')) CONTAINS term "
            "   OR toLower(coalesce(p.title_ja, '')) CONTAINS term "
            "RETURN term, count(DISTINCT p) AS cnt"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                counts = {r["term"]: int(r["cnt"]) for r in session.run(count_cypher, terms=terms)}
        except Exception as exc:
            print(f"[recommender] title-term lookup failed: {exc}", file=sys.stderr)
            return None

        matched_terms = [term for term in terms if counts.get(term, 0) > 0]
        specific_terms = [
            term for term in matched_terms
            if term not in _TITLE_GENERIC_TERMS and counts[term] <= 100
        ]
        if not specific_terms:
            return None

        display_term = next(
            (
                ja_term for ja_term, catalog_term in _TITLE_ALIASES.items()
                if catalog_term == specific_terms[0] and ja_term in query
            ),
            specific_terms[0],
        )
        explanation = (
            f"商品名・シリーズ名「{display_term}」に一致"
            if lang == "ja"
            else f'Matched product or series name "{display_term}"'
        )
        cypher = (
            "MATCH (p:Product) "
            "WITH p, [term IN $terms WHERE "
            "  toLower(coalesce(p.title, '')) CONTAINS term OR "
            "  toLower(coalesce(p.title_ja, '')) CONTAINS term] AS matched_terms "
            "WHERE any(term IN matched_terms WHERE term IN $specific_terms) "
            "WITH p, matched_terms, "
            "  toFloat(size(matched_terms)) * 10.0 "
            "  + coalesce(p.avg_rating, 3.5) * 0.5 "
            "  + log(toFloat(coalesce(p.rating_count, 0)) + 1) * 0.2 AS score "
            "RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, "
            "  p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, "
            "  p.rating_count AS rating_count, score, $explanation AS explanation, "
            "  [] AS matched_attrs "
            "ORDER BY score DESC LIMIT $limit"
        )
        results = self._execute_and_map(
            cypher,
            {
                "terms": matched_terms,
                "specific_terms": specific_terms,
                "explanation": explanation,
                "limit": limit,
            },
            lang,
        )
        return (cypher, explanation, results) if results else None

    def _get_product_catalog(self) -> list[dict[str, Any]]:
        """Load products, attributes, categories and review-mention evidence once
        for fast canonical reranking.

        ジャンル等のソフトファセットは属性としては疎なので、レビューMENTIONSの値
        （_SOFT_EVIDENCE_FACETSの語彙にマッチするものだけ事前フィルタ）と説明文も
        証拠としてキャッシュする。商品種別はCategoryノードから判定する。
        """
        if self._product_catalog is not None:
            return self._product_catalog

        catalog: list[dict[str, Any]] = []
        cypher = (
            "MATCH (p:Product) "
            "OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute) "
            "WITH p, collect(DISTINCT {"
            "  attr_type:a.attr_type, value:a.value, value_ja:a.value_ja, "
            "  canonical_type:a.canonical_type, canonical_value:a.canonical_value"
            "}) AS attrs "
            "RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, "
            "       p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, "
            "       p.rating_count AS rating_count, p.description AS description, attrs "
            "ORDER BY p.product_id"
        )
        category_cypher = (
            "MATCH (p:Product)-[:BELONGS_TO]->(c:Category) "
            "RETURN p.product_id AS product_id, collect(DISTINCT c.name) AS categories"
        )
        # 集計(WITH)後にWHEREを置くことで、distinct値(~12万行)に対してだけ照合する
        mention_cypher = (
            "MATCH (p:Product)<-[:ABOUT]-(:Review)-[:MENTIONS]->(a:Attribute) "
            "WITH p.product_id AS product_id, "
            "     toLower(coalesce(a.canonical_value, a.value)) AS val, count(*) AS cnt "
            "WHERE any(alias IN $aliases WHERE val CONTAINS alias) "
            "RETURN product_id, collect([val, cnt]) AS mentions"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                for record in session.run(cypher):
                    row = dict(record)
                    row["attrs"] = [a for a in (row.get("attrs") or []) if isinstance(a, dict)]
                    catalog.append(row)
                categories = {
                    r["product_id"]: [str(c) for c in (r["categories"] or [])]
                    for r in session.run(category_cypher)
                }
                mentions = {
                    r["product_id"]: {
                        str(pair[0]): int(pair[1])
                        for pair in (r["mentions"] or [])
                        if isinstance(pair, (list, tuple)) and len(pair) == 2
                    }
                    for r in session.run(mention_cypher, aliases=_soft_facet_aliases())
                }
        except Exception as exc:
            print(f"[recommender] product catalog load failed: {exc}", file=sys.stderr)
            self._product_catalog = catalog
            return catalog

        for row in catalog:
            pid = row.get("product_id")
            cats = categories.get(pid, [])
            kinds = _kinds_from_categories(cats)
            # 属性側にproduct_kind情報があればそれも種別集合へ加える
            for attr in row["attrs"]:
                if _normalize_search_text(
                    str(attr.get("canonical_type") or attr.get("attr_type") or "")
                ) != "product_kind":
                    continue
                raw = _normalize_search_text(str(attr.get("canonical_value") or attr.get("value") or ""))
                for kind, aliases in _FACET_ALIAS_MAPS["product_kind"].items():
                    if raw == kind or any(
                        _text_has_token(raw, _normalize_search_text(a)) for a in aliases
                    ):
                        kinds.add(kind)
            row["kinds"] = kinds
            # プラットフォームはタイトル・カテゴリ名に最も濃く現れる(ゲーム1359件中1011件)
            row["platform_text"] = _normalize_search_text(
                f"{row.get('title') or ''} {row.get('title_ja') or ''} {' '.join(cats)}"
            )
            row["mention_counts"] = mentions.get(pid, {})
            row["description_norm"] = _normalize_search_text(
                _strip_html(row.pop("description", None)) or ""
            )
        self._product_catalog = catalog
        return catalog

    def _extract_search_signals(self, query: str, user_ctx: dict | None = None) -> dict[str, Any]:
        """Extract deterministic signals used for canonical ranking."""
        title_terms = _extract_title_terms(query)
        facet_signals = _extract_facet_signals(query)
        if user_ctx:
            for pref in user_ctx.get("preferred_attrs", []):
                if not isinstance(pref, dict):
                    continue
                pref_text = f"{pref.get('attr_type', '')} {pref.get('value', '')}"
                for ctype, values in _extract_facet_signals(pref_text).items():
                    facet_signals.setdefault(ctype, set()).update(values)
        return {
            "title_terms": title_terms,
            "facets": {k: sorted(v) for k, v in facet_signals.items()},
        }

    def _match_attr_to_signal(self, attr: dict[str, Any], facet_type: str, facet_values: list[str]) -> bool:
        attr_type = _normalize_search_text(str(attr.get("canonical_type") or attr.get("attr_type") or ""))
        if attr_type != _normalize_search_text(facet_type):
            return False

        value = attr.get("canonical_value") or attr.get("value") or ""
        value_text = _normalize_search_text(str(value))
        if not facet_values:
            return bool(value_text)

        alias_map = _FACET_ALIAS_MAPS.get(facet_type, {})
        candidate_aliases = set()
        for canonical_value in facet_values:
            candidate_aliases.add(canonical_value)
            candidate_aliases.update(_normalize_search_text(alias) for alias in alias_map.get(canonical_value, ()))
        return bool(value_text and value_text in candidate_aliases)

    def _rank_local_candidates(
        self, query: str, user_ctx: dict | None, limit: int, lang: str
    ) -> tuple[str, str, list[Recommendation]] | None:
        signals = self._extract_search_signals(query, user_ctx)
        title_terms: list[str] = signals["title_terms"]
        facet_signals: dict[str, list[str]] = signals["facets"]
        if not title_terms and not facet_signals:
            return None

        catalog = self._get_product_catalog()
        if not catalog:
            return None

        seen_ids = set()
        if user_ctx:
            for section in ("rated", "viewed"):
                for item in user_ctx.get(section, []) or []:
                    pid = item.get("product_id")
                    if pid:
                        seen_ids.add(str(pid))

        title_term_set = {term for term in title_terms if term not in _TITLE_GENERIC_TERMS}
        # 「ゼルダみたいな」等の類似検索では、名指しタイトルはハードフィルタにせず
        # 加点のみ（そのタイトル以外の類似候補も出すのが意図のため）
        similarity_query = any(m in query.lower() for m in _SIMILARITY_MARKERS)
        platform_values = facet_signals.get("platform") or []
        product_kind_values = facet_signals.get("product_kind") or []
        soft_facet_types = [
            ft for ft in facet_signals if ft not in ("product_kind", "platform")
        ]
        # (score, product, matched_attrs, 証拠のあったfacet_type集合)
        scored: list[tuple[float, dict[str, Any], list[dict[str, Any]], set[str]]] = []
        for product in catalog:
            product_id = str(product.get("product_id", ""))
            if product_id in seen_ids:
                continue

            title = _normalize_search_text(str(product.get("title") or ""))
            title_ja = _normalize_search_text(str(product.get("title_ja") or ""))
            matched_title_terms = [
                term for term in title_terms
                if term and (term in title or term in title_ja)
            ]
            specific_title_terms = [term for term in matched_title_terms if term not in _TITLE_GENERIC_TERMS]
            if title_term_set and not specific_title_terms and not similarity_query:
                # If the query clearly names a title/franchise, keep the result pool tight.
                continue

            attrs = product.get("attrs") or []
            matched_attrs: list[dict[str, Any]] = []
            evidence: set[str] = set()
            score = (
                _to_float(product.get("avg_rating")) or 3.5
            ) * _POPULARITY_RATING_WEIGHT + math.log(((_to_int(product.get("rating_count")) or 0) + 1)) * _POPULARITY_COUNT_WEIGHT

            # 商品種別: Category由来のkinds集合で判定する。種別が判明していて
            # 不一致なら除外、不明(カテゴリ・属性とも無し)なら除外せず残す。
            if product_kind_values:
                kinds: set[str] = product.get("kinds") or set()
                kind_hits = kinds & set(product_kind_values)
                if kinds and not kind_hits:
                    continue
                if kind_hits:
                    evidence.add("product_kind")
                    score += _FACET_WEIGHTS.get("product_kind", 4.0)
                    kind_value = sorted(kind_hits)[0]
                    matched_attrs.append({
                        "attr_type": "product_kind",
                        "value": kind_value,
                        "value_ja": _facet_value_ja("product_kind", kind_value),
                        "canonical_type": "product_kind",
                        "canonical_value": kind_value,
                    })

            if specific_title_terms:
                score += len(specific_title_terms) * _TITLE_MATCH_BOOST
                matched_attrs.extend(
                    {
                        "attr_type": "title",
                        "value": term,
                        "value_ja": _TITLE_ALIAS_JA.get(term),
                        "canonical_type": "title",
                        "canonical_value": term,
                    }
                    for term in specific_title_terms
                )

            platform_text = product.get("platform_text") or ""
            mention_counts: dict[str, int] = product.get("mention_counts") or {}
            description_norm = product.get("description_norm") or ""
            for facet_type, facet_values in facet_signals.items():
                if facet_type == "product_kind":
                    continue  # Categoryベースで判定済み
                weight = _FACET_WEIGHTS.get(facet_type, 1.0)
                facet_matches = [
                    attr for attr in attrs
                    if self._match_attr_to_signal(attr, facet_type, facet_values)
                ]

                if facet_type == "platform" and not facet_matches:
                    # プラットフォームは属性(554商品)よりタイトル・カテゴリ名に濃く
                    # 現れる(ゲーム1359件中1011件)ため、そちらも一致源として使う
                    for value in facet_values:
                        aliases = _facet_alias_set("platform", [value])
                        if any(_text_has_token(platform_text, a) for a in aliases):
                            facet_matches.append({
                                "attr_type": "platform",
                                "value": _PLATFORM_DISPLAY.get(value, value),
                                "value_ja": _PLATFORM_DISPLAY.get(value, value),
                                "canonical_type": "platform",
                                "canonical_value": value,
                            })

                mention_total = 0
                desc_hit = False
                if facet_type in _SOFT_EVIDENCE_FACETS:
                    # ジャンル等は属性が疎(genre実質~150商品)なので、レビュー
                    # MENTIONSの言及回数と説明文中の語も証拠として数える
                    aliases = _facet_alias_set(facet_type, facet_values)
                    mention_total = sum(
                        cnt for val, cnt in mention_counts.items()
                        if any(_text_has_token(val, a) for a in aliases)
                    )
                    desc_hit = any(_text_has_token(description_norm, a) for a in aliases)

                if facet_matches:
                    score += weight * min(len(facet_matches), 3)
                elif mention_total or desc_hit:
                    evidence_value = facet_values[0] if facet_values else facet_type
                    facet_matches = [{
                        "attr_type": facet_type,
                        "value": evidence_value,
                        "value_ja": _facet_value_ja(facet_type, evidence_value),
                        "canonical_type": facet_type,
                        "canonical_value": facet_values[0] if facet_values else None,
                    }]
                    score += weight
                if mention_total:
                    score += min(mention_total, 5) * 0.4
                if not facet_matches:
                    continue
                evidence.add(facet_type)
                matched_attrs.extend(facet_matches[:3])

            if platform_values and "platform" not in evidence:
                continue
            if not matched_attrs:
                continue

            scored.append((score, product, _unique_list(matched_attrs), evidence))

        # 適応的ハードフィルタ: 要求されたソフトファセット(ジャンル等)に証拠のある
        # 候補が十分(表示件数 or 3件以上)あるなら、証拠なし候補を落とす。証拠のある
        # 候補が少ない場合はソフト加点のみに留め、候補ゼロ化を防ぐ。
        for facet_type in soft_facet_types:
            with_evidence = [s for s in scored if facet_type in s[3]]
            if len(with_evidence) >= min(limit, 3):
                scored = with_evidence

        # 非ゲーム種別(コントローラー等)の要求では、種別確定の候補が1件でもあれば
        # 種別不明の候補を落とす。カテゴリ未整備の商品はほぼゲームなので、"ゲームが
        # 欲しい"では逆に種別不明を残す（本物のゲームを取りこぼさないため）。
        if product_kind_values and "game" not in product_kind_values:
            with_kind = [s for s in scored if "product_kind" in s[3]]
            if with_kind:
                scored = with_kind

        if not scored:
            return None

        scored.sort(
            key=lambda item: (
                item[0],
                _to_float(item[1].get("avg_rating")) or 0.0,
                _to_int(item[1].get("rating_count")) or 0,
                str(item[1].get("title") or ""),
            ),
            reverse=True,
        )
        top = scored[:limit]

        matched_labels = []
        specific_title_terms = [term for term in title_terms if term not in _TITLE_GENERIC_TERMS]
        if specific_title_terms:
            term = specific_title_terms[0]
            matched_labels.append(_TITLE_ALIAS_JA.get(term, term) if lang == "ja" else term)
        for facet_type in ("platform", "genre", "multiplayer_type", "play_mode", "product_kind"):
            values = facet_signals.get(facet_type)
            if values:
                label = values[0]
                if lang == "ja":
                    label = _facet_value_ja(facet_type, label) or label
                matched_labels.append(label)
        if len(matched_labels) > 3:
            matched_labels = matched_labels[:3]
        explanation = (
            "タイトルと正規化属性で再ランキングしました"
            if lang == "ja"
            else "Reranked by title and canonical attributes"
        )
        if matched_labels:
            if lang == "ja":
                explanation = " / ".join(matched_labels) + " に一致した候補を再ランキング"
            else:
                explanation = f"Reranked candidates matching {' / '.join(matched_labels)}"

        cypher = (
            "CANONICAL_RANKING "
            "MATCH (p:Product) ... "
            "using cached canonical_type/canonical_value reranking in Python"
        )
        results: list[Recommendation] = []
        for score, product, matched_attrs, _evidence in top:
            row = dict(product)
            row["score"] = score
            row["explanation"] = explanation
            row["matched_attrs"] = matched_attrs
            results.append(_record_to_recommendation(row, lang))
        return cypher, explanation, results

    def recommend(
        self, query: str, user_id: str | None = None, limit: int = 10, lang: str = "en",
        hints: list[str] | None = None,
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        """hints: chat()のLLMが会話全体から抽出した英語検索キーワード（フランチャイズ名・
        プラットフォーム・ジャンル等）。決定的マッチングのシグナル抽出テキストに加える。
        日本語の言い回しがLLM側で英語カタログ語彙に正規化されるため、エイリアス表に
        無い表現の取りこぼしを補える。LLM呼び出し失敗時はNone（従来動作）。"""
        search_id = str(uuid.uuid4())
        fallback = False
        normalized_lang = _normalize_lang(lang)
        signal_query = f"{query}\n{' '.join(hints)}" if hints else query
        user_ctx = self._get_user_context(user_id) if user_id else None
        # $uidが使えるのはuser_idがあり、かつ実際にRATED/属性の履歴がある場合のみ
        # （履歴が無ければ$uidを束縛しても個人化の意味が無く、誤ってuidを使われるのを防ぐ）
        has_uid = bool(user_ctx and (user_ctx.get("rated") or user_ctx.get("preferred_attrs")))
        dynamic_few_shot = self._get_dynamic_few_shot(user_id) if user_id else []
        canonical_search = self._rank_local_candidates(signal_query, user_ctx, limit, normalized_lang)
        if canonical_search:
            cypher, explanation, results = canonical_search
        else:
            direct_title_match = self._run_title_match(signal_query, limit, normalized_lang)
            if direct_title_match:
                cypher, explanation, results = direct_title_match
            else:
                system_prompt = _build_search_prompt(
                    self._genre, user_ctx, dynamic_few_shot, self._get_attr_vocab_text(), normalized_lang, has_uid
                )
                try:
                    params: dict[str, Any] = {"limit": limit}
                    if has_uid:
                        params["uid"] = user_id
                    cypher, explanation, results = self._generate_cypher_and_execute(
                        system_prompt, query, limit, params, has_uid, normalized_lang
                    )
                except Exception as exc:
                    print(f"[recommender] recommend failed, using fallback: {exc}", file=sys.stderr)
                    cypher, explanation, results = "", "", []
        # _generate_cypher_and_execute()は構文エラー・0件ヒットのどちらも内部でリトライ
        # した上で、それでも結果が得られない場合にだけ ("", "", []) を返す。
        # ここではその「リトライを使い切っても駄目だった」場合にだけフォールバックする。
        if not results:
            cypher, explanation = _FALLBACK_CYPHER, _popular_explanation(normalized_lang)
            results = self._run_popular(limit, normalized_lang)
            fallback = True
        intent = SearchIntent(cypher=cypher, cypher_explanation=explanation)
        if user_id:
            self.log_search(user_id, search_id, query, cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    _HOME_CACHE_TTL_SECONDS = 3600  # RATEDはこのデモでは実行時に変化しないので長めでよい

    def _get_home_cache_lock(self, cache_key: str) -> threading.Lock:
        """Return the per-home-cache-key lock, creating it atomically if needed."""
        with self._home_cache_locks_guard:
            lock = self._home_cache_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._home_cache_locks[cache_key] = lock
            return lock

    def _get_or_generate_home(
        self, user_id: str, limit: int, lang: str
    ) -> tuple[str, str, list[Recommendation]] | None:
        """履歴のあるユーザー向けホーム推薦をキャッシュ優先で返す（生成に失敗したらNone）。

        recommend_home()（通常の表示・SearchLogに残る）とwarm_home_cache()（タブを
        バックグラウンドに回した時などの先読み・ログに残さない）の両方から使う共通ロジック。
        """
        cache_key = f"{user_id}:{lang}:{limit}"
        cached = self._home_cache.get(cache_key)
        if cached and (time.time() - cached["cached_at"]) < self._HOME_CACHE_TTL_SECONDS:
            return cached["cypher"], cached["explanation"], cached["results"]

        # キャッシュを確認してからロックを取ることで、通常のキャッシュヒットは待たせない。
        # 待機したリクエストはロック取得後に必ず再確認するため、生成中に作られた結果を
        # そのまま返せる（double-checked single-flight）。
        with self._get_home_cache_lock(cache_key):
            cached = self._home_cache.get(cache_key)
            if cached and (time.time() - cached["cached_at"]) < self._HOME_CACHE_TTL_SECONDS:
                return cached["cypher"], cached["explanation"], cached["results"]

            user_ctx = self._get_user_context(user_id)
            dynamic_few_shot = self._get_dynamic_few_shot(user_id)
            try:
                system_prompt = _build_home_prompt(
                    self._genre, user_ctx, self._get_attr_vocab_text(), lang, dynamic_few_shot
                )
                user_msg = "Generate personalized home-page product recommendations based on user history."
                cypher, explanation, results = self._generate_cypher_and_execute(
                    system_prompt, user_msg, limit, {"limit": limit, "uid": user_id}, True, lang
                )
            except Exception as exc:
                print(f"[recommender] home generation failed: {exc}", file=sys.stderr)
                return None
            if not results:
                return None
            self._home_cache[cache_key] = {
                "cypher": cypher, "explanation": explanation, "results": results, "cached_at": time.time(),
            }
            return cypher, explanation, results

    def warm_home_cache(self, user_id: str | None, limit: int = 10, lang: str = "en") -> None:
        """タブを閉じる/バックグラウンドに回した時などに呼ばれるfire-and-forget用途。

        次回開いた時にrecommend_home()が即座に返せるよう、ホーム推薦を先読みして
        キャッシュしておく。応答を待つ相手がいないのでSearchLogには残さない。
        キャッシュが既に新しければ_get_or_generate_home()内で即returnされ、LLMは呼ばれない。
        """
        if not user_id:
            return  # 非個人化モード（user_id無し）はキャッシュ対象がないため何もしない
        normalized_lang = _normalize_lang(lang)
        user_ctx = self._get_user_context(user_id)
        has_history = bool(user_ctx.get("rated") or user_ctx.get("preferred_attrs"))
        if not has_history:
            return  # 履歴が無いユーザーは人気商品フォールバックの高速パスで十分、キャッシュ不要
        self._get_or_generate_home(user_id, limit, normalized_lang)

    def recommend_home(
        self, user_id: str | None, limit: int = 10, lang: str = "en"
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        normalized_lang = _normalize_lang(lang)
        # VIEWEDだけでは属性情報が得られないため、RATEDまたは属性があるときのみパーソナライズ。
        # user_id自体が無い（非個人化モード）場合も同様にhas_history=Falseとして扱う。
        user_ctx = self._get_user_context(user_id) if user_id else None
        has_history = bool(user_ctx and (user_ctx.get("rated") or user_ctx.get("preferred_attrs")))
        generated = self._get_or_generate_home(user_id, limit, normalized_lang) if has_history and user_id else None
        if generated:
            cypher, explanation, results = generated
            fallback = False
        else:
            # has_history=Falseなら想定内（個人化する材料が無いだけ）、
            # has_history=Trueなのに生成に失敗した場合だけ本当のフォールバック扱いにする。
            cypher, explanation = _FALLBACK_CYPHER, _popular_explanation(normalized_lang)
            results = self._run_popular(limit, normalized_lang)
            fallback = has_history
        intent = SearchIntent(cypher=cypher, cypher_explanation=explanation)
        if user_id:
            self.log_search(user_id, search_id, "[home]", cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    def chat(
        self, messages: list[dict[str, Any]], limit: int = 10, lang: str = "ja",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """対話型推薦の1ターン。

        どの属性について聞くか・いつ検索に切り替えるかはハードコードせず、LLM自身の
        action/filled_slotsに委ねる（カテゴリ非依存）。ただし初回は必ず一度だけ
        聞き返すため、最低2ユーザーターン後に推薦する。Python側はMAX_QUESTIONSの
        安全網と、LLM呼び出し自体が失敗した場合の安全な初回質問を持つ。
        search が決まった後の商品検索は self.recommend() 経由の Text2Cypher に委譲する。
        """
        all_user_msgs = [m for m in messages if m.get("role") == "user"]
        asked = sum(1 for m in messages if m.get("role") == "assistant")
        normalized_lang = _normalize_lang(lang)
        target_language = "Japanese" if normalized_lang == "ja" else "English"

        system = _build_chat_system_prompt(self._genre, self._get_attr_vocab_text(), target_language)
        llm_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
            llm_messages.append({"role": role, "content": m.get("content", "")})

        try:
            response = self._llm.chat.completions.create(
                model=self._model, messages=llm_messages,
                response_format={"type": "text"}, temperature=0,
            )
            data = _parse_llm_json(response.choices[0].message.content or "{}")
        except Exception as exc:
            print(f"[recommender] chat LLM call failed, forcing search: {exc}", file=sys.stderr)
            data = {}

        summary = data.get("preference_summary") or []
        try:
            filled_slots = int(data.get("filled_slots", 0))
        except (TypeError, ValueError):
            filled_slots = 0

        # 初回入力では、希望が十分具体的でも一度は確認質問を返す。LLMが初手で
        # searchを選んだ・一時的に失敗した場合も、空の質問画面を出さないよう
        # 固定の確認質問へフォールバックする。
        must_ask_first_question = asked == 0
        should_search = (
            not must_ask_first_question
            and (
                not data
                or data.get("action") == "search"
                or filled_slots >= 2
                or asked >= MAX_QUESTIONS
            )
        )

        # ── 結果を返す：search は Text2Cypher に委譲 ─────────────────────────
        if should_search:
            query_text = " ".join(m.get("content", "") for m in all_user_msgs)
            # LLMが会話全体から抽出した英語キーワードを構造化ヒントとして渡す
            # （従来は全ユーザー発言の連結文字列だけで、LLMの理解結果を捨てていた）
            hints = [
                k.strip() for k in (data.get("search_keywords") or [])
                if isinstance(k, str) and k.strip()
            ][:8]
            search_id, intent, products, _fallback = self.recommend(
                query_text, user_id, limit, normalized_lang, hints=hints or None
            )
            return {
                "action": "search",
                "question": None,
                "options": [],
                "preference_summary": summary,
                "intent": intent,
                "recommendations": products,
                "search_id": search_id,
            }

        if must_ask_first_question:
            fallback_question = (
                "よりぴったりなゲームを選ぶため、どの遊び方を重視しますか？"
                if normalized_lang == "ja"
                else "To find a better match, what kind of play do you prefer?"
            )
            fallback_options = (
                ["友達と協力して遊びたい", "一人でじっくり遊びたい", "アクションを楽しみたい", "こだわりなし"]
                if normalized_lang == "ja"
                else ["Play cooperatively with friends", "Enjoy playing solo", "Enjoy action", "No preference"]
            )
            question = data.get("question") if data.get("action") == "ask" else None
            options = data.get("options") if data.get("action") == "ask" else None
        else:
            fallback_question = "他にご希望はありますか？" if normalized_lang == "ja" else "Any other preferences?"
            fallback_options = []
            question = data.get("question")
            options = data.get("options")
        return {
            "action": "ask",
            "question": question or fallback_question,
            "options": options or fallback_options,
            "preference_summary": summary,
            "intent": None,
            "recommendations": [],
            "search_id": None,
        }

    def _get_attr_vocab_text(self) -> str:
        """グラフに実在するattr_typeの語彙をプロンプト用テキストにして返す（初回のみNeo4jに問い合わせてキャッシュ）。"""
        if self._attr_vocab_text is not None:
            return self._attr_vocab_text
        vocab: list[dict[str, Any]] = []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (a:Attribute) "
                    "RETURN a.attr_type AS attr_type, "
                    "       collect(DISTINCT a.value)[0..6] AS examples, "
                    "       count(*) AS freq "
                    "ORDER BY freq DESC LIMIT 20"
                )
                vocab = [{"attr_type": r["attr_type"], "examples": r["examples"]} for r in res]
        except Exception as exc:
            print(f"[recommender] attr vocabulary lookup failed: {exc}", file=sys.stderr)
        self._attr_vocab_text = _format_attr_vocab(vocab)
        return self._attr_vocab_text

    def sample_users(self, limit: int = 10) -> list[dict[str, Any]]:
        """パーソナライズのデモ用に、評価履歴を持つ実ユーザーを何件か返す。"""
        with self._driver.session(database=self._neo4j_db) as session:
            res = session.run(
                "MATCH (u:User)-[r:RATED]->(:Product) "
                "WITH u, count(r) AS rated_count "
                "WHERE rated_count >= 3 "
                "RETURN u.user_id AS user_id, rated_count "
                "ORDER BY rated_count DESC LIMIT $limit",
                limit=limit,
            )
            return [{"user_id": r["user_id"], "rated_count": r["rated_count"]} for r in res]

    def get_reviews(self, product_id: str, limit: int = 5, lang: str = "en") -> list[dict[str, Any]]:
        """商品IDに紐づくレビューをhelpful_vote降順で返す。(Review)-[:ABOUT]->(Product) エッジを使用。
        lang="ja"の場合、backfill_display_fields.py --reviews-jaで付与済みのtitle_ja/text_jaが
        あればそちらを本文として返す（無ければ通常のtext/titleにフォールバック）。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})<-[:ABOUT]-(r:Review)
WHERE r.text IS NOT NULL AND size(coalesce(r.text, '')) > 10
RETURN r.title AS title,
       r.title_ja AS title_ja,
       r.text AS text,
       r.text_ja AS text_ja,
       toFloat(r.rating) AS rating,
       toInteger(r.helpful_vote) AS helpful_vote,
       r.verified AS verified_purchase
ORDER BY r.helpful_vote DESC, r.rating DESC, r.review_id ASC
LIMIT $limit
"""
        use_ja = _normalize_lang(lang) == "ja"
        with self._driver.session(database=self._neo4j_db) as session:
            result = session.run(cypher, product_id=product_id, limit=limit)
            rows = []
            for record in result:
                r = dict(record)
                text_ja = r.pop("text_ja", None)
                title_ja = r.pop("title_ja", None)
                r["text"] = text_ja if (use_ja and text_ja) else _strip_html(r.get("text"))
                r["title"] = title_ja if (use_ja and title_ja) else _strip_html(r.get("title"))
                rows.append(r)
            return rows

    # 説明文の80%はこの範囲に全文が収まる（中央値1640字・超過分は定型文が多い）。
    # kg_build/backfill_display_fields.py の DESCRIPTION_MAX_CHARS と同じ値。
    _DESCRIPTION_MAX_CHARS: int = 4000

    @staticmethod
    def _cut_at_sentence(text: str, max_chars: int) -> str:
        """max_charsを超えるテキストを直近の文末（. ! ? 。等）で切り詰める。
        文の途中でぶつ切りにしない（kg_build/backfill_display_fields.pyの
        cut_at_sentence()と同じロジック）。"""
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars]
        last = max(cut.rfind(ch) for ch in ".!?。！？")
        if last >= max_chars // 2:
            return cut[: last + 1].rstrip()
        trimmed = cut.rsplit(" ", 1)[0].rstrip()
        return (trimmed or cut.rstrip()) + "…"

    def get_description(self, product_id: str, lang: str = "en") -> dict[str, Any] | None:
        """商品説明文(description)を返す。Text2Cypherの検索結果には含めず、UIが商品IDを
        指定して個別取得する(get_reviews()と同じ設計)。理由: description は最大2万字超と
        長く、生成Cypherの必須RETURN項目に加えると全検索クエリの負荷・失敗要因が増える。
        lang="ja"の場合、backfill_display_fields.py --descriptions-jaで付与済みの
        description_jaがあればそちらを返す（無ければ英語原文にフォールバック）。
        英語原文は末尾に著作権表示等の定型文が続くことが多いため、表示用に
        _DESCRIPTION_MAX_CHARSまで・文末境界で切る（description_jaも同じ長さの
        原文から翻訳されているので一貫性がある）。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})
WHERE p.description IS NOT NULL AND p.description <> ''
RETURN p.description AS description, p.description_ja AS description_ja
"""
        use_ja = _normalize_lang(lang) == "ja"
        with self._driver.session(database=self._neo4j_db) as session:
            record = session.run(cypher, product_id=product_id).single()
            if record is None:
                return None
            description = record["description"]
            description_ja = record["description_ja"]
            if use_ja and description_ja:
                text = description_ja
            else:
                text = self._cut_at_sentence(
                    _strip_html(description) or "", self._DESCRIPTION_MAX_CHARS
                )
            return {"description": text, "translated": bool(use_ja and description_ja)}

    def save_feedback(
        self,
        product_id: str,
        user_id: str | None,
        search_id: str | None,
        helpful: bool,
        lang: str = "ja",
    ) -> bool:
        """「この推薦は役に立ちましたか？」のユーザー回答をFeedbackノードとして保存する。

        (f:Feedback)-[:ABOUT]->(p:Product) を必ず張り、user_id/search_idが実在すれば
        (u:User)-[:GAVE]->(f) と (f)-[:FOR_SEARCH]->(sl:SearchLog) も張る。
        SearchLogの照合キーはlog_idプロパティ（recommend()が返すsearch_idと同じ値）。
        positiveなフィードバックは_get_dynamic_few_shot()がクリックより強い正例シグナル
        として利用する。

        商品が存在しない場合は ``False`` を返す。Neo4jの接続・書込み失敗は握り潰さず
        呼び出し元へ送出し、APIが成功表示を返さないようにする。"""
        fid = str(uuid.uuid4())
        ts = int(time.time() * 1000)
        write_cypher = """
MATCH (p:Product {product_id: $product_id})
CREATE (f:Feedback {feedback_id: $fid, product_id: $product_id,
                    helpful: $helpful, lang: $lang, created_at: $ts})
CREATE (f)-[:ABOUT]->(p)
WITH f
OPTIONAL MATCH (sl:SearchLog {log_id: $search_id})
FOREACH (_ IN CASE WHEN sl IS NULL THEN [] ELSE [1] END | CREATE (f)-[:FOR_SEARCH]->(sl))
WITH f
OPTIONAL MATCH (u:User {user_id: $user_id})
FOREACH (_ IN CASE WHEN u IS NULL THEN [] ELSE [1] END | CREATE (u)-[:GAVE]->(f))
RETURN count(DISTINCT f) AS saved
"""
        with self._driver.session(database=self._neo4j_db) as session:
            record = session.run(
                write_cypher,
                product_id=product_id,
                fid=fid,
                helpful=helpful,
                lang=lang,
                ts=ts,
                search_id=search_id,
                user_id=user_id,
            ).single()
        return bool(record and record["saved"] == 1)

    _MAX_VIEWED: int = 20
    _MAX_SEARCHES: int = 30

    def log_search(
        self,
        user_id: str,
        search_id: str,
        query: str,
        cypher: str,
        explanation: str,
        result_product_ids: list[str],
    ) -> None:
        """Record a search as a SearchLog node linked to the user."""
        ts = int(time.time() * 1000)
        write_cypher = (
            "MERGE (u:User {user_id: $uid}) "
            "CREATE (sl:SearchLog {"
            "  log_id: $log_id, query: $q, cypher: $cypher,"
            "  explanation: $explanation, result_product_ids: $pids,"
            "  result_count: $rc, timestamp: $ts"
            "}) "
            "CREATE (u)-[:SEARCHED]->(sl)"
        )
        # Keep only the latest _MAX_SEARCHES SearchLog nodes per user.
        trim_cypher = (
            "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
            "WITH sl ORDER BY sl.timestamp DESC "
            "WITH collect(sl) AS all_sl "
            "FOREACH (old IN all_sl[$keep..] | DETACH DELETE old)"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                session.run(
                    write_cypher,
                    uid=user_id, log_id=search_id, q=query,
                    cypher=cypher, explanation=explanation,
                    pids=result_product_ids, rc=len(result_product_ids), ts=ts,
                )
                session.run(trim_cypher, uid=user_id, keep=self._MAX_SEARCHES)
        except Exception as exc:
            print(f"[recommender] log_search failed: {exc}", file=sys.stderr)

    def log_view(self, user_id: str, product_id: str, search_id: str | None = None) -> None:
        """Record that a user viewed a product (VIEWED edge in Neo4j)."""
        ts = int(time.time() * 1000)
        write_cypher = (
            "MERGE (u:User {user_id: $uid}) "
            "WITH u "
            "MATCH (p:Product {product_id: $pid}) "
            "CREATE (u)-[:VIEWED {timestamp: $ts, search_id: $sid}]->(p)"
        )
        # Keep only the latest _MAX_VIEWED edges per user; delete the rest.
        trim_cypher = (
            "MATCH (u:User {user_id: $uid})-[v:VIEWED]->() "
            "WITH v ORDER BY v.timestamp DESC "
            "WITH collect(v) AS vs "
            "FOREACH (old IN vs[$keep..] | DELETE old)"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                session.run(write_cypher, uid=user_id, pid=product_id, ts=ts, sid=search_id)
                session.run(trim_cypher, uid=user_id, keep=self._MAX_VIEWED)
        except Exception as exc:
            print(f"[recommender] log_view failed: {exc}", file=sys.stderr)

    def clear_behavior_history(self, user_id: str) -> dict[str, int]:
        """RATED（データセット由来の評価履歴）はそのままに、このユーザーの永続的な行動データ
        ——VIEWED行動ログ・SearchLog検索履歴・Feedback——だけを削除する。デモで行動ログを
        まっさらな状態に戻したい時に使う（推薦の根拠であるRATED自体は変更しない）。"""
        with self._driver.session(database=self._neo4j_db) as session:
            viewed_result = session.run(
                "MATCH (:User {user_id: $uid})-[r:VIEWED]->() "
                "WITH collect(r) AS rs "
                "FOREACH (x IN rs | DELETE x) "
                "RETURN size(rs) AS n",
                uid=user_id,
            ).single()
            search_result = session.run(
                "MATCH (:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                "WITH collect(sl) AS sls "
                "FOREACH (x IN sls | DETACH DELETE x) "
                "RETURN size(sls) AS n",
                uid=user_id,
            ).single()
            feedback_result = session.run(
                "MATCH (:User {user_id: $uid})-[:GAVE]->(f:Feedback) "
                "WITH collect(f) AS fs "
                "FOREACH (x IN fs | DETACH DELETE x) "
                "RETURN size(fs) AS n",
                uid=user_id,
            ).single()
        # 古い履歴を前提に生成されたホーム推薦キャッシュも破棄する
        for key in [k for k in self._home_cache if k.startswith(f"{user_id}:")]:
            del self._home_cache[key]
        return {
            "viewed_deleted": viewed_result["n"] if viewed_result else 0,
            "searches_deleted": search_result["n"] if search_result else 0,
            "feedback_deleted": feedback_result["n"] if feedback_result else 0,
        }

    def close(self) -> None:
        self._driver.close()

    # ── user context from Neo4j ─────────────────────────────────────────────────

    def _get_user_context(self, user_id: str) -> dict:
        rated: list[dict] = []
        viewed: list[dict] = []
        preferred_attrs: list[dict] = []
        recent_queries: list[str] = []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product) "
                    "RETURN p.product_id AS product_id, p.title AS title, r.rating AS rating "
                    "ORDER BY r.rating DESC, r.timestamp DESC LIMIT 6",
                    uid=user_id,
                )
                rated = [{"product_id": r["product_id"], "title": r["title"], "rating": r["rating"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[v:VIEWED]->(p:Product) "
                    "RETURN p.product_id AS product_id, p.title AS title "
                    "ORDER BY v.timestamp DESC LIMIT 4",
                    uid=user_id,
                )
                viewed = [{"product_id": r["product_id"], "title": r["title"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product)"
                    "-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "WHERE r.rating >= 4 "
                    "RETURN coalesce(a.canonical_type, a.attr_type) AS attr_type, "
                    "       coalesce(a.canonical_value, a.value) AS value, "
                    "count(*) AS freq "
                    "ORDER BY freq DESC LIMIT 8",
                    uid=user_id,
                )
                preferred_attrs = [
                    {"attr_type": r["attr_type"], "value": r["value"], "freq": r["freq"]}
                    for r in res
                ]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                    "WHERE sl.query <> '[home]' "
                    "RETURN sl.query AS query "
                    "ORDER BY sl.timestamp DESC LIMIT 5",
                    uid=user_id,
                )
                recent_queries = [r["query"] for r in res]
        except Exception as exc:
            print(f"[recommender] _get_user_context failed: {exc}", file=sys.stderr)
        return {
            "rated": rated,
            "viewed": viewed,
            "preferred_attrs": preferred_attrs,
            "recent_queries": recent_queries,
        }

    def _get_dynamic_few_shot(self, user_id: str) -> list[dict]:
        """クリックまたはpositiveフィードバックにつながった検索を返す
        （negativeが付いた検索は除外）。"""
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                    "WHERE sl.cypher IS NOT NULL AND sl.query <> '[home]' "
                    "WITH u, sl "
                    "OPTIONAL MATCH (u)-[v:VIEWED]->(:Product) WHERE v.search_id = sl.log_id "
                    "WITH u, sl, count(v) AS clicks "
                    "OPTIONAL MATCH (f:Feedback)-[:FOR_SEARCH]->(sl) "
                    "WITH sl, clicks, "
                    "     sum(CASE WHEN f.helpful THEN 1 ELSE 0 END) AS pos, "
                    "     sum(CASE WHEN f.helpful = false THEN 1 ELSE 0 END) AS neg "
                    "WHERE neg = 0 AND (clicks > 0 OR pos > 0) "
                    "RETURN sl.query AS query, sl.cypher AS cypher, sl.explanation AS explanation "
                    "ORDER BY (clicks + pos * 3) DESC, sl.timestamp DESC LIMIT 3",
                    uid=user_id,
                )
                return [
                    {"query": r["query"], "cypher": r["cypher"], "explanation": r["explanation"]}
                    for r in res
                ]
        except Exception as exc:
            print(f"[recommender] _get_dynamic_few_shot failed: {exc}", file=sys.stderr)
            return []

    # ── LLM call ────────────────────────────────────────────────────────────────

    def _call_llm(self, system: str, user: str) -> dict[str, Any]:
        resp = self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "text"},
            temperature=0,
            max_tokens=3000,
        )
        return _parse_llm_json(resp.choices[0].message.content or "{}")

    # ── Cypher generation with retry-on-error / retry-on-empty ──────────────────

    def _generate_cypher_and_execute(
        self,
        system_prompt: str,
        user_msg: str,
        limit: int,
        exec_params: dict[str, Any],
        has_uid: bool = False,
        lang: str = "en",
    ) -> tuple[str, str, list[Recommendation]]:
        """Cypherの生成・検証・実行を1つのリトライループの中で行う。

        構文/検証エラーだけでなく、「構文的には正しいが実行結果が0件」だった場合も
        リトライ対象にする（以前は0件を検証エラーと区別できず、即座に諦めて全面
        フォールバックしていた）。全試行を使い切っても結果が得られなければ
        ("", "", []) を返し、呼び出し元が全面フォールバックするための合図とする。
        """
        data = self._call_llm(system_prompt, user_msg)
        cypher: str = data.get("cypher", "").strip()
        explanation: str = data.get("explanation", "")
        fix_prompt = _build_fix_prompt(lang)

        for attempt in range(self._max_attempts):
            is_last = attempt >= self._max_attempts - 1
            try:
                self._validate_cypher(cypher, limit, has_uid)
            except Exception as exc:
                if is_last:
                    break
                cypher, explanation = self._request_cypher_fix(
                    fix_prompt, user_msg, cypher, f"Neo4j error:\n{exc}"
                )
                continue

            results = self._execute_and_map(cypher, exec_params, lang)
            if results:
                return cypher, explanation, results
            if is_last:
                break
            cypher, explanation = self._request_cypher_fix(
                fix_prompt, user_msg, cypher,
                "This query ran without error but matched 0 products. Broaden the "
                "filters (loosen attribute matches, remove overly specific conditions, "
                "or widen thresholds) and try again — do not repeat the same query.",
            )

        return "", "", []

    def _request_cypher_fix(self, fix_prompt: str, user_msg: str, cypher: str, feedback: str) -> tuple[str, str]:
        fix_user = f"Original request: {user_msg}\n\nCurrent Cypher:\n{cypher}\n\n{feedback}"
        try:
            fix_data = self._call_llm(fix_prompt, fix_user)
            return fix_data.get("cypher", cypher).strip(), fix_data.get("explanation", "")
        except Exception:
            return cypher, ""

    _UID_REF = re.compile(r"\$uid\b")
    _HARDCODED_USER_ID = re.compile(r"user_id\s*[:=]\s*['\"]")

    def _validate_cypher(self, cypher: str, limit: int, has_uid: bool = False) -> None:
        if not cypher:
            raise ValueError("Empty Cypher query")
        if self._HARDCODED_USER_ID.search(cypher):
            raise ValueError(
                "Query hardcodes a literal user_id string instead of using the $uid "
                "parameter. NEVER hardcode a user_id — always reference $uid (only when "
                "a user is actually bound for this request)."
            )
        if not has_uid and self._UID_REF.search(cypher):
            raise ValueError(
                "Query references $uid but no user_id was provided for this request — "
                "$uid will not be bound. Rewrite the query without $uid or any pattern "
                "that requires a specific User node."
            )
        with self._driver.session(database=self._neo4j_db) as session:
            session.run(f"EXPLAIN {cypher}", limit=limit).consume()

    # ── query execution ──────────────────────────────────────────────────────────

    def _execute_and_map(self, cypher: str, params: dict, lang: str = "en") -> list[Recommendation]:
        if not cypher:
            return []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                result = session.run(cypher, **params)
                return [_record_to_recommendation(dict(record), lang) for record in result]
        except Exception as exc:
            print(f"[recommender] Cypher execution failed: {exc}", file=sys.stderr)
            return []

    def _run_popular(self, limit: int, lang: str) -> list[Recommendation]:
        """LLMを介さず、人気・高評価商品を直接クエリする（フォールバック用途と、
        パーソナライズ不要な初期表示の高速パス用途で共有する）。"""
        return self._execute_and_map(
            _FALLBACK_CYPHER, {"limit": limit, "explanation": _popular_explanation(lang)}, lang
        )
