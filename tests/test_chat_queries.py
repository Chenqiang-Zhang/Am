import pytest

import math

from app.api.chat_queries import (
    CO_PLAY_MIN_HOURS,
    CO_PLAY_QUERY,
    PLAYERS_QUERY,
    POPULARITY_WEIGHT,
    STUDIO_STRENGTH,
    T1_QUERY,
    T3_STUDIO_QUERY,
    UNRANKED_TAG_STRENGTH,
    AnchorProfile,
    GameAttributes,
    GraphRetriever,
    Vocabulary,
    anchor_profile,
    co_play_lift,
    score_co_play_rows,
    score_similar_rows,
    score_studio_rows,
    score_t1_rows,
    t1_parameters,
    tag_strength,
)
from app.api.chat_state import ChatState

VOCABULARY = Vocabulary(
    tag_ids_by_key={
        "tag:action": ["genre:action", "usertag:action"],
        "tag:rogue_like": ["usertag:rogue_like"],
        "tag:horror": ["usertag:horror"],
    },
    studio_keys={"studio:valve"},
    appids={"10", "20", "30", "105600"},
)


class RecordingReader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "max(COUNT" in cypher:
            return [{"n": 1000}]
        return self.rows


def test_tag_strength_runs_from_rank_one_to_the_cut():
    assert tag_strength(1) == pytest.approx(1.0)
    assert tag_strength(10) == pytest.approx(0.1)
    assert tag_strength(None) == UNRANKED_TAG_STRENGTH
    assert tag_strength(1) > tag_strength(2) > tag_strength(10)


def test_parameters_expand_tag_groups_and_split_studios():
    state = ChatState(
        include=["tag:action", "studio:valve"],
        exclude=["tag:horror"],
        max_price=10,
        anchors=["105600"],
        shown=["10"],
    )
    params = t1_parameters(state, VOCABULARY)
    assert params["include_tags"] == [["genre:action", "usertag:action"]]
    assert params["exclude_tags"] == [["usertag:horror"]]
    assert params["include_studios"] == ["valve"]
    assert params["exclude_studios"] == []
    assert params["wanted_tag_ids"] == ["genre:action", "usertag:action"]
    assert params["max_price"] == 10
    assert params["unavailable"] == ["105600", "10"]


def test_scores_add_the_strongest_edge_of_each_included_tag_group():
    rows = [
        {"appid": "10", "players": 0, "matched": [{"tag_id": "genre:action", "rank": None},
                                                  {"tag_id": "usertag:action", "rank": 1}]},
        {"appid": "20", "players": 0, "matched": [{"tag_id": "usertag:action", "rank": 5}]},
    ]
    ranked = score_t1_rows(rows, max_players=100)
    assert [candidate.appid for candidate in ranked] == ["10", "20"]
    assert ranked[0].score == pytest.approx(1.0)
    assert [(item.bridge_id, item.share) for item in ranked[0].evidence] == [("usertag:action", pytest.approx(1.0))]


def test_popularity_only_breaks_near_ties():
    rows = [
        {"appid": "10", "players": 1000, "matched": [{"tag_id": "usertag:action", "rank": 2}]},
        {"appid": "20", "players": 0, "matched": [{"tag_id": "usertag:action", "rank": 1}]},
        {"appid": "30", "players": 1000, "matched": [{"tag_id": "usertag:action", "rank": 1}]},
    ]
    ranked = score_t1_rows(rows, max_players=1000)
    assert [candidate.appid for candidate in ranked] == ["30", "20", "10"]
    assert ranked[0].score == pytest.approx(1.0 + POPULARITY_WEIGHT)


def test_equal_scores_come_out_by_appid():
    rows = [{"appid": appid, "players": 0, "matched": []} for appid in ("30", "100", "20")]
    assert [candidate.appid for candidate in score_t1_rows(rows, max_players=0)] == ["100", "20", "30"]


def test_retriever_drops_unknown_ids_before_querying_and_caps_k():
    reader = RecordingReader([{"appid": str(i), "players": i, "matched": []} for i in range(5)])
    retriever = GraphRetriever(reader, VOCABULARY)
    state = ChatState(include=["tag:action", "tag:made_up"], anchors=["999"])
    result = retriever.retrieve(state, k=3)
    cypher, params = reader.calls[-1]
    assert params["include_tags"] == [["genre:action", "usertag:action"]]
    assert params["unavailable"] == []
    assert len(result) == 3
    assert [candidate.appid for candidate in result] == ["4", "3", "2"]


def test_parameters_carry_the_price_floor_and_the_excluded_player():
    params = t1_parameters(ChatState(min_price=5), VOCABULARY, exclude_player="76561")
    assert params["min_price"] == 5
    assert params["exclude_player"] == "76561"
    assert t1_parameters(ChatState(), VOCABULARY)["exclude_player"] is None


def test_excluding_a_player_is_a_copy_that_passes_the_player_to_t1():
    reader = RecordingReader(rows=[])
    retriever = GraphRetriever(reader, vocabulary=VOCABULARY)
    mine = retriever.excluding_player("76561")
    mine.retrieve(ChatState(), k=10)
    retriever.retrieve(ChatState(), k=10)
    assert [params["exclude_player"] for _, params in reader.calls[-2:]] == ["76561", None]
    assert "p.player_id <> $exclude_player" in reader.calls[-1][0]


def test_studio_rows_add_one_strength_per_shared_developer_and_cite_the_anchor():
    rows = [
        {"appid": "10", "players": 0, "matched": [], "studios": [{"studio_id": "valve", "anchor": "620"}]},
        {"appid": "20", "players": 0, "matched": [],
         "studios": [{"studio_id": "valve", "anchor": "620"}, {"studio_id": "hidden_path", "anchor": "620"}]},
    ]
    ranked = score_studio_rows(rows, max_players=0)
    assert [candidate.appid for candidate in ranked] == ["20", "10"]
    assert ranked[1].score == pytest.approx(STUDIO_STRENGTH)
    assert [(item.path, item.bridge_id, item.anchor_appid) for item in ranked[1].evidence] == [
        ("studio", "valve", "620")
    ]


def test_co_play_lift_favours_games_the_fans_play_more_than_everyone_does():
    # Half of the fans play both games, but the second one is played by everyone.
    assert co_play_lift(co=50, fans=100, players=100, all_players=1000) > co_play_lift(
        co=50, fans=100, players=1000, all_players=1000)
    # With few players the pseudo-count holds the lift down.
    assert co_play_lift(co=5, fans=100, players=5, all_players=1000) < co_play_lift(
        co=50, fans=100, players=100, all_players=1000)


def test_co_play_rows_keep_only_games_that_pass_t1_and_that_a_fan_played():
    rows = [{"appid": appid, "players": 100, "matched": []} for appid in ("10", "20", "30")]
    co_rows = [
        {"anchor": "620", "fans": 100, "appid": "10", "co": 20},
        {"anchor": "620", "fans": 100, "appid": "20", "co": 60},
        {"anchor": "620", "fans": 100, "appid": "40", "co": 90},  # fails T1
    ]
    ranked = score_co_play_rows(rows, co_rows, max_players=100, all_players=1000)
    assert [candidate.appid for candidate in ranked] == ["20", "10"]
    lift = co_play_lift(60, 100, 100, 1000)
    assert ranked[0].score == pytest.approx(math.log(lift) + POPULARITY_WEIGHT)
    evidence = ranked[0].evidence[0]
    assert (evidence.path, evidence.anchor_appid, evidence.count, evidence.share) == ("co_play", "620", 60, 0.6)


class RoutingReader:
    """Answers each template with its own rows and records the calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "max(COUNT" in cypher:
            return [{"n": 1000}]
        return self.routes.get(cypher, [])


def test_relation_picks_the_template():
    reader = RoutingReader({
        T3_STUDIO_QUERY: [{"appid": "10", "players": 0, "matched": [],
                           "studios": [{"studio_id": "valve", "anchor": "105600"}]}],
        T1_QUERY: [{"appid": "20", "players": 0, "matched": []}],
        CO_PLAY_QUERY: [{"anchor": "105600", "fans": 10, "appid": "20", "co": 5}],
        PLAYERS_QUERY: [{"n": 1000}],
    })
    retriever = GraphRetriever(reader, VOCABULARY).excluding_player("76561")

    same = retriever.retrieve(ChatState(anchors=["105600"], relation="same_developer"), k=10)
    assert [candidate.appid for candidate in same] == ["10"]
    assert reader.calls[-1][0] == T3_STUDIO_QUERY and reader.calls[-1][1]["anchors"] == ["105600"]

    co = retriever.retrieve(ChatState(anchors=["105600"], relation="co_played"), k=10)
    assert [candidate.appid for candidate in co] == ["20"]
    co_call = next(params for cypher, params in reader.calls if cypher == CO_PLAY_QUERY)
    assert co_call == {"anchors": ["105600"], "min_hours": CO_PLAY_MIN_HOURS, "exclude_player": "76561"}
    assert reader.calls[-1] == (PLAYERS_QUERY, {"exclude_player": "76561"})

    retriever._profile_box["profile"] = AnchorProfile({"20": frozenset({"tag:action"})}, {"tag:action": 1.0})
    similar = retriever.retrieve(ChatState(anchors=["105600"]), k=10)
    assert [candidate.appid for candidate in similar] == ["20"]
    assert reader.calls[-1][0] == T1_QUERY


def test_co_play_and_studio_queries_leave_the_anchor_and_the_evaluated_player_out():
    assert "NOT c.appid IN $anchors" in CO_PLAY_QUERY
    assert "p.player_id <> $exclude_player" in CO_PLAY_QUERY
    assert "p.player_id <> $exclude_player" in PLAYERS_QUERY
    assert "NOT g.appid IN $unavailable" in T3_STUDIO_QUERY
    assert "s.studio_id <> ''" in T3_STUDIO_QUERY


def test_co_play_counts_are_cached_per_anchor_set_and_not_shared_across_players():
    reader = RoutingReader({
        T1_QUERY: [{"appid": "20", "players": 0, "matched": []}],
        CO_PLAY_QUERY: [{"anchor": "105600", "fans": 10, "appid": "20", "co": 5}],
        PLAYERS_QUERY: [{"n": 1000}],
    })
    base = GraphRetriever(reader, VOCABULARY)
    mine = base.excluding_player("76561")
    state = ChatState(anchors=["105600"], relation="co_played")
    mine.retrieve(state, k=10)
    mine.retrieve(state.model_copy(update={"include": ["tag:action"]}), k=10)
    base.excluding_player("99999").retrieve(state, k=10)
    co_calls = [params["exclude_player"] for cypher, params in reader.calls if cypher == CO_PLAY_QUERY]
    assert co_calls == ["76561", "99999"]


def attributes(tags, studios=()):
    return GameAttributes(tag_keys=frozenset(tags), studio_keys=frozenset(studios), price=None, players=0)


def test_anchor_profile_weights_rare_keys_more():
    profile = anchor_profile({
        "1": attributes({"tag:action", "tag:rogue_like"}, {"studio:valve"}),
        "2": attributes({"tag:action"}),
        "3": attributes({"tag:action"}),
        "4": attributes({"tag:horror"}),
    })
    assert profile.idf["tag:rogue_like"] == pytest.approx(math.log(4))
    assert profile.idf["tag:action"] == pytest.approx(math.log(4 / 3))
    assert profile.keys_by_appid["1"] == {"tag:action", "tag:rogue_like", "studio:valve"}


def test_similar_rows_add_the_idf_of_what_each_game_shares_with_the_anchor():
    profile = anchor_profile({
        "620": attributes({"tag:action", "tag:rogue_like"}, {"studio:valve"}),
        "10": attributes({"tag:action", "tag:rogue_like"}),
        "20": attributes({"tag:action"}, {"studio:valve"}),
        "30": attributes({"tag:horror"}),
        "40": attributes({"tag:horror"}),
    })
    rows = [{"appid": appid, "players": 0, "matched": []} for appid in ("10", "20", "30")]
    ranked = score_similar_rows(rows, profile, ["620"], max_players=0)
    # 10 shares action and rogue_like, 20 shares action and Valve: both rare keys sit in 2 of 5 games
    assert [candidate.appid for candidate in ranked] == ["10", "20", "30"]
    assert ranked[0].score == pytest.approx(math.log(5 / 3) + math.log(5 / 2))
    assert ranked[1].score == pytest.approx(ranked[0].score)
    assert ranked[2].score == 0
    studio = [item for item in ranked[1].evidence if item.path == "studio"]
    assert [(item.bridge_id, item.anchor_appid) for item in studio] == [("valve", "620")]
    assert {item.bridge_id for item in ranked[0].evidence if item.path == "anchor_tag"} == {
        "tag:action", "tag:rogue_like"}


def test_the_profile_is_loaded_once_for_all_player_copies():
    reader = RoutingReader({})
    base = GraphRetriever(reader, VOCABULARY)
    first = base.excluding_player("1").profile()
    second = base.excluding_player("2").profile()
    assert first is second
