from __future__ import annotations

import re
import unittest
from pathlib import Path

from api.models import Recommendation
from scripts.check_api_contract import RECOMMENDATION_FIELDS


ROOT = Path(__file__).resolve().parents[1]


class StaticApiContractTest(unittest.TestCase):
    def test_contract_script_matches_backend_recommendation_model(self) -> None:
        backend_fields = set(Recommendation.model_fields)
        self.assertEqual(RECOMMENDATION_FIELDS, backend_fields)

    def test_frontend_recommendation_type_contains_backend_fields(self) -> None:
        source = (ROOT / "web/src/types/recommend.ts").read_text(encoding="utf-8")
        match = re.search(r"export interface Recommendation \{(?P<body>.*?)\n\}", source, flags=re.S)
        self.assertIsNotNone(match)
        body = match.group("body") if match else ""
        frontend_fields = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??:", body, flags=re.M))

        self.assertTrue(RECOMMENDATION_FIELDS <= frontend_fields)


if __name__ == "__main__":
    unittest.main()
