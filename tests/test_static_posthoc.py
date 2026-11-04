"""eval/static_posthoc.py on small synthetic inputs."""

import math

import numpy as np
import pytest

from eval.static_posthoc import HOURS_CONTRASTS, exploration, hours_groups

CASES = {
    "validation": [("v1", "1", 0, "10"), ("v2", "11+", 0, "20"), ("v2", "11+", 1, "30")],
    "test_sample": [("t1", "1", 0, "10"), ("t2", "2-3", 0, "40"), ("t2", "2-3", 1, "50"), ("t3", "11+", 0, "60")],
}


def test_exploration_counts_users_and_cases_per_source_and_over_all():
    sources = [
        {"name": "a", "pairs": [["v1", "10"], ["t1", "10"], ["t2", "41"], ["x", "1"]]},
        {"name": "b", "pairs": [["t2", "50"], ["t1", "10"]]},
    ]
    result = exploration(CASES, sources)
    assert result["sources"]["a"] == {
        "pairs": 4,
        "users": 4,
        "validation_users": 1,
        "test_users": 2,
        "validation_cases": 1,
        "test_cases": 1,
        "test_users_by_stratum": {"1": 1, "2-3": 1, "4-10": 0, "11+": 0},
    }
    every = result["all"]
    assert (every["pairs"], every["users"], every["test_users"], every["test_cases"]) == (5, 4, 2, 2)


def test_hours_groups_are_ratios_of_post_stratified_means():
    players = {"1": ["a", "b"], "2-3": ["c"], "4-10": ["d"], "11+": ["e", "f"]}
    cases = []
    hours = {}
    played = {"a": [0.5, 3.0], "b": [20.0], "c": [0.2, 0.7, 12.0], "d": [5.0], "e": [1.5, 30.0], "f": [0.1]}
    for name, ids in players.items():
        for player_id in ids:
            for rep, value in enumerate(played[player_id]):
                appid = f"{player_id}{rep}"
                cases.append((player_id, name, rep, appid))
                hours[(player_id, appid)] = value
    rows = {"peer_log1p_10", "peer_uniform_10", "peer_uniform_all"}
    rank_of = {"peer_log1p_10": 12, "peer_uniform_10": 3, "peer_uniform_all": 1}
    ranks = {row: {p: [rank_of[row]] * len(played[p]) for p in played} for row in rows}
    pool = {"1": 10, "2-3": 20, "4-10": 30, "11+": 40}
    result = hours_groups(ranks, players, cases, hours, pool, replicates=50, seed=0)
    weight = {name: pool[name] / 100 for name in players}

    def post_stratified(value):
        return sum(weight[name] * np.mean([value(p) for p in ids]) for name, ids in players.items())

    groups = (("under_1h", lambda h: h < 1), ("1h_to_10h", lambda h: 1 <= h < 10), ("10h_or_more", lambda h: h >= 10))
    for group, inside in groups:
        share = post_stratified(lambda p: sum(inside(h) for h in played[p]) / len(played[p]))
        assert result[group]["share"] == pytest.approx(share)
        assert result[group]["evaluations"] == sum(inside(hours[(p, a)]) for p, _, _, a in cases)
        for row in rows:
            gain = 0.0 if rank_of[row] > 10 else 1 / math.log2(rank_of[row] + 1)
            assert result[group]["rows"][row] == pytest.approx(gain)  # every rep of the group has the same rank
        assert set(result[group]["contrasts"]) == {f"{a} - {b}" for a, b in HOURS_CONTRASTS}
        contrast = result[group]["contrasts"]["peer_uniform_all - peer_log1p_10"]
        assert contrast["delta"] == pytest.approx(1.0) and contrast["lo"] == pytest.approx(1.0)
