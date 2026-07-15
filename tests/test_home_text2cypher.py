from __future__ import annotations

import pytest

from app.api.models import Recommendation
from app.api.recommender import (
    Recommender,
    _HOME_REQUIRED_RETURN_ALIASES,
    _build_cypher_explanation_prompt,
    _build_home_prompt,
    _record_to_recommendation,
    _sanitize_home_cypher,
)


def test_home_prompt_is_constrained_to_rich_graph_paths() -> None:
    prompt = _build_home_prompt(
        "Video_Games",
        {
            "rated": [
                {
                    "product_id": "seed-1",
                    "title": "Mario Seed",
                    "rating": 5.0,
                    "attributes": [
                        {"attr_type": "domain_franchise", "value": "mario"}
                    ],
                }
            ],
            "viewed": [],
            "preferred_attrs": [],
            "recent_queries": [],
        },
        "domain_franchise: mario, zelda",
        "ja",
    )

    assert "P1 — rated-item attribute similarity" in prompt
    assert "P2 — peer collaborative filtering" in prompt
    assert "P3 — chronological peer transition" in prompt
    assert "P4 — review-confirmed shared attribute" in prompt
    assert "P5 — category or brand affinity" in prompt
    assert "Think freely" not in prompt
    assert "product_id=seed-1" in prompt
    assert "domain_franchise: mario" in prompt
    assert "This User's Past Successful Queries" not in prompt


def test_text2cypher_result_preserves_graph_evidence() -> None:
    rec = _record_to_recommendation(
        {
            "product_id": "candidate-1",
            "title": "Candidate",
            "score": 7.5,
            "matched_attrs": [
                {"attr_type": "domain_franchise", "value": "mario", "value_ja": "マリオ"}
            ],
            "reason_metrics": {"shared_rated_attributes": 1},
            "explanation": "",
            "recommendation_source": "behavior_only",
            "recommendation_strategy": "attribute_similarity",
            "graph_path": "User -> high-rated product -> shared attribute -> candidate product",
            "seed_titles": ["Mario Seed"],
        },
        "ja",
    )

    assert rec.recommendation_strategy == "attribute_similarity"
    assert rec.graph_path is not None
    assert rec.seed_titles == ["Mario Seed"]
    assert rec.matched_attrs[0].value == "マリオ"


def test_fallback_product_reason_never_claims_purchase() -> None:
    rec = Recommendation(
        product_id="candidate-1",
        title="Candidate",
        score=1.0,
        explanation="",
        recommendation_source="behavior_only",
        recommendation_strategy="attribute_similarity",
        graph_path="User -> seed -> attribute -> candidate",
        seed_titles=["Mario Seed"],
        matched_attrs=[{"attr_type": "domain_franchise", "value": "マリオ"}],
    )

    reason = Recommender._fallback_product_reason(rec, "ja")

    assert "高く評価" in reason
    assert "購入" not in reason
    assert "買" not in reason


def test_cypher_explanation_prompt_explains_final_query_without_purchase_claims() -> None:
    system, user = _build_cypher_explanation_prompt(
        "MATCH (u:User)-[:RATED]->(p:Product) RETURN p", "ja"
    )

    assert "FINAL Cypher" in system
    assert "Never say the user purchased" in system
    assert "MATCH (u:User)" in user


def test_validator_rejects_write_queries_before_neo4j() -> None:
    recommender = Recommender.__new__(Recommender)

    with pytest.raises(ValueError, match="read-only"):
        recommender._validate_cypher(
            "MATCH (p:Product) DELETE p RETURN p.product_id AS product_id "
            "ORDER BY score DESC LIMIT $limit",
            8,
            False,
            {"limit": 8},
        )


def test_home_alias_contract_contains_explanation_evidence() -> None:
    assert "recommendation_strategy" in _HOME_REQUIRED_RETURN_ALIASES
    assert "graph_path" in _HOME_REQUIRED_RETURN_ALIASES
    assert "seed_titles" in _HOME_REQUIRED_RETURN_ALIASES
    assert "reason_metrics" in _HOME_REQUIRED_RETURN_ALIASES


def test_home_sanitizer_removes_duplicate_positive_attribute_filter() -> None:
    broken = (
        "MATCH (seed)-[:HAS_ATTRIBUTE]->(a)<-[:HAS_ATTRIBUTE]-(p) "
        "WHERE NOT a.attr_type IN $ignored_attr_types "
        "AND (seed)-[:HAS_ATTRIBUTE]->(a) "
        "WHERE a.attr_type IN ['domain_platform'] WITH p RETURN p"
    )

    cleaned = _sanitize_home_cypher(broken)

    assert "a.attr_type IN ['domain_platform']" not in cleaned
    assert "AND (seed)-[:HAS_ATTRIBUTE]->(a)" not in cleaned
    assert "WHERE NOT a.attr_type IN $ignored_attr_types" in cleaned
