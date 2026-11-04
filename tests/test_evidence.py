"""M4: evidence as paths. The reason text states nothing but evidence fields."""

import pytest
from fastapi.testclient import TestClient

from app.api import static_recommend
from app.api.main import app, get_graph_reader
from app.api.models import Evidence, Reason, Recommendation
from app.api.static_recommend import (
    EVIDENCE_PER_PATH,
    PATHS,
    SeedScheme,
    build_evidence,
    collect_path_scores,
    combine,
    describe_evidence,
    generic_tags,
    recommend_games,
    tag_anchor,
)
from app.api.static_recommend_v0 import recommend_games_v0
from tests.fake_graph import GAMES, FakeGraphReader
from utils.graph_reader import tag_candidate

# Every path on and ln(1 + hours) seed weights, as the fixture was written for (the tuned defaults switch
# studio and popular off).
ALL_PATHS = {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}
U1 = recommend_games(FakeGraphReader(), "u1", len(GAMES), "en", frozenset({"30"}), weights=ALL_PATHS, scheme=SeedScheme())
U1_PATH_SCORES = collect_path_scores(FakeGraphReader(), "u1", frozenset({"30"}), SeedScheme())
U1_RANKED = {item["appid"]: item for item in combine(U1_PATH_SCORES, len(GAMES), ALL_PATHS)}


def test_every_recommendation_carries_evidence_that_explains_its_score():
    assert len(U1) == len(GAMES) - 3  # every game u1 has not played
    generic = generic_tags(U1_PATH_SCORES)
    for item in U1:
        assert item.evidence, item.appid
        assert item.contributions is not None
        assert item.score == pytest.approx(sum(item.contributions.values()))
        assert sum(evidence.share for evidence in item.evidence) <= 1 + 1e-9
        anchor = tag_anchor(U1_PATH_SCORES, U1_RANKED[item.appid])
        assert describe_evidence(item.evidence, "en", anchor, generic) == (item.reasons, item.explanation)


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
    items = recommend_games(FakeGraphReader(), "u1", len(GAMES), "en", frozenset({"30"}), weights={"peer": 1.0})
    juliet = next(item for item in items if item.appid == "100")
    assert juliet.score == 0.0 and juliet.contributions == {}
    assert [(evidence.path, evidence.count) for evidence in juliet.evidence] == [("popular", 0)]
    assert juliet.explanation == "No other player has played this game yet"


def tag(seed, name):
    return Evidence(path="tag", seed_appid=seed.lower(), seed_name=seed, bridge_id=name.lower(), bridge_name=name, share=0.1)


def test_the_tag_sentence_states_the_tags_of_one_seed():
    evidence = [tag("Alpha", "Action"), tag("Bravo", "Indie"), tag("Alpha", "Shooter")]
    reasons, text = describe_evidence(evidence, "en", "bravo")
    assert text == "Shares Indie with Bravo"
    assert [(reason.kind, reason.label) for reason in reasons] == [("game", "Bravo"), ("tag", "Indie")]
    assert describe_evidence(evidence, "ja", "bravo")[1] == "『Bravo』と共通するタグ: Indie"
    # No instance names the anchor (or there is none): the seed of the first instance.
    for anchor in ("charlie", None):
        reasons, text = describe_evidence(evidence, "en", anchor)
        assert text == "Shares Action, Shooter with Alpha"
        assert [(reason.kind, reason.label) for reason in reasons] == [
            ("game", "Alpha"),
            ("tag", "Action"),
            ("tag", "Shooter"),
        ]
    assert describe_evidence(evidence, "ja")[1] == "『Alpha』と共通するタグ: Action、Shooter"


def test_generic_tags_are_stated_only_if_all_instances_of_the_seed_are_generic():
    generic = {"multiplayer", "singleplayer"}
    evidence = [tag("Alpha", "Multiplayer"), tag("Alpha", "Shooter"), tag("Alpha", "Singleplayer")]
    reasons, text = describe_evidence(evidence, "en", "alpha", generic)
    assert text == "Shares Shooter with Alpha"
    assert [(reason.kind, reason.label) for reason in reasons] == [("game", "Alpha"), ("tag", "Shooter")]
    # Only the instances count: Bravo's Shooter does not, nor would a tag of Alpha outside the 3.
    only_generic = [evidence[0], evidence[2], tag("Bravo", "Shooter")]
    assert describe_evidence(only_generic, "en", "alpha", generic)[1] == "Shares Multiplayer, Singleplayer with Alpha"


def test_seeds_with_the_same_name_are_told_apart_by_appid():
    # The real graph has two games named Dark Matter (251410 and 345130).
    first = Evidence(path="tag", seed_appid="251410", seed_name="Dark Matter", bridge_id="action", bridge_name="Action")
    other = first.model_copy(update={"seed_appid": "345130", "bridge_id": "puzzle", "bridge_name": "Puzzle"})
    evidence = [first, other, first.model_copy(update={"bridge_id": "indie", "bridge_name": "Indie"})]
    assert describe_evidence(evidence, "en", "345130") == (
        [Reason(kind="game", label="Dark Matter"), Reason(kind="tag", label="Puzzle")],
        "Shares Puzzle with Dark Matter",
    )
    assert describe_evidence(evidence, "en")[1] == "Shares Action, Indie with Dark Matter"


def concept(name, weight, *holders):
    """A row of the concept table; holders are (appid, w_s), heaviest first."""
    held = [{"appid": appid, "name": f"Seed {appid}", "weight": seed_weight} for appid, seed_weight in holders]
    return {
        "concept": name,
        "weight": weight,
        "seed_appid": held[0]["appid"],
        "seed_name": held[0]["name"],
        "name": name.upper(),
        "name_ja": None,
        "tag_ids": [f"usertag:{name}"],
        "holders": held,
        "document_frequency": 10,
    }


def tag_only(*concepts):
    """PathScores where game g matched all the concepts and only the tag path scored, and g as ranked."""
    row = tag_candidate("g", len(concepts), list(concepts), EVIDENCE_PER_PATH)
    path_scores = {
        "player_id": "x",
        "seeds": [],
        "paths": {"tag": {"g": row}},
        "tag_concepts": {item["concept"]: item for item in concepts},
    }
    [ranked] = combine(path_scores, 1, {"tag": 1.0})
    return path_scores, ranked


def test_the_anchor_holds_the_largest_part_of_the_tag_score():
    # Seed 1 has the strongest concept a and is the heaviest seed with b, but seed 2's b, c and d add up to
    # more. d is not among the 3 evidence instances and still counts: without it seed 1 is the anchor.
    concepts = [
        concept("a", 3.0, ("1", 3.0)),
        concept("b", 2.0, ("1", 3.0), ("2", 1.0)),
        concept("c", 2.0, ("2", 1.0)),
        concept("d", 2.0, ("2", 1.0)),
    ]
    path_scores, ranked = tag_only(*concepts)
    assert tag_anchor(path_scores, ranked) == "2"
    evidence = build_evidence(path_scores, ranked, "en", "2")
    assert [(item.bridge_id, item.seed_appid, item.seed_name) for item in evidence] == [
        ("a", "1", "Seed 1"),
        ("b", "2", "Seed 2"),
        ("c", "2", "Seed 2"),
    ]
    assert [item.share for item in evidence] == [item.share for item in build_evidence(path_scores, ranked, "en")]
    assert describe_evidence(evidence, "en", "2")[1] == "Shares B, C with Seed 2"
    assert tag_anchor(*tag_only(*concepts[:3])) == "1"


@pytest.mark.parametrize("weight_20,weight_30,anchor", [(2.0, 1.0, "20"), (1.0, 2.0, "30"), (1.0, 1.0, "20")])
def test_anchor_ties_go_to_the_larger_seed_weight_then_the_smaller_appid(weight_20, weight_30, anchor):
    # a and b weigh the same, so each seed holds half of the tag score; seed 30's concept comes first.
    path_scores, ranked = tag_only(concept("a", 2.0, ("30", weight_30)), concept("b", 2.0, ("20", weight_20)))
    assert tag_anchor(path_scores, ranked) == anchor


def test_without_an_instance_of_the_anchor_the_first_instance_names_the_seed():
    # Seed 2 holds five weaker concepts that add up to more than seed 1's three, which fill the evidence.
    strong = [concept(name, 3.0, ("1", 3.0)) for name in "abc"]
    weak = [concept(name, 2.0, ("2", 1.0)) for name in "vwxyz"]
    path_scores, ranked = tag_only(*strong, *weak)
    assert tag_anchor(path_scores, ranked) == "2"
    evidence = build_evidence(path_scores, ranked, "en", "2")
    assert {item.seed_appid for item in evidence} == {"1"}
    assert describe_evidence(evidence, "en", "2")[1] == "Shares A, B, C with Seed 1"


def test_a_tag_instance_names_the_anchor_if_the_anchor_has_the_tag():
    # India(90) for u1: Alpha (100 h) is the heaviest seed with Action and Shooter, but Bravo's Action,
    # Indie and Puzzle make up more of India's tag score. Puzzle is not in the evidence, but without it
    # Alpha would be the anchor. Shooter, which Bravo does not have, keeps Alpha.
    india = next(item for item in U1 if item.appid == "90")
    assert tag_anchor(U1_PATH_SCORES, U1_RANKED["90"]) == "20"
    assert [(item.bridge_id, item.seed_name) for item in india.evidence if item.path == "tag"] == [
        ("action", "Bravo"),
        ("shooter", "Alpha"),
        ("indie", "Bravo"),
    ]
    assert india.explanation == "Shares Action, Indie with Bravo"
    assert [(reason.kind, reason.label) for reason in india.reasons] == [
        ("game", "Bravo"),
        ("tag", "Action"),
        ("tag", "Indie"),
    ]


def test_generic_tags_stay_in_the_evidence(monkeypatch):
    monkeypatch.setattr(static_recommend, "GENERIC_TAG_GAMES", 6)  # only Action is on 6 games
    assert generic_tags(U1_PATH_SCORES) == {"action"}
    recommended = recommend_games(FakeGraphReader(), "u1", 10, "en", frozenset({"30"}), ALL_PATHS, SeedScheme())
    items = {item.appid: item for item in recommended}
    assert items["90"].explanation == "Shares Indie with Bravo"
    assert [item.bridge_id for item in items["90"].evidence if item.path == "tag"] == ["action", "shooter", "indie"]
    assert items["110"].explanation == "Shares Action with Alpha"  # Kilo(110) has no other tag


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


def test_counts_are_written_with_thousands_separators():
    peer = Evidence(path="peer", seed_appid="10", seed_name="Alpha", count=31390, share=0.5)
    assert describe_evidence([peer], "en")[1] == "31,390 other players who played Alpha also played this game"
    assert describe_evidence([peer], "ja")[1] == "『Alpha』を遊んだ他のプレイヤーのうち 31,390人が、このゲームも遊んでいます"
    popular = Evidence(path="popular", count=1234567, share=1.0)
    assert describe_evidence([popular], "en")[1] == "Played by 1,234,567 other players"
    assert describe_evidence([popular], "ja")[1] == "他のプレイヤー 1,234,567人が遊んでいるゲーム"


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
