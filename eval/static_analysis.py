"""What the static evaluation reports besides the estimates: the claims C1-C5 of the plan, the
descriptive groups of hidden games and the beyond-accuracy metrics.

Ranks are 1-based positions of the hidden game; with a cutoff (the largest K), two ranks agree when
they are equal or both above the cutoff.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .metrics import coverage_at_k, diversity_at_k, novelty_at_k
from .static_cache import Case, Key
from .static_split import STRATA
from .static_stats import PairedBootstrap, holm, metric_value

Ranked = Mapping[Key, tuple[int | None, list[str] | None]]  # (rank, rep-0 top list) per user x rep


def capped(rank: int | None, cutoff: int) -> int | None:
    return rank if rank is not None and rank <= cutoff else None


def agreement(first: Mapping[Key, int | None], second: Mapping[Key, int | None], cutoff: int) -> dict[str, Any]:
    keys = list(first)
    disagreeing = [key for key in keys if capped(first[key], cutoff) != capped(second[key], cutoff)]
    return {
        "evaluations": len(keys),
        "agreeing": len(keys) - len(disagreeing),
        "agreement": (len(keys) - len(disagreeing)) / len(keys),
        "same_rank": sum(first[key] == second[key] for key in keys),
        "disagreements": [
            {"player_id": key[0], "rep": key[1], "rank": first[key], "baseline_rank": second[key]}
            for key in disagreeing[:100]
        ],
    }


def c1_claim(
    claim: Mapping[str, Any],
    graph_ranks: Mapping[str, Mapping[Key, int]],
    baselines: Mapping[str, Mapping[Key, int]],
    cutoff: int,
) -> dict[str, Any]:
    """graph_ranks[check]: the check's graph config; it must agree with its baseline on enough user x reps."""
    checks = {
        name: {"baseline": check["baseline"], **agreement(graph_ranks[name], baselines[check["baseline"]], cutoff)}
        for name, check in claim["checks"].items()
    }
    passed = all(check["agreement"] >= claim["threshold"] for check in checks.values())
    return {"threshold": claim["threshold"], "cutoff": cutoff, "checks": checks, "status": "pass" if passed else "fail"}


def contrast_claims(claims: Mapping[str, Any], bootstrap: PairedBootstrap) -> dict[str, Any]:
    """C2, C3 and C4 from the paired bootstrap of the test rows."""

    def contrast(pair: Sequence[str], metric: str, k: int, group: str, level: float) -> dict[str, Any]:
        first, second = pair
        return {"contrast": f"{first} - {second}", "group": group} | bootstrap.contrast(
            (first, metric, k), (second, metric, k), group, level
        )

    c2 = claims["C2"]
    row = contrast(c2["contrast"], c2["metric"], c2["k"], c2["group"], c2["level"])
    equivalent = row["lo"] >= -c2["margin"] and row["hi"] <= c2["margin"]

    c3 = claims["C3"]
    tests = {
        role: {
            "ci_975": contrast(c3["contrast"], c3["metric"], c3["k"], group, c3["ci_level"]),
            "ci_95": contrast(c3["contrast"], c3["metric"], c3["k"], group, 0.95),
        }
        for role, group in c3["tests"].items()
    }
    adjusted = holm({role: test["ci_975"]["p"] for role, test in tests.items()})
    for role, test in tests.items():
        test["p_holm"] = adjusted[role]
        test["status"] = "pass" if adjusted[role] <= c3["alpha"] and test["ci_975"]["delta"] > 0 else "fail"

    c4 = claims["C4"]
    checks = []
    for check in c4["checks"]:
        row4 = contrast(check["contrast"], c4["metric"], c4["k"], c4["group"], c4["level"])
        passed = row4["lo"] > check["lower_bound_above"]
        checks.append(row4 | {"lower_bound_above": check["lower_bound_above"], "status": "pass" if passed else "fail"})
    return {
        "C2": {"margin": c2["margin"], **row, "status": "pass" if equivalent else "fail"},
        "C3": {"alpha": c3["alpha"], "tests": tests, "status": tests["primary"]["status"]},
        "C4": {"checks": checks, "status": "pass" if all(check["status"] == "pass" for check in checks) else "fail"},
    }


def c5_claim(path: Path, source: str, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Per-recommendation evidence validity from eval/explanation_check.py's result, once it exists.
    expected: the split_sha256, k, code_defaults and links_sha256 the result must have been made with; a
    result made otherwise, on part of the subset or from uncommitted code gives an error, not a verdict."""
    path = Path(path)
    if not path.exists():
        return {"source": source, "status": "pending"}
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        current = data["versions"]["current"]["recommendation_validity"]
        v0 = data["versions"]["v0"]
        found = {"current": current, "v0": v0["recommendation_validity"], "v0_ci": v0["recommendation_validity_ci"]}
    except (KeyError, TypeError) as error:
        return {"source": source, "status": "error", "error": f"missing {error}"}
    problems = c5_problems(data, expected) if expected is not None else []
    if problems:
        return {"source": source, "status": "error", "error": "; ".join(problems)}
    if v0.get("recommendation_validity_post_stratified") is not None:
        found["v0_post_stratified"] = v0["recommendation_validity_post_stratified"]
        found["v0_post_stratified_ci"] = v0["recommendation_validity_post_stratified_ci"]
    return {"source": source, **found, "status": "pass" if current == 1.0 else "fail"}


def c5_problems(data: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """Why the explanation result is not the one C5 asks for: the whole explanations subset of this split
    at rep 0, k, the code defaults and the tag and studio links of the test run, committed code."""
    users_from, problems = data.get("users_from") or {}, []
    if users_from.get("split_sha256") != expected["split_sha256"]:
        problems.append("made from another split")
    if (users_from.get("subset"), users_from.get("rep"), users_from.get("limit_per_stratum")) != ("explanations", 0, None):
        problems.append("not the whole explanations subset at rep 0")
    if data.get("k") != expected["k"]:
        problems.append(f"k = {data.get('k')}, not {expected['k']}")
    if data.get("code_defaults") != expected["code_defaults"]:
        problems.append("made with other code defaults than the test run")
    if (data.get("graph") or {}).get("links_sha256") != expected["links_sha256"]:
        problems.append("made on other tag or studio links")
    if (data.get("git") or {}).get("dirty") is not False:
        problems.append("made from uncommitted code")
    return problems


def descriptive_groups(
    descriptive: Mapping[str, Any],
    bootstrap_plan: Mapping[str, Any],
    ranks: Mapping[str, Mapping[str, list[int | None]]],
    players: Mapping[str, list[str]],
    cases: Sequence[Case],
    hours: Mapping[tuple[str, str], float],
    studio_games: set[str],
    pool_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Every row on the user x reps whose hidden game is in a group: per user, the metric summed over the
    user's reps in the group and the share of reps in it, each divided by the user's reps; the estimate
    is the ratio of their post-stratified means, with the same paired bootstrap for the CIs."""
    threshold = descriptive["hours_threshold"]
    members: dict[str, Callable[[str, str], bool]] = {
        "hidden_under_1h": lambda player_id, appid: hours[(player_id, appid)] < threshold,
        "hidden_1h_or_more": lambda player_id, appid: hours[(player_id, appid)] >= threshold,
        "hidden_valve": lambda player_id, appid: appid in studio_games,
        "hidden_not_valve": lambda player_id, appid: appid not in studio_games,
    }
    hidden: dict[str, list[str]] = {}
    for player_id, _, _, appid in cases:
        hidden.setdefault(player_id, []).append(appid)
    measures = [(metric, k) for metric, k in descriptive["metrics"]]
    specs = [(group, None, "share", 0) for group in members] + [
        (group, row, metric, k) for group in members for row in ranks for metric, k in measures
    ]

    def user_value(group: str, row: str | None, metric: str, k: int, player_id: str) -> float:
        total = 0.0
        for rep, appid in enumerate(hidden[player_id]):
            if members[group](player_id, appid):
                total += 1.0 if row is None else metric_value(metric, ranks[row][player_id][rep], k)
        return total / len(hidden[player_id])

    columns = [(f"{row}|{group}" if row else group, metric, k) for group, row, metric, k in specs]
    values = {
        name: np.array(
            [[user_value(*spec, player_id) for player_id in players[name]] for spec in specs], dtype=np.float64
        ).reshape(len(specs), len(players[name]))
        for name in STRATA
    }
    bootstrap = PairedBootstrap(columns, values, pool_counts, bootstrap_plan["B"], bootstrap_plan["seed"])
    level = bootstrap_plan["level"]
    tail = (1 - level) / 2

    def ratio(estimate: float, replicates: np.ndarray, share: float, shares: np.ndarray) -> dict[str, float]:
        with np.errstate(divide="ignore", invalid="ignore"):
            lo, hi = np.quantile(replicates / shares, [tail, 1 - tail])
        return {"estimate": estimate / share, "lo": float(lo), "hi": float(hi)}

    groups = {}
    for group in members:
        evaluations = sum(members[group](player_id, appid) for player_id, _, _, appid in cases)
        groups[group] = {"definition": descriptive["groups"][group], "evaluations": evaluations}
        if not evaluations:
            continue
        share = bootstrap.estimate((group, "share", 0), "all")
        shares = bootstrap.distribution((group, "share", 0), "all")

        def column(row: str, metric: str, k: int) -> tuple[float, np.ndarray]:
            name = (f"{row}|{group}", metric, k)
            return bootstrap.estimate(name, "all"), bootstrap.distribution(name, "all")

        metric, k = measures[0]
        contrasts = {}
        for first, second in descriptive["contrasts"]:
            first_estimate, first_replicates = column(first, metric, k)
            second_estimate, second_replicates = column(second, metric, k)
            difference = ratio(first_estimate - second_estimate, first_replicates - second_replicates, share, shares)
            contrasts[f"{first} - {second}"] = difference | {"metric": f"{metric}@{k}"}
        groups[group] |= {
            "share": share,
            "estimates": {
                row: {f"{metric}@{k}": ratio(*column(row, metric, k), share, shares) for metric, k in measures}
                for row in ranks
            },
            "contrasts": contrasts,
        }
    return {"level": level, "groups": groups}


def beyond_accuracy(
    settings: Mapping[str, Any],
    results: Mapping[str, Ranked],
    cases: Sequence[Case],
    tag_ids_by_game: Mapping[str, set[str]],
    counts: Counter,
    player_total: int,
) -> dict[str, Any]:
    """Coverage, diversity, novelty and the popular head of every row's rep-0 top list on the cases."""
    k, denominator, head_size = settings["k"], settings["coverage"]["denominator"], settings["popular_head"]["size"]
    head = {appid for appid, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:head_size]}
    rows = {}
    for row, ranked in results.items():
        tops = [ranked[(player_id, rep)][1][:k] for player_id, _, rep, _ in cases]
        distinct = {appid for top in tops for appid in top}
        rows[row] = {
            "users": len(tops),
            "distinct": len(distinct),
            "coverage": coverage_at_k(tops, denominator, k),
            "never_played": len(distinct - set(counts)),
            "diversity": sum(diversity_at_k(top, tag_ids_by_game, k) for top in tops) / len(tops),
            "novelty": sum(novelty_at_k(top, counts, player_total, k) for top in tops) / len(tops),
            "popular_head_share": sum(appid in head for top in tops for appid in top) / sum(len(top) for top in tops),
        }
    return {
        "k": k,
        "coverage_denominator": denominator,
        "popular_head": head_size,
        "player_total": player_total,
        "rows": rows,
    }
