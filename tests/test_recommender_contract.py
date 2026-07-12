from app.api.models import ReviewItem, SearchIntent
from app.api.recommender import _domain_constraints_from_terms, _record_to_recommendation


def test_recommendation_uses_requested_japanese_fields_and_reason_metrics() -> None:
    recommendation = _record_to_recommendation(
        {
            "product_id": "B000TEST",
            "title": "English title",
            "title_ja": "日本語タイトル",
            "description": "English description",
            "description_ja": "日本語の説明",
            "score": 12.5,
            "matched_attrs": [
                {"attr_type": "domain_franchise", "value": "mario", "value_ja": "マリオ"}
            ],
            "reason_metrics": {
                "condition_matches": 2,
                "transition_peers": 3,
                "review_confirmations": 1,
            },
            "explanation": "test",
            "recommendation_source": "dialogue_personalized",
        },
        lang="ja",
    )

    assert recommendation.display_title == "日本語タイトル"
    assert recommendation.description == "日本語の説明"
    assert recommendation.matched_attrs[0].value == "マリオ"
    assert recommendation.reason_metrics.condition_matches == 2
    assert recommendation.reason_metrics.transition_peers == 3
    assert recommendation.reason_metrics.review_confirmations == 1


def test_domain_aliases_emit_normalized_v3_constraints() -> None:
    constraints = _domain_constraints_from_terms(["Nintendo Switch Mario game"])

    assert "nintendo switch" in constraints["platform_keywords"]
    assert "mario" in constraints["franchise_keywords"]
    assert "video game" in constraints["product_type_keywords"]


def test_api_models_preserve_diagnostics_and_review_language_state() -> None:
    intent = SearchIntent(
        cypher="MATCH (p:Product) RETURN p",
        cypher_explanation="test",
        condition_source="heuristic_fallback",
        retrieval_status="no_match",
        no_result_reason="No catalog item satisfied all dialogue constraints",
    )
    review = ReviewItem(text="English original", translated=False, display_language="en")

    assert intent.retrieval_status == "no_match"
    assert intent.condition_source == "heuristic_fallback"
    assert review.translated is False
    assert review.display_language == "en"
