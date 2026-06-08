from __future__ import annotations

import json
import os
from pathlib import Path

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

# Find products whose attributes match the extracted filters, score by confidence × weight.
# Dedup by (attribute_type, value) first so duplicate Attribute nodes don't inflate the score.
_SEARCH_CYPHER = """
UNWIND $filters AS f
MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
WHERE a.attribute_type = f.attribute_type
  AND toLower(a.value) CONTAINS toLower(f.value)
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
  AND ($min_rating IS NULL OR toFloat(p.average_rating) >= $min_rating)
  AND ($price_max IS NULL OR (p.price IS NOT NULL AND toFloat(p.price) <= $price_max))
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       matched_attrs,
       score
ORDER BY score DESC, toFloat(p.average_rating) DESC
LIMIT $limit
"""


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
        response = self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(response.choices[0].message.content)
        return SearchIntent(
            attribute_filters=[AttributeFilter(**f) for f in data.get("attribute_filters", [])],
            keywords=data.get("keywords", []),
            price_max=data.get("price_max"),
            min_rating=data.get("min_rating"),
        )

    def search_products(self, intent: SearchIntent, limit: int) -> list[Recommendation]:
        if not intent.attribute_filters:
            return []
        filters = [
            {"attribute_type": f.attribute_type, "value": f.value, "weight": f.weight}
            for f in intent.attribute_filters
        ]
        with self.driver.session(database=self.neo4j_database) as session:
            result = session.run(
                _SEARCH_CYPHER,
                filters=filters,
                min_rating=intent.min_rating,
                price_max=intent.price_max,
                limit=limit,
            )
            recommendations = []
            for record in result:
                matched = [
                    MatchedAttribute(
                        attribute_type=m["attribute_type"],
                        name=m["name"],
                        value=m["value"],
                        confidence=m["confidence"],
                        evidence=m.get("evidence"),
                    )
                    for m in record["matched_attrs"]
                ]
                price = record["price"]
                avg_rating = record["average_rating"]
                rating_num = record["rating_number"]
                recommendations.append(
                    Recommendation(
                        product_id=record["product_id"],
                        title=record["title"],
                        price=float(price) if price not in (None, "") else None,
                        average_rating=float(avg_rating) if avg_rating not in (None, "") else None,
                        rating_number=int(rating_num) if rating_num not in (None, "") else None,
                        score=record["score"],
                        matched_attributes=matched,
                        explanation=_build_explanation(matched),
                    )
                )
        return recommendations

    def recommend(self, query: str, limit: int = 10) -> tuple[SearchIntent, list[Recommendation]]:
        intent = self.extract_intent(query)
        products = self.search_products(intent, limit)
        return intent, products

    def close(self) -> None:
        self.driver.close()


def _build_explanation(matched: list[MatchedAttribute]) -> str:
    by_type: dict[str, list[str]] = {}
    for m in matched:
        by_type.setdefault(m.attribute_type, []).append(m.value)
    parts = [f"{t}: {', '.join(vs)}" for t, vs in by_type.items()]
    return "Matched — " + " | ".join(parts)
