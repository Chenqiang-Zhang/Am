"""The chat shares popular_games / played_games / tag_preferences and the API models with the static
recommender. These tests make sure the chat side still works with them."""

from fastapi.testclient import TestClient

from app.api.chat import chat
from app.api.main import app, get_graph_reader, get_llm_client
from app.api.models import ChatMessage, ChatResponse
from app.api.player_profile import build_player_profile
from tests.fake_graph import FakeGraphReader


class FakeLlmClient:
    def __init__(self, analysis, answer):
        self.replies = [analysis, answer]
        self.prompts = []

    def complete_json(self, messages, temperature=0.0):
        self.prompts.append(messages)
        return self.replies[len(self.prompts) - 1]


def test_player_profile_lists_played_games_and_tags_without_hidden_games():
    reader = FakeGraphReader()
    profile = build_player_profile(reader, "u1", {"30"})
    assert "Alpha（100.0 時間）" in profile
    assert "Charlie" not in profile.split("## 書いたレビュー")[0]
    assert "## 好むタグ" in profile
    assert "Loved the characters" not in profile


def test_chat_returns_picked_candidates_with_tag_reasons():
    reader = FakeGraphReader()
    llm = FakeLlmClient(
        {"tags": ["Puzzle", "NotATag"], "aspects": [], "keywords": [], "max_price": None},
        {"reply": "How about these?", "options": ["Cheaper"], "picks": [{"appid": "40", "reason": "Puzzle game"}]},
    )
    response = chat(reader, llm, [ChatMessage(role="user", content="puzzle games")], "u1", 5, "en")
    assert isinstance(response, ChatResponse)
    assert [item.appid for item in response.recommendations] == ["40"]
    assert response.recommendations[0].reasons[0].label == "Puzzle"
    assert response.options == ["Cheaper"]


def test_chat_falls_back_to_popular_games_when_nothing_matches():
    reader = FakeGraphReader()
    llm = FakeLlmClient({"tags": [], "aspects": [], "keywords": [], "max_price": None}, {})
    response = chat(reader, llm, [ChatMessage(role="user", content="anything")], None, 3, "ja")
    assert [item.appid for item in response.recommendations] == ["10", "30", "20"]
    assert response.recommendations[0].explanation == "遊んだプレイヤーが多いゲーム"


def make_client(reader, llm=None):
    app.dependency_overrides[get_graph_reader] = lambda: reader
    app.dependency_overrides[get_llm_client] = lambda: llm
    return TestClient(app)


def test_api_endpoints_respond_with_the_fake_graph():
    reader = FakeGraphReader()
    llm = FakeLlmClient(
        {"tags": ["Action"], "aspects": ["gameplay"], "keywords": ["fun"], "max_price": 50},
        {"reply": "ok", "options": [], "picks": [{"appid": "110", "reason": "Action"}]},
    )
    client = make_client(reader, llm)
    try:
        recommend = client.post("/recommend", json={"player_id": "u1", "limit": 5, "lang": "en"})
        assert recommend.status_code == 200
        assert len(recommend.json()["recommendations"]) == 5

        popular = client.post("/recommend", json={"player_id": None, "limit": 3, "lang": "en"})
        assert [item["appid"] for item in popular.json()["recommendations"]] == ["10", "30", "20"]

        chat_response = client.post(
            "/chat", json={"messages": [{"role": "user", "content": "action"}], "player_id": "u1", "limit": 3}
        )
        assert chat_response.status_code == 200
        assert [item["appid"] for item in chat_response.json()["recommendations"]] == ["110"]

        players = client.get("/players/sample", params={"limit": 3})
        assert [player["player_id"] for player in players.json()["players"]] == ["p1", "p2", "p3"]

        reviews = client.get("/games/10/reviews", params={"limit": 1})
        assert reviews.json()["reviews"][0]["votes_up"] == 10
    finally:
        app.dependency_overrides.clear()
