from __future__ import annotations

import html as _html_mod
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from openai import OpenAI

from .models import AttributeFilter, MatchedAttribute, QueryPlan, Recommendation, SearchIntent
from .query_plan import build_controlled_query_plan, enabled_action_names

ATTRIBUTE_TYPES = [
    "benefit", "skin_type", "scent", "texture", "ingredient",
    "material", "color", "size", "target_area", "usage",
    "brand", "product_type",
]

INTENT_SYSTEM_PROMPT = f"""You are a beauty product search assistant. Extract structured search criteria from the user's natural language query and return a JSON object.

Map user intent to these attribute_type values:
- benefit: desired effects (moisturizing, anti-aging, brightening, soothing, etc.)
- skin_type: skin concern (dry, oily, sensitive, combination, acne-prone, etc.)
- scent: fragrance preference (floral, unscented, fresh, citrus, etc.)
- texture: product feel (lightweight, creamy, gel, thick, watery, etc.)
- ingredient: specific ingredients (vitamin c, retinol, hyaluronic acid, niacinamide, etc.)
- material: material or formula type
- color: product color if relevant
- size: size preference
- target_area: area of application (face, eye, lip, body, hair, etc.)
- usage: when or how to use (daytime, nighttime, daily, weekly, etc.)
- brand: specific brand preference
- product_type: type of product (serum, moisturizer, cleanser, toner, sunscreen, etc.)

For weight: 1.0 = essential, 0.7 = important, 0.4 = nice-to-have.

Return JSON with this exact structure:
{{
  "attribute_filters": [
    {{"attribute_type": "...", "value": "...", "weight": 0.0}}
  ],
  "keywords": ["..."],
  "price_max": null,
  "min_rating": null
}}"""

# 対話型推薦：聞き返しは最大この回数まで（しつこくしない）
MAX_QUESTIONS = 5  # 無限ループ防止の安全網。通常はLLMが先にsearchを選ぶ。

CHAT_SYSTEM_PROMPT = f"""You are a warm, friendly beauty-product shopping assistant. The catalog is English (Amazon All_Beauty). The user may write in Japanese or English — always respond in the TARGET LANGUAGE at the end of these instructions.

## SLOTS to collect
- product_type: serum, moisturizer, cleanser, toner, sunscreen, shampoo, conditioner, perfume, makeup ...
- skin_type: dry, oily, sensitive, combination, acne-prone, normal
- hair_type: damaged, dry, oily, color-treated, curly, fine
- scent: unscented, floral, citrus, woody, fresh, sweet, musky
- benefit: moisturizing, brightening, anti-aging, soothing, volumizing, strengthening ...
- texture: cream, gel, lotion, oil, balm, foam, mist ...
- ingredient: hyaluronic acid, vitamin c, retinol, niacinamide ... (or to avoid)
- price_max (USD), min_rating, brand

## DECISION RULE
filled_slots = distinct answered slots from: skin_type, hair_type, scent, benefit, texture, ingredient, price_max, min_rating. (product_type does NOT count)
- action="ask"    if product_type unknown OR filled_slots < 2 AND questions < {MAX_QUESTIONS}
- action="search" if filled_slots >= 2 OR no-preference stated OR questions >= {MAX_QUESTIONS}

## QUESTION STYLE — most important rule
Always echo the user's own words or concern in your question. Never ask a generic question that ignores what they said.
- user: "髪ダメージが多い"  → "髪のダメージケアに、どんなアイテムをお探しですか？" ✓
- user: "髪ダメージが多い"  → "どんなアイテムをお探しですか？" ✗
- user: "肌荒れが気になる"  → "肌荒れのケアには、どんな効果を重視しますか？" ✓
- user: "プレゼントに"      → "素敵ですね！誰へのプレゼントですか？" ✓
Keep tone warm and natural — not robotic.

## CONTEXT QUESTIONS (ask before normal slots, max 1 per conversation)
If a context clue would meaningfully narrow the search, ask ONE follow-up. Otherwise infer and add keywords.
- プレゼント/gift → ask recipient if unknown ("お母さん・年配", "彼女", "友人", "男性", "こだわらない") → then ask budget if unknown
- 旅行/travel/出張 → ask duration ("数日→travel size", "1週間以上→通常サイズ", "こだわらない")
- 運動/スポーツ → ask type ("屋外ランニング", "ジム", "水泳", "こだわらない")
- ご褒美/特別な日/luxury → no question, add keywords: luxury, premium
- 夏 → no question: lightweight, oil-free, SPF | 冬 → rich, nourishing
- 職場/オフィス → no question: subtle, light, professional
- Other contexts: use judgment — ask if it clearly helps, otherwise infer keywords

## SLOT PRIORITY (after context resolved)
- skincare: skin_type → benefit → texture → ingredient → price_max
- haircare:  hair_type → benefit → scent → ingredient → price_max
- fragrance: scent → benefit → price_max
- makeup:    skin_type → benefit → texture → price_max
- other:     product_type → benefit → price_max

When action="ask": "options" MUST contain 3–5 short quick-reply strings in TARGET LANGUAGE (NEVER an empty array). Always include "こだわらない" / "No preference" as the last option.
NOTE: benefit inferred from product name alone (e.g. 保湿クリーム) does NOT count as filled — user must confirm.

## SEARCH INTENT (when action="search")
- attribute_filters: [{{"attribute_type": one of [{", ".join(ATTRIBUTE_TYPES)}], "value": "...", "weight": 1.0|0.7|0.4}}]
- keywords: translate ALL context and slot values to English catalog terms.
  Examples: 敏感肌→sensitive, 無香料→unscented, プレゼント→gift gift-set, 旅行→travel-size portable,
  ご褒美→luxury premium, 夏→lightweight SPF, 職場→subtle professional, メンズ→men for-him,
  お母さん向け→anti-aging mature-skin, 水泳→waterproof chlorine-resistant
- preference_summary: confirmed preferences as short labels in TARGET LANGUAGE. e.g. ["化粧水","敏感肌","無香料"]
- price_max, min_rating: number or null

## PRODUCT CATEGORY
Classify the user's request into ONE of: skincare, haircare, fragrance, makeup, other.
Use this to catch cases a simple keyword list would miss — e.g. "肌荒れ", "頭皮が乾燥", "唇が荒れる", synonyms, typos, or indirect descriptions of a concern.
If genuinely unclear from the conversation so far, use null.

ALWAYS return ONLY valid JSON — no text before or after. Example for action="ask" (options non-empty):
{{
  "action": "ask",
  "question": "髪のダメージケアに、どんなアイテムをお探しですか？",
  "options": ["シャンプー", "トリートメント", "ヘアオイル", "コンディショナー", "こだわらない"],
  "slot": "product_type",
  "filled_slots": 0,
  "intent": {{"attribute_filters": [], "keywords": [], "price_max": null, "min_rating": null}},
  "preference_summary": [],
  "product_category": "haircare"
}}
Field reference:
  "action": "ask" | "search"
  "question": "質問文" or null (null only when action="search")
  "options": non-empty array when action="ask", [] when action="search"
  "slot": slot_name or null
  "filled_slots": <int>
  "intent": {{"attribute_filters": [], "keywords": [], "price_max": null, "min_rating": null}}
  "preference_summary": []
  "product_category": "skincare" | "haircare" | "fragrance" | "makeup" | "other" | null
}}"""

# Attribute recall: precise, explainable matches from LLM-extracted product attributes.
# Dedup by (attribute_type, value) first so duplicate Attribute nodes do not inflate the score.
_ATTRIBUTE_SEARCH_CYPHER = """
UNWIND $filters AS f
MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
WHERE a.attribute_type = f.attribute_type
  AND toLower(a.value) CONTAINS toLower(f.value)
  AND coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p, a.attribute_type AS atype, a.value AS aval,
     max(toFloat(r.confidence)) AS best_confidence,
     head(collect(r.evidence)) AS evidence,
     f.weight AS weight
WITH p,
     collect({
       attribute_type: atype,
       name: atype,
       value: aval,
       confidence: best_confidence,
       evidence: evidence,
       weight: weight
     }) AS matched_attrs,
     sum(best_confidence * weight) AS score
WHERE size(matched_attrs) >= 1
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       matched_attrs,
       score AS attribute_score
ORDER BY score DESC, toFloat(p.average_rating) DESC
LIMIT $candidate_limit
"""

# Feature recall: broader recall from raw product feature/description text.
_FEATURE_SEARCH_CYPHER = """
UNWIND $terms AS term
MATCH (p:Product)-[:HAS_FEATURE]->(f:Feature)
WHERE toLower(coalesce(f.normalized_text, f.text, "")) CONTAINS term
  AND coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p, term, head(collect(f.text)) AS evidence, count(DISTINCT f) AS hits
WITH p,
     collect({term: term, evidence: evidence, hits: hits}) AS feature_matches,
     count(DISTINCT term) AS feature_term_hits,
     sum(hits) AS feature_hit_count
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       feature_matches,
       feature_term_hits,
       feature_hit_count
ORDER BY feature_term_hits DESC, feature_hit_count DESC, toFloat(p.average_rating) DESC
LIMIT $candidate_limit
"""

# Field recall: cheap title/category/store matches that cover products without attributes/features.
_FIELD_SEARCH_CYPHER = """
UNWIND $terms AS term
MATCH (p:Product)
OPTIONAL MATCH (p)-[:SOLD_BY]->(s:Store)
WITH p, term, collect(DISTINCT s.name) AS store_names
WHERE (
    toLower(coalesce(p.title, "")) CONTAINS term
    OR toLower(coalesce(p.main_category, "")) CONTAINS term
    OR any(store_name IN store_names WHERE toLower(coalesce(store_name, "")) CONTAINS term)
  )
  AND coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p, collect(DISTINCT term) AS field_terms
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       field_terms
ORDER BY size(field_terms) DESC, toFloat(p.average_rating) DESC
LIMIT $candidate_limit
"""

_HOME_RECOMMEND_CYPHER = """
MATCH (p:Product)
WHERE coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
WITH p,
     coalesce(toFloat(p.average_rating), 3.8) AS rating,
     coalesce(toInteger(p.rating_number), 0) AS rating_count,
     coalesce(toFloat(p.data_quality_score), 0.0) AS quality
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       rating * 0.7 + log(toFloat(rating_count) + 1) * 0.3 + quality AS field_score,
       [] AS field_terms
ORDER BY field_score DESC
LIMIT $candidate_limit
"""

_ITEM_CF_RECALL_CYPHER = """
MATCH (h:Product)<-[:RATED]-(u:User)-[r:RATED]->(p:Product)
WHERE h.product_id IN $history_ids
  AND u.user_id <> $user_id
  AND NOT p.product_id IN $history_ids
  AND coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p,
     count(DISTINCT u) AS co_users,
     count(DISTINCT h) AS matched_history,
     avg(toFloat(coalesce(r.rating, 0))) AS avg_neighbor_rating
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       co_users,
       matched_history,
       avg_neighbor_rating
ORDER BY co_users DESC,
         matched_history DESC,
         avg_neighbor_rating DESC,
         coalesce(toFloat(p.average_rating), 0.0) DESC,
         coalesce(toInteger(p.rating_number), 0) DESC
LIMIT $candidate_limit
"""

_TRANSITION_RECALL_CYPHER = """
MATCH (h:Product)<-[hr:RATED]-(u:User)-[r:RATED]->(p:Product)
WHERE h.product_id IN $history_ids
  AND u.user_id <> $user_id
  AND hr.timestamp IS NOT NULL
  AND r.timestamp IS NOT NULL
  AND r.timestamp > hr.timestamp
  AND NOT p.product_id IN $history_ids
  AND coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p,
     count(*) AS transitions,
     count(DISTINCT u) AS transition_users,
     min(r.timestamp - hr.timestamp) AS min_time_gap,
     avg(toFloat(coalesce(r.rating, 0))) AS avg_next_rating
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       transitions,
       transition_users,
       min_time_gap,
       avg_next_rating
ORDER BY transitions DESC,
         transition_users DESC,
         min_time_gap ASC,
         avg_next_rating DESC,
         coalesce(toInteger(p.rating_number), 0) DESC
LIMIT $candidate_limit
"""

RATING_PRIOR = 3.8
RATING_PRIOR_COUNT = 50
POPULARITY_REFERENCE_COUNT = 5000
SECOND_STAGE_RECALL_LIMIT = 50
DEFAULT_MIN_RECOMMENDATION_QUALITY_SCORE = 0.6
FEEDBACK_LOG_PATH = Path("logs/recommendation_feedback.jsonl")
BEHAVIOR_EVENT_WEIGHTS = {
    "impression": 0.15,
    "product_click": 1.0,
    "review_open": 1.5,
    "amazon_click": 3.0,
    "feedback_yes": 2.5,
    "feedback_no": -2.0,
    "filter_change": 0.1,
    "restart": -0.2,
}
POSITIVE_BEHAVIOR_EVENTS = ["product_click", "review_open", "amazon_click", "feedback_yes"]


_CHAT_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "chat_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action":             {"type": "string"},
                "question":           {"type": ["string", "null"]},
                "options":            {"type": "array", "items": {"type": "string"}},
                "slot":               {"type": ["string", "null"]},
                "filled_slots":       {"type": "integer"},
                "intent":             {"type": ["object", "null"]},
                "preference_summary": {"type": "array", "items": {"type": "string"}},
                "product_category":   {"type": ["string", "null"]},
            },
            "required": ["action", "question", "options", "slot", "filled_slots", "intent", "preference_summary", "product_category"],
        },
    },
}


def _json_format_kwargs() -> dict[str, Any]:
    """LM Studio は json_schema、OpenAI/DeepSeek は json_object を使う。
    両者に対応するためプロバイダーを問わず json_schema を先に試す。
    外部で例外を捕まえるのではなく、呼び出し側で try/except を避けるためここで dict を返す。"""
    return {"response_format": _CHAT_JSON_SCHEMA}


# filled_slots のカウント対象スロット（product_type は含めない）
_PERSONALIZATION_SLOTS = {"skin_type", "hair_type", "scent", "benefit", "texture", "ingredient", "price_max", "min_rating"}

# 製品カテゴリ別のスロット優先順位
_SLOT_PRIORITY: dict[str, list[str]] = {
    "skincare":   ["skin_type", "benefit", "texture", "ingredient", "price_max"],
    "haircare":   ["hair_type", "benefit", "scent",   "ingredient", "price_max"],
    "fragrance":  ["scent",     "benefit", "price_max"],
    "makeup":     ["skin_type", "benefit", "texture", "price_max"],
    "other":      ["benefit",   "texture", "price_max"],
}

# キーワード→製品カテゴリマッピング
_PRODUCT_TYPE_MAP: list[tuple[list[str], str]] = [
    (["serum", "moisturizer", "lotion", "cleanser", "toner", "sunscreen", "eye cream",
      "美容液", "化粧水", "乳液", "保湿クリーム", "クレンザー", "日焼け止め", "洗顔",
      "肌", "顔", "肌荒れ", "毛穴", "しわ", "たるみ", "くすみ", "ニキビ", "目元"], "skincare"),
    (["shampoo", "conditioner", "hair", "treatment", "シャンプー", "コンディショナー",
      "ヘアオイル", "ヘアケア", "トリートメント", "髪", "頭皮", "枝毛"], "haircare"),
    (["perfume", "fragrance", "cologne", "mist", "香水", "フレグランス", "ミスト", "香り"], "fragrance"),
    (["foundation", "lipstick", "mascara", "eyeshadow", "blush", "concealer", "makeup",
      "ファンデ", "リップ", "マスカラ", "アイシャドウ", "チーク", "コンシーラー", "メイク", "唇"], "makeup"),
]

# 質問テンプレート（lang → slot_key → {question, options}）
_QUESTION_TEMPLATES: dict[str, dict[str, dict[str, Any]]] = {
    "ja": {
        "product_type": {
            "question": "どんなアイテムをお探しですか？",
            "options": ["スキンケア（化粧水・クリーム）", "ヘアケア（シャンプー等）", "フレグランス", "メイクアップ", "こだわらない"],
            "slot": "product_type",
        },
        "skin_type": {
            "question": "肌タイプを教えてください",
            "options": ["乾燥肌", "脂性肌", "敏感肌", "混合肌", "こだわらない"],
            "slot": "skin_type",
        },
        "hair_type": {
            "question": "髪タイプを教えてください",
            "options": ["ダメージ毛", "乾燥した髪", "細い髪", "カラー毛", "こだわらない"],
            "slot": "hair_type",
        },
        "scent": {
            "question": "香りの好みはありますか？",
            "options": ["フローラル", "シトラス", "ウッディ", "フレッシュ", "こだわらない"],
            "slot": "scent",
        },
        "benefit_skincare": {
            "question": "どんな効果を重視しますか？",
            "options": ["高保湿", "美白・ブライトニング", "エイジングケア", "鎮静・バリア強化", "こだわらない"],
            "slot": "benefit",
        },
        "benefit_haircare": {
            "question": "どんな効果を重視しますか？",
            "options": ["補修・ダメージケア", "保湿・潤い", "ボリュームアップ", "頭皮ケア", "こだわらない"],
            "slot": "benefit",
        },
        "benefit_fragrance": {
            "question": "予算や雰囲気の好みはありますか？",
            "options": ["$30以下", "$50以下", "大人っぽい", "爽やか・軽め", "こだわらない"],
            "slot": "benefit",
        },
        "benefit_makeup": {
            "question": "どんな仕上がりを求めますか？",
            "options": ["カバー力重視", "自然な仕上がり", "長時間キープ", "ツヤ感", "こだわらない"],
            "slot": "benefit",
        },
        "benefit_other": {
            "question": "どんな効果を求めますか？",
            "options": ["保湿", "美白", "エイジングケア", "センシティブ対応", "こだわらない"],
            "slot": "benefit",
        },
        "texture": {
            "question": "テクスチャの好みはありますか？",
            "options": ["さっぱり（ジェル・ローション）", "しっとり（クリーム）", "軽め（ミルク）", "こだわらない"],
            "slot": "texture",
        },
        "ingredient": {
            "question": "特定の成分のご希望はありますか？",
            "options": ["ヒアルロン酸", "ビタミンC", "レチノール", "ナイアシンアミド", "こだわらない"],
            "slot": "ingredient",
        },
        "price_max": {
            "question": "ご予算はいかがですか？",
            "options": ["$20以下", "$30以下", "$50以下", "$100以下", "こだわらない"],
            "slot": "price_max",
        },
    },
    "en": {
        "product_type": {
            "question": "What type of product are you looking for?",
            "options": ["Skincare (serum, moisturizer)", "Haircare (shampoo, conditioner)", "Fragrance", "Makeup", "Don't mind"],
            "slot": "product_type",
        },
        "skin_type": {
            "question": "What's your skin type?",
            "options": ["Dry", "Oily", "Sensitive", "Combination", "Don't mind"],
            "slot": "skin_type",
        },
        "hair_type": {
            "question": "What's your hair type?",
            "options": ["Damaged", "Dry", "Fine", "Color-treated", "Don't mind"],
            "slot": "hair_type",
        },
        "scent": {
            "question": "Any scent preference?",
            "options": ["Floral", "Citrus", "Woody", "Fresh", "Don't mind"],
            "slot": "scent",
        },
        "benefit_skincare": {
            "question": "What effect do you want?",
            "options": ["Deep moisturizing", "Brightening", "Anti-aging", "Soothing", "Don't mind"],
            "slot": "benefit",
        },
        "benefit_haircare": {
            "question": "What effect do you want?",
            "options": ["Repair & strengthen", "Moisture & hydration", "Volume", "Scalp care", "Don't mind"],
            "slot": "benefit",
        },
        "benefit_fragrance": {
            "question": "Any budget or mood preference?",
            "options": ["Under $30", "Under $50", "Sophisticated", "Fresh & light", "Don't mind"],
            "slot": "benefit",
        },
        "benefit_makeup": {
            "question": "What finish do you prefer?",
            "options": ["Full coverage", "Natural look", "Long-lasting", "Dewy glow", "Don't mind"],
            "slot": "benefit",
        },
        "benefit_other": {
            "question": "What benefit are you looking for?",
            "options": ["Moisturizing", "Brightening", "Anti-aging", "Sensitive-skin friendly", "Don't mind"],
            "slot": "benefit",
        },
        "texture": {
            "question": "Any texture preference?",
            "options": ["Lightweight (gel/lotion)", "Rich (cream)", "Medium (milk)", "Don't mind"],
            "slot": "texture",
        },
        "ingredient": {
            "question": "Any key ingredient preference?",
            "options": ["Hyaluronic acid", "Vitamin C", "Retinol", "Niacinamide", "Don't mind"],
            "slot": "ingredient",
        },
        "price_max": {
            "question": "What's your budget?",
            "options": ["Under $20", "Under $30", "Under $50", "Under $100", "Don't mind"],
            "slot": "price_max",
        },
    },
}


def _extract_asked_slots(messages: list[dict[str, Any]], lang: str) -> set[str]:
    """会話履歴のassistantメッセージをテンプレートと照合し、既に聞いたスロットを返す。"""
    tpls = _QUESTION_TEMPLATES.get(lang, _QUESTION_TEMPLATES["ja"])
    asked: set[str] = set()
    for m in messages:
        if m.get("role") != "assistant":
            continue
        q_text = (m.get("content") or "").strip()
        for tpl in tpls.values():
            if tpl.get("question") == q_text:
                slot = tpl.get("slot", "")
                if slot:
                    asked.add(slot)
                break
    return asked


def _guess_product_type(text: str) -> str | None:
    for keywords, category in _PRODUCT_TYPE_MAP:
        if any(kw in text for kw in keywords):
            return category
    return None


def _pick_next_question(product_type: str | None, filled: set[str], lang: str) -> dict[str, Any]:
    """製品タイプと充填済みスロットから次の質問テンプレートを返す。"""
    tpls = _QUESTION_TEMPLATES.get(lang, _QUESTION_TEMPLATES["ja"])
    category = product_type or "other"
    priority = _SLOT_PRIORITY.get(category, _SLOT_PRIORITY["other"])

    if product_type is None:
        return tpls["product_type"]

    for slot in priority:
        if slot in filled:
            continue
        # benefit は製品カテゴリ別テンプレートを使う
        if slot == "benefit":
            key = f"benefit_{category}" if f"benefit_{category}" in tpls else "benefit_other"
            return tpls[key]
        if slot in tpls:
            return tpls[slot]

    # すべてのスロットが埋まっていれば price_max（最後の手段）
    return tpls.get("price_max", tpls["product_type"])


def _phrase_question(llm: OpenAI, model: str, user_text: str, base: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """product_type不明時の最初の質問だけ、ユーザーの発言を踏まえて自然な言い回しに変換する。
    このスロットは _extract_asked_slots の追跡対象外なので、言い換えても質問の重複バグが起きない。
    失敗時は None を返し、呼び出し元は既存テンプレートにフォールバックする。"""
    target_language = "Japanese" if lang == "ja" else "English"
    prompt = (
        f"Rephrase this shopping-assistant question so it naturally acknowledges what the user just said, "
        f"in {target_language}. Keep the same meaning. You may lightly rephrase the options too, but keep the same count and intent. "
        f'Return ONLY JSON: {{"question": "...", "options": ["...", "..."]}}\n\n'
        f"User said: {user_text!r}\n"
        f"Original question: {base['question']!r}\n"
        f"Original options: {base['options']!r}"
    )
    try:
        response = llm.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        data = _safe_json(response.choices[0].message.content)
        if not data:
            return None
        question = data.get("question")
        options = data.get("options")
        if not isinstance(question, str) or not question.strip():
            return None
        if not isinstance(options, list) or len(options) < 2:
            return None
        return {"question": question.strip(), "options": [str(o) for o in options[:5]]}
    except Exception:
        return None


def _parse_attribute_filters(raw: list[Any]) -> list[AttributeFilter]:
    """LLM出力のattribute_filtersをパース。value/attribute_typeがNullや非文字列の行を除外する。"""
    result = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        if not isinstance(f.get("attribute_type"), str) or not isinstance(f.get("value"), str):
            continue
        if not f["attribute_type"] or not f["value"]:
            continue
        try:
            result.append(AttributeFilter(**f))
        except Exception:
            continue
    return result


def _extract_json_object(content: str | None) -> str:
    """Extract a JSON object from an LLM response.

    Provider-agnostic: handles clean JSON, markdown-fenced JSON (```json ... ```),
    and responses with surrounding prose / reasoning by slicing the outermost {...}.
    Lets the same code work with LM Studio (no json_object mode), DeepSeek and OpenAI.
    """
    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _safe_json(content: str | None) -> dict[str, Any] | None:
    """LLM応答からJSONを頑健に取り出す。壊れていたら軽く補修し、ダメなら None を返す
    （json.loads の例外で 500 を出さないためのガード）。"""
    text = _extract_json_object(content)
    try:
        return json.loads(text)
    except Exception:
        pass
    # よくある崩れ（末尾カンマ）を補修して再挑戦
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


_HEURISTIC_RULES: list[dict[str, Any]] = [
    {
        "slot": "product_type",
        "attribute_type": "product_type",
        "value": "moisturizer",
        "summary": {"en": "moisturizer", "ja": "保湿クリーム"},
        "terms": ["moisturizer", "moisturiser", "face cream", "cream", "lotion", "保湿クリーム", "クリーム", "乳液"],
    },
    {
        "slot": "product_type",
        "attribute_type": "product_type",
        "value": "serum",
        "summary": {"en": "serum", "ja": "美容液"},
        "terms": ["serum", "essence", "美容液"],
    },
    {
        "slot": "product_type",
        "attribute_type": "product_type",
        "value": "toner",
        "summary": {"en": "toner", "ja": "化粧水"},
        "terms": ["toner", "化粧水"],
    },
    {
        "slot": "product_type",
        "attribute_type": "product_type",
        "value": "cleanser",
        "summary": {"en": "cleanser", "ja": "洗顔料"},
        "terms": ["cleanser", "face wash", "洗顔", "洗顔料"],
    },
    {
        "slot": "skin_type",
        "attribute_type": "skin_type",
        "value": "dry",
        "summary": {"en": "dry skin", "ja": "乾燥肌"},
        "terms": ["dry skin", "dry", "乾燥肌", "乾燥"],
    },
    {
        "slot": "skin_type",
        "attribute_type": "skin_type",
        "value": "sensitive",
        "summary": {"en": "sensitive skin", "ja": "敏感肌"},
        "terms": ["sensitive skin", "sensitive", "敏感肌", "敏感"],
    },
    {
        "slot": "scent",
        "attribute_type": "scent",
        "value": "unscented",
        "summary": {"en": "fragrance-free", "ja": "無香料"},
        "terms": ["fragrance-free", "fragrance free", "unscented", "no fragrance", "無香料", "無香"],
    },
    {
        "slot": "ingredient",
        "attribute_type": "ingredient",
        "value": "hyaluronic acid",
        "summary": {"en": "hyaluronic acid", "ja": "ヒアルロン酸"},
        "terms": ["hyaluronic acid", "ヒアルロン酸"],
    },
    {
        "slot": "ingredient",
        "attribute_type": "ingredient",
        "value": "vitamin c",
        "summary": {"en": "vitamin C", "ja": "ビタミンC"},
        "terms": ["vitamin c", "ビタミンc", "ビタミンC"],
    },
    {
        "slot": "ingredient",
        "attribute_type": "ingredient",
        "value": "retinol",
        "summary": {"en": "retinol", "ja": "レチノール"},
        "terms": ["retinol", "レチノール"],
    },
    {
        "slot": "benefit",
        "attribute_type": "benefit",
        "value": "moisturizing",
        "summary": {"en": "moisturizing", "ja": "保湿"},
        "terms": ["moisturizing", "hydrating", "hydration", "保湿", "うるおい", "潤い"],
    },
    {
        "slot": "benefit",
        "attribute_type": "benefit",
        "value": "soothing",
        "summary": {"en": "soothing", "ja": "鎮静"},
        "terms": ["soothing", "calming", "鎮静", "肌荒れ"],
    },
]


def _matches_any(text: str, terms: list[str]) -> bool:
    folded = text.lower()
    return any(term.lower() in folded for term in terms)


def _detect_filled_slots(text: str) -> set[str]:
    slots: set[str] = set()
    for rule in _HEURISTIC_RULES:
        if rule["slot"] == "product_type":
            continue
        if _matches_any(text, rule["terms"]):
            slots.add(rule["slot"])
    if re.search(r"(?:under|below|less than)\s*\$?\d+|\$\d+|\d+\s*ドル|\d+\s*円", text, re.I):
        slots.add("price_max")
    if re.search(r"(?:rating|stars?|評価).*(?:[4-5](?:\.\d)?)", text, re.I):
        slots.add("min_rating")
    return slots


def _heuristic_intent(text: str) -> SearchIntent:
    filters: list[AttributeFilter] = []
    keywords: list[str] = []
    seen: set[tuple[str, str]] = set()

    for rule in _HEURISTIC_RULES:
        if not _matches_any(text, rule["terms"]):
            continue
        key = (rule["attribute_type"], rule["value"])
        if key in seen:
            continue
        seen.add(key)
        weight = 1.0 if rule["slot"] in {"product_type", "skin_type", "ingredient"} else 0.7
        filters.append(
            AttributeFilter(
                attribute_type=rule["attribute_type"],
                value=rule["value"],
                weight=weight,
            )
        )
        keywords.append(rule["value"])

    price_max = None
    price_match = re.search(r"(?:under|below|less than)\s*\$?(\d+)|\$(\d+)", text, re.I)
    if price_match:
        price_max = float(price_match.group(1) or price_match.group(2))

    min_rating = None
    rating_match = re.search(r"(?:rating|stars?).*?([4-5](?:\.\d)?)", text, re.I)
    if rating_match:
        min_rating = float(rating_match.group(1))

    return SearchIntent(
        attribute_filters=filters,
        keywords=list(dict.fromkeys(keywords)),
        price_max=price_max,
        min_rating=min_rating,
    )


def _heuristic_summary(text: str, lang: str) -> list[str]:
    normalized_lang = _normalize_lang(lang)
    summary: list[str] = []
    for rule in _HEURISTIC_RULES:
        if _matches_any(text, rule["terms"]):
            label = rule["summary"][normalized_lang]
            if label not in summary:
                summary.append(label)
    return summary


def _load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Recommender:
    def __init__(self) -> None:
        _load_env()
        self.driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        )
        self.neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")
        self.min_quality_score = float(
            os.environ.get("MIN_RECOMMENDATION_QUALITY_SCORE", DEFAULT_MIN_RECOMMENDATION_QUALITY_SCORE)
        )
        provider = os.environ.get("LLM_PROVIDER", "openai").lower()
        if provider == "deepseek":
            self.llm = OpenAI(
                api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            )
            self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        else:
            self.llm = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def extract_intent(self, query: str) -> SearchIntent:
        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                temperature=0,
            )
            data = _safe_json(response.choices[0].message.content) or {}
        except Exception:
            return _heuristic_intent(query)
        return SearchIntent(
            attribute_filters=_parse_attribute_filters(data.get("attribute_filters", [])),
            keywords=data.get("keywords", []),
            price_max=data.get("price_max"),
            min_rating=data.get("min_rating"),
        )

    def _fetch_title_ja(self, product_ids: list[str]) -> dict[str, str]:
        """GPU翻訳バッチで付与されたtitle_jaを持つ商品だけを一括取得する。"""
        if not product_ids:
            return {}
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                "MATCH (p:Product) WHERE p.product_id IN $ids AND p.title_ja IS NOT NULL "
                "RETURN p.product_id AS product_id, p.title_ja AS title_ja",
                ids=product_ids,
            )
            return {r["product_id"]: r["title_ja"] for r in result}

    def _apply_feature_evidence_ja(self, candidates: dict[str, dict[str, Any]]) -> None:
        """feature_evidence（商品説明の引用文）を Feature.text_ja で日本語に差し替える（lang=ja時のみ呼ぶ）。
        evidence は _strip_html 済みのため、text_ja 側も同じ正規化キーで照合する。"""
        product_ids = [pid for pid, c in candidates.items() if c.get("feature_evidence")]
        if not product_ids:
            return
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                "MATCH (p:Product)-[:HAS_FEATURE]->(f:Feature) "
                "WHERE p.product_id IN $ids AND f.text_ja IS NOT NULL "
                "RETURN DISTINCT f.text AS text, f.text_ja AS text_ja",
                ids=product_ids,
            )
            ja_map = {_strip_html(r["text"]): r["text_ja"] for r in result}
        if not ja_map:
            return
        for c in candidates.values():
            c["feature_evidence"] = [ja_map.get(ev, ev) for ev in c.get("feature_evidence", [])]

    def _apply_value_ja(self, candidates: dict[str, dict[str, Any]]) -> None:
        """matched_attributes の value を Attribute.value_ja で日本語に差し替える（lang=ja時のみ呼ぶ）。"""
        values = {
            attr.value
            for c in candidates.values()
            for attr in c.get("matched_attributes", [])
            if attr.value
        }
        if not values:
            return
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                "MATCH (a:Attribute) WHERE a.value IN $values AND a.value_ja IS NOT NULL "
                "RETURN a.value AS value, a.value_ja AS value_ja",
                values=list(values),
            )
            ja_map = {r["value"]: r["value_ja"] for r in result}
        for c in candidates.values():
            for attr in c.get("matched_attributes", []):
                ja = ja_map.get(attr.value)
                if ja:
                    attr.value = ja

    def search_products(
        self,
        intent: SearchIntent,
        limit: int,
        lang: str = "en",
        user_id: str | None = None,
        query_plan: QueryPlan | None = None,
    ) -> list[Recommendation]:
        terms = _search_terms(intent)
        if not intent.attribute_filters and not terms:
            return []
        filters = [
            {"attribute_type": f.attribute_type, "value": f.value, "weight": f.weight}
            for f in intent.attribute_filters
        ]
        candidate_limit = max(SECOND_STAGE_RECALL_LIMIT, min(200, limit * 5))
        actions = enabled_action_names(query_plan)

        with self.driver.session(database=self.neo4j_database) as session:
            candidates: dict[str, dict[str, Any]] = {}

            if filters and "attribute_recall" in actions:
                result = session.run(
                    _ATTRIBUTE_SEARCH_CYPHER,
                    filters=filters,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
                    min_quality_score=self.min_quality_score,
                    candidate_limit=candidate_limit,
                )
                for record in result:
                    candidate = _candidate(candidates, record)
                    candidate["attribute_score"] = float(record["attribute_score"] or 0)
                    candidate["matched_attributes"] = [
                        MatchedAttribute(
                            attribute_type=m["attribute_type"],
                            name=m["name"],
                            value=m["value"],
                            confidence=float(m["confidence"] or 0),
                            evidence=_strip_html(m.get("evidence")),
                        )
                        for m in record["matched_attrs"]
                    ]

            if terms and "feature_text_recall" in actions:
                result = session.run(
                    _FEATURE_SEARCH_CYPHER,
                    terms=terms,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
                    min_quality_score=self.min_quality_score,
                    candidate_limit=candidate_limit,
                )
                for record in result:
                    candidate = _candidate(candidates, record)
                    candidate["feature_terms"].update(
                        m["term"] for m in record["feature_matches"] if m.get("term")
                    )
                    for match in record["feature_matches"]:
                        evidence = _strip_html(match.get("evidence"))
                        if evidence and evidence not in candidate["feature_evidence"]:
                            candidate["feature_evidence"].append(evidence)
                    candidate["feature_hit_count"] += int(record["feature_hit_count"] or 0)

            if terms and "field_recall" in actions:
                result = session.run(
                    _FIELD_SEARCH_CYPHER,
                    terms=terms,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
                    min_quality_score=self.min_quality_score,
                    candidate_limit=candidate_limit,
                )
                for record in result:
                    candidate = _candidate(candidates, record)
                    candidate["field_terms"].update(record["field_terms"] or [])

            if user_id:
                history_ids = self._history_product_ids(session, user_id)
                if history_ids and "item_cf_recall" in actions:
                    self._apply_item_cf_recall(session, candidates, user_id, history_ids, intent, candidate_limit)
                if history_ids and "transition_recall" in actions:
                    self._apply_transition_recall(session, candidates, user_id, history_ids, intent, candidate_limit)

            if candidates:
                if "apply_user_history_boost" in actions:
                    self._apply_behavior_context(session, candidates, user_id)
                if "apply_review_mention_ranking" in actions:
                    self._apply_review_mention_context(session, candidates, intent, terms)

        title_ja_map = {}
        if _normalize_lang(lang) == "ja":
            title_ja_map = self._fetch_title_ja(list(candidates.keys()))
            self._apply_value_ja(candidates)
            self._apply_feature_evidence_ja(candidates)
        return _rank_candidates(candidates, intent, terms, max(limit, SECOND_STAGE_RECALL_LIMIT), lang, title_ja_map)[:limit]

    def make_query_plan(self, query: str, intent: SearchIntent, user_id: str | None = None) -> QueryPlan:
        return build_controlled_query_plan(
            user_input=query,
            intent=intent,
            user_id=user_id,
            min_quality_score=self.min_quality_score,
        )

    def recommend(
        self,
        query: str,
        limit: int = 10,
        lang: str = "en",
        user_id: str | None = None,
    ) -> tuple[SearchIntent, QueryPlan, list[Recommendation]]:
        intent = self.extract_intent(query)
        query_plan = self.make_query_plan(query, intent, user_id)
        products = self.search_products(intent, limit, lang, user_id, query_plan)
        return intent, query_plan, products

    def recommend_home(self, user_id: str | None, limit: int = 10, lang: str = "en") -> tuple[SearchIntent, QueryPlan, list[Recommendation]]:
        intent = self._behavior_home_intent(user_id)
        query_plan = self.make_query_plan("[home]", intent, user_id)
        if intent.attribute_filters or intent.keywords:
            products = self.search_products(intent, limit, lang, user_id, query_plan)
            if products:
                return intent, query_plan, products

        fallback_intent = SearchIntent(attribute_filters=[], keywords=["popular"], price_max=None, min_rating=None)
        fallback_plan = self.make_query_plan("[home]", fallback_intent, user_id)
        with self.driver.session(database=self.neo4j_database) as session:
            candidates: dict[str, dict[str, Any]] = {}
            result = session.run(
                _HOME_RECOMMEND_CYPHER,
                min_quality_score=self.min_quality_score,
                candidate_limit=max(SECOND_STAGE_RECALL_LIMIT, min(200, limit * 5)),
            )
            for record in result:
                candidate = _candidate(candidates, record)
                candidate["field_terms"].add("popular")
                candidate["field_score"] = float(record.get("field_score") or 0.0)
            if candidates:
                fallback_actions = enabled_action_names(fallback_plan)
                if "apply_user_history_boost" in fallback_actions:
                    self._apply_behavior_context(session, candidates, user_id)
                if "apply_review_mention_ranking" in fallback_actions:
                    self._apply_review_mention_context(session, candidates, fallback_intent, ["popular"])
        title_ja_map = {}
        if _normalize_lang(lang) == "ja":
            title_ja_map = self._fetch_title_ja(list(candidates.keys()))
            self._apply_value_ja(candidates)
            self._apply_feature_evidence_ja(candidates)
        reranked = _rank_candidates(candidates, fallback_intent, ["popular"], max(limit, SECOND_STAGE_RECALL_LIMIT), lang, title_ja_map)
        return fallback_intent, fallback_plan, reranked[:limit]

    def chat(
        self,
        messages: list[dict[str, Any]],
        limit: int = 10,
        lang: str = "ja",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """対話型推薦の1ターン。

        ask/search の判断を Python で担当し LLM の行動決定ルール遵守に依存しない安定動作を実現。
        - py_filled: ユーザーが答えた質問数（会話履歴から直接カウント）
        - asked_slots: 過去に聞いたスロット（テンプレートマッチングで特定）
        """
        all_user_msgs = [m for m in messages if m.get("role") == "user"]
        answer_msgs = all_user_msgs[1:]  # 1番目はproduct_typeの質問なので除外
        asked = sum(1 for m in messages if m.get("role") == "assistant")

        # ── Python で ask/search と次スロットを決定（LLM 不要）─────────────
        # ユーザーがグローバルに「おまかせ」と言ったか
        user_said_no_pref = any(
            kw in m.get("content", "")
            for m in all_user_msgs
            for kw in ("おまかせ", "なんでも", "no preference", "don't mind", "どちらでも")
        )
        # 「こだわらない」を含む回答も「質問に答えた」ので asked_slots へは加算
        # （次のスロットに進むためのトラッキング）
        asked_slots = _extract_asked_slots(messages, lang)

        # 製品タイプを会話テキストから推定（LLM 結果より後で補完）
        user_text = " ".join(m.get("content", "") for m in all_user_msgs).lower()
        product_type = _guess_product_type(user_text)
        detected_slots = _detect_filled_slots(user_text)
        asked_slots |= detected_slots
        py_filled = len(detected_slots)

        should_search = py_filled >= 2 or user_said_no_pref or asked >= MAX_QUESTIONS

        # ── LLM 呼び出し: intent 抽出 + preference_summary ─────────────────
        target_language = "Japanese" if lang == "ja" else "English"
        system = CHAT_SYSTEM_PROMPT + (
            f"\n\nTARGET LANGUAGE = {target_language}. Write preference_summary ONLY in this language. "
            "intent values and keywords stay in ENGLISH regardless of language."
        )
        llm_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
            llm_messages.append({"role": role, "content": m.get("content", "")})

        try:
            response = self.llm.chat.completions.create(
                model=self.model, messages=llm_messages, temperature=0, **_json_format_kwargs()
            )
            data = _safe_json(response.choices[0].message.content) or {}
        except Exception:
            data = {}
        summary = data.get("preference_summary") or []
        intent_data = data.get("intent") or {}
        if not summary:
            summary = _heuristic_summary(user_text, lang)

        # キーワード辞書に無い言い回し（頭皮・肌荒れ等の未登録語彙）を LLM 分類で補完。
        # LLM が失敗/nullを返した場合は元のキーワード判定のまま安定動作を維持する。
        if product_type is None:
            llm_category = data.get("product_category")
            if llm_category in ("skincare", "haircare", "fragrance", "makeup"):
                product_type = llm_category

        # ── 結果を返す ────────────────────────────────────────────────────────
        if should_search:
            intent = self._intent_from_data(intent_data if intent_data else None, messages)
            query_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
            query_plan = self.make_query_plan(query_text, intent, user_id)
            products = self.search_products(intent, limit, lang, user_id, query_plan)
            return {
                "action": "search",
                "question": None,
                "options": [],
                "preference_summary": summary,
                "intent": intent,
                "query_plan": query_plan,
                "recommendations": products,
            }

        next_q = _pick_next_question(product_type, asked_slots, lang)
        if product_type is None:
            phrased = _phrase_question(self.llm, self.model, user_text, next_q, lang)
            if phrased:
                next_q = phrased
        return {
            "action": "ask",
            "question": next_q["question"],
            "options": next_q["options"],
            "preference_summary": summary,
            "intent": None,
            "recommendations": [],
        }

    def _intent_from_data(self, intent_data: Any, messages: list[dict[str, Any]]) -> SearchIntent:
        """LLMが返した intent を SearchIntent 化。無ければ会話全体から抽出にフォールバック。"""
        if intent_data:
            try:
                return SearchIntent(
                    attribute_filters=_parse_attribute_filters(intent_data.get("attribute_filters", [])),
                    keywords=intent_data.get("keywords", []),
                    price_max=intent_data.get("price_max"),
                    min_rating=intent_data.get("min_rating"),
                )
            except Exception:
                pass
        text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
        return self.extract_intent(text)

    def get_reviews(self, product_id: str, limit: int = 5, lang: str = "en") -> list[dict[str, Any]]:
        """商品IDに紐づくレビューをhelpful_vote降順で返す。REVIEWS/WROTEエッジを使用。
        lang=ja の場合、GPU翻訳バッチで付与された text_ja があればそちらを本文として返す。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})<-[:REVIEWS]-(r:Review)
WHERE r.text IS NOT NULL AND size(coalesce(r.text, '')) > 10
RETURN r.title AS title,
       r.title_ja AS title_ja,
       r.text AS text,
       r.text_ja AS text_ja,
       toFloat(r.rating) AS rating,
       toInteger(r.helpful_vote) AS helpful_vote,
       r.verified_purchase AS verified_purchase
ORDER BY r.helpful_vote DESC, r.rating DESC
LIMIT $limit
"""
        use_ja = _normalize_lang(lang) == "ja"
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(cypher, product_id=product_id, limit=limit)
            rows = []
            for record in result:
                r = dict(record)
                text_ja = r.pop("text_ja", None)
                title_ja = r.pop("title_ja", None)
                if use_ja and text_ja:
                    r["text"] = text_ja
                else:
                    r["text"] = _strip_html(r.get("text"))
                if use_ja and title_ja:
                    r["title"] = title_ja
                else:
                    r["title"] = _strip_html(r.get("title"))
                rows.append(r)
            return rows

    def save_feedback(self, product_id: str, payload: dict[str, Any]) -> None:
        """推薦理由のユーザーフィードバックをJSONLで保存する。"""
        FEEDBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "product_id": product_id,
            **payload,
        }
        with FEEDBACK_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def log_behavior_event(self, payload: dict[str, Any]) -> int:
        user_id = _clean_user_id(payload.get("user_id"))
        if not user_id:
            return 0
        product_ids = [pid for pid in payload.get("product_ids", []) if isinstance(pid, str) and pid]
        if payload.get("product_id"):
            product_ids.insert(0, str(payload["product_id"]))
        deduped_product_ids = list(dict.fromkeys(product_ids))
        event_type = _clean_text(payload.get("event_type")).lower() or "unknown"
        weight = BEHAVIOR_EVENT_WEIGHTS.get(event_type, 0.0)
        event_id = "evt_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        products = [
            {"product_id": product_id, "rank": int(payload.get("rank") or index + 1)}
            for index, product_id in enumerate(deduped_product_ids)
        ]
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        cypher = """
MERGE (u:User {user_id: $user_id})
CREATE (e:BehaviorEvent {
  event_id: $event_id,
  event_type: $event_type,
  query: $event_query,
  source: $source,
  created_at: $created_at,
  weight: $weight,
  metadata_json: $metadata_json
})
CREATE (u)-[:PERFORMED]->(e)
WITH e
UNWIND $products AS item
MATCH (p:Product {product_id: item.product_id})
CREATE (e)-[:ON_PRODUCT {rank: item.rank}]->(p)
"""
        cypher_without_products = """
MERGE (u:User {user_id: $user_id})
CREATE (e:BehaviorEvent {
  event_id: $event_id,
  event_type: $event_type,
  query: $event_query,
  source: $source,
  created_at: $created_at,
  weight: $weight,
  metadata_json: $metadata_json
})
CREATE (u)-[:PERFORMED]->(e)
"""
        with self.driver.session(database=self.neo4j_database) as session:
            session.run(
                cypher if products else cypher_without_products,
                user_id=user_id,
                event_id=event_id,
                event_type=event_type,
                event_query=payload.get("query"),
                source=payload.get("source") or "chat",
                created_at=created_at,
                weight=weight,
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                products=products,
            ).consume()
        return max(len(products), 1)

    def _behavior_home_intent(self, user_id: str | None) -> SearchIntent:
        user_id = _clean_user_id(user_id)
        if not user_id:
            return SearchIntent(attribute_filters=[], keywords=["popular"], price_max=None, min_rating=None)
        cypher = """
MATCH (:User {user_id: $user_id})-[:PERFORMED]->(e:BehaviorEvent)-[:ON_PRODUCT]->(:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE e.event_type IN $positive_events
WITH a.attribute_type AS attribute_type, a.value AS value, sum(toFloat(coalesce(e.weight, 1.0))) AS weight
ORDER BY weight DESC
LIMIT 5
RETURN attribute_type, value, weight
"""
        with self.driver.session(database=self.neo4j_database) as session:
            rows = [dict(record) for record in session.run(cypher, user_id=user_id, positive_events=POSITIVE_BEHAVIOR_EVENTS)]
        filters = [
            AttributeFilter(attribute_type=row["attribute_type"], value=row["value"], weight=0.7)
            for row in rows
            if row.get("attribute_type") and row.get("value")
        ]
        keywords = [row["value"] for row in rows if row.get("value")]
        if not filters and not keywords:
            keywords = ["popular"]
        return SearchIntent(attribute_filters=filters, keywords=keywords[:5], price_max=None, min_rating=None)

    def _history_product_ids(self, session: Any, user_id: str | None, limit: int = 20) -> list[str]:
        user_id = _clean_user_id(user_id)
        if not user_id:
            return []
        record = session.run(
            """
            MATCH (u:User {user_id: $user_id})
            OPTIONAL MATCH (u)-[:PERFORMED]->(e:BehaviorEvent)-[:ON_PRODUCT]->(bp:Product)
            WHERE e.event_type IN $positive_events
            WITH u, collect(DISTINCT bp.product_id) AS behavior_ids
            OPTIONAL MATCH (u)-[r:RATED]->(rp:Product)
            WHERE toFloat(coalesce(r.rating, 0)) >= 4
            WITH behavior_ids, collect(DISTINCT rp.product_id) AS rated_ids
            RETURN behavior_ids + rated_ids AS product_ids
            """,
            user_id=user_id,
            positive_events=POSITIVE_BEHAVIOR_EVENTS,
        ).single()
        raw_ids = record["product_ids"] if record else []
        history_ids: list[str] = []
        for product_id in raw_ids or []:
            if isinstance(product_id, str) and product_id and product_id not in history_ids:
                history_ids.append(product_id)
            if len(history_ids) >= limit:
                break
        return history_ids

    def _apply_item_cf_recall(
        self,
        session: Any,
        candidates: dict[str, dict[str, Any]],
        user_id: str,
        history_ids: list[str],
        intent: SearchIntent,
        candidate_limit: int,
    ) -> None:
        result = session.run(
            _ITEM_CF_RECALL_CYPHER,
            user_id=_clean_user_id(user_id),
            history_ids=history_ids,
            min_rating=intent.min_rating,
            price_max=intent.price_max,
            min_quality_score=self.min_quality_score,
            candidate_limit=candidate_limit,
        )
        for record in result:
            candidate = _candidate(candidates, record)
            co_users = float(record["co_users"] or 0.0)
            matched_history = float(record["matched_history"] or 0.0)
            rating_score = _clamp((float(record["avg_neighbor_rating"] or 0.0) - 3.0) / 2.0)
            candidate["item_cf_score"] = max(
                candidate.get("item_cf_score", 0.0),
                _clamp((co_users / 8.0) * 0.55 + (matched_history / max(len(history_ids), 1)) * 0.30 + rating_score * 0.15),
            )
            candidate["recall_sources"].add("item_cf")

    def _apply_transition_recall(
        self,
        session: Any,
        candidates: dict[str, dict[str, Any]],
        user_id: str,
        history_ids: list[str],
        intent: SearchIntent,
        candidate_limit: int,
    ) -> None:
        result = session.run(
            _TRANSITION_RECALL_CYPHER,
            user_id=_clean_user_id(user_id),
            history_ids=history_ids,
            min_rating=intent.min_rating,
            price_max=intent.price_max,
            min_quality_score=self.min_quality_score,
            candidate_limit=candidate_limit,
        )
        for record in result:
            candidate = _candidate(candidates, record)
            transitions = float(record["transitions"] or 0.0)
            transition_users = float(record["transition_users"] or 0.0)
            rating_score = _clamp((float(record["avg_next_rating"] or 0.0) - 3.0) / 2.0)
            candidate["transition_score"] = max(
                candidate.get("transition_score", 0.0),
                _clamp((transitions / 8.0) * 0.50 + (transition_users / 6.0) * 0.35 + rating_score * 0.15),
            )
            candidate["recall_sources"].add("transition")

    def _apply_behavior_context(
        self,
        session: Any,
        candidates: dict[str, dict[str, Any]],
        user_id: str | None,
    ) -> None:
        user_id = _clean_user_id(user_id)
        if not user_id or not candidates:
            return
        product_ids = list(candidates)
        preference_rows = session.run(
            """
            MATCH (:User {user_id: $user_id})-[:PERFORMED]->(e:BehaviorEvent)-[:ON_PRODUCT]->(:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)
            WHERE e.event_type IN $positive_events
            RETURN a.attribute_type AS attribute_type, a.value AS value, sum(toFloat(coalesce(e.weight, 1.0))) AS weight
            ORDER BY weight DESC
            LIMIT 30
            """,
            user_id=user_id,
            positive_events=POSITIVE_BEHAVIOR_EVENTS,
        )
        preferred: dict[tuple[str, str], float] = {}
        for record in preference_rows:
            key = (_clean_text(record["attribute_type"]).lower(), _clean_text(record["value"]).lower())
            if key[0] and key[1]:
                preferred[key] = float(record["weight"] or 0.0)
        seen_rows = session.run(
            """
            MATCH (:User {user_id: $user_id})-[:PERFORMED]->(e:BehaviorEvent)-[:ON_PRODUCT]->(p:Product)
            WHERE e.event_type IN $strong_events
            RETURN collect(DISTINCT p.product_id) AS product_ids
            """,
            user_id=user_id,
            strong_events=["product_click", "review_open", "amazon_click", "feedback_yes"],
        )
        seen_record = seen_rows.single()
        seen_product_ids = set(seen_record["product_ids"] if seen_record else [])
        for candidate in candidates.values():
            candidate["seen_penalty"] = 1.0 if candidate["product_id"] in seen_product_ids else 0.0
        if not preferred:
            return

        attr_rows = session.run(
            """
            MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)
            WHERE p.product_id IN $product_ids
            RETURN p.product_id AS product_id,
                   collect(DISTINCT {attribute_type: a.attribute_type, value: a.value}) AS attrs
            """,
            product_ids=product_ids,
        )
        for record in attr_rows:
            candidate = candidates.get(record["product_id"])
            if candidate is None:
                continue
            shared_weight = 0.0
            for attr in record["attrs"] or []:
                key = (_clean_text(attr.get("attribute_type")).lower(), _clean_text(attr.get("value")).lower())
                shared_weight += preferred.get(key, 0.0)
            candidate["behavior_score"] = min(shared_weight / 6.0, 1.0)

    def _apply_review_mention_context(
        self,
        session: Any,
        candidates: dict[str, dict[str, Any]],
        intent: SearchIntent,
        terms: list[str],
    ) -> None:
        if not candidates:
            return
        signals = {
            _clean_text(value).lower()
            for value in terms + [f.value for f in intent.attribute_filters]
            if _clean_text(value)
        }
        if not signals:
            return
        rel_check = session.run(
            "CALL db.relationshipTypes() YIELD relationshipType "
            "WHERE relationshipType = 'MENTIONS' RETURN count(*) AS count"
        ).single()
        if not rel_check or int(rel_check["count"] or 0) == 0:
            return
        result = session.run(
            """
            MATCH (p:Product)<-[:REVIEWS]-(r:Review)-[m:MENTIONS]->(a:Attribute)
            WHERE p.product_id IN $product_ids
            RETURN p.product_id AS product_id,
                   a.attribute_type AS attribute_type,
                   a.value AS value,
                   m.sentiment AS sentiment,
                   count(*) AS mention_count,
                   avg(toFloat(coalesce(m.confidence, 0.7))) AS confidence,
                   head(collect(m.evidence)) AS evidence
            """,
            product_ids=list(candidates),
        )
        for record in result:
            value = _clean_text(record["value"]).lower()
            attr_type = _clean_text(record["attribute_type"]).lower()
            if not any(signal in value or value in signal or signal in attr_type for signal in signals):
                continue
            candidate = candidates.get(record["product_id"])
            if candidate is None:
                continue
            mention_strength = min(float(record["mention_count"] or 0) * float(record["confidence"] or 0.0) / 5.0, 1.0)
            sentiment = _clean_text(record["sentiment"]).lower()
            if sentiment == "negative":
                candidate["review_negative_score"] = min(candidate.get("review_negative_score", 0.0) + mention_strength, 1.0)
            else:
                candidate["review_positive_score"] = min(candidate.get("review_positive_score", 0.0) + mention_strength, 1.0)
                evidence = _strip_html(record.get("evidence"))
                if evidence and evidence not in candidate["feature_evidence"]:
                    candidate["feature_evidence"].append(evidence)

    def close(self) -> None:
        self.driver.close()


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str | None:
    """HTMLタグとエンティティを除去してプレーンテキストに変換する。"""
    if not text:
        return text
    text = _HTML_TAG_RE.sub(" ", text)          # タグを空白に置換
    text = _html_mod.unescape(text)             # &amp; &ndash; &#160; 等を全て展開
    return re.sub(r"\s+", " ", text).strip()    # 連続空白を1つに圧縮


def _clean_text(text: Any) -> str:
    cleaned = _strip_html("" if text is None else str(text)) or ""
    cleaned = cleaned.replace("\x00", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_user_id(value: Any) -> str:
    cleaned = _clean_text(value)
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", cleaned)[:80]


def _dedupe_key(title: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", key).strip()


def _build_explanation(matched: list[MatchedAttribute]) -> str:
    if not matched:
        return "Matched"
    by_type: dict[str, list[str]] = {}
    for m in matched:
        by_type.setdefault(m.attribute_type, []).append(m.value)
    parts = [f"{t}: {', '.join(vs)}" for t, vs in by_type.items()]
    return "Matched - " + " | ".join(parts)


def _search_terms(intent: SearchIntent) -> list[str]:
    terms: list[str] = []
    for value in intent.keywords + [f.value for f in intent.attribute_filters]:
        term = _clean_text(value).lower()
        if len(term) < 2 or term in terms:
            continue
        terms.append(term)
    return terms


def _text_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 2 and token not in {"the", "and", "for", "with", "skin", "hair", "product"}
    }


def _title_text_similarity(title: str, terms: list[str]) -> float:
    title_tokens = _text_tokens(title)
    query_tokens = set().union(*(_text_tokens(term) for term in terms)) if terms else set()
    if not title_tokens or not query_tokens:
        return 0.0
    overlap = len(title_tokens & query_tokens)
    return _clamp(overlap / max(len(query_tokens), 1))


def _candidate(candidates: dict[str, dict[str, Any]], record: Any) -> dict[str, Any]:
    product_id = record["product_id"]
    candidate = candidates.get(product_id)
    if candidate is None:
        title = _clean_text(record["title"])
        candidate = {
            "product_id": product_id,
            "title": title,
            "dedupe_key": _dedupe_key(title),
            "price": _optional_float(record["price"]),
            "average_rating": _optional_float(record["average_rating"]),
            "rating_number": _optional_int(record["rating_number"]),
            "image_url": record.get("image_url"),
            "sellable_status": record.get("sellable_status"),
            "data_quality_score": _optional_float(record.get("data_quality_score")),
            "attribute_score": 0.0,
            "field_score": _optional_float(record.get("field_score")) or 0.0,
            "behavior_score": 0.0,
            "item_cf_score": 0.0,
            "transition_score": 0.0,
            "text_similarity_score": 0.0,
            "seen_penalty": 0.0,
            "review_positive_score": 0.0,
            "review_negative_score": 0.0,
            "recall_sources": set(),
            "matched_attributes": [],
            "feature_terms": set(),
            "field_terms": set(),
            "feature_evidence": [],
            "feature_hit_count": 0,
        }
        candidates[product_id] = candidate
    return candidate


def _rank_candidates(
    candidates: dict[str, dict[str, Any]],
    intent: SearchIntent,
    terms: list[str],
    limit: int,
    lang: str,
    title_ja_map: dict[str, str] | None = None,
) -> list[Recommendation]:
    title_ja_map = title_ja_map or {}
    total_attribute_weight = sum(max(f.weight, 0.0) for f in intent.attribute_filters) or 1.0
    total_terms = len(terms) or 1
    total_signals = len(intent.attribute_filters) + len(terms) or 1

    recommendations: list[Recommendation] = []
    for candidate in candidates.values():
        if not candidate["title"]:
            continue
        matched_attributes = candidate["matched_attributes"]
        matched_terms = sorted(candidate["feature_terms"] | candidate["field_terms"])

        attribute_score = float(candidate["attribute_score"])
        attribute_match_score = min(attribute_score / total_attribute_weight, 1.0)
        feature_text_match_score = min(len(candidate["feature_terms"]) / total_terms, 1.0)
        field_match_score = min(len(candidate["field_terms"]) / total_terms, 1.0)
        rating_quality_score = _rating_quality_score(candidate["average_rating"], candidate["rating_number"])
        popularity_score = _popularity_score(candidate["rating_number"])
        query_coverage_score = min((len(matched_attributes) + len(matched_terms)) / total_signals, 1.0)
        price_availability_score = 1.0 if candidate["price"] is not None else 0.0
        data_quality_score = _clamp(float(candidate.get("data_quality_score") or 0.0))
        user_behavior_score = float(candidate.get("behavior_score") or 0.0)
        item_cf_score = float(candidate.get("item_cf_score") or 0.0)
        transition_score = float(candidate.get("transition_score") or 0.0)
        text_similarity_score = max(
            float(candidate.get("text_similarity_score") or 0.0),
            _title_text_similarity(candidate["title"], terms),
        )
        seen_penalty_score = float(candidate.get("seen_penalty") or 0.0)
        review_positive_score = float(candidate.get("review_positive_score") or 0.0)
        review_negative_score = float(candidate.get("review_negative_score") or 0.0)

        final_score = (
            4.5 * attribute_match_score
            + 2.0 * feature_text_match_score
            + 1.0 * field_match_score
            + 1.1 * text_similarity_score
            + 1.5 * rating_quality_score
            + 0.8 * data_quality_score
            + 0.75 * popularity_score
            + 1.25 * query_coverage_score
            + 2.0 * price_availability_score
            + 1.2 * user_behavior_score
            + 1.6 * item_cf_score
            + 1.8 * transition_score
            + 1.0 * review_positive_score
            - 1.2 * review_negative_score
            - 0.75 * seen_penalty_score
        )

        breakdown = {
            "attribute_match": round(attribute_match_score, 4),
            "feature_text_match": round(feature_text_match_score, 4),
            "field_match": round(field_match_score, 4),
            "text_similarity": round(text_similarity_score, 4),
            "rating_quality": round(rating_quality_score, 4),
            "data_quality": round(data_quality_score, 4),
            "popularity": round(popularity_score, 4),
            "query_coverage": round(query_coverage_score, 4),
            "price_availability": round(price_availability_score, 4),
            "user_behavior": round(user_behavior_score, 4),
            "item_cf": round(item_cf_score, 4),
            "transition": round(transition_score, 4),
            "review_positive": round(review_positive_score, 4),
            "review_negative": round(review_negative_score, 4),
            "seen_penalty": round(seen_penalty_score, 4),
        }
        explanation = _build_rich_explanation(candidate, matched_terms, lang)

        recommendations.append(
            Recommendation(
                product_id=candidate["product_id"],
                title=candidate["title"],
                display_title=title_ja_map.get(candidate["product_id"]) or _localized_title(candidate["title"], lang),
                display_language=_normalize_lang(lang),
                image_url=candidate.get("image_url"),
                price=candidate["price"],
                price_display=_price_display(candidate["price"], lang),
                availability_status=_availability_status(candidate["price"], candidate.get("sellable_status")),
                data_quality_score=candidate.get("data_quality_score"),
                average_rating=candidate["average_rating"],
                rating_number=candidate["rating_number"],
                score=round(final_score, 4),
                matched_attributes=matched_attributes,
                matched_terms=matched_terms,
                matched_feature_evidence=candidate["feature_evidence"][:5],
                score_breakdown=breakdown,
                reason_quantification=breakdown,
                explanation=explanation,
                display_explanation=explanation,
            )
        )

    recommendations.sort(
        key=lambda r: (
            r.price is not None,
            r.score,
            r.score_breakdown.get("query_coverage", 0.0),
            r.average_rating or 0.0,
            r.rating_number or 0,
        ),
        reverse=True,
    )
    return _dedupe_recommendations(recommendations, limit)


def _build_rich_explanation(candidate: dict[str, Any], matched_terms: list[str], lang: str = "en") -> str:
    parts: list[str] = []
    attribute_explanation = _build_explanation(candidate["matched_attributes"])
    if attribute_explanation != "Matched":
        parts.append(attribute_explanation)
    if matched_terms:
        label = "一致テキスト" if _normalize_lang(lang) == "ja" else "text terms"
        parts.append(label + ": " + ", ".join(matched_terms[:6]))
    if candidate["average_rating"] is not None:
        rating_number = candidate["rating_number"] or 0
        if _normalize_lang(lang) == "ja":
            parts.append(f"評価: {candidate['average_rating']:.1f} / {rating_number}件")
        else:
            parts.append(f"rating: {candidate['average_rating']:.1f} from {rating_number} ratings")
    if candidate.get("behavior_score", 0.0) > 0:
        parts.append("ユーザー行動と類似" if _normalize_lang(lang) == "ja" else "similar to your activity")
    if candidate.get("item_cf_score", 0.0) > 0:
        parts.append("似たユーザーの行動に基づく推薦" if _normalize_lang(lang) == "ja" else "co-used by similar users")
    if candidate.get("transition_score", 0.0) > 0:
        parts.append("履歴後の次の商品行動に基づく推薦" if _normalize_lang(lang) == "ja" else "often chosen after similar history")
    if candidate.get("review_positive_score", 0.0) > 0:
        parts.append("レビュー内の好意的な言及あり" if _normalize_lang(lang) == "ja" else "positive review mentions")
    fallback = "商品品質シグナルに基づく推薦" if _normalize_lang(lang) == "ja" else "Matched by product quality signals"
    return " | ".join(parts) if parts else fallback


def _dedupe_recommendations(recommendations: list[Recommendation], limit: int) -> list[Recommendation]:
    seen_products: set[str] = set()
    seen_titles: set[str] = set()
    result: list[Recommendation] = []
    for rec in recommendations:
        title_key = _dedupe_key(rec.title)
        if rec.product_id in seen_products or (title_key and title_key in seen_titles):
            continue
        seen_products.add(rec.product_id)
        if title_key:
            seen_titles.add(title_key)
        result.append(rec)
        if len(result) >= limit:
            break
    return result


def _normalize_lang(lang: str | None) -> str:
    return "ja" if (lang or "").lower().startswith("ja") else "en"


_JA_TITLE_GLOSSARY = {
    "moisturizer": "保湿クリーム",
    "moisturizing": "保湿",
    "cream": "クリーム",
    "serum": "美容液",
    "cleanser": "洗顔料",
    "toner": "化粧水",
    "sunscreen": "日焼け止め",
    "shampoo": "シャンプー",
    "conditioner": "コンディショナー",
    "fragrance free": "無香料",
    "sensitive skin": "敏感肌",
    "dry skin": "乾燥肌",
    "hyaluronic acid": "ヒアルロン酸",
    "vitamin c": "ビタミンC",
    "retinol": "レチノール",
}


def _localized_title(title: str, lang: str) -> str:
    if _normalize_lang(lang) != "ja":
        return title
    localized = title
    for source, target in sorted(_JA_TITLE_GLOSSARY.items(), key=lambda item: len(item[0]), reverse=True):
        localized = re.sub(re.escape(source), target, localized, flags=re.I)
    return localized


def _price_display(price: float | None, lang: str) -> str | None:
    if price is None:
        return None
    if _normalize_lang(lang) == "ja":
        # UI側では最新レートを使って円換算するため、APIは元通貨を明示する。
        return f"${price:.2f} USD"
    return f"${price:.2f}"


def _availability_status(price: float | None, sellable_status: str | None = None) -> str:
    if sellable_status:
        return sellable_status
    return "available" if price is not None else "currently_unavailable"


def _rating_quality_score(average_rating: float | None, rating_number: int | None) -> float:
    if average_rating is None:
        return 0.0
    count = max(rating_number or 0, 0)
    bayesian_rating = ((average_rating * count) + (RATING_PRIOR * RATING_PRIOR_COUNT)) / (
        count + RATING_PRIOR_COUNT
    )
    return _clamp((bayesian_rating - 3.0) / 2.0)


def _popularity_score(rating_number: int | None) -> float:
    count = max(rating_number or 0, 0)
    if count == 0:
        return 0.0
    return _clamp(math.log1p(count) / math.log1p(POPULARITY_REFERENCE_COUNT))


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))
