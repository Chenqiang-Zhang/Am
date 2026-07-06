from __future__ import annotations

import unittest

from api.models import SearchIntent
from api.recommender import _rank_candidates, _title_text_similarity


def _candidate(product_id: str, title: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "product_id": product_id,
        "title": title,
        "price": 12.0,
        "average_rating": 4.4,
        "rating_number": 120,
        "image_url": None,
        "sellable_status": "available",
        "data_quality_score": 0.9,
        "attribute_score": 0.0,
        "field_score": 0.0,
        "behavior_score": 0.0,
        "item_cf_score": 0.0,
        "transition_score": 0.0,
        "text_similarity_score": 0.0,
        "seen_penalty": 0.0,
        "review_positive_score": 0.0,
        "review_negative_score": 0.0,
        "matched_attributes": [],
        "feature_terms": set(),
        "field_terms": set(),
        "feature_evidence": [],
        "feature_hit_count": 0,
    }
    base.update(overrides)
    return base


class RecommenderRankingTest(unittest.TestCase):
    def test_title_text_similarity_uses_query_terms(self) -> None:
        score = _title_text_similarity("Fragrance Free Dry Skin Moisturizer", ["dry skin", "moisturizer"])
        self.assertGreater(score, 0.5)

    def test_second_stage_ranking_exposes_behavior_scores(self) -> None:
        intent = SearchIntent(attribute_filters=[], keywords=["dry skin moisturizer"], price_max=None, min_rating=None)
        candidates = {
            "behavior": _candidate(
                "behavior",
                "Dry Skin Moisturizer",
                item_cf_score=0.8,
                transition_score=0.7,
                behavior_score=0.5,
            ),
            "baseline": _candidate("baseline", "Generic Beauty Product", data_quality_score=0.7),
        }

        ranked = _rank_candidates(candidates, intent, ["dry skin moisturizer"], 2, "en")

        self.assertEqual(ranked[0].product_id, "behavior")
        self.assertGreater(ranked[0].score_breakdown["item_cf"], 0.0)
        self.assertGreater(ranked[0].score_breakdown["transition"], 0.0)
        self.assertGreater(ranked[0].score_breakdown["text_similarity"], 0.0)
        self.assertGreater(ranked[0].score_breakdown["data_quality"], 0.0)


if __name__ == "__main__":
    unittest.main()
