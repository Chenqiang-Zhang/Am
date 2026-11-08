import pytest

from app.api.chat_questioner import EntropyQuestioner, build_catalog, price_key
from app.api.chat_queries import GameAttributes
from app.api.chat_state import Candidate, ChatState
from eval.crs.baselines import PopularityRetriever
from eval.crs.constraints import GameFacts, check, counted_keys
from eval.crs.simulator import (
    MAX_TURNS,
    Target,
    choose_target,
    is_dev,
    make_target,
    opening_tags,
    simulate,
)
from eval.metrics import hdcg, success_by_turn, turns_taken
from eval.stats import holm, mcnemar_exact, paired_bootstrap_ci, wilcoxon_signed_rank


def facts(tags=(), developers=(), price=None):
    edges = [{"tag_id": tag_id, "rank": rank} for tag_id, rank in tags]
    return GameFacts(
        price=price,
        keys=counted_keys(edges, list(developers)),
        tag_edges=tuple(sorted(tags, key=lambda edge: edge[0])),
        developers=tuple(developers),
    )


TARGET_FACTS = facts(
    tags=[("genre:action", None), ("usertag:rogue_like", 2), ("usertag:pixel_graphics", 9), ("usertag:horror", 14)],
    developers=["supergiant"],
    price=8.0,
)
TARGET = make_target("p1", "100", TARGET_FACTS)


# ---- the simulated user ----

def test_a_star_holds_counted_tags_developers_and_the_bands_the_price_is_within():
    assert TARGET.known == {
        "tag:action", "tag:rogue_like", "tag:pixel_graphics", "studio:supergiant", price_key(20), price_key(10),
    }


def test_answers_follow_the_plan():
    assert TARGET.answer("tag:rogue_like") == "yes"
    assert TARGET.answer("tag:horror") == "no_preference"  # only at rank 14
    assert TARGET.answer("tag:puzzle") == "no"
    assert TARGET.answer("studio:supergiant") == "yes"
    assert TARGET.answer("studio:valve") == "no"
    assert TARGET.answer(price_key(10)) == "yes"
    assert TARGET.answer(price_key(5)) == "no"
    unknown_price = make_target("p1", "100", facts(tags=[("genre:action", None)]))
    assert unknown_price.answer(price_key(5)) == "no_preference"


def test_target_is_a_long_play_picked_by_hash():
    hours = {"1": 1.0, "2": 50.0, "3": 30.0}
    medians = {"1": 5.0, "2": 10.0, "3": 30.0}
    assert choose_target("p1", hours, medians) in {"2", "3"}
    assert choose_target("p1", hours, medians) == choose_target("p1", hours, medians)
    assert choose_target("p1", {"1": 1.0}, medians) is None


def test_dev_split_is_about_thirty_percent():
    share = sum(is_dev(str(i)) for i in range(20_000)) / 20_000
    assert 0.28 < share < 0.32


def test_opening_tags_are_whitelisted_tags_of_the_target_and_vary_with_the_seed():
    whitelist = ("tag:action", "tag:rogue_like", "tag:pixel_graphics", "tag:horror", "studio:supergiant")
    openings = [opening_tags(TARGET, whitelist, seed) for seed in range(6)]
    for opening in openings:
        assert len(opening) == 2
        assert set(opening) <= {"tag:action", "tag:rogue_like", "tag:pixel_graphics"}
    assert len({tuple(opening) for opening in openings}) > 1


# ---- one dialogue ----

class FilterRetriever:
    """Games in a fixed order that have every included tag, are within the price bounds and were not shown."""

    def __init__(self, games: dict[str, GameFacts]):
        self.games = games
        self.calls = []

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]:
        self.calls.append(state)
        unavailable = set(state.unavailable_appids())
        ranked = [
            Candidate(appid=appid, score=1.0) for appid, game in self.games.items()
            if appid not in unavailable and check(game, state).all
        ]
        return ranked[:k]


def catalog_for(games: dict[str, GameFacts]):
    attributes = {
        appid: GameAttributes(
            tag_keys=frozenset(key for key in game.keys if key.startswith("tag:")),
            studio_keys=frozenset(key for key in game.keys if key.startswith("studio:")),
            price=game.price, players=0,
        )
        for appid, game in games.items()
    }
    kinds = {key: {"genre", "usertag"} for game in attributes.values() for key in game.tag_keys}
    return build_catalog(attributes, kinds)


def test_a_hit_ends_the_dialogue_and_shown_games_never_come_back():
    games = {str(i): facts(tags=[("genre:action", None)], price=1.0) for i in range(1, 40)}
    games["100"] = TARGET_FACTS
    retriever = FilterRetriever(games)
    records = simulate(TARGET, ["tag:action"], retriever, None, pool_size=1000)
    shown = [c.appid for record in records for c in record.shown]
    assert len(shown) == len(set(shown))
    assert records[-1].hit_rank is not None
    assert [record.turn for record in records] == list(range(1, len(records) + 1))
    assert len(records) == 4  # 39 decoys before the target, ten a turn


def test_answers_reach_the_next_search_and_no_question_follows_the_last_list():
    # 30 cheap action games without rogue_like, then the target: a "no" to <=5 dollars removes the decoys
    games = {str(i): facts(tags=[("genre:action", None)], price=1.0) for i in range(1, 31)}
    games["100"] = TARGET_FACTS
    games.update({str(i): facts(tags=[("genre:action", None)], price=30.0) for i in range(200, 240)})
    catalog = catalog_for(games)
    records = simulate(TARGET, ["tag:action"], FilterRetriever(games), EntropyQuestioner(catalog), pool_size=1000)
    assert any(record.question for record in records), "the questioner should ask something"
    for record, following in zip(records, records[1:]):
        assert following.state.turn == record.turn + 1
        if record.question is None:
            continue
        key, state = record.question, following.state
        price = float(key.split(":")[1]) if key.startswith("price:") else None
        if record.answer == "yes":
            assert key in state.include or state.max_price == price
        elif record.answer == "no":
            assert key in state.exclude or state.min_price == price
        else:
            assert key in state.asked and key not in state.include + state.exclude
    assert all(record.question is None for record in records if record.turn == MAX_TURNS)
    assert records[-1].hit_rank is not None or len(records) == MAX_TURNS


def test_a_no_to_a_price_band_becomes_a_floor():
    cheap = {str(i): facts(tags=[("genre:action", None)], price=1.0) for i in range(1, 30)}
    games = {**cheap, "100": TARGET_FACTS}

    class AsksPrice:
        def choose(self, state, candidates):
            return price_key(5) if price_key(5) not in state.asked else None

    records = simulate(TARGET, ["tag:action"], FilterRetriever(games), AsksPrice(), pool_size=1000)
    assert records[0].answer == "no"
    assert records[1].state.min_price == 5
    assert records[1].hit_rank == 1  # every cheap game is gone


# ---- P ----

def test_popularity_ignores_conditions_and_leaves_out_the_evaluated_player():
    retriever = PopularityRetriever({"1": 10, "2": 10, "3": 5})
    state = ChatState(include=["tag:horror"], max_price=1, shown=["1"])
    assert [c.appid for c in retriever.retrieve(state, 2)] == ["2", "3"]
    mine = retriever.excluding_player(["2"])
    assert mine.players_by_appid["2"] == 9
    assert [c.appid for c in mine.retrieve(ChatState(), 3)] == ["1", "2", "3"]
    assert retriever.players_by_appid["2"] == 10


# ---- independent constraint check ----

def test_constraint_check_splits_include_exclude_and_budget():
    game = facts(tags=[("usertag:horror", 12), ("genre:action", None)], price=8.0)
    result = check(game, ChatState(include=["tag:action"], exclude=["tag:horror"], max_price=10, min_price=5))
    assert result.include and result.exclude and result.budget  # horror at rank 12 does not count
    assert not check(game, ChatState(min_price=8)).budget
    assert not check(facts(price=None), ChatState(min_price=0)).budget


# ---- metrics and statistics ----

def test_hdcg_is_one_at_the_first_turn_and_rank_and_falls_with_both():
    assert hdcg(1, 1) == pytest.approx(1.0)
    assert hdcg(None, None) == 0.0
    assert hdcg(1, 1) > hdcg(1, 5) > hdcg(2, 1) > hdcg(2, 5) > hdcg(5, 10) > 0


def test_success_and_turns():
    assert success_by_turn(3, 2) == 0.0 and success_by_turn(3, 3) == 1.0
    assert turns_taken(None, 5) == 5 and turns_taken(2, 5) == 2


def test_wilcoxon_matches_the_normal_approximation():
    w, p = wilcoxon_signed_rank([1, 2, 3, 4, 5, -6, 0])
    assert w == 15
    assert p == pytest.approx(0.4017, abs=1e-3)
    assert wilcoxon_signed_rank([0, 0]) == (0.0, 1.0)


def test_mcnemar_and_holm():
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(3, 3) == 1.0
    assert holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.06})


def test_bootstrap_interval_contains_the_mean():
    low, high = paired_bootstrap_ci([0.0, 1.0] * 50, resamples=2000)
    assert low < 0.5 < high
