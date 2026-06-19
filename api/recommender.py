from __future__ import annotations

import json
import math
import os
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
       field_terms
ORDER BY size(field_terms) DESC, toFloat(p.average_rating) DESC
LIMIT $candidate_limit
"""

RATING_PRIOR = 3.8
RATING_PRIOR_COUNT = 50
POPULARITY_REFERENCE_COUNT = 5000


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
                            evidence=m.get("evidence"),
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
                        evidence = match.get("evidence")
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

        return _rank_candidates(candidates, intent, terms, limit)

    def recommend(self, query: str, limit: int = 10) -> tuple[SearchIntent, list[Recommendation]]:
        intent = self.extract_intent(query)
        products = self.search_products(intent, limit)
        return intent, products

    def close(self) -> None:
        self.driver.close()


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
        term = " ".join(value.lower().strip().split())
        if len(term) < 2 or term in terms:
            continue
        terms.append(term)
    return terms


def _candidate(candidates: dict[str, dict[str, Any]], record: Any) -> dict[str, Any]:
    product_id = record["product_id"]
    candidate = candidates.get(product_id)
    if candidate is None:
        candidate = {
            "product_id": product_id,
            "title": record["title"],
            "price": _optional_float(record["price"]),
            "average_rating": _optional_float(record["average_rating"]),
            "rating_number": _optional_int(record["rating_number"]),
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
) -> list[Recommendation]:
    total_attribute_weight = sum(max(f.weight, 0.0) for f in intent.attribute_filters) or 1.0
    total_terms = len(terms) or 1
    total_signals = len(intent.attribute_filters) + len(terms) or 1

    recommendations: list[Recommendation] = []
    for candidate in candidates.values():
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

        recommendations.append(
            Recommendation(
                product_id=candidate["product_id"],
                title=candidate["title"],
                price=candidate["price"],
                average_rating=candidate["average_rating"],
                rating_number=candidate["rating_number"],
                score=round(final_score, 4),
                matched_attributes=matched_attributes,
                matched_terms=matched_terms,
                matched_feature_evidence=candidate["feature_evidence"][:5],
                score_breakdown=breakdown,
                explanation=_build_rich_explanation(candidate, matched_terms),
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
    return recommendations[:limit]


def _build_rich_explanation(candidate: dict[str, Any], matched_terms: list[str]) -> str:
    parts: list[str] = []
    attribute_explanation = _build_explanation(candidate["matched_attributes"])
    if attribute_explanation != "Matched":
        parts.append(attribute_explanation)
    if matched_terms:
        parts.append("text terms: " + ", ".join(matched_terms[:6]))
    if candidate["average_rating"] is not None:
        rating_number = candidate["rating_number"] or 0
        parts.append(f"rating: {candidate['average_rating']:.1f} from {rating_number} ratings")
    return " | ".join(parts) if parts else "Matched by product quality signals"


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
