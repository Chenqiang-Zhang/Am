"""F5: eval/static_stats.py on synthetic ranks and values."""

import math
import random

import numpy as np
import pytest

from eval.metrics import hit_rate_at_k, ndcg_at_k
from eval.static_split import STRATA
from eval.static_stats import (
    GROUPS,
    KS,
    METRICS,
    PairedBootstrap,
    bootstrap_p_value,
    contrast_rows,
    estimate_rows,
    holm,
    interval,
    metric_value,
    paired_bootstrap,
    user_value,
    value_matrices,
)

POOL = {"1": 2310, "2-3": 3853, "4-10": 9837, "11+": 47670}
SIZES = {"1": 40, "2-3": 50, "4-10": 30, "11+": 30}


def synthetic_ranks(sizes=SIZES, seed=0):
    """Ranks of one method: 2 reps for 1 visible game, 3 otherwise; about 30% not ranked."""
    rng = random.Random(seed)
    players = {name: [f"{name}/{index}" for index in range(size)] for name, size in sizes.items()}
    ranks = {
        player_id: [None if rng.random() < 0.3 else rng.randint(1, 80) for _ in range(2 if name == "1" else 3)]
        for name in STRATA
        for player_id in players[name]
    }
    return ranks, players


def shifted(ranks, shift):
    return {player_id: [None if rank is None else rank + shift for rank in reps] for player_id, reps in ranks.items()}


def direct_estimate(ranks, players, metric, k, strata):
    """Σ_s W_s · mean_s computed without eval.static_stats."""
    total = sum(POOL[name] for name in strata)
    estimate = 0.0
    for name in strata:
        values = [
            sum(metric_value_by_hand(metric, rank, k) for rank in ranks[player_id]) / len(ranks[player_id])
            for player_id in players[name]
        ]
        estimate += POOL[name] / total * sum(values) / len(values)
    return estimate


def metric_value_by_hand(metric, rank, k):
    if rank is None or rank > k:
        return 0.0
    return 1.0 if metric == "HR" else 1 / math.log2(rank + 1)


def test_metrics_agree_with_eval_metrics():
    others = [f"g{index}" for index in range(60)]
    for rank in [None, *range(1, 61)]:
        ranked = list(others)
        if rank is not None:
            ranked.insert(rank - 1, "hidden")
        for k in KS:
            assert metric_value("HR", rank, k) == hit_rate_at_k(ranked, "hidden", k)
            assert metric_value("NDCG", rank, k) == ndcg_at_k(ranked, "hidden", k)


def test_ranks_are_positive_integers():
    assert metric_value("NDCG", np.int64(3), 10) == 0.5
    with pytest.raises(ValueError):
        metric_value("HR", 0, 10)
    with pytest.raises(TypeError):
        metric_value("HR", 2.0, 10)
    with pytest.raises(ValueError):
        metric_value("MRR", 1, 10)


def test_a_user_value_is_the_mean_over_reps():
    assert user_value([1, None, 3], "HR", 10) == pytest.approx(2 / 3)
    assert user_value([1, None, 3], "NDCG", 10) == pytest.approx((1 + 0 + 0.5) / 3)
    assert user_value([11, 2], "HR", 10) == 0.5
    with pytest.raises(ValueError):
        user_value([], "HR", 10)


def test_identical_methods_give_delta_zero_and_a_zero_interval():
    ranks, players = synthetic_ranks()
    copy = {player_id: list(reps) for player_id, reps in ranks.items()}
    bootstrap = paired_bootstrap({"a": ranks, "b": copy}, players, POOL, replicates=500)
    rows = contrast_rows(bootstrap, [("a", "b")])
    assert len(rows) == len(METRICS) * len(KS) * len(GROUPS)
    for row in rows:
        assert (row["delta"], row["lo"], row["hi"], row["se"], row["p"]) == (0.0, 0.0, 0.0, 0.0, 1.0)


def test_shifted_ranks_give_the_known_difference_inside_the_interval():
    ranks, players = synthetic_ranks()
    worse = shifted(ranks, 5)
    bootstrap = paired_bootstrap({"a": ranks, "b": worse}, players, POOL, replicates=2000)
    for row in contrast_rows(bootstrap, [("a", "b")]):
        strata = GROUPS[row["group"]]
        expected = direct_estimate(ranks, players, row["metric"], row["k"], strata) - direct_estimate(
            worse, players, row["metric"], row["k"], strata
        )
        assert row["delta"] == pytest.approx(expected, abs=1e-12)
        assert row["lo"] - 1e-12 <= expected <= row["hi"] + 1e-12
        assert row["n_users"] == sum(SIZES[name] for name in strata)
    overall = bootstrap.contrast(("a", "NDCG", 10), ("b", "NDCG", 10), "all")
    assert overall["delta"] > 0 and overall["lo"] > 0


def test_a_constant_difference_is_recovered_only_because_the_bootstrap_is_paired():
    rng = np.random.default_rng(3)
    values = {name: rng.random((1, size)) for name, size in SIZES.items()}
    both = {name: np.vstack([matrix, matrix + 0.125]) for name, matrix in values.items()}
    bootstrap = PairedBootstrap([("a", "HR", 10), ("b", "HR", 10)], both, POOL, replicates=1000)
    contrast = bootstrap.contrast(("b", "HR", 10), ("a", "HR", 10), "all")
    assert contrast["delta"] == pytest.approx(0.125, abs=1e-12)
    assert contrast["lo"] == pytest.approx(0.125, abs=1e-12) and contrast["hi"] == pytest.approx(0.125, abs=1e-12)
    single = bootstrap.summary(("a", "HR", 10), "all")
    assert single["hi"] - single["lo"] > 0.05  # each method alone is uncertain


def test_the_interval_covers_the_population_difference_about_95_percent_of_the_time():
    # Strata differ in their true difference and the sample over-represents short histories, so a
    # plain mean of the sample would be far off; the post-stratified estimate is not.
    rate_a = {"1": 0.40, "2-3": 0.36, "4-10": 0.30, "11+": 0.20}
    rate_b = {"1": 0.30, "2-3": 0.30, "4-10": 0.28, "11+": 0.20}
    truth = sum(POOL[name] * (rate_a[name] - rate_b[name]) for name in STRATA) / sum(POOL.values())
    sizes = {"1": 400, "2-3": 400, "4-10": 200, "11+": 200}
    rng = np.random.default_rng(11)
    covered = 0
    plain_deltas = []
    simulations = 200
    for simulation in range(simulations):
        values = {}
        for name, size in sizes.items():
            values[name] = np.vstack([rng.random(size) < rate_a[name], rng.random(size) < rate_b[name]]).astype(float)
        bootstrap = PairedBootstrap([("a", "HR", 10), ("b", "HR", 10)], values, POOL, replicates=400, seed=simulation)
        contrast = bootstrap.contrast(("a", "HR", 10), ("b", "HR", 10), "all")
        covered += contrast["lo"] <= truth <= contrast["hi"]
        plain_deltas.append(np.mean(np.concatenate([values[name][0] - values[name][1] for name in STRATA])))
    assert 0.88 <= covered / simulations <= 0.99
    assert np.mean(plain_deltas) > 3 * truth


def test_post_stratified_mean_equals_the_plain_mean_for_a_proportional_sample():
    pool = {"1": 100, "2-3": 200, "4-10": 300, "11+": 400}
    rng = np.random.default_rng(5)
    values = {name: rng.random((1, pool[name] // 10)) for name in STRATA}
    bootstrap = PairedBootstrap([("a", "NDCG", 10)], values, pool, replicates=10)
    plain = np.concatenate([values[name][0] for name in STRATA]).mean()
    assert bootstrap.estimate(("a", "NDCG", 10), "all") == pytest.approx(plain, rel=1e-12)


def test_groups_renormalise_the_pool_weights():
    ranks, players = synthetic_ranks()
    bootstrap = paired_bootstrap({"a": ranks}, players, POOL, replicates=10)
    column = ("a", "NDCG", 20)
    means = {name: direct_estimate(ranks, players, "NDCG", 20, (name,)) for name in STRATA}
    assert bootstrap.weights("1-3") == {"1": 2310 / 6163, "2-3": 3853 / 6163}
    assert bootstrap.estimate(column, "1-3") == pytest.approx((2310 * means["1"] + 3853 * means["2-3"]) / 6163)
    for name in STRATA:
        assert bootstrap.estimate(column, name) == pytest.approx(means[name], rel=1e-12)
        assert bootstrap.weights(name) == {name: 1.0}
    assert bootstrap.n_users("1-3") == SIZES["1"] + SIZES["2-3"]
    assert bootstrap.n_users("all") == sum(SIZES.values())


def test_one_index_matrix_per_stratum_from_default_rng():
    rng = np.random.default_rng(9)
    values = {name: rng.random((3, size)) for name, size in SIZES.items()}
    columns = [("a", "HR", 10), ("b", "HR", 10), ("c", "HR", 10)]
    bootstrap = PairedBootstrap(columns, values, POOL, replicates=300, seed=0)
    draws = np.random.default_rng(0)
    for name in STRATA:
        index = draws.integers(0, SIZES[name], size=(300, SIZES[name]))
        assert np.array_equal(bootstrap.replicate_means[name], values[name][:, index].mean(axis=2))


def test_chunking_and_reruns_do_not_change_the_replicates():
    ranks, players = synthetic_ranks()
    ranks_by_method = {"a": ranks, "b": shifted(ranks, 2)}
    reference = paired_bootstrap(ranks_by_method, players, POOL, replicates=777)
    again = paired_bootstrap(ranks_by_method, players, POOL, replicates=777)
    columns = reference.columns
    chunked = PairedBootstrap(columns, value_matrices(ranks_by_method, players, columns), POOL, 777, chunk_elements=1)
    other_seed = paired_bootstrap(ranks_by_method, players, POOL, replicates=777, seed=1)
    for name in STRATA:
        assert np.array_equal(reference.replicate_means[name], chunked.replicate_means[name])
        assert np.array_equal(reference.replicate_means[name], again.replicate_means[name])
        assert not np.array_equal(reference.replicate_means[name], other_seed.replicate_means[name])


def test_the_methods_must_share_every_players_reps():
    ranks, players = synthetic_ranks()
    fewer = {player_id: reps[:1] for player_id, reps in ranks.items()}
    with pytest.raises(ValueError, match="share the reps"):
        paired_bootstrap({"a": ranks, "b": fewer}, players, POOL, replicates=10)
    missing = dict(ranks)
    missing.pop("11+/0")
    with pytest.raises(ValueError, match="share the reps"):
        paired_bootstrap({"a": ranks, "b": missing}, players, POOL, replicates=10)


def test_a_group_with_an_empty_stratum_cannot_be_estimated():
    ranks, players = synthetic_ranks()
    players["1"] = []
    bootstrap = paired_bootstrap({"a": ranks}, players, POOL, replicates=10)
    with pytest.raises(ValueError, match="no users in stratum 1"):
        bootstrap.estimate(("a", "HR", 10), "all")
    assert bootstrap.summary(("a", "HR", 10), "11+")["n_users"] == SIZES["11+"]


def test_rows_carry_the_documented_fields():
    ranks, players = synthetic_ranks()
    bootstrap = paired_bootstrap({"a": ranks, "b": shifted(ranks, 1)}, players, POOL, replicates=50, seed=4)
    contrasts = contrast_rows(bootstrap, [("a", "b")], metrics=["NDCG"], ks=[10], groups=["all", "1-3"], level=0.975)
    assert [row["group"] for row in contrasts] == ["all", "1-3"]
    assert set(contrasts[0]) == {
        "contrast", "metric", "k", "group", "delta", "lo", "hi", "se", "p", "level", "n_users", "B", "seed"
    }
    assert (contrasts[0]["contrast"], contrasts[0]["B"], contrasts[0]["seed"], contrasts[0]["level"]) == (
        "a - b", 50, 4, 0.975
    )
    estimates = estimate_rows(bootstrap, ["a", "b"], metrics=["HR"], ks=[10, 20], groups=["2-3"])
    assert len(estimates) == 4
    assert set(estimates[0]) == {
        "method", "metric", "k", "group", "estimate", "lo", "hi", "se", "level", "n_users", "B", "seed"
    }


def test_wider_levels_give_wider_intervals():
    replicates = np.random.default_rng(2).normal(size=10_000)
    lo95, hi95 = interval(replicates, 0.95)
    lo975, hi975 = interval(replicates, 0.975)
    assert lo975 < lo95 < 0 < hi95 < hi975
    assert (lo95, hi95) == pytest.approx((-1.96, 1.96), abs=0.06)


def test_bootstrap_p_values():
    assert bootstrap_p_value(np.zeros(100)) == 1.0
    assert bootstrap_p_value(np.full(99, 0.5)) == 2 / 100
    assert bootstrap_p_value(np.array([-1.0] * 30 + [1.0] * 69)) == pytest.approx(2 * 31 / 100)


def test_holm_adjusts_step_down():
    adjusted = holm({"overall": 0.01, "short": 0.04, "other": 0.03})
    assert adjusted == pytest.approx({"overall": 0.03, "short": 0.06, "other": 0.06})
    assert list(adjusted) == ["overall", "short", "other"]
    assert holm({"only": 0.2}) == {"only": 0.2}
    assert holm({"a": 0.6, "b": 0.7}) == {"a": 1.0, "b": 1.0}
    # With two tests the smaller p-value is judged at α/2, as with a 97.5% interval.
    assert holm({"overall": 0.02, "short": 0.045}) == pytest.approx({"overall": 0.04, "short": 0.045})
