"""M3: the three paths, the popularity prior and combine()."""

import itertools
import json
import math

import pytest

from app.api.static_recommend import STUDIO_NORMALIZATIONS, SeedScheme, collect_path_scores, combine, recommend_games
from eval.rankers import build_item_knn_ranker
from tests.fake_graph import GAMES, PLAYS, FakeGraphReader

W = math.log1p  # seed weight ln(1 + hours) under the default scheme
TOTAL_GAMES = len(GAMES)


def path_scores_of(reader, player_id, hidden=(), scheme=SeedScheme()):
    return collect_path_scores(reader, player_id, frozenset(hidden), scheme)


# u1 hides Charlie(30) and keeps Alpha(10, 100h), Bravo(20, 20h) and Golf(70, 0.5h).
U1 = path_scores_of(FakeGraphReader(), "u1", {"30"})["paths"]


def describe_links(links):
    return [(link["seed_appid"], link["studio_id"], link["seed_role"], link["game_role"]) for link in links]


# ---- co-play -------------------------------------------------------------------------------------


def test_co_play_is_normalised_by_both_degrees_without_the_player():
    # Echo(50) is played by p1, p2, solo. Alpha's other players: p1, p2, p5; Bravo's: p1, p3.
    echo = U1["peer"]["50"]
    assert echo["score"] == pytest.approx(W(100) * 2 / math.sqrt(3 * 3) + W(20) * 1 / math.sqrt(2 * 3))
    assert [(seed["appid"], seed["co_players"], seed["seed_players"]) for seed in echo["seeds"]] == [
        ("10", 2, 3),
        ("20", 1, 2),
    ]
    assert echo["game_players"] == 3


def test_the_hidden_game_does_not_count_the_player():
    # u1 still has the PLAYED edge to Charlie(30) in the graph; p2, p4, p5 are the other players.
    assert U1["peer"]["30"]["game_players"] == 3
    assert U1["popular"]["30"]["players"] == 3


def test_hiding_a_game_equals_deleting_the_edge():
    with_edge = FakeGraphReader()
    without_edge = FakeGraphReader(plays=[play for play in PLAYS if play[:2] != ("u1", "30")])
    hidden = frozenset({"30"})
    assert collect_path_scores(with_edge, "u1", hidden) == collect_path_scores(without_edge, "u1", hidden)
    assert [item.model_dump() for item in recommend_games(with_edge, "u1", 20, "en", hidden)] == [
        item.model_dump() for item in recommend_games(without_edge, "u1", 20, "en", hidden)
    ]


def test_hiding_all_of_a_players_games_leaks_nothing():
    # p7 only played Delta(40). With it hidden only popularity is left, and it must not count p7.
    hidden = frozenset({"40"})
    with_edge = FakeGraphReader()
    without_edge = FakeGraphReader(plays=[play for play in PLAYS if play[:2] != ("p7", "40")])
    items = recommend_games(with_edge, "p7", len(GAMES), "en", hidden)
    assert [item.model_dump() for item in items] == [
        item.model_dump() for item in recommend_games(without_edge, "p7", len(GAMES), "en", hidden)
    ]
    assert {evidence.path for item in items for evidence in item.evidence} == {"popular"}
    delta = next(item for item in items if item.appid == "40")
    assert delta.evidence[0].count == 1  # p3; p7's own hidden edge does not count


def test_one_shared_game_is_enough_for_co_play():
    # solo only played Echo(50), so v0's "peer shares at least 2 games" rule left solo with nothing.
    reader = FakeGraphReader()
    solo_seeds = reader.played_games("solo", [])
    assert reader.games_played_by_peers("solo", solo_seeds, ["50"], 100) == []
    assert set(path_scores_of(reader, "solo")["paths"]["peer"]) == {"10", "20", "30", "60"}


@pytest.mark.parametrize("player_id,hidden", [("u1", {"30"}), ("u1", set()), ("p2", {"60"}), ("p6", {"90"})])
def test_uniform_weights_and_all_seeds_reproduce_item_knn(player_id, hidden):
    """E2's premise: with w_s = 1 and every visible game as a seed, co-play is the leak-free item-KNN."""
    reader = FakeGraphReader()
    path_scores = path_scores_of(reader, player_id, hidden, SeedScheme(weighting="uniform", peer_limit=None))
    ranked = [item["appid"] for item in combine(path_scores, 50, {"peer": 1.0}) if not item["backfill"]]
    item_knn = build_item_knn_ranker(reader.all_play_records())(player_id, len(ranked), set(hidden))
    # Same order up to ties: item-KNN orders tied games its own way.
    position = 0
    for _, group in itertools.groupby(ranked, key=lambda appid: round(path_scores["paths"]["peer"][appid]["score"], 6)):
        members = set(group)
        assert set(item_knn[position : position + len(members)]) == members
        position += len(members)
    assert position == len(ranked) > 0


def test_peer_limit_keeps_the_most_played_seeds():
    reader = FakeGraphReader()
    one_seed = path_scores_of(reader, "u1", {"30"}, SeedScheme(peer_limit=1))["paths"]["peer"]
    assert {seed["appid"] for row in one_seed.values() for seed in row["seeds"]} == {"10"}
    assert SeedScheme(weighting="uniform").weight(250.0) == 1.0


# ---- tags ----------------------------------------------------------------------------------------


def tag_weight(preference, document_frequency):
    return preference * math.log(TOTAL_GAMES / document_frequency)


def test_tag_idf_and_matching_use_the_same_edges():
    # Action: genre or rank <= 10 on Alpha, Bravo, Echo, Foxtrot, India, Kilo. Charlie's rank-11 edge
    # is used neither for matching nor for the IDF.
    action = next(match for match in U1["tag"]["50"]["matches"] if match["concept"] == "action")
    assert action["weight"] == pytest.approx(tag_weight(W(100) + W(20), 6))
    assert (action["seed_appid"], action["seed_name"], action["name"]) == ("10", "Alpha", "Action")


def test_tag_score_is_divided_by_the_square_root_of_the_tag_count():
    # India has 10 tags after merging genres with user tags (Casual is rank 11): Action, Indie, RPG,
    # Strategy, Puzzle, Shooter, Open World, Relaxing, Simulation, Multiplayer.
    expected = (
        tag_weight(W(100) + W(20), 6)  # action
        + tag_weight(W(100), 3)  # shooter
        + tag_weight(W(20), 4)  # indie
        + tag_weight(W(20), 4)  # puzzle
        + tag_weight(W(0.5), 2)  # simulation
        + tag_weight(W(0.5), 2)  # relaxing
    ) / math.sqrt(10)
    india = U1["tag"]["90"]
    assert (india["concept_count"], india["match_count"]) == (10, 6)
    assert india["score"] == pytest.approx(expected)


def test_genre_and_user_tag_with_one_name_count_once():
    # Alpha has both genre:action and usertag:action; Kilo(110) too. Kilo has one tag: action.
    kilo = U1["tag"]["110"]
    assert kilo["concept_count"] == 1
    assert kilo["score"] == pytest.approx(tag_weight(W(100) + W(20), 6))


def test_feature_tags_are_not_used():
    assert "120" not in U1["tag"]  # Lima only has a Feature tag
    concepts = {match["concept"] for row in U1["tag"].values() for match in row["matches"]}
    assert not concepts & {"single_player", "co_op", "online_pvp"}


# ---- studios -------------------------------------------------------------------------------------


def test_self_published_seed_counts_once():
    # Valve developed and published both Alpha and Echo; Valve has 2 titles.
    echo = U1["studio"]["50"]
    assert echo["score"] == pytest.approx(W(100) * 1.0 / math.log(2 + 2))
    assert describe_links(echo["links"]) == [("10", "valve", "developer", "developer")]


def test_seed_sharing_two_studios_counts_once_through_the_stronger_one():
    # Mike(130) and Bravo(20): Indie Co developed both (3 titles), Big Pub published both (6 titles).
    # Golf(70) is only linked through Big Pub, as publisher of both.
    mike = U1["studio"]["130"]
    assert mike["score"] == pytest.approx(W(20) * 1.0 / math.log(2 + 3) + W(0.5) * 0.5 / math.log(2 + 6))
    assert describe_links(mike["links"]) == [
        ("20", "indie_co", "developer", "developer"),
        ("70", "big_pub", "publisher", "publisher"),
    ]


def test_mixed_roles_get_the_publisher_weight():
    # Big Pub developed Kilo(110) but only published Bravo(20).
    link = next(link for link in U1["studio"]["110"]["links"] if link["seed_appid"] == "20")
    assert (link["seed_role"], link["game_role"]) == ("publisher", "developer")
    assert link["term"] == pytest.approx(W(20) * 0.5 / math.log(2 + 6))


# ---- popularity and combine() --------------------------------------------------------------------


def test_popularity_prior_is_log_degree_without_the_player():
    assert U1["popular"]["30"]["score"] == pytest.approx(math.log(1 + 3))
    assert set(U1["popular"]) == {game["appid"] for game in GAMES} - {"10", "20", "70"}


def synthetic(seed_weights=(), **rows_by_path):
    seeds = [
        {"appid": f"s{index}", "name": f"Seed {index}", "hours": math.expm1(weight), "weight": weight}
        for index, weight in enumerate(seed_weights)
    ]
    paths = {}
    for path, rows in rows_by_path.items():
        if path == "popular":
            paths[path] = {appid: {"appid": appid, "score": math.log1p(n), "players": n} for appid, n in rows.items()}
        else:
            paths[path] = {appid: {"appid": appid, "score": score} for appid, score in rows.items()}
    return {"player_id": "x", "seeds": seeds, "paths": paths}


EXAMPLE = synthetic(peer={"a": 4.0, "b": 2.0}, tag={"b": 10.0, "c": 5.0}, studio={}, popular={"a": 9, "b": 1, "c": 4, "d": 0})


def test_combine_normalises_peer_tag_and_popular_by_their_maximum():
    ranked = combine(EXAMPLE, 4, {"peer": 1.0, "tag": 0.5, "popular": 0.1})
    assert [(item["appid"], item["backfill"]) for item in ranked] == [("a", False), ("b", False), ("c", False), ("d", True)]
    by_appid = {item["appid"]: item for item in ranked}
    assert by_appid["a"]["contributions"] == pytest.approx({"peer": 1.0, "popular": 0.1})
    assert by_appid["b"]["contributions"] == pytest.approx({"peer": 0.5, "tag": 0.5, "popular": 0.1 * math.log(2) / math.log(10)})
    for item in ranked:
        assert item["score"] == pytest.approx(sum(item["contributions"].values()))


def test_zero_weight_switches_a_path_off_and_popularity_fills_up():
    ranked = combine(EXAMPLE, 4, {"tag": 1.0})
    assert [(item["appid"], item["backfill"]) for item in ranked] == [("b", False), ("c", False), ("a", True), ("d", True)]
    assert all("peer" not in item["contributions"] for item in ranked)


def test_ties_are_broken_by_appid_even_after_rounding_noise():
    tied = synthetic(peer={"b": 1.0, "c": 1.0, "a": 1.0 - 1e-15}, popular={})
    assert [item["appid"] for item in combine(tied, 3, {"peer": 1.0})] == ["a", "b", "c"]


def test_combine_rejects_unknown_paths_and_negative_weights():
    with pytest.raises(ValueError):
        combine(EXAMPLE, 3, {"peers": 1.0})
    with pytest.raises(ValueError):
        combine(EXAMPLE, 3, {"peer": -1.0})


def test_combine_rejects_an_unknown_studio_normalization():
    with pytest.raises(ValueError):
        combine(EXAMPLE, 3, studio_normalization="sum")


def test_totals_and_the_studio_divisor_are_added_from_left_to_right():
    # 1 + 1e-16 + 1e-16 is 1 when added in order, as sum() does on Python 3.11 (the API image), but not with
    # the compensated sum() of Python 3.12+, so this fails if combine() uses sum() on a newer Python.
    tiny = 1e-16
    assert math.fsum([1.0, tiny, tiny]) != 1.0
    totals = synthetic(seed_weights=(1.0,), peer={"a": 1.0}, tag={"a": 1.0}, studio={"a": 1.0}, popular={"a": 1})
    [ranked] = combine(totals, 1, {"peer": 1.0, "tag": tiny, "studio": tiny})
    assert ranked["contributions"] == {"peer": 1.0, "tag": tiny, "studio": tiny}
    assert ranked["score"] == 1.0
    divisor = synthetic(seed_weights=(1.0, tiny, tiny), studio={"a": 1.0}, popular={"a": 1})
    [ranked] = combine(divisor, 1, {"studio": 1.0})
    assert ranked["contributions"] == {"studio": 1.0}


def test_combine_never_returns_played_games_and_fills_the_whole_catalogue():
    path_scores = path_scores_of(FakeGraphReader(), "u1", {"30"})
    ranked = combine(path_scores, 100)
    assert [item["appid"] for item in ranked].count("30") == 1
    assert {item["appid"] for item in ranked} == {game["appid"] for game in GAMES} - {"10", "20", "70"}


def test_path_scores_survive_a_json_round_trip_for_offline_tuning():
    path_scores = path_scores_of(FakeGraphReader(), "u1", {"30"})
    cached = json.loads(json.dumps(path_scores))
    assert cached == path_scores
    for weights in ({"peer": 1.0}, {"peer": 1.0, "tag": 0.25, "studio": 0.5, "popular": 0.1}, None):
        assert combine(cached, 10, weights) == combine(path_scores, 10, weights)


# ---- the studio divisor ---------------------------------------------------------------------------


# Every path on, as these tests were written for (the tuned defaults switch studio and popular off).
ALL_PATHS = {"peer": 1.0, "tag": 1.0, "studio": 0.5, "popular": 0.1}


def studio_contributions(path_scores, weights=ALL_PATHS, studio_normalization="seed_weight"):
    ranked = combine(path_scores, 10, weights, studio_normalization)
    return {item["appid"]: item["contributions"].get("studio") for item in ranked}


def test_studio_contribution_doubles_with_the_studio_linked_share_of_seed_weight():
    # Both players' seeds weigh 4 in total. A developer with 2 titles links g to seeds of weight 1 or 2.
    quarter = synthetic(seed_weights=(1.0, 3.0), studio={"g": 1.0 / math.log(4)})
    half = synthetic(seed_weights=(2.0, 2.0), studio={"g": 2.0 / math.log(4)})
    assert studio_contributions(quarter)["g"] == pytest.approx(0.5 * 0.25 / math.log(4))
    assert studio_contributions(half)["g"] == pytest.approx(2 * studio_contributions(quarter)["g"])
    # Dividing by the highest studio score gave both the full λ.
    for path_scores in (quarter, half):
        assert studio_contributions(path_scores, studio_normalization="max") == {"g": 0.5}


def test_studio_divides_by_the_weight_of_every_visible_seed():
    # u1 keeps Alpha(100h), Bravo(20h) and Golf(0.5h). Co-play only uses Alpha here; the studio divisor uses all three.
    path_scores = path_scores_of(FakeGraphReader(), "u1", {"30"}, SeedScheme(peer_limit=1))
    expected = 0.5 * (W(100) / math.log(2 + 2)) / (W(100) + W(20) + W(0.5))
    assert studio_contributions(path_scores)["50"] == pytest.approx(expected)


@pytest.mark.parametrize("studio_normalization", STUDIO_NORMALIZATIONS)
def test_zero_weight_switches_the_studio_path_off(studio_normalization):
    path_scores = synthetic(seed_weights=(2.0,), peer={"a": 1.0}, studio={"a": 1.0, "b": 1.0}, popular={"a": 1, "b": 1})
    ranked = combine(path_scores, 2, {"peer": 1.0, "studio": 0.0}, studio_normalization)
    assert [(item["appid"], item["contributions"], item["backfill"]) for item in ranked] == [
        ("a", {"peer": 1.0}, False),
        ("b", {}, True),
    ]


@pytest.mark.parametrize("seed_weights", [(), (0.0, 0.0)])
def test_studio_path_is_skipped_when_the_seeds_weigh_nothing(seed_weights):
    # No visible seeds, or only games played for 0 hours under ln(1 + hours).
    path_scores = synthetic(seed_weights=seed_weights, peer={"a": 2.0}, studio={"a": 1.0})
    assert [item["contributions"] for item in combine(path_scores, 1, ALL_PATHS)] == [{"peer": 1.0}]


def test_max_normalization_keeps_the_previous_rule():
    # Titles of one studio share one score, so the highest score gives each of them the full λ.
    path_scores = synthetic(seed_weights=(3.0, 1.0), studio={"a": 0.8, "b": 0.8, "c": 0.2})
    assert studio_contributions(path_scores, {"studio": 0.5}, "max") == {"a": 0.5, "b": 0.5, "c": 0.125}
    assert studio_contributions(path_scores, {"studio": 0.5}) == pytest.approx({"a": 0.1, "b": 0.1, "c": 0.025})


def test_max_normalization_differs_only_in_the_studio_path():
    path_scores = path_scores_of(FakeGraphReader(), "u1", {"30"})
    studio = path_scores["paths"]["studio"]
    highest = max(row["score"] for row in studio.values())
    current = {item["appid"]: item["contributions"] for item in combine(path_scores, 20, ALL_PATHS)}
    for item in combine(path_scores, 20, ALL_PATHS, studio_normalization="max"):
        previous = dict(item["contributions"])
        if "studio" in previous:
            assert previous.pop("studio") == 0.5 * studio[item["appid"]]["score"] / highest
        assert previous == {path: value for path, value in current[item["appid"]].items() if path != "studio"}
