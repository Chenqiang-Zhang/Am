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
     response) whether to ask another clarifying question or move to search;
     Python only enforces MAX_QUESTIONS as a hard cap and falls back to
     searching immediately if the LLM call itself fails
  3. Once search is triggered, delegate to the same Text2Cypher search used
     by /recommend
"""
from __future__ import annotations

import html as _html_mod
import json
import os
import re
import sys
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
               value_ja (string|null, Japanese translation of value) }
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
- Use $uid when referencing the user; NEVER hardcode a user_id string
- End every query with: ORDER BY score DESC LIMIT $limit
- Case-insensitive match: toLower(a.value) CONTAINS toLower("keyword")
- Price filter: toFloat(p.price) <= X  AND p.price IS NOT NULL
- NEVER use CREATE, MERGE, DELETE, SET, or any write clause
- Required RETURN aliases (exact names):
    product_id, title, title_ja, price, avg_rating, rating_count, score, explanation, matched_attrs, image_url
- matched_attrs: collect({attr_type: a.attr_type, value: a.value, value_ja: a.value_ja})  — use [] when no attrs
  (always include value_ja alongside value — Python picks whichever fits the requested language)

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
        if isinstance(m, dict) and m.get("attr_type") and m.get("value"):
            value_ja = m.get("value_ja")
            display_value = value_ja if (lang == "ja" and value_ja) else str(m["value"])
            matched_attrs.append(
                MatchedAttr(attr_type=str(m["attr_type"]), value=display_value)
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
- action = "ask" while filled_slots < 2 AND fewer than {MAX_QUESTIONS} questions have been
  asked so far AND the user hasn't said they have no preferences at all.
- action = "search" once filled_slots >= 2, OR the user said they have no preference at
  all, OR {MAX_QUESTIONS} questions have already been asked.
- Use the full conversation history (including your own prior questions) to avoid asking
  about something already answered or already skipped.

Always include "preference_summary": ALL of the user's confirmed preferences accumulated
across the ENTIRE conversation so far (not just this latest turn) as short labels for
display, written in {target_language} only (do not mix languages). This MUST include
the product category/type itself if it's known (e.g. what kind of product the user
originally asked for), in addition to every other preference confirmed in any turn.

CONVERSATION HISTORY NOTE: previous assistant messages in the history contain only the
question text shown to the user. This does NOT mean you should respond in plain text —
you MUST ALWAYS respond with a valid JSON object.

Return ONLY this JSON object (no other text before or after):
{{
  "action": "ask" | "search",
  "question": "(if action=ask) the question text, otherwise null",
  "options": ["(if action=ask) quick-reply options"],
  "slot": "(a short snake_case name for what this question is asking about) | null",
  "filled_slots": <integer>,
  "preference_summary": []
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
        self._genre: str = str(cfg.get("genre", "products"))
        self._attr_vocab_text: str | None = None  # lazily populated, see _get_attr_vocab_text()
        self._home_cache: dict[str, dict[str, Any]] = {}  # see _get_or_generate_home()

    # ── public API ──────────────────────────────────────────────────────────────

    def recommend(
        self, query: str, user_id: str | None = None, limit: int = 10, lang: str = "en"
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        fallback = False
        normalized_lang = _normalize_lang(lang)
        user_ctx = self._get_user_context(user_id) if user_id else None
        # $uidが使えるのはuser_idがあり、かつ実際にRATED/属性の履歴がある場合のみ
        # （履歴が無ければ$uidを束縛しても個人化の意味が無く、誤ってuidを使われるのを防ぐ）
        has_uid = bool(user_ctx and (user_ctx.get("rated") or user_ctx.get("preferred_attrs")))
        dynamic_few_shot = self._get_dynamic_few_shot(user_id) if user_id else []
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
        action/filled_slotsに委ねる（カテゴリ非依存）。Python側はMAX_QUESTIONSの
        安全網と、LLM呼び出し自体が失敗した場合に検索へフォールバックする処理のみ持つ。
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
                response_format={"type": "json_object"}, temperature=0,
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

        # LLM呼び出し自体が失敗した場合(dataが空)は、聞き返しを続けられないので検索へ倒す
        should_search = (
            not data
            or data.get("action") == "search"
            or filled_slots >= 2
            or asked >= MAX_QUESTIONS
        )

        # ── 結果を返す：search は Text2Cypher に委譲 ─────────────────────────
        if should_search:
            query_text = " ".join(m.get("content", "") for m in all_user_msgs)
            search_id, intent, products, _fallback = self.recommend(query_text, user_id, limit, normalized_lang)
            return {
                "action": "search",
                "question": None,
                "options": [],
                "preference_summary": summary,
                "intent": intent,
                "recommendations": products,
                "search_id": search_id,
            }

        fallback_question = "他にご希望はありますか？" if normalized_lang == "ja" else "Any other preferences?"
        return {
            "action": "ask",
            "question": data.get("question") or fallback_question,
            "options": data.get("options") or [],
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
        ——VIEWED行動ログとSearchLog検索履歴——だけを削除する。デモで行動ログをまっさらな
        状態に戻したい時に使う（推薦の根拠であるRATED自体は変更しない）。"""
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
        # 古い履歴を前提に生成されたホーム推薦キャッシュも破棄する
        for key in [k for k in self._home_cache if k.startswith(f"{user_id}:")]:
            del self._home_cache[key]
        return {
            "viewed_deleted": viewed_result["n"] if viewed_result else 0,
            "searches_deleted": search_result["n"] if search_result else 0,
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
                    "RETURN p.title AS title, r.rating AS rating "
                    "ORDER BY r.rating DESC, r.timestamp DESC LIMIT 6",
                    uid=user_id,
                )
                rated = [{"title": r["title"], "rating": r["rating"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[v:VIEWED]->(p:Product) "
                    "RETURN p.title AS title "
                    "ORDER BY v.timestamp DESC LIMIT 4",
                    uid=user_id,
                )
                viewed = [{"title": r["title"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product)"
                    "-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "WHERE r.rating >= 4 "
                    "RETURN a.attr_type AS attr_type, a.value AS value, "
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

