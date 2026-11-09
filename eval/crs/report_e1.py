"""The E1 report: the plan's tests over the anchor layers, read from finished run_e1 outputs (E1_PLAN.md, 統計).

    python -m eval.crs.report_e1 --similar <run> --co-play <run> --same-developer <run> [--none <run>]

Each <run> is a run_e1 output directory of that layer. The report gives:
  - main: G vs V-entropy in SR@5 (with the anchor in an anchor layer), in every run given.
  - graph effect: in the similar and co-play layers, G's gain from the anchor minus V-entropy's, in hDCG and
    SR@1. The four tests are Holm-adjusted together.
  - same developer: G vs V-entropy with the anchor, in hDCG and SR@1, Holm-adjusted together. G reaches the
    ceiling in this layer, so its gain says little.
  - by series and by popularity: the same effects within same-series and other anchors, and within popular
    and long-tail targets. These are descriptive (no Holm) and labelled as such.
Users are the unit: each value is a user's mean over seeds, with a paired bootstrap interval and Wilcoxon's p.
Writes the report as JSON under data/eval/e1_reports/ and prints it.
"""

import argparse
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from eval.crs.run_e1 import METRICS, code_sha256, git_commit
from eval.stats import holm, paired_bootstrap_ci, wilcoxon_signed_rank

OUT_ROOT = Path("data/eval/e1_reports")
EFFECT_METRICS = ("hDCG@(5,10)", "SR@1")
SECONDARY_METRICS = ("SR@3", "SR@5")
PAIR = ("G", "V-entropy")
GAIN_LAYERS = ("similar", "co_play")


def load(run: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((run / "manifest.json").read_text())
    dialogues = [json.loads(line) for line in (run / "dialogues.jsonl").open(encoding="utf-8")]
    return manifest, dialogues


def user_means(dialogues: list[dict], group: str, metric: str, anchored: bool) -> dict[str, float]:
    """Per user, the mean over seeds of one metric, for dialogues with or without the anchor."""
    values: dict[str, list[float]] = {}
    for d in dialogues:
        if d["group"] == group and bool(d.get("anchored", False)) == anchored:
            values.setdefault(d["player_id"], []).append(METRICS[metric](d))
    return {player: sum(v) / len(v) for player, v in values.items()}


def test(differences: list[float]) -> dict:
    if not differences:
        return {"users": 0, "mean": None, "ci95": None, "wilcoxon_p": None}
    _, p = wilcoxon_signed_rank(differences)
    low, high = paired_bootstrap_ci(differences)
    return {"users": len(differences), "mean": sum(differences) / len(differences), "ci95": [low, high],
            "wilcoxon_p": p}


def graph_effect(dialogues: list[dict], metric: str) -> dict:
    """G's gain from naming the anchor minus V-entropy's, per user."""
    gains = []
    for group in PAIR:
        with_anchor = user_means(dialogues, group, metric, anchored=True)
        without = user_means(dialogues, group, metric, anchored=False)
        gains.append({p: with_anchor[p] - without[p] for p in with_anchor if p in without})
    g, v = gains
    return test([g[p] - v[p] for p in sorted(g) if p in v])


def anchored_gap(dialogues: list[dict], metric: str) -> dict:
    """G minus V-entropy, both with the anchor, per user."""
    g = user_means(dialogues, PAIR[0], metric, anchored=True)
    v = user_means(dialogues, PAIR[1], metric, anchored=True)
    return test([g[p] - v[p] for p in sorted(g) if p in v])


def subsets(dialogues: list[dict]) -> dict[str, list[dict]]:
    """Descriptive strata: same-series anchors or not, popular target or long tail."""
    return {
        "same_series": [d for d in dialogues if d.get("same_series") is True],
        "other_series": [d for d in dialogues if d.get("same_series") is False],
        "popular": [d for d in dialogues if d["popular"]],
        "long_tail": [d for d in dialogues if not d["popular"]],
    }


def with_holm(tests: dict[str, dict]) -> dict[str, dict]:
    adjusted = holm({name: t["wilcoxon_p"] for name, t in tests.items() if t["wilcoxon_p"] is not None})
    return {name: {**t, "holm_p": adjusted.get(name)} for name, t in tests.items()}


def family(runs: dict[str, list[dict]], layers: tuple[str, ...], effect: Callable[[list[dict], str], dict]) -> dict:
    return with_holm({f"{layer} {metric}": effect(runs[layer], metric)
                      for layer in layers if layer in runs for metric in EFFECT_METRICS})


def report(runs: dict[str, list[dict]]) -> dict:
    result: dict = {"main": {}, "graph_effect": {}, "same_developer": {}, "descriptive": {}}
    for layer, dialogues in runs.items():
        anchored = layer != "none"
        g = user_means(dialogues, PAIR[0], "SR@5", anchored)
        v = user_means(dialogues, PAIR[1], "SR@5", anchored)
        result["main"][layer] = test([g[p] - v[p] for p in sorted(g) if p in v])
    result["graph_effect"] = family(runs, GAIN_LAYERS, graph_effect)
    result["same_developer"] = family(runs, ("same_developer",), anchored_gap)
    for layer, dialogues in runs.items():
        if layer == "none":
            continue
        effect = anchored_gap if layer == "same_developer" else graph_effect
        rows = {}
        for stratum, items in subsets(dialogues).items():
            for metric in EFFECT_METRICS + SECONDARY_METRICS:
                rows[f"{stratum} {metric}"] = effect(items, metric)
        for metric in SECONDARY_METRICS:
            rows[f"all {metric}"] = effect(dialogues, metric)
        result["descriptive"][layer] = rows
    return result


def fmt(t: dict) -> str:
    if t["mean"] is None:
        return "n/a"
    low, high = t["ci95"]
    holm_p = f" Holm {t['holm_p']:.4f}" if t.get("holm_p") is not None else ""
    return f"{t['mean']:+.3f} [{low:+.3f}, {high:+.3f}] p {t['wilcoxon_p']:.4f}{holm_p} (n={t['users']})"


def print_report(result: dict) -> None:
    print("main: G vs V-entropy, SR@5 (with the anchor in anchor layers)")
    for layer, t in result["main"].items():
        print(f"  {layer:16s} {fmt(t)}")
    print("\ngraph effect: G's gain from the anchor minus V-entropy's (Holm over these)")
    for name, t in result["graph_effect"].items():
        print(f"  {name:24s} {fmt(t)}")
    print("\nsame developer: G vs V-entropy with the anchor (Holm over these)")
    for name, t in result["same_developer"].items():
        print(f"  {name:24s} {fmt(t)}")
    print("\ndescriptive (no correction)")
    for layer, rows in result["descriptive"].items():
        print(f"  [{layer}]")
        for name, t in rows.items():
            print(f"    {name:26s} {fmt(t)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--similar", type=Path)
    parser.add_argument("--co-play", type=Path)
    parser.add_argument("--same-developer", type=Path)
    parser.add_argument("--none", type=Path, help="a run without anchors, for the main comparison only")
    args = parser.parse_args()
    given = {"similar": args.similar, "co_play": args.co_play, "same_developer": args.same_developer,
             "none": args.none}
    runs, inputs, splits = {}, {}, set()
    for layer, path in given.items():
        if path is None:
            continue
        manifest, dialogues = load(path)
        if manifest.get("layer", "none") != layer:
            raise SystemExit(f"{path} is the {manifest.get('layer', 'none')} layer, not {layer}")
        splits.add(manifest["split"])
        runs[layer] = dialogues
        inputs[layer] = {"run": str(path), "split": manifest["split"], "users": manifest["users"],
                         "seeds": manifest["seeds"], "git_commit": manifest.get("git_commit"),
                         "error_rate": manifest.get("error_rate", 0.0)}
    if not runs:
        raise SystemExit(__doc__)
    if len(splits) > 1:
        raise SystemExit(f"runs from different splits: {sorted(splits)}")

    result = report(runs)
    result["inputs"] = inputs
    result["report"] = {"git_commit": git_commit(), "code_sha256": code_sha256(),
                        "made": datetime.now(timezone.utc).isoformat()}
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    out = OUT_ROOT / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{splits.pop()}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print_report(result)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
