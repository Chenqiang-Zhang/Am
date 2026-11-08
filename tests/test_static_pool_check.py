"""eval/static_pool_check.py on synthetic play records."""

import hashlib
import math
import random

import pytest

from eval.static_eval import baseline_rankings
from eval.static_pool_check import compare, pool_cases, pool_values
from eval.static_split import STRATA, build_split

SIZES = {
    "validation_size": 40,
    "test_sample_sizes": {"1": None, "2-3": None, "4-10": 20, "11+": 15},
    "subsets": {"v0": ("per_stratum", 5), "explanations": ("per_stratum", 2), "beyond_accuracy": ("proportional", 20)},
}


def synthetic_records(players=260, seed=3):
    rng = random.Random(seed)
    records = []
    for index in range(players):
        player_id = hashlib.sha256(f"pool player {index}".encode()).hexdigest()[:32]
        for appid in rng.sample(range(10, 120), rng.choice([1, 2, 3, 4, 6, 9, 12, 20])):
            records.append((player_id, str(appid), round(rng.uniform(0, 100), 1)))
    return sorted(records, key=lambda record: (record[1], record[0]))


RECORDS = synthetic_records()
SPLIT = build_split(RECORDS, **SIZES)


def test_the_pool_is_every_eligible_player_outside_validation_with_the_splits_hidden_games():
    pool, cases = pool_cases(RECORDS, SPLIT)
    validation = {record["player_id"] for name in STRATA for record in SPLIT["validation"][name]}
    eligible = {player_id for player_id, _, _ in RECORDS if sum(1 for p, _, _ in RECORDS if p == player_id) >= 2}
    assert {player_id for name in STRATA for player_id in pool[name]} == eligible - validation
    assert {name: len(pool[name]) for name in STRATA} == {name: SPLIT["counts"]["test_pool"][name] for name in STRATA}
    hidden = {}
    for player_id, _, rep, appid in cases:
        hidden.setdefault(player_id, []).append(appid)
    for name in STRATA:
        assert pool[name][: len(SPLIT["test_sample"][name])] == [r["player_id"] for r in SPLIT["test_sample"][name]]
        for record in SPLIT["test_sample"][name]:
            assert hidden[record["player_id"]] == record["hidden"]


def test_a_pool_that_is_not_the_splits_is_refused():
    with pytest.raises(SystemExit):
        pool_cases(RECORDS, dict(SPLIT, test_pool_sha256="0" * 64))


def test_pool_values_are_the_pool_weighted_means_of_the_users_rep_means():
    pool, cases = pool_cases(RECORDS, SPLIT)
    ranked = baseline_rankings(RECORDS, cases)
    values = pool_values(ranked, pool, cases, metrics=("HR", "NDCG"), ks=(10,))
    total = sum(len(pool[name]) for name in STRATA)
    for method in ("popularity", "item_knn"):
        for metric in ("HR", "NDCG"):
            expected = 0.0
            for name in STRATA:
                users = []
                for player_id in pool[name]:
                    ranks = [ranked[method][(p, rep)][0] for p, _, rep, _ in cases if p == player_id]
                    gains = [0.0 if r > 10 else (1.0 if metric == "HR" else 1 / math.log2(r + 1)) for r in ranks]
                    users.append(sum(gains) / len(gains))
                if users:
                    expected += len(pool[name]) / total * (sum(users) / len(users))
            assert values[(method, metric, 10)] == pytest.approx(expected, rel=1e-12)


def test_compare_marks_the_values_outside_the_ci():
    values = {("popularity", "NDCG", 10): 0.2, ("item_knn", "NDCG", 10): 0.3}
    row = {"metric": "NDCG", "k": 10, "group": "all", "estimate": 0.21, "lo": 0.19, "hi": 0.23}
    test = {
        "estimates": [
            dict(row, method="popularity"),
            dict(row, method="item_knn"),
            dict(row, method="item_knn", group="1"),
            dict(row, method="graph_tuned"),
        ]
    }
    covered = {(r["method"], r["covered"]) for r in compare(values, test)}
    assert covered == {("popularity", True), ("item_knn", False)}
