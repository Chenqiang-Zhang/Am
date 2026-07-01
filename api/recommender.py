"""
LLM-driven autonomous Text2Cypher recommender.

Endpoints served:
  POST /recommend                        — keyword search with optional personalization
  POST /recommend/home                   — behavior-based recommendations (no query text)
  POST /behavior/view                    — log product view to Neo4j
  POST /chat                             — multi-turn conversational recommendation (CRS)
  GET  /products/{product_id}/reviews    — top reviews for a product
  POST /recommendations/{product_id}/feedback — user feedback on a recommendation reason

Flow (search):
  1. Build user context from Neo4j (rated/viewed products, inferred attributes)
  2. LLM chooses query strategy and generates Cypher
  3. Execute Cypher; retry on error up to max_cypher_attempts
  4. Return results

Flow (home):
  1. Build user context
  2. LLM generates personalized Cypher (collaborative filtering or attribute similarity)
  3. Falls back to popular products when user has no history

Flow (chat):
  1. Python tracks which preference slots have been answered across turns
     (stable ask/search decision, independent of LLM instruction-following)
  2. While slots are missing, ask one clarifying question at a time
  3. Once enough preferences are collected, delegate to the same Text2Cypher
     search used by /recommend
"""
from __future__ import annotations

import html as _html_mod
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase

from .models import MatchedAttr, Recommendation, SearchIntent

# ── graph schema ───────────────────────────────────────────────────────────────

_SCHEMA = """\
Nodes:
  Product   { product_id, title, price (float|null), avg_rating (float|null),
               rating_count (int|null), description }
  User      { user_id }
  Review    { review_id, rating (float 1-5), timestamp (int unix-ms),
               helpful_vote (int), verified (bool), title, text }
  Category  { category_id, name, level (int, 0=root) }
  Brand     { brand_id, name }
  Attribute { attribute_id, attr_type, value }
    attr_type examples: scent, texture, ingredient, skin_type, benefit,
                        product_type, hair_type, color, size, target_area, usage

Relationships:
  (User)-[:RATED {rating (float), timestamp}]->(Product)
  (User)-[:WROTE]->(Review)
  (User)-[:VIEWED {timestamp}]->(Product)
  (Review)-[:ABOUT]->(Product)
  (Product)-[:MADE_BY]->(Brand)
  (Product)-[:BELONGS_TO]->(Category)
  (Category)-[:SUBCATEGORY_OF]->(Category)
  (Product)-[:HAS_ATTRIBUTE {confidence (float), evidence}]->(Attribute)
"""

_FEW_SHOT_EXAMPLES = """\
## Examples (for reference — invent your own approach as needed)

User: "sunscreen for sensitive skin"
{"cypher": "MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute) WHERE (a.attr_type='skin_type' AND toLower(a.value) CONTAINS 'sensitive') OR (a.attr_type IN ['benefit','product_type'] AND (toLower(a.value) CONTAINS 'sun' OR toLower(a.value) CONTAINS 'spf')) WITH p, collect({attr_type:a.attr_type,value:a.value}) AS matched_attrs, sum(toFloat(r.confidence)) AS cs RETURN p.product_id AS product_id, p.title AS title, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, cs*2+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Sunscreen for sensitive skin' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Finds sunscreens matched to sensitive skin type"}

User: "recommend something I would like" (user_id provided as $uid)
{"cypher": "MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(rec:Product) WHERE r.rating>=4 AND NOT (u)-[:RATED]->(rec) WITH rec AS p, collect(DISTINCT {attr_type:a.attr_type,value:a.value}) AS matched_attrs, count(DISTINCT a) AS shared RETURN p.product_id AS product_id, p.title AS title, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(shared)*1.5+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Similar to products you rated highly' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Attribute similarity from user's high-rated products"}

User: "vitamin c serum well reviewed by users"
{"cypher": "MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute) WHERE a.attr_type='ingredient' AND toLower(a.value) CONTAINS 'vitamin c' WITH p, collect({attr_type:a.attr_type,value:a.value}) AS matched_attrs, sum(toFloat(r.confidence)) AS attr_score MATCH (p)<-[:ABOUT]-(rev:Review) WHERE rev.rating>=4 WITH p, matched_attrs, attr_score, count(DISTINCT rev) AS pos_reviews RETURN p.product_id AS product_id, p.title AS title, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, attr_score+toFloat(pos_reviews)*0.3+coalesce(p.avg_rating,3.5)*0.5+log(toFloat(coalesce(p.rating_count,1))+1)*0.2 AS score, 'Vitamin C serum with strong positive reviews' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Vitamin C serums scored by attribute confidence and high-rated reviews"}
"""

_RULES = """\
## Rules
- Use $uid when referencing the user; NEVER hardcode a user_id string
- End every query with: ORDER BY score DESC LIMIT $limit
- Case-insensitive match: toLower(a.value) CONTAINS toLower("keyword")
- Price filter: toFloat(p.price) <= X  AND p.price IS NOT NULL
- NEVER use CREATE, MERGE, DELETE, SET, or any write clause
- Required RETURN aliases (exact names):
    product_id, title, price, avg_rating, rating_count, score, explanation, matched_attrs
- matched_attrs: collect({attr_type: a.attr_type, value: a.value})  — use [] when no attrs

## Excluding already-rated/viewed products (CRITICAL)
Bind the user node in MATCH first, then filter in WHERE:
  CORRECT:   MATCH (u:User {user_id:$uid}) ... WHERE NOT (u)-[:RATED]->(p) AND NOT (u)-[:VIEWED]->(p)
  CORRECT:   MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product) ... WHERE NOT (u)-[:RATED]->(rec)
  WRONG:     WHERE NOT (p)<-[:RATED]-(u:User {user_id:$uid}) AND NOT (p)<-[:VIEWED]-(u)
  — in the WRONG pattern, 'u' in the second NOT is unbound and excludes ALL viewed products

Output — JSON only, no markdown fences:
{"cypher": "<valid Cypher>", "explanation": "<one sentence>"}
"""

_FALLBACK_CYPHER = (
    "MATCH (p:Product) "
    "WHERE p.avg_rating IS NOT NULL AND p.rating_count >= 50 "
    "WITH p, "
    "  coalesce(p.avg_rating, 3.5) * 0.5 "
    "  + log(toFloat(p.rating_count) + 1) * 0.2 AS score, "
    "  [] AS matched_attrs "
    "RETURN p.product_id AS product_id, p.title AS title, p.price AS price, "
    "  p.avg_rating AS avg_rating, p.rating_count AS rating_count, score, "
    "  'Popular highly-rated product' AS explanation, matched_attrs "
    "ORDER BY score DESC LIMIT $limit"
)
_FALLBACK_EXPLANATION = "Popular highly-rated products (fallback)"

_FIX_PROMPT = f"""\
You are a Cypher expert. Fix the broken query so it runs correctly in Neo4j 5.
Preserve the original intent and all required RETURN columns.

Graph Schema:
{_SCHEMA}

Required RETURN: product_id, title, price, avg_rating, rating_count, score, explanation, matched_attrs
Always end with: ORDER BY score DESC LIMIT $limit

Output JSON only: {{"cypher": "<fixed Cypher>", "explanation": "<one sentence>"}}
"""


def _build_search_prompt(user_ctx: dict | None, dynamic_few_shot: list[dict] | None = None) -> str:
    parts = [
        "You are a Cypher query generator for a Neo4j beauty-product knowledge graph.\n"
        "Given a user search query, generate ONE Cypher READ query that best answers it.\n"
        "Think freely — design the query that fits the request. "
        "You may traverse any relationships in the schema, combine multiple MATCH clauses, "
        "or invent a novel scoring expression. Do not limit yourself to a fixed set of patterns.",
        f"## Graph Schema\n{_SCHEMA}",
        _FEW_SHOT_EXAMPLES,
    ]
    if dynamic_few_shot:
        parts.append(_format_dynamic_few_shot(dynamic_few_shot))
    if user_ctx and any(user_ctx.values()):
        parts.append(_format_user_ctx(user_ctx))
        parts.append(
            "## Personalization\n"
            "Incorporate the user context above to personalize results.\n"
            "- Use $uid when referencing this user in Cypher\n"
            "- Exclude products the user already RATED or VIEWED when possible"
        )
    parts.append(_RULES)
    return "\n\n".join(parts)


def _build_home_prompt(user_ctx: dict | None) -> str:
    parts = [
        "You are a Cypher query generator for a Neo4j beauty-product knowledge graph.\n"
        "TASK: Generate home-page recommendations shown when the user opens the app (no search query).\n"
        "Think freely — invent the query approach that best serves the user based on their history.",
        f"## Graph Schema\n{_SCHEMA}",
        _FEW_SHOT_EXAMPLES,
    ]
    if user_ctx and any(user_ctx.values()):
        parts.append(_format_user_ctx(user_ctx))
        parts.append(
            "## Hint\n"
            "User history exists. Generate a personalized query using $uid.\n"
            "Exclude products the user already RATED or VIEWED."
        )
    else:
        parts.append(
            "## Hint\n"
            "No user history available. Show popular highly-rated products.\n"
            "Do NOT reference $uid in the query."
        )
    parts.append(_RULES)
    return "\n\n".join(parts)


def _format_user_ctx(ctx: dict) -> str:
    lines = ["## User Context"]
    if ctx.get("rated"):
        lines.append("Rated products (high rating first):")
        for p in ctx["rated"][:6]:
            lines.append(f"  [{p['rating']:.1f}★] {p['title']}")
    if ctx.get("viewed"):
        lines.append("Recently viewed:")
        for p in ctx["viewed"][:4]:
            lines.append(f"  {p['title']}")
    if ctx.get("preferred_attrs"):
        lines.append("Inferred preferred attributes (from 4+ star ratings):")
        for a in ctx["preferred_attrs"][:8]:
            lines.append(f"  {a['attr_type']}: {a['value']}  (×{a['freq']})")
    if ctx.get("recent_queries"):
        lines.append("Recent searches (newest first):")
        for q in ctx["recent_queries"][:5]:
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


def _record_to_recommendation(record: Any) -> Recommendation:
    matched_attrs: list[MatchedAttr] = []
    for m in (record.get("matched_attrs") or []):
        if isinstance(m, dict) and m.get("attr_type") and m.get("value"):
            matched_attrs.append(
                MatchedAttr(attr_type=str(m["attr_type"]), value=str(m["value"]))
            )
    return Recommendation(
        product_id=str(record.get("product_id", "")),
        title=str(record.get("title", "")),
        price=_to_float(record.get("price")),
        avg_rating=_to_float(record.get("avg_rating")),
        rating_count=_to_int(record.get("rating_count")),
        score=float(record.get("score") or 0.0),
        matched_attrs=matched_attrs,
        explanation=str(record.get("explanation", "")),
    )


# ── conversational recommendation (CRS) — chat constants ────────────────────────

ATTRIBUTE_TYPES = [
    "benefit", "skin_type", "scent", "texture", "ingredient",
    "material", "color", "size", "target_area", "usage",
    "brand", "product_type",
]

# 対話型推薦：聞き返しは最大この回数まで（しつこくしない）
MAX_QUESTIONS = 5  # 無限ループ防止の安全網。通常は py_filled >= 2 で先に search になる。

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

When action is "search", produce a structured intent summary (for reference only — the actual product search is performed separately via graph-path retrieval):
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


_HEURISTIC_RULES: list[dict[str, Any]] = [
    {
        "slot": "product_type",
        "value": "moisturizer",
        "summary": {"en": "moisturizer", "ja": "保湿クリーム"},
        "terms": ["moisturizer", "moisturiser", "face cream", "cream", "lotion", "保湿クリーム", "クリーム", "乳液"],
    },
    {
        "slot": "product_type",
        "value": "serum",
        "summary": {"en": "serum", "ja": "美容液"},
        "terms": ["serum", "essence", "美容液"],
    },
    {
        "slot": "product_type",
        "value": "toner",
        "summary": {"en": "toner", "ja": "化粧水"},
        "terms": ["toner", "化粧水"],
    },
    {
        "slot": "product_type",
        "value": "cleanser",
        "summary": {"en": "cleanser", "ja": "洗顔料"},
        "terms": ["cleanser", "face wash", "洗顔", "洗顔料"],
    },
    {
        "slot": "skin_type",
        "value": "dry",
        "summary": {"en": "dry skin", "ja": "乾燥肌"},
        "terms": ["dry skin", "dry", "乾燥肌", "乾燥"],
    },
    {
        "slot": "skin_type",
        "value": "sensitive",
        "summary": {"en": "sensitive skin", "ja": "敏感肌"},
        "terms": ["sensitive skin", "sensitive", "敏感肌", "敏感"],
    },
    {
        "slot": "scent",
        "value": "unscented",
        "summary": {"en": "fragrance-free", "ja": "無香料"},
        "terms": ["fragrance-free", "fragrance free", "unscented", "no fragrance", "無香料", "無香"],
    },
    {
        "slot": "ingredient",
        "value": "hyaluronic acid",
        "summary": {"en": "hyaluronic acid", "ja": "ヒアルロン酸"},
        "terms": ["hyaluronic acid", "ヒアルロン酸"],
    },
    {
        "slot": "ingredient",
        "value": "vitamin c",
        "summary": {"en": "vitamin C", "ja": "ビタミンC"},
        "terms": ["vitamin c", "ビタミンc", "ビタミンC"],
    },
    {
        "slot": "ingredient",
        "value": "retinol",
        "summary": {"en": "retinol", "ja": "レチノール"},
        "terms": ["retinol", "レチノール"],
    },
    {
        "slot": "benefit",
        "value": "moisturizing",
        "summary": {"en": "moisturizing", "ja": "保湿"},
        "terms": ["moisturizing", "hydrating", "hydration", "保湿", "うるおい", "潤い"],
    },
    {
        "slot": "benefit",
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


def _normalize_lang(lang: str | None) -> str:
    return "ja" if (lang or "").lower().startswith("ja") else "en"


def _heuristic_summary(text: str, lang: str) -> list[str]:
    normalized_lang = _normalize_lang(lang)
    summary: list[str] = []
    for rule in _HEURISTIC_RULES:
        if _matches_any(text, rule["terms"]):
            label = rule["summary"][normalized_lang]
            if label not in summary:
                summary.append(label)
    return summary


def _strip_html(text: str | None) -> str | None:
    if text is None:
        return None
    return _html_mod.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# ── Recommender ────────────────────────────────────────────────────────────────

class Recommender:
    def __init__(self, config_path: Path | None = None) -> None:
        cfg_path = config_path or (Path(__file__).parent.parent / "config.yaml")
        cfg: dict = {}
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

        _load_env(cfg_path.parent / ".env")

        llm_cfg: dict = cfg.get("llm", {})
        t2c_cfg: dict = cfg.get("text2cypher", {})
        neo4j_cfg: dict = cfg.get("neo4j", {})

        provider = str(llm_cfg.get("provider", "gemini"))
        model = llm_cfg.get("model") or None
        base_url = llm_cfg.get("base_url") or None
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

    # ── public API ──────────────────────────────────────────────────────────────

    def recommend(
        self, query: str, user_id: str | None = None, limit: int = 10
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        fallback = False
        user_ctx = self._get_user_context(user_id) if user_id else None
        dynamic_few_shot = self._get_dynamic_few_shot(user_id) if user_id else []
        system_prompt = _build_search_prompt(user_ctx, dynamic_few_shot)
        try:
            cypher, explanation = self._generate_cypher(system_prompt, query, limit)
            params: dict[str, Any] = {"limit": limit}
            if user_id:
                params["uid"] = user_id
            results = self._execute_and_map(cypher, params)
        except Exception as exc:
            print(f"[recommender] recommend failed, using fallback: {exc}", file=sys.stderr)
            cypher, explanation = _FALLBACK_CYPHER, _FALLBACK_EXPLANATION
            results = self._execute_and_map(cypher, {"limit": limit})
            fallback = True
        intent = SearchIntent(cypher=cypher, cypher_explanation=explanation)
        if user_id:
            self.log_search(user_id, search_id, query, cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    def recommend_home(
        self, user_id: str, limit: int = 10
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        fallback = False
        user_ctx = self._get_user_context(user_id)
        # VIEWEDだけでは属性情報が得られないため、RATEDまたは属性があるときのみパーソナライズ
        has_history = bool(user_ctx.get("rated") or user_ctx.get("preferred_attrs"))
        try:
            system_prompt = _build_home_prompt(user_ctx if has_history else None)
            user_msg = (
                "Generate personalized home-page product recommendations based on user history."
                if has_history
                else "No user history. Show popular and highly-rated beauty products."
            )
            cypher, explanation = self._generate_cypher(system_prompt, user_msg, limit)
            params: dict[str, Any] = {"limit": limit}
            if has_history:
                params["uid"] = user_id
            results = self._execute_and_map(cypher, params)
        except Exception as exc:
            print(f"[recommender] recommend_home failed, using fallback: {exc}", file=sys.stderr)
            results = []
        if not results:
            cypher, explanation = _FALLBACK_CYPHER, _FALLBACK_EXPLANATION
            results = self._execute_and_map(cypher, {"limit": limit})
            fallback = True
        intent = SearchIntent(cypher=cypher, cypher_explanation=explanation)
        self.log_search(user_id, search_id, "[home]", cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    def chat(self, messages: list[dict[str, Any]], limit: int = 10, lang: str = "ja") -> dict[str, Any]:
        """対話型推薦の1ターン。

        ask/search の判断を Python で担当し LLM の行動決定ルール遵守に依存しない安定動作を実現。
        - detected_slots: 会話全文から検出した回答済みスロット（テンプレートマッチング）
        - asked: これまでにassistantが発言した回数
        search が決まった後の商品検索は self.recommend() 経由の Text2Cypher に委譲する。
        """
        all_user_msgs = [m for m in messages if m.get("role") == "user"]
        asked = sum(1 for m in messages if m.get("role") == "assistant")

        user_said_no_pref = any(
            kw in m.get("content", "")
            for m in all_user_msgs
            for kw in ("おまかせ", "なんでも", "no preference", "don't mind", "どちらでも")
        )
        asked_slots = _extract_asked_slots(messages, lang)

        user_text = " ".join(m.get("content", "") for m in all_user_msgs).lower()
        product_type = _guess_product_type(user_text)
        detected_slots = _detect_filled_slots(user_text)
        asked_slots |= detected_slots
        py_filled = len(detected_slots)

        should_search = py_filled >= 2 or user_said_no_pref or asked >= MAX_QUESTIONS

        # ── LLM 呼び出し: preference_summary の生成 ─────────────────────────
        target_language = "Japanese" if lang == "ja" else "English"
        system = CHAT_SYSTEM_PROMPT + (
            f"\n\nTARGET LANGUAGE = {target_language}. Write preference_summary ONLY in this language."
        )
        llm_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
            llm_messages.append({"role": role, "content": m.get("content", "")})

        try:
            response = self._llm.chat.completions.create(
                model=self._model, messages=llm_messages, temperature=0, **_json_format_kwargs()
            )
            data = _parse_llm_json(response.choices[0].message.content or "{}")
        except Exception:
            data = {}
        summary = data.get("preference_summary") or []
        if not summary:
            summary = _heuristic_summary(user_text, lang)

        # ── 結果を返す：search は Text2Cypher に委譲 ─────────────────────────
        if should_search:
            query_text = " ".join(m.get("content", "") for m in all_user_msgs)
            _search_id, intent, products, _fallback = self.recommend(query_text, None, limit)
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

    def get_reviews(self, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """商品IDに紐づくレビューをhelpful_vote降順で返す。(Review)-[:ABOUT]->(Product) エッジを使用。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})<-[:ABOUT]-(r:Review)
WHERE r.text IS NOT NULL AND size(coalesce(r.text, '')) > 10
RETURN r.title AS title,
       r.text AS text,
       toFloat(r.rating) AS rating,
       toInteger(r.helpful_vote) AS helpful_vote,
       r.verified AS verified_purchase
ORDER BY r.helpful_vote DESC, r.rating DESC
LIMIT $limit
"""
        with self._driver.session(database=self._neo4j_db) as session:
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
                    "RETURN p.title AS title, r.rating AS rating "
                    "ORDER BY r.rating DESC, r.timestamp DESC LIMIT 10",
                    uid=user_id,
                )
                rated = [{"title": r["title"], "rating": r["rating"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[v:VIEWED]->(p:Product) "
                    "RETURN p.title AS title "
                    "ORDER BY v.timestamp DESC LIMIT 5",
                    uid=user_id,
                )
                viewed = [{"title": r["title"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product)"
                    "-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "WHERE r.rating >= 4 "
                    "RETURN a.attr_type AS attr_type, a.value AS value, "
                    "count(*) AS freq "
                    "ORDER BY freq DESC LIMIT 10",
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
        """Return past (query, cypher) pairs that led to at least one click."""
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                    "WHERE sl.cypher IS NOT NULL AND sl.query <> '[home]' "
                    "WITH u, sl "
                    "OPTIONAL MATCH (u)-[v:VIEWED]->(:Product) "
                    "WHERE v.search_id = sl.log_id "
                    "WITH sl, count(v) AS clicks "
                    "WHERE clicks > 0 "
                    "RETURN sl.query AS query, sl.cypher AS cypher, sl.explanation AS explanation "
                    "ORDER BY clicks DESC, sl.timestamp DESC LIMIT 3",
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
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1500,
        )
        return _parse_llm_json(resp.choices[0].message.content or "{}")

    # ── Cypher generation with retry-on-error ───────────────────────────────────

    def _generate_cypher(
        self, system_prompt: str, user_msg: str, limit: int
    ) -> tuple[str, str]:
        data = self._call_llm(system_prompt, user_msg)
        cypher: str = data.get("cypher", "").strip()
        explanation: str = data.get("explanation", "")

        for _ in range(1, self._max_attempts):
            try:
                self._validate_cypher(cypher, limit)
                return cypher, explanation
            except Exception as exc:
                fix_user = (
                    f"Original request: {user_msg}\n\n"
                    f"Broken Cypher:\n{cypher}\n\n"
                    f"Neo4j error:\n{exc}"
                )
                try:
                    fix_data = self._call_llm(_FIX_PROMPT, fix_user)
                    cypher = fix_data.get("cypher", cypher).strip()
                    explanation = fix_data.get("explanation", explanation)
                except Exception:
                    break

        return cypher, explanation

    def _validate_cypher(self, cypher: str, limit: int) -> None:
        if not cypher:
            raise ValueError("Empty Cypher query")
        with self._driver.session(database=self._neo4j_db) as session:
            session.run(f"EXPLAIN {cypher}", limit=limit).consume()

    # ── query execution ──────────────────────────────────────────────────────────

    def _execute_and_map(self, cypher: str, params: dict) -> list[Recommendation]:
        if not cypher:
            return []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                result = session.run(cypher, **params)
                return [_record_to_recommendation(dict(record)) for record in result]
        except Exception as exc:
            print(f"[recommender] Cypher execution failed: {exc}", file=sys.stderr)
            return []
