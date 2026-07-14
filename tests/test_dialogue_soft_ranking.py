from __future__ import annotations

import json
from types import SimpleNamespace

from app.api.models import SearchIntent
from app.api.recommender import (
    MIN_CONFIRMED_PREFERENCES,
    Recommender,
    _dialogue_condition_groups,
)


def _conditions() -> dict[str, object]:
    return {
        "product_keywords": ["mario"],
        "category_keywords": ["games"],
        "attribute_keywords": ["action"],
        "platform_keywords": ["nintendo 3ds"],
        "franchise_keywords": ["mario"],
        "product_type_keywords": ["video game"],
        "min_rating": 4.0,
        "condition_source": "llm",
    }


def test_dialogue_condition_groups_keep_only_domain_fields_hard() -> None:
    hard, soft = _dialogue_condition_groups(_conditions())

    assert any(item.startswith("product_type:") for item in hard)
    assert any(item.startswith("platform:") for item in hard)
    assert any(item.startswith("franchise:") for item in hard)
    assert any("action" in item for item in soft)
    assert any(item.startswith("rating_preference:") for item in soft)


def test_llm_soft_attributes_are_not_polluted_with_raw_domain_tokens() -> None:
    recommender = object.__new__(Recommender)
    recommender._genre = "Video_Games"
    recommender._get_attr_vocab_text = lambda: "genre: action"  # type: ignore[method-assign]
    recommender._call_llm = lambda system, user: {  # type: ignore[method-assign]
        "product_keywords": ["mario"],
        "category_keywords": ["video game"],
        "attribute_keywords": ["action"],
        "platform_keywords": ["nintendo_ds"],
        "franchise_keywords": ["mario"],
        "product_type_keywords": ["video_game"],
        "min_rating": None,
    }

    conditions = recommender._extract_conditions(
        "マリオのゲームが欲しい Nintendo 3DS アクション", "ja"
    )

    assert conditions["attribute_keywords"] == ["action"]
    assert "nintendo 3ds" in conditions["platform_keywords"]
    assert "nintendo ds" not in conditions["platform_keywords"]


def test_dialogue_search_makes_open_preferences_ranking_only() -> None:
    recommender = object.__new__(Recommender)
    recommender._extract_conditions = lambda query, lang: _conditions()  # type: ignore[method-assign]
    calls: list[dict[str, object]] = []

    def execute(cypher: str, params: dict[str, object], lang: str) -> list[object]:
        calls.append(params)
        return [object()]

    recommender._execute_and_map = execute  # type: ignore[method-assign]

    _, _, results, diagnostics = recommender._run_metapath_recommendation(
        "3DSのアクション系マリオゲーム",
        None,
        8,
        "ja",
        dialogue_soft_preferences=True,
    )

    assert results
    assert len(calls) == 1
    assert calls[0]["required_condition_keywords"] == []
    assert calls[0]["dialogue_soft_preferences"] is True
    assert calls[0]["min_rating"] == 0.0
    assert calls[0]["platform_required"] is True
    assert calls[0]["franchise_required"] is True
    assert calls[0]["product_type_required"] is True
    assert diagnostics["hard_conditions"]
    assert diagnostics["soft_conditions"]


def test_dialogue_search_does_not_relax_franchise_when_no_result() -> None:
    recommender = object.__new__(Recommender)
    recommender._extract_conditions = lambda query, lang: _conditions()  # type: ignore[method-assign]
    calls: list[dict[str, object]] = []

    def execute(cypher: str, params: dict[str, object], lang: str) -> list[object]:
        calls.append(params)
        return []

    recommender._execute_and_map = execute  # type: ignore[method-assign]

    _, _, results, diagnostics = recommender._run_metapath_recommendation(
        "3DSのマリオゲーム",
        None,
        8,
        "ja",
        dialogue_soft_preferences=True,
    )

    assert results == []
    assert len(calls) == 1
    assert calls[0]["franchise_required"] is True
    assert diagnostics["retrieval_status"] == "no_match"


class _ChatCompletion:
    def __init__(self, payload: dict[str, object]) -> None:
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        self.choices = [SimpleNamespace(message=message)]


class _Completions:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def create(self, **_: object) -> _ChatCompletion:
        return _ChatCompletion(self.payload)


def _chat_recommender(payload: dict[str, object]) -> Recommender:
    recommender = object.__new__(Recommender)
    recommender._genre = "Video_Games"
    recommender._model = "test-model"
    recommender._chat_temperature = 0.0
    recommender._llm = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(payload)))
    recommender._get_attr_vocab_text = lambda: "genre: action"  # type: ignore[method-assign]
    intent = SearchIntent(
        cypher="MATCH (p:Product) RETURN p",
        cypher_explanation="dialogue test",
        hard_conditions=["franchise: mario"],
        soft_conditions=["attribute: action"],
    )
    recommender.recommend_dialogue = (  # type: ignore[method-assign]
        lambda query, limit, lang: ("search-id", intent, [], False)
    )
    return recommender


def test_ask_response_contains_provisional_recommendations_contract() -> None:
    recommender = _chat_recommender(
        {
            "action": "ask",
            "question": "どの機種で遊びますか？",
            "options": ["Nintendo Switch", "Nintendo 3DS", "こだわらない"],
            "slot": "platform",
            "filled_slots": 1,
            "preference_summary": ["マリオ"],
        }
    )

    result = recommender.chat(
        [{"role": "user", "content": "マリオのゲームが欲しい"}],
        limit=8,
        lang="ja",
        user_id=None,
    )

    assert result["action"] == "ask"
    assert result["provisional"] is True
    assert result["intent"].hard_conditions == ["franchise: mario"]
    assert result["search_id"] is None


def test_python_finalizes_after_minimum_confirmed_preferences() -> None:
    recommender = _chat_recommender(
        {
            "action": "ask",
            "question": "さらに希望はありますか？",
            "options": ["こだわらない"],
            "slot": "mood",
            "filled_slots": MIN_CONFIRMED_PREFERENCES,
            "preference_summary": ["マリオ", "Nintendo 3DS", "アクション"],
        }
    )

    result = recommender.chat(
        [{"role": "user", "content": "3DSで遊べるアクション系のマリオ"}],
        limit=8,
        lang="ja",
        user_id=None,
    )

    assert result["action"] == "search"
    assert result["provisional"] is False
    assert result["search_id"] == "search-id"
