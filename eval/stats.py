"""Paired tests and confidence intervals shared by the static and the dialogue evaluations. Standard library only.

- wilcoxon_signed_rank: two-sided, zero differences dropped (Wilcoxon's method), average ranks for ties, normal
  approximation with tie and continuity corrections. Meant for the hundreds of users of an evaluation.
- mcnemar_exact: two-sided exact binomial test on the discordant pairs.
- paired_bootstrap_ci: percentile interval of the mean difference, resampling units (users) with replacement.
- holm: Holm's step-down adjustment of several p-values.
"""

import math
import random


def _normal_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def wilcoxon_signed_rank(differences: list[float]) -> tuple[float, float]:
    """(W+, p) for paired differences a - b."""
    nonzero = [d for d in differences if d != 0]
    n = len(nonzero)
    if n == 0:
        return 0.0, 1.0
    order = sorted(range(n), key=lambda i: abs(nonzero[i]))
    ranks = [0.0] * n
    tie_term = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(nonzero[order[j + 1]]) == abs(nonzero[order[i]]):
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        size = j - i + 1
        tie_term += size**3 - size
        i = j + 1
    w_plus = sum(rank for rank, d in zip(ranks, nonzero) if d > 0)
    mean = n * (n + 1) / 4
    variance = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if variance <= 0:
        return w_plus, 1.0
    deviation = max(abs(w_plus - mean) - 0.5, 0.0)
    return w_plus, _normal_two_sided(deviation / math.sqrt(variance))


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """p for pairs where only a succeeded (`only_a`) against pairs where only b did (`only_b`)."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_bootstrap_ci(
    differences: list[float], resamples: int = 10_000, level: float = 0.95, seed: int = 0
) -> tuple[float, float]:
    if not differences:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(differences)
    means = sorted(sum(rng.choices(differences, k=n)) / n for _ in range(resamples))
    low = means[int((1 - level) / 2 * resamples)]
    high = means[min(resamples - 1, int((1 + level) / 2 * resamples))]
    return low, high


def holm(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values, key=lambda name: p_values[name])
    adjusted: dict[str, float] = {}
    running = 0.0
    for position, name in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - position) * p_values[name]))
        adjusted[name] = running
    return adjusted
