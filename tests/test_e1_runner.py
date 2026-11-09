import pytest

from eval.crs.run_e1 import GROUPS, anchor_gain


def dialogue(group, player, seed, anchored, success_turn, popular=False):
    return {"group": group, "player_id": player, "seed": seed, "anchored": anchored, "popular": popular,
            "success_turn": success_turn, "hdcg": 0.0}


def test_anchor_gain_compares_the_same_users_with_and_without_the_anchor():
    dialogues = []
    for player in ("a", "b"):
        for seed in (0, 1):
            # G: found at turn 3 without the anchor, at turn 1 with it; V-entropy: turn 3 either way
            dialogues.append(dialogue("G", player, seed, False, 3))
            dialogues.append(dialogue("G", player, seed, True, 1))
            dialogues.append(dialogue("V-entropy", player, seed, False, 3))
            dialogues.append(dialogue("V-entropy", player, seed, True, 3))
    gain = anchor_gain(dialogues)["all"]
    assert gain["SR@1"]["G"]["gain"] == pytest.approx(1.0)
    assert gain["SR@1"]["V-entropy"]["gain"] == pytest.approx(0.0)
    assert gain["SR@1"]["G gain - V-entropy gain"]["mean"] == pytest.approx(1.0)
    assert gain["SR@5"]["G gain - V-entropy gain"]["mean"] == pytest.approx(0.0)
    assert set(gain["SR@1"]) <= set(GROUPS) | {"G gain - V-entropy gain"}
    assert "popular" not in anchor_gain(dialogues)
