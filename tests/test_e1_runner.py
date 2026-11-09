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


def test_freeze_mismatches_name_every_change():
    from eval.crs.run_e1 import freeze_mismatches

    frozen = {"code_sha256": {"eval/a.py": "1", "eval/b.py": "2"}, "plan_sha256": "p", "graph_sha256": "g",
              "played_edges": 10, "embedding_digest": "e"}
    assert freeze_mismatches(frozen, dict(frozen)) == []
    changed = {**frozen, "code_sha256": {"eval/a.py": "1", "eval/c.py": "3"}, "plan_sha256": "q"}
    assert freeze_mismatches(frozen, changed) == [
        "code changed: eval/b.py", "code changed: eval/c.py", "plan_sha256 changed"]


def test_a_layer_counts_as_run_on_test_only_once_it_wrote_its_manifest(tmp_path):
    import json

    from eval.crs.run_e1 import finished_test_runs

    (tmp_path / "1_similar").mkdir()
    (tmp_path / "1_similar" / "manifest.json").write_text(json.dumps({"split": "test", "layer": "similar"}))
    (tmp_path / "2_co_play").mkdir()  # died before its manifest
    (tmp_path / "3").mkdir()
    (tmp_path / "3" / "manifest.json").write_text(json.dumps({"split": "test"}))  # no layer: none
    assert [p.name for p in finished_test_runs(tmp_path, "similar")] == ["1_similar"]
    assert finished_test_runs(tmp_path, "co_play") == []
    assert [p.name for p in finished_test_runs(tmp_path, "none")] == ["3"]
