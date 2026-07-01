"""
LLM-driven autonomous Text2Cypher recommender.

Endpoints served:
  POST /recommend        — keyword search with optional personalization
  POST /recommend/home   — behavior-based recommendations (no query text)
  POST /behavior/view    — log product view to Neo4j

Flow (search):
  1. Build user context from Neo4j (rated/viewed products, inferred attributes)
  2. LLM chooses query strategy and generates Cypher
  3. Execute Cypher; retry on error up to max_cypher_attempts
  4. Return results

Flow (home):
  1. Build user context
  2. LLM generates personalized Cypher (collaborative filtering or attribute similarity)
  3. Falls back to popular products when user has no history
"""
from __future__ import annotations

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
