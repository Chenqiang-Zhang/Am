from __future__ import annotations

import html as _html_mod
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from openai import OpenAI

from .models import AttributeFilter, MatchedAttribute, Recommendation, SearchIntent

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

CHAT_SYSTEM_PROMPT = f"""You are a friendly beauty-product shopping assistant. The product catalog is in English (Amazon All_Beauty). The user may write in Japanese or English. Write everything you SHOW the user (questions, quick-reply options, preference_summary) in the TARGET LANGUAGE specified at the very end of these instructions.

Through conversation, collect the user's preferences across these slots:
- product_type (serum, moisturizer, cleanser, toner, sunscreen, shampoo, conditioner, hand wash, nail, perfume, makeup ...)
- skin_type (dry, oily, sensitive, combination, acne-prone, normal, all)
- hair_type (damaged, dry, oily, color-treated, curly, fine, normal, all)
- scent (unscented/fragrance-free, floral, citrus, woody, fresh, sweet, musky)
- benefit (moisturizing, brightening, anti-aging, soothing, volumizing, strengthening, long-lasting ...)
- texture (cream, gel, lotion, powder, oil, balm, foam, mist ...)
- ingredient to use (hyaluronic acid, vitamin c, retinol, niacinamide, argan oil ...) or avoid
- price_max (USD number), min_rating (number), brand

DECISION RULE — follow precisely:
Count filled_slots = number of distinct answered slots from: skin_type, hair_type, scent, benefit, texture, ingredient, price_max, min_rating.
NOTE: product_type alone does NOT count as a filled_slot.

- action = "ask"    if product_type is unknown OR filled_slots < 2 (AND user has NOT said おまかせ AND questions asked < {MAX_QUESTIONS})
- action = "search" if filled_slots >= 2 OR user said おまかせ/no preference OR questions >= {MAX_QUESTIONS}

Ask ONE question at a time. Slot priority (first unanswered slot in the list):
- skincare (cream, lotion, serum, toner, cleanser, sunscreen, eye cream):
    skin_type → benefit → texture → ingredient → price_max
- haircare (shampoo, conditioner, treatment, hair oil):
    hair_type → benefit → scent → ingredient → price_max
- fragrance / body mist / perfume:
    scent → benefit → price_max
- makeup (foundation, lipstick, mascara, eyeshadow, blush, concealer):
    skin_type → benefit → texture → price_max
- nail / other / unknown product_type:
    product_type → benefit → texture → price_max

Give 3-5 quick-reply options in the TARGET LANGUAGE. ALWAYS include one "no preference" option (こだわらない / Don't mind).

IMPORTANT: "benefit" like "moisturizing" inferred from product name (e.g. 保湿クリーム) does NOT count as a filled benefit slot — the user must explicitly confirm it.

Step-by-step example (skincare):
  Turn 1 user: "保湿クリームが欲しい"
    → product_type=moisturizer, filled_slots=0 → ask skin_type
    → question: "肌タイプを教えてください", options: ["乾燥肌","脂性肌","敏感肌","混合肌","こだわらない"]
  Turn 2 user: "乾燥肌です"
    → filled_slots=1 (skin_type) → ask benefit (next in priority)
    → question: "どんな効果を重視しますか？", options: ["高保湿","美白・ブライトニング","エイジングケア","鎮静・バリア強化","こだわらない"]
  Turn 3 user: "高保湿がいい"
    → filled_slots=2 (skin_type + benefit) → action = "search"

Step-by-step example (fragrance):
  Turn 1 user: "香水が欲しい"
    → product_type=perfume, filled_slots=0 → ask scent
    → options: ["フローラル","シトラス","ウッディ","フレッシュ","こだわらない"]
  Turn 2 user: "フローラル"
    → filled_slots=1 (scent) → ask benefit (price, mood, etc.)
    → question: "予算や雰囲気の好みはありますか？", options: ["$30以下","$50以下","大人っぽい","軽くて爽やか","こだわらない"]
  Turn 3 user: "$30以下"
    → filled_slots=2 (scent + price_max) → action = "search"

When action is "search", produce a structured intent:
- attribute_filters: list of {{"attribute_type": one of [{", ".join(ATTRIBUTE_TYPES)}], "value": ..., "weight": 1.0|0.7|0.4}}
- Write ALL values and keywords in ENGLISH (the catalog is English): 敏感肌->"sensitive", 無香料->"unscented", ヒアルロン酸->"hyaluronic acid", 化粧水->"toner", 美容液->"serum".
- keywords: English words. price_max/min_rating: number or null.

Always include "preference_summary": the user's confirmed preferences as short labels for display, written in the TARGET LANGUAGE (do not mix other languages), e.g. (Japanese) ["化粧水","敏感肌","無香料"] or (English) ["toner","sensitive skin","fragrance-free"].

CONVERSATION HISTORY NOTE: Previous assistant messages in the conversation history contain only the question text shown to the user (extracted from your prior JSON responses). This does NOT mean you should respond in plain text — you MUST ALWAYS respond with a valid JSON object.

Return ONLY this JSON object (no other text before or after):
{{
  "action": "ask" | "search",
  "question": "(ask時) 質問文 / それ以外は null",
  "options": ["(ask時の選択肢)"],
  "slot": "(今聞いているスロット名: skin_type / hair_type / scent / benefit / texture / ingredient / price_max / product_type) | null",
  "filled_slots": <integer: count of distinct answered personalization slots so far, NOT counting product_type>,
  "intent": {{"attribute_filters": [], "keywords": [], "price_max": null, "min_rating": null}},
  "preference_summary": []
}}"""

# Attribute recall: precise, explainable matches from LLM-extracted product attributes.
# Dedup by (attribute_type, value) first so duplicate Attribute nodes do not inflate the score.
_ATTRIBUTE_SEARCH_CYPHER = """
UNWIND $filters AS f
MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
WHERE a.attribute_type = f.attribute_type
  AND toLower(a.value) CONTAINS toLower(f.value)
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
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
WITH p, collect(DISTINCT term) AS field_terms
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       properties(p).image_url AS image_url,
       field_terms
ORDER BY size(field_terms) DESC, toFloat(p.average_rating) DESC
LIMIT $candidate_limit
"""

RATING_PRIOR = 3.8
RATING_PRIOR_COUNT = 50
POPULARITY_REFERENCE_COUNT = 5000
FEEDBACK_LOG_PATH = Path("logs/recommendation_feedback.jsonl")


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
            },
            "required": ["action", "question", "options", "slot", "filled_slots", "intent", "preference_summary"],
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
      "美容液", "化粧水", "乳液", "保湿クリーム", "クレンザー", "日焼け止め", "洗顔"], "skincare"),
    (["shampoo", "conditioner", "hair", "treatment", "シャンプー", "コンディショナー",
      "ヘアオイル", "ヘアケア", "トリートメント"], "haircare"),
    (["perfume", "fragrance", "cologne", "mist", "香水", "フレグランス", "ミスト"], "fragrance"),
    (["foundation", "lipstick", "mascara", "eyeshadow", "blush", "concealer", "makeup",
      "ファンデ", "リップ", "マスカラ", "アイシャドウ", "チーク", "コンシーラー", "メイク"], "makeup"),
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

    def search_products(self, intent: SearchIntent, limit: int, lang: str = "en") -> list[Recommendation]:
        terms = _search_terms(intent)
        if not intent.attribute_filters and not terms:
            return []
        filters = [
            {"attribute_type": f.attribute_type, "value": f.value, "weight": f.weight}
            for f in intent.attribute_filters
        ]
        candidate_limit = max(100, min(500, limit * 50))

        with self.driver.session(database=self.neo4j_database) as session:
            candidates: dict[str, dict[str, Any]] = {}

            if filters:
                result = session.run(
                    _ATTRIBUTE_SEARCH_CYPHER,
                    filters=filters,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
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

            if terms:
                result = session.run(
                    _FEATURE_SEARCH_CYPHER,
                    terms=terms,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
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

                result = session.run(
                    _FIELD_SEARCH_CYPHER,
                    terms=terms,
                    min_rating=intent.min_rating,
                    price_max=intent.price_max,
                    candidate_limit=candidate_limit,
                )
                for record in result:
                    candidate = _candidate(candidates, record)
                    candidate["field_terms"].update(record["field_terms"] or [])

        return _rank_candidates(candidates, intent, terms, limit, lang)

    def recommend(self, query: str, limit: int = 10, lang: str = "en") -> tuple[SearchIntent, list[Recommendation]]:
        intent = self.extract_intent(query)
        products = self.search_products(intent, limit, lang)
        return intent, products

    def chat(self, messages: list[dict[str, Any]], limit: int = 10, lang: str = "ja") -> dict[str, Any]:
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

        # ── 結果を返す ────────────────────────────────────────────────────────
        if should_search:
            intent = self._intent_from_data(intent_data if intent_data else None, messages)
            products = self.search_products(intent, limit, lang)
            return {
                "action": "search",
                "question": None,
                "options": [],
                "preference_summary": summary,
                "intent": intent,
                "recommendations": products,
            }

        next_q = _pick_next_question(product_type, asked_slots, lang)
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

    def get_reviews(self, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """商品IDに紐づくレビューをhelpful_vote降順で返す。REVIEWS/WROTEエッジを使用。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})<-[:REVIEWS]-(r:Review)
WHERE r.text IS NOT NULL AND size(coalesce(r.text, '')) > 10
RETURN r.title AS title,
       r.text AS text,
       toFloat(r.rating) AS rating,
       toInteger(r.helpful_vote) AS helpful_vote,
       r.verified_purchase AS verified_purchase
ORDER BY r.helpful_vote DESC, r.rating DESC
LIMIT $limit
"""
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(cypher, product_id=product_id, limit=limit)
            rows = []
            for record in result:
                r = dict(record)
                r["text"] = _strip_html(r.get("text"))
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
            "attribute_score": 0.0,
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
) -> list[Recommendation]:
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

        final_score = (
            4.5 * attribute_match_score
            + 2.0 * feature_text_match_score
            + 1.0 * field_match_score
            + 1.5 * rating_quality_score
            + 0.75 * popularity_score
            + 1.25 * query_coverage_score
        )

        breakdown = {
            "attribute_match": round(attribute_match_score, 4),
            "feature_text_match": round(feature_text_match_score, 4),
            "field_match": round(field_match_score, 4),
            "rating_quality": round(rating_quality_score, 4),
            "popularity": round(popularity_score, 4),
            "query_coverage": round(query_coverage_score, 4),
        }
        explanation = _build_rich_explanation(candidate, matched_terms, lang)

        recommendations.append(
            Recommendation(
                product_id=candidate["product_id"],
                title=candidate["title"],
                display_title=_localized_title(candidate["title"], lang),
                display_language=_normalize_lang(lang),
                image_url=candidate.get("image_url"),
                price=candidate["price"],
                price_display=_price_display(candidate["price"], lang),
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
