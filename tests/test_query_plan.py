from __future__ import annotations

import unittest

from api.models import AttributeFilter, SearchIntent
from api.query_plan import build_controlled_query_plan, enabled_action_names


class QueryPlanTest(unittest.TestCase):
    def test_user_plan_includes_behavior_recall_and_second_stage_pool(self) -> None:
        intent = SearchIntent(
            attribute_filters=[AttributeFilter(attribute_type="skin_type", value="dry", weight=1.0)],
            keywords=["moisturizer"],
            price_max=None,
            min_rating=None,
        )

        plan = build_controlled_query_plan("dry moisturizer", intent, user_id="user-1")
        action_names = enabled_action_names(plan)

        self.assertIn("item_cf_recall", action_names)
        self.assertIn("transition_recall", action_names)
        self.assertIn("apply_user_history_boost", action_names)
        self.assertEqual(plan.constraints["second_stage_rerank_pool"], 50)
        self.assertTrue(any("raw Cypher" in note for note in plan.safety_notes))


if __name__ == "__main__":
    unittest.main()
