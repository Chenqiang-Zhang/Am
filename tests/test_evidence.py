"""M4: evidence as paths. The reason text states nothing but evidence fields."""

import pytest
from fastapi.testclient import TestClient

from app.api.main import app, get_graph_reader
from app.api.models import Evidence, Recommendation
from app.api.static_recommend import PATHS, describe_evidence, recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
from tests.fake_graph import FakeGraphReader

U1 = recommend_games(FakeGraphReader(), "u1", 13, "en", frozenset({"30"}))


def test_every_recommendation_carries_evidence_that_explains_its_score():
    assert len(U1) == 10  # every game u1 has not played
    for item in U1:
        assert item.evidence, item.appid
        assert item.contributions is not None
        assert item.score == pytest.approx(sum(item.contributions.values()))
        assert sum(evidence.share for evidence in item.evidence) <= 1 + 1e-9
        assert describe_evidence(item.evidence, "en") == (item.reasons, item.explanation)


def test_shares_add_up_when_every_path_instance_is_listed():
    # Echo(50): 2 co-play seeds, 2 tags, 1 studio link and popularity, all within the listing limit.
    echo = next(item for item in U1 if item.appid == "50")
    assert [evidence.path for evidence in echo.evidence].count("peer") == 2
    assert sum(evidence.share for evidence in echo.evidence) == pytest.approx(1.0)
    for path in ("peer", "tag", "studio", "popular"):
        listed = sum(evidence.share for evidence in echo.evidence if evidence.path == path)
        assert listed == pytest.approx(echo.contributions[path] / echo.score)


def test_headline_is_the_strongest_path_other_than_popularity():
    for item in U1:
        others = {path: value for path, value in item.contributions.items() if path != "popular"}
        if others:
            strongest = max(others, key=lambda path: (others[path], -PATHS.index(path)))
            assert item.evidence[0].path == strongest
        else:
            assert item.evidence[0].path == "popular"


def test_backfilled_games_say_how_many_others_played_them():
    # Juliet(100) and Lima(120) have no players and no path reaches them.
    items = recommend_games(FakeGraphReader(), "u1", 13, "en", frozenset({"30"}), weights={"peer": 1.0})
    juliet = next(item for item in items if item.appid == "100")
    assert juliet.score == 0.0 and juliet.contributions == {}
    assert [(evidence.path, evidence.count) for evidence in juliet.evidence] == [("popular", 0)]
    assert juliet.explanation == "No other player has played this game yet"


def tag(seed, name):
    return Evidence(path="tag", seed_appid=seed.lower(), seed_name=seed, bridge_id=name.lower(), bridge_name=name, share=0.1)


def test_each_tag_is_stated_with_its_own_seed():
    evidence = [tag("Alpha", "Action"), tag("Bravo", "Indie"), tag("Alpha", "Shooter")]
    reasons, text = describe_evidence(evidence, "en")
    assert text == "Shares Action, Shooter with Alpha; Indie with Bravo"
    assert [(reason.kind, reason.label) for reason in reasons] == [
        ("game", "Alpha"),
        ("game", "Bravo"),
        ("tag", "Action"),
        ("tag", "Indie"),
        ("tag", "Shooter"),
    ]
    assert describe_evidence(evidence, "ja")[1] == "『Alpha』と共通するタグ: Action、Shooter／『Bravo』と共通するタグ: Indie"


@pytest.mark.parametrize(
    "seed_role,candidate_role,english,japanese",
    [
        ("developer", "developer", "Developed by Valve, the developer of Alpha", "『Alpha』の開発元 Valve が開発したゲーム"),
        ("publisher", "publisher", "Published by Valve, the publisher of Alpha", "『Alpha』の販売元 Valve が販売したゲーム"),
        ("developer", "publisher", "Published by Valve, the developer of Alpha", "『Alpha』の開発元 Valve が販売したゲーム"),
        ("publisher", "developer", "Developed by Valve, the publisher of Alpha", "『Alpha』の販売元 Valve が開発したゲーム"),
    ],
)
def test_studios_are_stated_with_their_roles(seed_role, candidate_role, english, japanese):
    evidence = [
        Evidence(
            path="studio",
            seed_appid="10",
            seed_name="Alpha",
            bridge_id="valve",
            bridge_name="Valve",
            count=2,
            seed_role=seed_role,
            candidate_role=candidate_role,
        )
    ]
    reasons, text = describe_evidence(evidence, "en")
    assert text == english
    assert [(reason.kind, reason.label) for reason in reasons] == [("game", "Alpha"), ("studio", "Valve")]
    assert describe_evidence(evidence, "ja")[1] == japanese


def test_co_play_states_the_exact_count_of_its_seed():
    peer = Evidence(path="peer", seed_appid="10", seed_name="Alpha", count=2, share=0.5)
    assert describe_evidence([peer], "en")[1] == "2 other players who played Alpha also played this game"
    assert describe_evidence([peer.model_copy(update={"count": 1})], "en")[1] == (
        "1 other player who played Alpha also played this game"
    )
    assert describe_evidence([peer], "ja")[1] == "『Alpha』を遊んだ他のプレイヤーのうち 2人が、このゲームも遊んでいます"


def test_only_the_headline_path_is_stated():
    evidence = [
        Evidence(path="peer", seed_appid="10", seed_name="Alpha", count=2, share=0.5),
        tag("Bravo", "Indie"),
        Evidence(path="popular", count=7, share=0.1),
    ]
    reasons, text = describe_evidence(evidence, "en")
    assert "Bravo" not in text and "Indie" not in text and "7" not in text
    assert [(reason.kind, reason.label) for reason in reasons] == [("game", "Alpha")]


def test_popularity_states_the_count():
    popular = Evidence(path="popular", count=3, share=1.0)
    assert describe_evidence([popular], "en") == ([], "Played by 3 other players")
    assert describe_evidence([popular], "ja") == ([], "他のプレイヤー 3人が遊んでいるゲーム")


def test_v0_lists_what_its_text_claims():
    for item in recommend_games_v0(FakeGraphReader(), "u1", 10, "en", frozenset({"30"})):
        assert item.evidence and item.contributions is None
        for claim in item.evidence:
            assert claim.seed_name in item.explanation
            assert claim.seed_appid in {"10", "20", "70"}


def test_api_returns_evidence_and_contributions():
    app.dependency_overrides[get_graph_reader] = FakeGraphReader
    try:
        body = TestClient(app).post("/recommend", json={"player_id": "u1", "limit": 3, "lang": "ja"}).json()
    finally:
        app.dependency_overrides.clear()
    first = Recommendation(**body["recommendations"][0])
    assert first.evidence and set(first.contributions) <= set(PATHS)
    assert body["recommendations"][0]["evidence"][0].keys() == Evidence.model_fields.keys()
