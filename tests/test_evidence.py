"""M4: evidence as paths. The reason text states nothing but evidence fields."""

import math

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
    ordered_sum,
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
UCSD_GAMES = 9192


def test_the_fixtures_share_one_configuration():
    # The anchors of U1's items are taken from U1_PATH_SCORES and U1_RANKED.
    assert [(item.appid, item.score, item.contributions) for item in U1] == [
        (appid, ranked["score"], ranked["contributions"]) for appid, ranked in U1_RANKED.items()
    ]


def test_every_recommendation_carries_evidence_that_explains_its_score():
    assert len(U1) == len(GAMES) - 3  # every game u1 has not played
    generic = generic_tags(U1_PATH_SCORES)
    for item in U1:
        assert item.evidence, item.appid
        assert item.contributions is not None
        assert item.score == pytest.approx(sum(item.contributions.values()))
        assert sum(evidence.share for evidence in item.evidence) <= 1 + 1e-9
        anchor = tag_anchor(U1_PATH_SCORES, U1_RANKED[item.appid], generic)
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
        "games": UCSD_GAMES,
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
    # A seed is credited with its own part of a matched concept's term, term · w_s / Σ w of the seeds with the
    # concept. Seed 1 has the strongest concept a and 3/4 of b, but seed 2's 1/4 of b, c and d add up to more
    # (2.25 against 2.0). d is not among the 3 evidence instances and still counts: without it seed 1 is the
    # anchor.
    concepts = [
        concept("a", 2.5, ("1", 3.0)),
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


def test_anchor_parts_tie_to_score_decimals_and_appids_compare_as_strings():
    # Seed 20's part is larger only in the 15th decimal, so the parts tie; "102700" comes before "20".
    path_scores, ranked = tag_only(concept("a", 2.0 + 1e-14, ("20", 1.0)), concept("b", 2.0, ("102700", 1.0)))
    assert path_scores["paths"]["tag"]["g"]["matched"] == ["a", "b"]
    assert tag_anchor(path_scores, ranked) == "102700"


def test_anchor_parts_that_differ_before_the_12th_decimal_do_not_tie():
    # Seed 20's part is larger by 1e-9, so it is the anchor although "102700" comes first as a string.
    path_scores, ranked = tag_only(concept("a", 2.0 + 1e-9, ("20", 1.0)), concept("b", 2.0, ("102700", 1.0)))
    assert tag_anchor(path_scores, ranked) == "20"


def test_without_an_instance_of_the_anchor_the_first_instance_names_the_seed():
    # Seed 2 holds five weaker concepts that add up to more than seed 1's three, which fill the evidence. Seed 1
    # is the only seed with a listed tag, so it is the anchor, unless all of its tags are generic: then every
    # seed is a candidate, seed 2 owns the most, and the reason names the first instance's seed instead.
    strong = [concept(name, 3.0, ("1", 3.0)) for name in "abc"]
    weak = [concept(name, 2.0, ("2", 1.0)) for name in "vwxyz"]
    path_scores, ranked = tag_only(*strong, *weak)
    assert tag_anchor(path_scores, ranked) == "1"
    generic = {"a", "b", "c"}
    assert tag_anchor(path_scores, ranked, generic) == "2"
    evidence = build_evidence(path_scores, ranked, "en", "2")
    assert {item.seed_appid for item in evidence} == {"1"}
    assert describe_evidence(evidence, "en", "2", generic)[1] == "Shares A, B, C with Seed 1"


def test_a_tag_instance_names_the_anchor_if_the_anchor_has_the_tag():
    # p8 with the default seeds (w_s = 1), tag path only. Mike(130) and India(90) both have Indie and Puzzle,
    # and the heaviest seed with them is Mike: equal w_s, and "130" comes before "90" as a string.
    items = {item.appid: item for item in recommend_games(FakeGraphReader(), "p8", 10, "en", weights={"tag": 1.0})}
    # Delta(40) has only Indie and Puzzle: Mike and India own equal parts, and the tie goes to "130".
    assert items["40"].explanation == "Shares Indie, Puzzle with Mike"
    # Bravo(20) also has Action, which only India has, so India is the anchor and Indie and Puzzle name it.
    bravo = items["20"]
    assert [(item.bridge_id, item.seed_name) for item in bravo.evidence if item.path == "tag"] == [
        ("indie", "India"),
        ("puzzle", "India"),
        ("action", "India"),
    ]
    assert bravo.explanation == "Shares Indie, Puzzle, Action with India"
    assert [(reason.kind, reason.label) for reason in bravo.reasons] == [
        ("game", "India"),
        ("tag", "Indie"),
        ("tag", "Puzzle"),
        ("tag", "Action"),
    ]


def idf_concept(name, document_frequency, *holders):
    """A row of the concept table as TAG_CONCEPTS makes it on a graph of 9,192 games: weight(c) = Σ w_s ·
    ln(N / df(c)). holders are (appid, w_s)."""
    held = sorted(holders, key=lambda holder: (-holder[1], holder[0]))
    weight = ordered_sum(seed_weight for _, seed_weight in held) * math.log(UCSD_GAMES / document_frequency)
    row = concept(name, weight, *held)
    return dict(row, name=name.replace("_", " ").title(), document_frequency=document_frequency)


def test_a_seed_with_only_a_widely_held_tag_is_not_the_anchor():
    # w_s = 1. Multiplayer (on 1,328 games) is on seeds 10-50, Roguelike (150) and Pixel Graphics (300) only
    # on seed 70. Multiplayer has the largest term, but each of its five seeds owns a fifth of it.
    concepts = [
        idf_concept("multiplayer", 1328, *[(appid, 1.0) for appid in ("10", "20", "30", "40", "50")]),
        idf_concept("roguelike", 150, ("70", 1.0)),
        idf_concept("pixel_graphics", 300, ("70", 1.0)),
    ]
    path_scores, ranked = tag_only(*concepts)
    assert tag_anchor(path_scores, ranked) == "70"
    evidence = build_evidence(path_scores, ranked, "en", "70")
    assert [(item.bridge_id, item.seed_appid) for item in evidence] == [
        ("multiplayer", "10"),
        ("roguelike", "70"),
        ("pixel_graphics", "70"),
    ]
    generic = generic_tags(path_scores)
    assert generic == {"multiplayer"}
    assert describe_evidence(evidence, "en", "70", generic)[1] == "Shares Roguelike, Pixel Graphics with Seed 70"


def test_a_seed_played_for_a_minute_is_not_the_anchor_over_one_played_for_500_hours():
    # w_s = ln(1 + hours): seed 100 was played for 500 hours, seed 200 for a minute. Both have Roguelike and
    # Pixel Graphics, and only 200 has Cute; 200's own part of the tag score is under 1%.
    scheme = SeedScheme()
    heavy, light = ("100", scheme.weight(500.0)), ("200", scheme.weight(1 / 60))
    concepts = [
        idf_concept("roguelike", 150, heavy, light),
        idf_concept("pixel_graphics", 300, heavy, light),
        idf_concept("cute", 500, light),
    ]
    path_scores, ranked = tag_only(*concepts)
    assert tag_anchor(path_scores, ranked) == "100"
    evidence = build_evidence(path_scores, ranked, "en", "100")
    assert [(item.bridge_id, item.seed_appid) for item in evidence] == [
        ("roguelike", "100"),
        ("pixel_graphics", "100"),
        ("cute", "200"),
    ]
    assert describe_evidence(evidence, "en", "100")[1] == "Shares Roguelike, Pixel Graphics with Seed 100"


def test_the_anchor_is_a_seed_with_a_listed_tag_that_is_not_generic():
    # w_s = 1. Seed 10 owns the most of the tag score through four tags on 900 games each, which are not
    # among the 3 listed instances; its only listed one is Multiplayer (generic, shared by five seeds). Seed 70
    # has Roguelike and Pixel Graphics, both listed. Among the seeds with a listed tag that is not generic,
    # seed 70 owns the most, so the reason names it and states its tags instead of only Multiplayer.
    concepts = [
        idf_concept("multiplayer", 1328, *[(appid, 1.0) for appid in ("10", "20", "30", "40", "50")]),
        idf_concept("roguelike", 150, ("70", 1.0)),
        idf_concept("pixel_graphics", 300, ("70", 1.0)),
        *[idf_concept(f"niche_{index}", 900, ("10", 1.0)) for index in range(4)],
    ]
    path_scores, ranked = tag_only(*concepts)
    row = path_scores["paths"]["tag"]["g"]
    assert [match["concept"] for match in row["matches"]] == ["multiplayer", "roguelike", "pixel_graphics"]
    generic = generic_tags(path_scores)
    assert generic == {"multiplayer"}
    assert tag_anchor(path_scores, ranked) == "10"  # by its own part alone
    assert describe_evidence(build_evidence(path_scores, ranked, "en", "10"), "en", "10", generic)[1] == (
        "Shares Multiplayer with Seed 10"
    )
    assert tag_anchor(path_scores, ranked, generic) == "70"
    evidence = build_evidence(path_scores, ranked, "en", "70")
    assert describe_evidence(evidence, "en", "70", generic)[1] == "Shares Roguelike, Pixel Graphics with Seed 70"
    # When every listed instance is generic, the seed with the largest part is the anchor again.
    path_scores, ranked = tag_only(concepts[0], *concepts[3:])
    assert tag_anchor(path_scores, ranked, {"multiplayer", "niche_0", "niche_1"}) == "10"


@pytest.mark.parametrize("games,fewest", [(UCSD_GAMES, 1000), (100, 11), (len(GAMES), 3)])
def test_generic_tags_are_on_the_same_share_of_the_games_of_any_graph(games, fewest):
    # 1,000 of UCSD's 9,192 games, 11 of 100 (10.9), 3 of the fixture's 22 (2.4).
    rows = [
        dict(concept(f"on_{df}", 1.0, ("1", 1.0)), document_frequency=df, games=games) for df in (fewest - 1, fewest)
    ]
    assert generic_tags({"tag_concepts": {row["concept"]: row for row in rows}}) == {f"on_{fewest}"}


def test_the_fixture_has_generic_tags_too():
    # u1's tags on 3 or more of the 22 games; Simulation and Relaxing are on 2.
    assert {entry["concept"]: entry["document_frequency"] for entry in U1_PATH_SCORES["tag_concepts"].values()} == {
        "action": 6,
        "shooter": 3,
        "indie": 4,
        "puzzle": 4,
        "simulation": 2,
        "relaxing": 2,
    }
    assert generic_tags(U1_PATH_SCORES) == {"action", "shooter", "indie", "puzzle"}


def test_generic_tags_stay_in_the_evidence(monkeypatch):
    # Generic means on 6 of the 22 games: only Action (the next tags are on 4).
    monkeypatch.setattr(static_recommend, "GENERIC_TAG_GAMES", 6)
    monkeypatch.setattr(static_recommend, "GENERIC_TAG_GRAPH_GAMES", len(GAMES))
    assert generic_tags(U1_PATH_SCORES) == {"action"}
    # India(90) for u1: Alpha (100 h) owns 60% of Action and all of Shooter, more than Bravo's 40% of Action,
    # Indie and Puzzle, so Alpha is the anchor; its instances are Action and Shooter.
    assert tag_anchor(U1_PATH_SCORES, U1_RANKED["90"]) == "10"
    recommended = recommend_games(FakeGraphReader(), "u1", 10, "en", frozenset({"30"}), ALL_PATHS, SeedScheme())
    items = {item.appid: item for item in recommended}
    assert items["90"].explanation == "Shares Shooter with Alpha"
    assert [(item.bridge_id, item.seed_name) for item in items["90"].evidence if item.path == "tag"] == [
        ("action", "Alpha"),
        ("shooter", "Alpha"),
        ("indie", "Bravo"),
    ]
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
