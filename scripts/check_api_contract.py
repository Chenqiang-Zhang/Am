from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


RECOMMENDATION_FIELDS = {
    "product_id",
    "title",
    "display_title",
    "display_language",
    "image_url",
    "price",
    "price_display",
    "availability_status",
    "data_quality_score",
    "average_rating",
    "rating_number",
    "score",
    "matched_attributes",
    "matched_terms",
    "matched_feature_evidence",
    "score_breakdown",
    "reason_quantification",
    "explanation",
    "display_explanation",
}


def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def assert_fields(name: str, data: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(data))
    if missing:
        raise AssertionError(f"{name} missing fields: {missing}")


def check_recommend_response(name: str, data: dict[str, Any]) -> None:
    assert_fields(name, data, {"query", "intent", "query_plan", "recommendations"})
    assert_fields(f"{name}.intent", data["intent"], {"attribute_filters", "keywords", "price_max", "min_rating"})
    assert_fields(f"{name}.query_plan", data["query_plan"], {"source", "actions", "constraints", "safety_notes"})
    if data["recommendations"]:
        assert_fields(f"{name}.recommendations[0]", data["recommendations"][0], RECOMMENDATION_FIELDS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check frontend/backend API contract compatibility.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        recommend = post_json(
            args.base_url,
            "/recommend",
            {
                "query": "dry sensitive skin fragrance free moisturizer",
                "limit": 2,
                "lang": "en",
                "user_id": "contract_check_user",
            },
        )
        check_recommend_response("recommend", recommend)

        home = post_json(
            args.base_url,
            "/recommend/home",
            {"user_id": "contract_check_user", "limit": 2, "lang": "en"},
        )
        check_recommend_response("recommend_home", home)

        chat = post_json(
            args.base_url,
            "/chat",
            {
                "messages": [{"role": "user", "content": "乾燥肌向けの無香料クリームが欲しい"}],
                "limit": 2,
                "lang": "ja",
                "user_id": "contract_check_user",
            },
        )
        assert_fields("chat", chat, {"action", "question", "options", "preference_summary", "intent", "query_plan", "recommendations"})
        if chat["action"] == "search":
            if chat["query_plan"] is None:
                raise AssertionError("chat search response must include query_plan")
            if chat["recommendations"]:
                assert_fields("chat.recommendations[0]", chat["recommendations"][0], RECOMMENDATION_FIELDS)
    except (AssertionError, urllib.error.URLError, TimeoutError) as exc:
        print(f"API contract check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("API contract check passed")


if __name__ == "__main__":
    main()
