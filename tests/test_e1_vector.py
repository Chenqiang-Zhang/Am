import numpy as np
import pytest

from app.api.chat_state import ChatState
from eval.crs.vector import VectorCorpus, VectorRetriever, query_text, write_document

# three axes: horror, puzzle, co-op
AXES = {"Horror": 0, "Puzzle": 1, "Co-op": 2}


def fake_embed(texts):
    """A text's vector counts the axis names it mentions."""
    vectors = np.zeros((len(texts), len(AXES)), dtype=np.float32)
    for row, text in enumerate(texts):
        for name, axis in AXES.items():
            vectors[row, axis] = text.count(name)
    return vectors


NAMES = {"tag:horror": "Horror", "tag:puzzle": "Puzzle", "tag:co_op": "Co-op"}
CORPUS = VectorCorpus(
    appids=("1", "2", "3", "4", "5"),
    documents=("Tags: Horror.", "Tags: Horror, Puzzle.", "Tags: Puzzle.", "Tags: Puzzle.", "Tags: Co-op."),
    prices=(5.0, 15.0, None, 2.0, 1.0),
    names=NAMES,
)


def retriever(exclude_weight=0.5, anchor_weight=1.0):
    return VectorRetriever(CORPUS, fake_embed(list(CORPUS.documents)), fake_embed, exclude_weight, anchor_weight)


def test_document_lists_genres_then_user_tags_by_rank_and_drops_feature_only_groups():
    tags = [
        {"tag_id": "usertag:action", "name": "Action", "rank": 3},
        {"tag_id": "genre:action", "name": "Action", "rank": None},
        {"tag_id": "usertag:pixel_graphics", "name": "Pixel Graphics", "rank": 2},
        {"tag_id": "usertag:rogue_like", "name": "Rogue-like", "rank": 1},
        {"tag_id": "feature:co_op", "name": "Co-op", "rank": None},
        {"tag_id": "feature:steam_cloud", "name": "Steam Cloud", "rank": None},
    ]
    groups = {"tag:action", "tag:rogue_like", "tag:pixel_graphics", "tag:co_op"}
    document = write_document(tags, [{"studio_id": "s", "name": "Supergiant Games"}], 8.0, groups)
    assert document == ("Genres: Action. Tags: Rogue-like, Pixel Graphics, Co-op. "
                        "Developer: Supergiant Games. Price: $8.00.")
    assert write_document([], [], None, groups) == "Price: unknown."


def test_query_text_is_one_sentence_and_leaves_price_to_the_filter():
    names = {**NAMES, "studio:valve": "Valve"}
    assert query_text(["tag:horror", "tag:puzzle", "price:5"], names) == "A game tagged Horror, Puzzle."
    assert query_text(["tag:horror", "studio:valve"], names) == "A game tagged Horror. Developer: Valve."
    assert query_text([], names) == "A game."


def test_ranks_by_cosine_with_ties_by_appid():
    shown = [c.appid for c in retriever().retrieve(ChatState(include=["tag:puzzle"]), 5)]
    assert shown[:2] == ["3", "4"]  # equal scores, appid order
    assert shown[2] == "2"


def test_an_exclusion_pushes_games_down_but_does_not_remove_them():
    state = ChatState(include=["tag:puzzle"], exclude=["tag:horror"])
    ranked = retriever().retrieve(state, 5)
    assert [c.appid for c in ranked][:2] == ["3", "4"]
    scores = {c.appid: c.score for c in ranked}
    assert scores["2"] < retriever().retrieve(ChatState(include=["tag:puzzle"]), 5)[2].score
    assert "1" in scores  # still returned: V has no hard tag filter


def test_price_is_a_hard_filter_and_unknown_price_passes_no_bound():
    assert {c.appid for c in retriever().retrieve(ChatState(max_price=5), 5)} == {"1", "4", "5"}
    assert {c.appid for c in retriever().retrieve(ChatState(min_price=4), 5)} == {"1", "2"}
    assert "3" in {c.appid for c in retriever().retrieve(ChatState(), 5)}


def test_shown_games_do_not_come_back_and_scores_are_cosines():
    ranked = retriever().retrieve(ChatState(include=["tag:horror"], shown=["1"]), 5)
    assert "1" not in {c.appid for c in ranked}
    assert ranked[0].score == pytest.approx(1 / np.sqrt(2))  # "Tags: Horror, Puzzle." against horror


def test_an_anchor_pulls_the_query_towards_its_document_and_is_not_returned():
    plain = [c.appid for c in retriever().retrieve(ChatState(include=["tag:puzzle"]), 5)]
    anchored = retriever().retrieve(ChatState(include=["tag:puzzle"], anchors=["1"]), 5)
    assert plain[0] in ("3", "4")
    assert anchored[0].appid == "2"  # horror and puzzle: closest to the horror anchor plus the puzzle tag
    assert "1" not in [c.appid for c in anchored]
