import json

from app.api.chat_state import (
    MAX_ANCHORS,
    MAX_ATTRIBUTES,
    Candidate,
    ChatState,
    TurnLog,
    TurnUpdate,
    append_turn_log,
    apply_update,
    rank_candidates,
    record_question,
    record_shown,
    tag_key,
)


def test_tag_key_merges_kinds_of_the_same_name():
    assert tag_key("genre:action") == tag_key("usertag:action") == "tag:action"


def test_untrusted_state_drops_malformed_ids_and_duplicates():
    state = ChatState.model_validate(
        {
            "include": ["tag:rogue_like", "tag:rogue_like", "Rogue-like", "tag:", 3, "studio:"],
            "anchors": ["105600", "Terraria", "105600"],
            "shown": ["730", "<script>"],
            "unknown_field": "ignored",
        }
    )
    assert state.include == ["tag:rogue_like"]
    assert state.anchors == ["105600"]
    assert state.shown == ["730"]


def test_lists_are_capped_keeping_the_latest():
    state = ChatState(include=[f"tag:t{i}" for i in range(MAX_ATTRIBUTES + 5)], anchors=["1", "2", "3", "4", "5"])
    assert len(state.include) == MAX_ATTRIBUTES
    assert state.include[-1] == f"tag:t{MAX_ATTRIBUTES + 4}"
    assert state.anchors == [str(i) for i in range(5 - MAX_ANCHORS + 1, 6)]


def test_exclude_wins_when_a_key_is_in_both():
    state = ChatState(include=["tag:horror", "tag:co_op"], exclude=["tag:horror"])
    assert state.include == ["tag:co_op"]
    assert state.exclude == ["tag:horror"]


def test_price_keys_become_max_price():
    state = ChatState(include=["price:10", "tag:racing"], exclude=["price:5"], max_price=20)
    assert state.max_price == 10
    assert state.include == ["tag:racing"]
    assert state.exclude == []


def test_relation_follows_anchors():
    assert ChatState(anchors=["105600"]).relation == "similar"
    assert ChatState(relation="co_played").relation is None


def test_first_turn_with_negation_and_budget():
    update = TurnUpdate(include=["tag:rogue_like"], exclude=["tag:horror"], max_price=10)
    state = apply_update(ChatState(), update)
    assert state.include == ["tag:rogue_like"]
    assert state.exclude == ["tag:horror"]
    assert state.max_price == 10
    assert state.turn == 1


def test_conditions_carry_over_and_a_later_turn_overrides():
    state = apply_update(ChatState(), TurnUpdate(include=["tag:co_op"], max_price=30))
    state = apply_update(state, TurnUpdate(exclude=["tag:co_op"], max_price=20))
    assert state.include == []
    assert state.exclude == ["tag:co_op"]
    assert state.max_price == 20


def test_two_turn_example_keeps_both_conditions():
    # 「協力プレイ必須」→「予算は20ドルまで」: the budget turn must not drop co-op
    state = apply_update(ChatState(), TurnUpdate(include=["tag:co_op"]))
    state = apply_update(state, TurnUpdate(max_price=20))
    assert state.include == ["tag:co_op"]
    assert state.max_price == 20


def test_no_preference_removes_the_key_and_stops_asking_about_it():
    state = ChatState(include=["tag:co_op"], exclude=["tag:online_co_op"])
    state = apply_update(state, TurnUpdate(no_preference=["tag:co_op", "tag:online_co_op"]))
    assert state.include == []
    assert state.exclude == []
    assert state.asked == ["tag:co_op", "tag:online_co_op"]


def test_reject_shown_moves_presented_games_to_rejected():
    state = record_shown(ChatState(), ["1", "2", "3"])
    state = apply_update(state, TurnUpdate(reject_shown=True, include=["tag:co_op"]))
    assert state.rejected == ["1", "2", "3"]
    assert set(state.unavailable_appids()) == {"1", "2", "3"}


def test_new_anchor_without_relation_is_similar_and_relation_alone_updates():
    state = apply_update(ChatState(), TurnUpdate(anchors=["620"], relation="same_developer"))
    assert state.relation == "same_developer"
    state = apply_update(state, TurnUpdate(anchors=["105600"]))
    assert state.anchors == ["620", "105600"]
    assert state.relation == "similar"
    state = apply_update(state, TurnUpdate(relation="co_played"))
    assert state.relation == "co_played"


def test_anchors_are_unavailable_as_results():
    state = ChatState(anchors=["105600"], shown=["730"], rejected=["730", "4000"])
    assert state.unavailable_appids() == ["105600", "730", "4000"]


def test_restricted_to_drops_unknown_ids_but_keeps_conditions():
    state = ChatState(include=["tag:rogue_like", "tag:made_up"], anchors=["105600", "999"], max_price=10, turn=2)
    restricted = state.restricted_to({"tag:rogue_like"}, {"105600"})
    assert restricted.include == ["tag:rogue_like"]
    assert restricted.anchors == ["105600"]
    assert restricted.max_price == 10
    assert restricted.turn == 2


def test_record_question_and_shown_do_not_duplicate():
    state = record_question(record_question(ChatState(), "tag:pixel_graphics"), "tag:pixel_graphics")
    assert state.asked == ["tag:pixel_graphics"]
    state = record_shown(record_shown(state, ["1", "2"]), ["2", "3"])
    assert state.shown == ["1", "2", "3"]


def test_rank_candidates_breaks_ties_by_appid_as_string():
    ranked = rank_candidates(
        [Candidate(appid="20", score=1.0), Candidate(appid="100", score=1.0), Candidate(appid="5", score=2.0)]
    )
    assert [candidate.appid for candidate in ranked] == ["5", "100", "20"]


def test_turn_log_is_one_json_line(tmp_path):
    path = tmp_path / "logs" / "turns.jsonl"
    state = ChatState()
    log = TurnLog(
        dialogue_id="u1-s0",
        turn=1,
        state_before=state,
        update=TurnUpdate(include=["tag:co_op"]),
        state_after=apply_update(state, TurnUpdate(include=["tag:co_op"])),
        pool_size=42,
        shown=[Candidate(appid="730", score=1.5)],
        question="tag:pixel_graphics",
        entropy={"tag:pixel_graphics": 0.97},
    )
    append_turn_log(path, log)
    append_turn_log(path, log)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert TurnLog.model_validate(json.loads(lines[0])) == log
