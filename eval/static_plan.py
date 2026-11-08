"""What eval/results/static_plan_v1.json defines for the static evaluation: the stages and their
users, the seed schemes, the λ grid and the reference config, the selection rule and the test rows."""

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.api.static_recommend import PATHS, SeedScheme

from .static_cache import V0, Case, Spec
from .static_split import STRATA, players_by_stratum, user_key

SMOKE_USERS = 30
ROW_NAMES = (
    "popularity",
    "item_knn",
    "graph_v0",
    "graph_v1_default",
    "graph_tuned",
    "graph_tuned_minus_peer",
    "graph_tuned_minus_tag",
    "graph_tuned_minus_studio",
    "graph_tuned_minus_pop",
    "peer_only",
    "content_only",
    "pop_only",
)

Config = dict[str, Any]  # {"scheme": name, "weights": {path: λ}, "studio_normalization": ...}


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    names = tuple(row["name"] for row in plan["test_rows"])
    if names != ROW_NAMES:
        raise SystemExit(f"{path}: the test rows {names} are not the runner's {ROW_NAMES}")
    if len(grid_configs(plan)) != plan["grid"]["configs"]:
        raise SystemExit(f"{path}: the grid does not have {plan['grid']['configs']} configs")
    return plan


def plan_schemes(plan: Mapping[str, Any]) -> dict[str, SeedScheme]:
    return {name: SeedScheme(spec["weighting"], spec["peer_limit"]) for name, spec in plan["schemes"].items()}


def stage_players(split: Mapping[str, Any], stage: str) -> dict[str, list[str]]:
    """Players per stratum in user-key order."""
    if stage == "smoke":
        validation = players_by_stratum(split, "validation")
        everyone = [player_id for name in STRATA for player_id in validation[name]]
        chosen = set(sorted(everyone, key=user_key)[:SMOKE_USERS])
        return {name: [player_id for player_id in validation[name] if player_id in chosen] for name in STRATA}
    if stage not in ("validation", "test"):
        raise SystemExit(f"unknown stage {stage!r}")
    return players_by_stratum(split, "validation" if stage == "validation" else "test_sample")


def subset_players(split: Mapping[str, Any], stage: str, subset: str) -> dict[str, list[str]]:
    """A subset of the test sample; the smoke stage uses all of its players for every subset."""
    return stage_players(split, "smoke") if stage == "smoke" else players_by_stratum(split, subset)


def v0_players(split: Mapping[str, Any], stage: str) -> dict[str, list[str]]:
    """v0 runs on the v0 subset and on the beyond_accuracy subset."""
    if stage == "validation":
        raise SystemExit("v0 runs on the test stage only")
    wanted = {
        name: set(subset_players(split, stage, "v0")[name]) | set(subset_players(split, stage, "beyond_accuracy")[name])
        for name in STRATA
    }
    sample = stage_players(split, stage)
    return {name: [player_id for player_id in sample[name] if player_id in wanted[name]] for name in STRATA}


def stage_cases(
    split: Mapping[str, Any], players: Mapping[str, list[str]], reps: Sequence[int] | None = None
) -> list[Case]:
    """(player, stratum, rep, hidden appid) of the players, in the split's order."""
    hidden = {
        record["player_id"]: record["hidden"]
        for part in ("validation", "test_sample")
        for name in STRATA
        for record in split[part][name]
    }
    return [
        (player_id, name, rep, appid)
        for name in STRATA
        for player_id in players[name]
        for rep, appid in enumerate(hidden[player_id])
        if reps is None or rep in reps
    ]


def collect_targets(plan: Mapping[str, Any], names: Sequence[str]) -> dict[str, SeedScheme | None]:
    schemes = plan_schemes(plan)
    targets: dict[str, SeedScheme | None] = {}
    for name in names:
        if name == "all":
            targets.update({scheme: schemes[scheme] for scheme in plan["grid"]["schemes"]})
        elif name == V0:
            targets[V0] = None
        elif name in schemes:
            targets[name] = schemes[name]
        else:
            raise SystemExit(f"unknown scheme {name!r}; the plan has {', '.join(schemes)} (and v0)")
    return targets


def make_config(scheme: str, weights: Mapping[str, float], studio_normalization: str) -> Config:
    return {
        "scheme": scheme,
        "weights": {path: float(weights.get(path, 0.0)) for path in PATHS},
        "studio_normalization": studio_normalization,
    }


def config_id(config: Config) -> str:
    weights = " ".join(f"{path}={config['weights'][path]:g}" for path in PATHS)
    return f"{config['scheme']} {weights} {config['studio_normalization']}"


def spec(name: str, config: Config) -> Spec:
    return name, config["weights"], config["studio_normalization"]


def grid_configs(plan: Mapping[str, Any], schemes: Sequence[str] | None = None) -> list[Config]:
    grid = plan["grid"]
    values = grid["weights"]
    normalization = grid["studio_normalization"]
    return [
        make_config(scheme, {"peer": peer, "tag": tag, "studio": studio, "popular": popular}, normalization)
        for scheme in (grid["schemes"] if schemes is None else schemes)
        for peer in values["peer"]
        for tag in values["tag"]
        for studio in values["studio"]
        for popular in values["popular"]
    ]


def reference_config(plan: Mapping[str, Any]) -> Config:
    reference = plan["reference"]
    return make_config(reference["scheme"], reference["weights"], reference["studio_normalization"])


def c1_configs(plan: Mapping[str, Any]) -> dict[str, Config]:
    normalization = plan["grid"]["studio_normalization"]
    return {
        name: make_config(check["scheme"], check["weights"], normalization)
        for name, check in plan["claims"]["C1"]["checks"].items()
    }


def preference(config: Config, schemes: Mapping[str, SeedScheme]) -> tuple[Any, ...]:
    """The selection rule's order: peer_limit 10, 30, all; log_hours before uniform; more λ = 0 among
    tag, studio and popular; smaller Σλ; smaller (λ_tag, λ_studio, λ_pop)."""
    scheme = schemes[config["scheme"]]
    others = tuple(config["weights"][path] for path in ("tag", "studio", "popular"))
    return (
        scheme.peer_limit is None,
        scheme.peer_limit or 0,
        scheme.weighting != "log_hours",
        -others.count(0.0),
        round(sum(config["weights"].values()), 9),
        others,
    )


def select(
    configs: Sequence[Config],
    objective: Mapping[str, float],
    se_to_best: Callable[[Config, Config], float],
    schemes: Mapping[str, SeedScheme],
) -> tuple[Config, Config]:
    """The arg-max and the chosen config: the first in preference order whose difference to the
    arg-max is within one bootstrap SE of that difference."""
    best = min(configs, key=lambda config: (-objective[config_id(config)], preference(config, schemes)))
    top = objective[config_id(best)]
    qualifying = [config for config in configs if objective[config_id(config)] - top >= -se_to_best(config, best)]
    return best, min(qualifying, key=lambda config: preference(config, schemes))


def row_configs(chosen: Config, reference: Config) -> dict[str, Config]:
    """The configs of the test's graph rows, in plan order."""
    scheme, normalization, tuned = chosen["scheme"], chosen["studio_normalization"], chosen["weights"]

    def variant(**changes: float) -> Config:
        return make_config(scheme, {**tuned, **changes}, normalization)

    content = {"tag": tuned["tag"], "studio": tuned["studio"]}
    if not any(content.values()):
        content = {"tag": 1.0, "studio": 1.0}
    return {
        "graph_v1_default": reference,
        "graph_tuned": variant(),
        "graph_tuned_minus_peer": variant(peer=0.0),
        "graph_tuned_minus_tag": variant(tag=0.0),
        "graph_tuned_minus_studio": variant(studio=0.0),
        "graph_tuned_minus_pop": variant(popular=0.0),
        "peer_only": make_config(scheme, {"peer": 1.0}, normalization),
        "content_only": make_config(scheme, content, normalization),
        "pop_only": make_config(scheme, {"popular": 1.0}, normalization),
    }
