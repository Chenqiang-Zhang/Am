from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


MENTION_TYPES = [
    "benefit", "skin_type", "scent", "texture", "ingredient",
    "product_type", "hair_type", "target_area", "usage", "color",
]

SYSTEM_PROMPT = f"""Extract beauty product attribute mentions from customer reviews.

Return JSON only:
{{
  "mentions": [
    {{
      "attribute_type": "one of: {", ".join(MENTION_TYPES)}",
      "value": "short lowercase normalized value",
      "sentiment": "positive|negative|neutral",
      "confidence": 0.0,
      "evidence": "short quote from the review"
    }}
  ]
}}

Rules:
- Extract only attributes explicitly stated in the review.
- Skip shipping, seller, delivery, packaging damage, and generic praise without a product attribute.
- Use negative sentiment for complaints like irritating, too strong scent, drying, greasy, sticky, breakouts.
- Use positive sentiment for concrete praise like gentle, moisturizing, fragrance-free, lightweight, good for sensitive skin.
- Keep evidence short and copied from the review text.
"""


FETCH_REVIEWS_CYPHER = """
MATCH (r:Review)-[:REVIEWS]->(p:Product)
WHERE r.text IS NOT NULL
  AND size(coalesce(r.text, "")) >= $min_text_len
  AND NOT (r)-[:MENTIONS]->(:Attribute)
RETURN r.review_id AS review_id,
       p.product_id AS product_id,
       r.title AS title,
       r.text AS text,
       r.rating AS rating
ORDER BY coalesce(r.helpful_vote, 0) DESC
LIMIT $limit
"""


WRITE_MENTIONS_CYPHER = """
UNWIND $rows AS row
MATCH (r:Review {review_id: row.review_id})
MERGE (a:Attribute {attribute_id: row.attribute_id})
SET a.name = row.attribute_type,
    a.attribute_type = row.attribute_type,
    a.value = row.value
MERGE (r)-[rel:MENTIONS]->(a)
SET rel.sentiment = row.sentiment,
    rel.confidence = row.confidence,
    rel.evidence = row.evidence,
    rel.model = row.model,
    rel.created_at = row.created_at
"""


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_attr_type(value: Any) -> str:
    raw = clean_text(value).lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if raw in {"item_form", "formula"}:
        return "texture"
    if raw in {"product_benefit", "effect"}:
        return "benefit"
    return raw if raw in MENTION_TYPES else "benefit"


def normalize_value(value: Any) -> str:
    raw = clean_text(value).lower()
    raw = re.sub(r"\s+", " ", raw)
    return raw[:80]


def attribute_id(attribute_type: str, value: str) -> str:
    digest = hashlib.sha1(f"{attribute_type}|{value}".encode("utf-8")).hexdigest()[:16]
    return f"attr_{digest}"


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def build_client() -> tuple[Any, str]:
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    try:
        from openai import OpenAI
    except ImportError:
        print("Install openai: pip install openai", file=sys.stderr)
        raise
    if provider == "deepseek":
        return (
            OpenAI(
                api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            ),
            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        )
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"]), os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def extract_mentions(client: Any, model: str, review: dict[str, Any], retries: int) -> list[dict[str, Any]]:
    content = json.dumps(
        {
            "review_id": review["review_id"],
            "rating": review.get("rating"),
            "title": review.get("title"),
            "text": review.get("text"),
        },
        ensure_ascii=False,
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = parse_json(response.choices[0].message.content or "{}")
            mentions = []
            for mention in data.get("mentions", []):
                attribute_type = normalize_attr_type(mention.get("attribute_type"))
                value = normalize_value(mention.get("value"))
                confidence = float(mention.get("confidence") or 0.0)
                sentiment = clean_text(mention.get("sentiment")).lower()
                if sentiment not in {"positive", "negative", "neutral"}:
                    sentiment = "neutral"
                if not value or confidence <= 0:
                    continue
                mentions.append(
                    {
                        "attribute_type": attribute_type,
                        "value": value,
                        "sentiment": sentiment,
                        "confidence": max(0.0, min(confidence, 1.0)),
                        "evidence": clean_text(mention.get("evidence"))[:240],
                    }
                )
            return mentions
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    print(f"review {review['review_id']} failed: {last_error}", file=sys.stderr)
    return []


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Extract Review -[:MENTIONS]-> Attribute edges with an LLM.")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER") or os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--min-text-len", type=int, default=40)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.uri or not args.password:
        print("Set NEO4J_URI and NEO4J_PASSWORD in .env or pass --uri/--password.", file=sys.stderr)
        return 2
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("Install neo4j: pip install neo4j", file=sys.stderr)
        return 2

    client, model = build_client()
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    total_mentions = 0
    try:
        with driver.session(database=args.database) as session:
            reviews = [dict(record) for record in session.run(FETCH_REVIEWS_CYPHER, limit=args.limit, min_text_len=args.min_text_len)]
            print(f"reviews: {len(reviews):,}")
            rows: list[dict[str, Any]] = []
            for index, review in enumerate(reviews, 1):
                for mention in extract_mentions(client, model, review, args.retries):
                    rows.append(
                        {
                            "review_id": review["review_id"],
                            "attribute_id": attribute_id(mention["attribute_type"], mention["value"]),
                            "created_at": created_at,
                            "model": model,
                            **mention,
                        }
                    )
                if len(rows) >= args.batch_size:
                    if not args.dry_run:
                        session.run(WRITE_MENTIONS_CYPHER, rows=rows).consume()
                    total_mentions += len(rows)
                    print(f"processed {index:,}/{len(reviews):,}, mentions {total_mentions:,}")
                    rows = []
            if rows:
                if not args.dry_run:
                    session.run(WRITE_MENTIONS_CYPHER, rows=rows).consume()
                total_mentions += len(rows)
        print(f"mentions: {total_mentions:,}")
        if args.dry_run:
            print("dry-run: skipped Neo4j writes")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
