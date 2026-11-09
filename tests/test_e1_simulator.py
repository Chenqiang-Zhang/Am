import pytest

from app.api.chat_questioner import EntropyQuestioner, build_catalog, price_key
from app.api.chat_queries import GameAttributes
from app.api.chat_state import Candidate, ChatState
from eval.crs.baselines import PopularityRetriever
from eval.crs.constraints import GameFacts, check, counted_keys
from eval.crs.simulator import (
    MAX_TURNS,
    Anchor,
    Target,
    choose_anchor,
    choose_co_play_anchor,
    choose_target,
    is_dev,
    make_target,
    opening_tags,
    same_series,
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


# ---- wrong answers ----

def test_wrong_answers_swap_yes_and_no_at_the_given_rate_and_are_repeatable():
    from eval.crs.simulator import error_answers, flipped
    assert flipped("yes") == "no" and flipped("no") == "yes" and flipped("no_preference") == "no_preference"
    never = error_answers(TARGET, seed=0, error_rate=0.0)
    always = error_answers(TARGET, seed=0, error_rate=1.0)
    assert never(1, "tag:rogue_like") == "yes" and always(1, "tag:rogue_like") == "no"
    assert always(1, "tag:horror") == "no_preference"
    tenth = error_answers(TARGET, seed=0, error_rate=0.1)
    share = sum(tenth(turn, f"tag:t{i}") == "yes" for turn in range(1, 5) for i in range(5000)) / 20_000
    assert 0.09 < share < 0.11  # every key is a true "no", so a "yes" is an error
    assert [tenth(2, f"tag:t{i}") for i in range(50)] == [tenth(2, f"tag:t{i}") for i in range(50)]


def test_a_wrong_no_rules_the_target_out_for_a_filtering_retriever():
    from eval.crs.simulator import error_answers
    games = {str(i): facts(tags=[("genre:action", None)], price=1.0) for i in range(1, 30)}
    games["100"] = TARGET_FACTS

    class AsksRogueLike:
        def choose(self, state, candidates):
            return "tag:rogue_like" if "tag:rogue_like" not in state.asked else None

    records = simulate(TARGET, ["tag:action"], FilterRetriever(games), AsksRogueLike(), pool_size=1000,
                       answer=error_answers(TARGET, seed=0, error_rate=1.0))
    assert records[0].true_answer == "yes" and records[0].answer == "no"
    assert "tag:rogue_like" in records[1].state.exclude
    assert all(record.hit_rank is None for record in records)  # the target never comes back


# ---- anchors ----

def test_anchor_shares_a_developer_or_two_ranked_user_tags_and_never_is_the_target():
    games = {
        "100": TARGET_FACTS,
        # same developer, no shared tags
        "1": facts(tags=[("usertag:racing", 1)], developers=["supergiant"]),
        # two ranked user tags in common
        "2": facts(tags=[("usertag:rogue_like", 1), ("usertag:pixel_graphics", 3)]),
        # only one ranked tag; the shared genre and the rank-14 tag do not count
        "3": facts(tags=[("genre:action", None), ("usertag:rogue_like", 1), ("usertag:horror", 2)]),
        # empty studio ids are not a developer
        "4": facts(tags=[("usertag:racing", 1)], developers=[""]),
    }
    titles = {"100": "Bastion", "1": "Transistor", "2": "Dead Cells"}
    seen = set()
    for player in (f"p{i}" for i in range(60)):
        anchor = choose_anchor(player, "100", list(games), games, titles)
        seen.add((anchor.appid, anchor.relation))
    assert seen == {("1", "same_developer"), ("2", "similar")}
    assert choose_anchor("p1", "100", ["100", "3", "4"], games, titles) is None
    for player in (f"p{i}" for i in range(20)):
        assert choose_anchor(player, "100", list(games), games, titles, "same_developer").appid == "1"
        assert choose_anchor(player, "100", list(games), games, titles, "similar").appid == "2"
    assert choose_anchor("p1", "100", ["100", "2"], games, titles, "same_developer") is None


def test_co_play_anchor_is_the_longest_played_other_game():
    games = {"100": TARGET_FACTS, "7": facts(), "30": facts(), "200": facts()}
    hours = {"100": 500.0, "7": 20.0, "30": 20.0, "200": 3.0}
    assert choose_co_play_anchor("100", hours, games, {}) == Anchor("7", "co_played", False)
    assert choose_co_play_anchor("100", {"100": 1.0}, games, {}) is None


def test_series_needs_the_same_base_title():
    assert same_series("Portal", "Portal 2")
    assert same_series("Half-Life 2", "Half-Life 2: Episode One")
    assert same_series("Borderlands 2", "Borderlands: The Pre-Sequel")
    assert not same_series("Portal 2", "Portal Knights")
    assert not same_series("Dark Souls", "Dark Messiah of Might & Magic")
    assert not same_series("X", "X 2")  # too short to tell


def test_the_anchor_and_its_relation_are_in_the_opening_state():
    games = {str(i): facts(tags=[("genre:action", None)], price=1.0) for i in range(1, 5)}
    games["100"] = TARGET_FACTS
    records = simulate(TARGET, ["tag:action"], FilterRetriever(games), None, pool_size=1000,
                       anchor=Anchor("3", "same_developer", False))
    first = records[0].state
    assert first.anchors == ["3"] and first.relation == "same_developer"
    assert "3" not in [c.appid for record in records for c in record.shown]
