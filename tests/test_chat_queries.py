import pytest

from app.api.chat_queries import (
    POPULARITY_WEIGHT,
    UNRANKED_TAG_STRENGTH,
    GraphRetriever,
    Vocabulary,
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
