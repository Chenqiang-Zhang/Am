from __future__ import annotations

import asyncio

import app.api.main as api_main
from app.api.models import ChatMessage, ChatRequest


class _ChatSpy:
    received_user_id: str | None = "not-called"

    def chat(
        self,
        messages: list[dict[str, str]],
        limit: int,
        lang: str,
        user_id: str | None,
    ) -> dict[str, object]:
        self.received_user_id = user_id
        return {
            "action": "ask",
            "question": "希望を教えてください",
            "options": [],
            "preference_summary": [],
        }


def test_chat_endpoint_never_forwards_user_id(monkeypatch) -> None:
    spy = _ChatSpy()
    monkeypatch.setattr(api_main, "_recommender", spy)
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="協力プレイできるゲーム")],
        limit=8,
        lang="ja",
        user_id="real-user-with-history",
    )

    response = asyncio.run(api_main.chat(request))

    assert response.action == "ask"
    assert spy.received_user_id is None
