import pytest

from eval.crs.report_e1 import anchored_gap, graph_effect, report, subsets


def dialogue(group, player, seed, anchored, success_turn, popular=False, same_series=False):
    return {"group": group, "player_id": player, "seed": seed, "anchored": anchored, "popular": popular,
            "same_series": same_series, "success_turn": success_turn, "hdcg": 0.0}


def layer(g_without, g_with, v_without, v_with, players=("a", "b", "c"), **flags):
    """Every player gets the same success turn per group and condition, for two seeds."""
    rows = []
    for player in players:
        for seed in (0, 1):
            rows += [
                dialogue("G", player, seed, False, g_without, **flags),
                dialogue("G", player, seed, True, g_with, **flags),
                dialogue("V-entropy", player, seed, False, v_without, **flags),
                dialogue("V-entropy", player, seed, True, v_with, **flags),
            ]
    return rows


def test_graph_effect_is_g_gain_minus_v_gain():
    # G: turn 3 -> turn 1 (SR@1 +1); V: turn 3 -> turn 3 (+0)
    result = graph_effect(layer(3, 1, 3, 3), "SR@1")
    assert result["mean"] == pytest.approx(1.0)
    assert result["users"] == 3


def test_anchored_gap_compares_g_and_v_with_the_anchor_only():
    # both reach turn 1 with the anchor: no gap, whatever happened without it
    assert anchored_gap(layer(5, 1, None, 1), "SR@1")["mean"] == pytest.approx(0.0)
    assert anchored_gap(layer(5, 1, None, 2), "SR@1")["mean"] == pytest.approx(1.0)


def test_report_holm_adjusts_the_four_gain_tests_together_and_same_developer_apart():
    runs = {
        "similar": layer(3, 1, 3, 3, players=tuple("abcdefghij")),
        "co_play": layer(3, 3, 3, 3, players=tuple("abcdefghij")),
        "same_developer": layer(3, 1, 3, 2, players=tuple("abcdefghij")),
    }
    result = report(runs)
    assert set(result["graph_effect"]) == {"similar hDCG@(5,10)", "similar SR@1", "co_play hDCG@(5,10)",
                                           "co_play SR@1"}
    assert set(result["same_developer"]) == {"same_developer hDCG@(5,10)", "same_developer SR@1"}
    similar = result["graph_effect"]["similar SR@1"]
    assert similar["holm_p"] >= similar["wilcoxon_p"]
    assert result["same_developer"]["same_developer SR@1"]["mean"] == pytest.approx(1.0)
    assert "same_series SR@1" in result["descriptive"]["similar"]


def test_subsets_split_series_and_popularity():
    rows = layer(3, 1, 3, 3, same_series=True, popular=True) + layer(3, 1, 3, 3, players=("x",))
    parts = subsets(rows)
    assert {d["player_id"] for d in parts["same_series"]} == {"a", "b", "c"}
    assert {d["player_id"] for d in parts["other_series"]} == {"x"}
    assert {d["player_id"] for d in parts["long_tail"]} == {"x"}
