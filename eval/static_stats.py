"""Statistics of the static recommendation evaluation: HR and NDCG, post-stratified means and a paired,
stratified bootstrap.

- A rank is the 1-based position of the hidden game in a method's ranking, or None when the game is
  not in the list (v0 ranks only its top 50). HR@K = [rank <= K] and NDCG@K = [rank <= K] /
  log2(rank + 1), as eval/metrics.py computes them from the ranked list.
- A user's value is the mean over the user's reps. A group's estimate is the post-stratified mean
  Σ_s W_s · mean_s over its strata, W_s = N_s / Σ N from the test-pool sizes; the group 1-3 is
  strata 1 and 2-3 together.
- Bootstrap: numpy default_rng(seed) draws one B × n_s index matrix per stratum (strata in order),
  which resamples the users of that stratum for every method, metric and K at once, so every
  difference of two methods is paired. The matrix is drawn in chunks of rows, which gives the same
  numbers as one draw. CIs are percentile intervals; the SE is the standard deviation of the B
  replicates. Two-sided p-values from the same replicates feed Holm's adjustment when a claim
  is tested on more than one group.
"""

import math
import operator
from typing import Any, Mapping, Sequence

import numpy as np

from .static_split import STRATA

METRICS = ("HR", "NDCG")
KS = (10, 20, 50)
GROUPS: dict[str, tuple[str, ...]] = {"all": STRATA, "1-3": ("1", "2-3"), **{name: (name,) for name in STRATA}}
REPLICATES = 10_000
SEED = 0
LEVEL = 0.95
# Indices plus gathered values per chunk of bootstrap replicates (8 bytes each), at least one replicate.
CHUNK_ELEMENTS = 1 << 22
# A replicate difference this close to the null is the null: each column sums its strata on its own, so a
# difference that is exactly 0 on paper can come out as about ±1e-16.
P_VALUE_TOLERANCE = 1e-12

Rank = int | None
Column = tuple[str, str, int]  # (method, metric, K)


def metric_value(metric: str, rank: Rank, k: int) -> float:
    if rank is not None and operator.index(rank) < 1:
        raise ValueError(f"ranks start at 1, got {rank}")
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    if rank is None or rank > k:
        return 0.0
    return 1.0 if metric == "HR" else 1.0 / math.log2(rank + 1)


def user_value(ranks: Sequence[Rank], metric: str, k: int) -> float:
    """The mean over a user's reps."""
    if not ranks:
        raise ValueError("a user needs at least one rep")
    return math.fsum(metric_value(metric, rank, k) for rank in ranks) / len(ranks)


def value_matrices(
    ranks: Mapping[str, Mapping[str, Sequence[Rank]]],
    players: Mapping[str, Sequence[str]],
    columns: Sequence[Column],
) -> dict[str, np.ndarray]:
    """Per stratum, the per-user values as an array columns × users, users in the given order.

    ranks[method][player_id] holds the hidden game's rank of each rep; every method needs the same
    number of reps for a player, so that the methods are compared on the same evaluations."""
    methods = sorted({method for method, _, _ in columns})
    matrices = {}
    for name, player_ids in players.items():
        for player_id in player_ids:
            reps = {len(ranks[method].get(player_id, ())) for method in methods}
            if len(reps) != 1 or 0 in reps:
                raise ValueError(f"the methods do not share the reps of player {player_id}")
        rows = [
            [user_value(ranks[method][player_id], metric, k) for player_id in player_ids]
            for method, metric, k in columns
        ]
        matrices[name] = np.array(rows, dtype=np.float64).reshape(len(columns), len(player_ids))
    return matrices


def bootstrap_means(
    values: np.ndarray, replicates: int, rng: np.random.Generator, chunk_elements: int = CHUNK_ELEMENTS
) -> np.ndarray:
    """Mean of every row of values (columns × users) over `replicates` resamples of the users, as an
    array columns × replicates. The resamples are the rows of one replicates × users index matrix."""
    columns, users = values.shape
    means = np.empty((columns, replicates))
    rows = max(1, chunk_elements // (users * (columns + 1)))
    for start in range(0, replicates, rows):
        stop = min(start + rows, replicates)
        index = rng.integers(0, users, size=(stop - start, users))
        means[:, start:stop] = values[:, index].mean(axis=2)
    return means


def interval(replicates: np.ndarray, level: float = LEVEL) -> tuple[float, float]:
    """Percentile interval of the replicates."""
    tail = (1 - level) / 2
    lo, hi = np.quantile(replicates, [tail, 1 - tail])
    return float(lo), float(hi)


def bootstrap_p_value(differences: np.ndarray, null: float = 0.0) -> float:
    """Two-sided bootstrap p-value of 'difference = null': twice the smaller share of replicates on one
    side of null (null itself, within P_VALUE_TOLERANCE, counts on both sides), with the +1 of a Monte
    Carlo test."""
    below = int(np.count_nonzero(differences <= null + P_VALUE_TOLERANCE))
    above = int(np.count_nonzero(differences >= null - P_VALUE_TOLERANCE))
    return min(1.0, 2 * (min(below, above) + 1) / (len(differences) + 1))


def holm(p_values: Mapping[str, float]) -> dict[str, float]:
    """Holm's step-down adjusted p-values of a family of tests: reject a test at level α when its
    adjusted p-value is at most α."""
    order = sorted(p_values, key=lambda name: p_values[name])
    adjusted: dict[str, float] = {}
    running = 0.0
    for position, name in enumerate(order):
        running = max(running, min(1.0, (len(order) - position) * p_values[name]))
        adjusted[name] = running
    return {name: adjusted[name] for name in p_values}


class PairedBootstrap:
    """Post-stratified estimates of every column and their paired, stratified bootstrap replicates."""

    def __init__(
        self,
        columns: Sequence[Column],
        values: Mapping[str, np.ndarray],
        pool_counts: Mapping[str, int],
        replicates: int = REPLICATES,
        seed: int = SEED,
        chunk_elements: int = CHUNK_ELEMENTS,
    ) -> None:
        if replicates < 2:
            raise ValueError("the bootstrap needs at least 2 replicates")
        self.columns = list(columns)
        self.position = {column: index for index, column in enumerate(self.columns)}
        if len(self.position) != len(self.columns):
            raise ValueError("columns repeat")
        unknown = sorted(set(values) - set(STRATA))
        if unknown:
            raise ValueError(f"unknown strata {unknown}")
        self.pool_counts = {name: pool_counts[name] for name in STRATA}
        self.replicates = replicates
        self.seed = seed
        self.users: dict[str, int] = {}
        self.means: dict[str, np.ndarray] = {}
        self.replicate_means: dict[str, np.ndarray] = {}
        rng = np.random.default_rng(seed)
        for name in STRATA:
            matrix = np.asarray(values.get(name, np.empty((len(self.columns), 0))), dtype=np.float64)
            if matrix.shape[0] != len(self.columns):
                raise ValueError(f"stratum {name} has {matrix.shape[0]} rows for {len(self.columns)} columns")
            self.users[name] = matrix.shape[1]
            if matrix.shape[1]:
                self.means[name] = matrix.mean(axis=1)
                self.replicate_means[name] = bootstrap_means(matrix, replicates, rng, chunk_elements)

    def weights(self, group: str) -> dict[str, float]:
        strata = GROUPS[group]
        empty = [name for name in strata if not self.users[name]]
        if empty:
            raise ValueError(f"group {group} has no users in stratum {', '.join(empty)}")
        total = sum(self.pool_counts[name] for name in strata)
        if total <= 0:
            raise ValueError(f"group {group} has no test-pool players")
        return {name: self.pool_counts[name] / total for name in strata}

    def n_users(self, group: str) -> int:
        return sum(self.users[name] for name in GROUPS[group])

    def estimate(self, column: Column, group: str) -> float:
        index = self.position[column]
        total = 0.0
        for name, weight in self.weights(group).items():
            total += weight * float(self.means[name][index])
        return total

    def distribution(self, column: Column, group: str) -> np.ndarray:
        """The B bootstrap replicates of the column's post-stratified estimate."""
        index = self.position[column]
        total = np.zeros(self.replicates)
        for name, weight in self.weights(group).items():
            total += weight * self.replicate_means[name][index]
        return total

    def summary(self, column: Column, group: str, level: float = LEVEL) -> dict[str, Any]:
        replicates = self.distribution(column, group)
        lo, hi = interval(replicates, level)
        return {
            "estimate": self.estimate(column, group),
            "lo": lo,
            "hi": hi,
            "se": float(np.std(replicates, ddof=1)),
            "level": level,
            "n_users": self.n_users(group),
            "B": self.replicates,
            "seed": self.seed,
        }

    def contrast(self, first: Column, second: Column, group: str, level: float = LEVEL) -> dict[str, Any]:
        """first - second, paired: both columns are resampled with the same index matrices."""
        differences = self.distribution(first, group) - self.distribution(second, group)
        lo, hi = interval(differences, level)
        return {
            "delta": self.estimate(first, group) - self.estimate(second, group),
            "lo": lo,
            "hi": hi,
            "se": float(np.std(differences, ddof=1)),
            "p": bootstrap_p_value(differences),
            "level": level,
            "n_users": self.n_users(group),
            "B": self.replicates,
            "seed": self.seed,
        }


def paired_bootstrap(
    ranks: Mapping[str, Mapping[str, Sequence[Rank]]],
    players: Mapping[str, Sequence[str]],
    pool_counts: Mapping[str, int],
    metrics: Sequence[str] = METRICS,
    ks: Sequence[int] = KS,
    replicates: int = REPLICATES,
    seed: int = SEED,
) -> PairedBootstrap:
    """Every method of ranks × metric × K, over the players of each stratum (players[stratum])."""
    columns = [(method, metric, k) for method in sorted(ranks) for metric in metrics for k in ks]
    return PairedBootstrap(columns, value_matrices(ranks, players, columns), pool_counts, replicates, seed)


def estimate_rows(
    bootstrap: PairedBootstrap,
    methods: Sequence[str],
    metrics: Sequence[str] = METRICS,
    ks: Sequence[int] = KS,
    groups: Sequence[str] = tuple(GROUPS),
    level: float = LEVEL,
) -> list[dict[str, Any]]:
    """One row {method, metric, k, group, estimate, lo, hi, se, level, n_users, B, seed} per method,
    metric, K and group."""
    return [
        {"method": method, "metric": metric, "k": k, "group": group}
        | bootstrap.summary((method, metric, k), group, level)
        for method in methods
        for metric in metrics
        for k in ks
        for group in groups
    ]


def contrast_rows(
    bootstrap: PairedBootstrap,
    contrasts: Sequence[tuple[str, str]],
    metrics: Sequence[str] = METRICS,
    ks: Sequence[int] = KS,
    groups: Sequence[str] = tuple(GROUPS),
    level: float = LEVEL,
) -> list[dict[str, Any]]:
    """One row {contrast, metric, k, group, delta, lo, hi, se, p, level, n_users, B, seed} per contrast
    (first, second), metric, K and group; delta = first - second."""
    return [
        {"contrast": f"{first} - {second}", "metric": metric, "k": k, "group": group}
        | bootstrap.contrast((first, metric, k), (second, metric, k), group, level)
        for first, second in contrasts
        for metric in metrics
        for k in ks
        for group in groups
    ]
