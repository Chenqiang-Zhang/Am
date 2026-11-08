import pytest

from app.api.chat_queries import GameAttributes
from app.api.chat_questioner import (
    MAX_TURNS,
    STOP_POOL_SIZE,
    EntropyQuestioner,
    FixedOrderQuestioner,
    binary_entropy,
    build_catalog,
    entropy_table,
    price_key,
)
from app.api.chat_state import Candidate, ChatState, TurnUpdate, apply_update


def game(tags=(), studios=(), price=None, players=0):
    return GameAttributes(
        tag_keys=frozenset(tags), studio_keys=frozenset(studios), price=price, players=players
    )


# 20 games: half Action, a quarter Puzzle, every game made by one of two studios, prices 0..19.
GAMES = {
    str(i): game(
        tags={"tag:indie"} | ({"tag:action"} if i % 2 == 0 else set()) | ({"tag:puzzle"} if i % 4 == 0 else set()),
        studios={"studio:big" if i < 15 else "studio:small"},
        price=float(i),
    )
    for i in range(20)
}
TAG_KINDS = {
    "tag:indie": {"genre", "usertag"},
    "tag:action": {"genre", "usertag"},
    "tag:puzzle": {"usertag"},
    "tag:co_op": {"feature", "usertag"},
    "tag:steam_achievements": {"feature"},
}
CATALOG = build_catalog(GAMES, TAG_KINDS)
POOL = [Candidate(appid=appid, score=1.0) for appid in GAMES]


def test_binary_entropy():
    assert binary_entropy(0.5) == pytest.approx(1.0)
    assert binary_entropy(0.0) == binary_entropy(1.0) == 0.0
    assert binary_entropy(0.25) == pytest.approx(binary_entropy(0.75))


def test_catalog_order_is_genres_features_prices_tags_studios():
    # studio:small has 5 games and studio:big 15, both reach MIN_STUDIO_GAMES
    assert CATALOG.keys == (
        "tag:indie", "tag:action",
        price_key(20), price_key(10), price_key(5), price_key(0),
        "tag:puzzle",
        "studio:big", "studio:small",
    )


def test_feature_only_tags_are_not_asked():
    assert "tag:steam_achievements" not in CATALOG.keys


def test_price_band_holds_for_known_prices_within_it():
    assert CATALOG.has("5", price_key(5))
    assert not CATALOG.has("6", price_key(5))
    no_price = build_catalog({"1": game(price=None)}, {})
    assert not no_price.has("1", price_key(20))


def test_entropy_picks_the_most_even_split():
    # action splits 10/20, price<=10 splits 11/20, studio:big 15/20, puzzle 5/20, indie 20/20
    table = entropy_table(ChatState(), POOL, CATALOG)
    assert next(iter(table)) == "tag:action"
    assert table["tag:indie"] == 0.0
    assert EntropyQuestioner(CATALOG).choose(ChatState(), POOL) == "tag:action"


def test_entropy_is_weighted_by_retriever_scores():
    # put almost all weight on the Action games: asking about Action no longer splits the pool
    pool = [Candidate(appid=appid, score=10.0 if int(appid) % 2 == 0 else 0.01) for appid in GAMES]
    table = entropy_table(ChatState(), pool, CATALOG)
    assert table["tag:action"] < 0.1
    assert next(iter(table)) != "tag:action"


def test_never_asks_what_the_state_already_has():
    state = ChatState(include=["tag:action"], exclude=["tag:puzzle"], asked=["studio:big"])
    table = entropy_table(state, POOL, CATALOG)
    assert not {"tag:action", "tag:puzzle", "studio:big"} & set(table)


def test_price_bands_at_or_above_the_budget_are_not_asked():
    table = entropy_table(ChatState(max_price=10), POOL, CATALOG)
    assert price_key(20) not in table and price_key(10) not in table
    assert price_key(5) in table


def test_stops_on_a_small_pool_the_turn_cap_or_low_entropy():
    questioner = EntropyQuestioner(CATALOG)
    assert questioner.choose(ChatState(), POOL[:STOP_POOL_SIZE]) is None
    assert questioner.choose(ChatState(turn=MAX_TURNS), POOL) is None
    every_attribute_known = ChatState(
        include=["tag:action"], exclude=["tag:puzzle"], asked=list(CATALOG.keys)
    )
    assert questioner.choose(every_attribute_known, POOL) is None


def test_fixed_order_skips_attributes_that_do_not_split_the_pool():
    questioner = FixedOrderQuestioner(CATALOG)
    # indie is on every game, so the first useful question is action
    assert questioner.choose(ChatState(), POOL) == "tag:action"
    # price:20 holds for every game (prices 0..19), so it is skipped as well
    assert questioner.choose(ChatState(asked=["tag:action"]), POOL) == price_key(10)


def test_a_yes_to_a_price_band_becomes_the_budget():
    state = apply_update(ChatState(), TurnUpdate(include=[price_key(10)]))
    assert state.max_price == 10
    assert state.include == []


def test_price_bands_at_or_below_the_floor_are_not_asked():
    table = entropy_table(ChatState(min_price=5), POOL, CATALOG)
    assert price_key(5) not in table and price_key(0) not in table
    assert price_key(10) in table
