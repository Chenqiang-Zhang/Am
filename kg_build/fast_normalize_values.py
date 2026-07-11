"""Fast value normalization for recommendation-critical canonical types."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

from fast_normalize_recommendation import collect_stats
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call


VALUE_ONTOLOGY = {
    "platform": [
        "nintendo_switch", "wii_u", "wii", "nintendo_3ds", "nintendo_ds",
        "playstation_5", "playstation_4", "playstation_3", "playstation_2",
        "playstation_vita", "xbox_series", "xbox_one", "xbox_360", "pc",
        "mobile", "multi_platform", "other",
    ],
    "product_kind": [
        "game", "console", "controller", "accessory", "gift_card",
        "downloadable_content", "storage", "bundle", "other",
    ],
    "genre": [
        "action", "action_adventure", "adventure", "rpg", "jrpg", "strategy",
        "simulation", "sports", "racing", "fighting", "shooter", "fps",
        "platformer", "puzzle", "horror", "survival_horror", "party",
        "music", "educational", "other",
    ],
    "play_mode": [
        "single_player", "multiplayer", "co_op", "competitive", "campaign",
        "story_mode", "open_world", "sandbox", "arcade", "survival",
        "online", "local", "other",
    ],
    "multiplayer_type": [
        "single_player", "multiplayer", "local_multiplayer", "online_multiplayer",
        "co_op", "local_co_op", "online_co_op", "competitive", "other",
    ],
    "difficulty": ["easy", "normal", "hard", "very_hard", "adaptive", "other"],
    "online_support": ["supported", "required", "optional", "not_supported", "other"],
}

VALUE_DENY = {
    "platform": {"ps move", "wii_remote", "xbox"},
    "genre": {"resident evil", "final fantasy", "gta", "call of duty", "mario kart", "metroid"},
    "online_support": {"broken", "disconnected", "disconnects"},
}

VALUE_OVERRIDES = {
    "platform": {"nintendo_dsi": "nintendo_ds"},
    "genre": {"action/rpg": "rpg"},
    "play_mode": {"2 player": "multiplayer", "online multiplayer": "online"},
    "multiplayer_type": {
        "2 player": "multiplayer", "2 players": "multiplayer", "two player": "multiplayer",
        "two_player": "multiplayer", "4 player": "multiplayer", "4 players": "multiplayer",
        "4_player": "multiplayer", "4-player": "multiplayer",
        "playing with friends": "multiplayer",
    },
}

SYSTEM = """\
Normalize a value for one recommendation attribute into the supplied fixed vocabulary.
Use other for opinions, vague praise/complaints, unrelated concepts, or values that do not
represent the attribute. Examples: multiplayer=fun -> other; platform=ps4 -> playstation_4;
genre=action-adventure-game-genre -> action_adventure. Return every input item.
"""


def schema(values: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "raw": {"type": "string"},
                        "canonical": {"type": "string", "enum": values},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["raw", "canonical", "confidence"],
                },
            }
        },
        "required": ["classifications"],
    }


def normalized_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def safe_value_rule(canonical_type: str, value: str) -> str | None:
    token = normalized_token(value)
    if canonical_type == "platform":
        checks = [
            (("switch",), "nintendo_switch"), (("wii_u", "wiiu"), "wii_u"),
            (("wii",), "wii"), (("3ds", "nintendo_3ds"), "nintendo_3ds"),
            (("nintendo_ds", "nds"), "nintendo_ds"), (("ps5", "playstation_5"), "playstation_5"),
            (("ps4", "playstation_4"), "playstation_4"), (("ps3", "playstation_3"), "playstation_3"),
            (("ps2", "playstation_2"), "playstation_2"), (("vita",), "playstation_vita"),
            (("xbox_series",), "xbox_series"), (("xbox_one", "xboxone", "xbone"), "xbox_one"),
            (("xbox_360", "xbox360"), "xbox_360"),
            (("pc", "windows", "mac", "steam"), "pc"), (("android", "ios", "mobile"), "mobile"),
        ]
        for aliases, result in checks:
            if token in aliases:
                return result
    elif canonical_type == "genre":
        aliases = {
            "action_adventure_game_genre": "action_adventure", "action_adventure": "action_adventure",
            "action_game_genre": "action", "role_playing_game_genre": "rpg",
            "first_person_shooter": "fps", "first_person_shooter_game_genre": "fps",
            "survival_horror": "survival_horror", "fighting_action_game_genre": "fighting",
        }
        if token in aliases:
            return aliases[token]
        if token in VALUE_ONTOLOGY["genre"]:
            return token
    elif canonical_type == "multiplayer_type":
        aliases = {
            "coop": "co_op", "co_op": "co_op", "cooperative": "co_op",
            "local_coop": "local_co_op", "local_co_op": "local_co_op", "couch_co_op": "local_co_op",
            "online_coop": "online_co_op", "online_co_op": "online_co_op",
            "local": "local_multiplayer", "online": "online_multiplayer",
            "competitive": "competitive", "single_player": "single_player", "multiplayer": "multiplayer",
        }
        if token in aliases:
            return aliases[token]
    elif canonical_type == "difficulty":
        aliases = {"beginner": "easy", "casual": "easy", "difficult": "hard", "challenging": "hard", "extreme": "very_hard", "dynamic": "adaptive"}
        if token in VALUE_ONTOLOGY["difficulty"]:
            return token
        if token in aliases:
            return aliases[token]
    elif canonical_type == "online_support":
        aliases = {"yes": "supported", "online": "supported", "true": "supported", "required": "required", "optional": "optional", "no": "not_supported", "false": "not_supported", "none": "not_supported"}
        if token in aliases:
            return aliases[token]
    elif canonical_type == "play_mode":
        aliases = {"story": "story_mode", "story_mode": "story_mode", "open_world": "open_world", "single_player": "single_player", "multiplayer": "multiplayer", "co_op": "co_op", "coop": "co_op"}
        if token in aliases:
            return aliases[token]
    elif canonical_type == "product_kind":
        aliases = {"video_game": "game", "computer_game": "game", "software_download": "downloadable_content", "dlc": "downloadable_content", "handheld_console": "console", "home_console": "console", "gamepad": "controller", "memory_card": "storage", "product_bundle": "bundle"}
        if token in aliases:
            return aliases[token]
    return None


def classify(client: Any, model: str, canonical_type: str, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data, _ = chat_json_call(
        client, model,
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"canonical_type": canonical_type, "allowed_values": VALUE_ONTOLOGY[canonical_type], "items": batch}, ensure_ascii=False)},
        ],
        max_output_tokens=1400, retries=2,
        response_schema=schema(VALUE_ONTOLOGY[canonical_type]),
        schema_name=f"normalize_{canonical_type}_values",
    )
    return data.get("classifications", []) if isinstance(data, dict) else []


def chunks(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    p.add_argument("--attr-map", type=Path, required=True)
    p.add_argument("--output-path", type=Path, required=True)
    p.add_argument("--model")
    p.add_argument("--max-values-per-type", type=int, default=120)
    p.add_argument("--min-count", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    root = args.config.resolve().parent
    attrs = root / cfg.get("data", {}).get("output_dir", "kg_output/video_games") / "attributes"
    stats = collect_stats(attrs / "product_attributes.jsonl", attrs / "review_mentions.jsonl")
    attr_data = json.loads(args.attr_map.read_text(encoding="utf-8"))
    attr_map: dict[str, str] = attr_data["attr_type_map"]

    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for raw_type, canonical_type in attr_map.items():
        if canonical_type in VALUE_ONTOLOGY:
            counts[canonical_type].update(stats[raw_type]["values"])

    rule_map: dict[str, dict[str, str]] = defaultdict(dict)
    tasks: list[tuple[str, list[dict[str, Any]]]] = []
    for canonical_type, value_counts in counts.items():
        candidates = [(v, n) for v, n in value_counts.most_common(args.max_values_per_type) if n >= args.min_count]
        unresolved = []
        for value, count in candidates:
            ruled = safe_value_rule(canonical_type, value)
            if ruled:
                if normalized_token(value) != ruled:
                    rule_map[canonical_type][value] = ruled
            else:
                unresolved.append({"raw": value, "count": count})
        tasks.extend((canonical_type, batch) for batch in chunks(unresolved, args.batch_size))

    llm_cfg = cfg.get("llm", {})
    provider, config_model, base_url = provider_from_config(llm_cfg)
    client, model = build_client(provider, args.model or config_model, base_url)
    llm_map: dict[str, dict[str, str]] = defaultdict(dict)
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(classify, client, model, t, batch): (t, batch) for t, batch in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            canonical_type, batch = futures[future]
            valid = {x["raw"] for x in batch}
            try:
                rows = future.result()
            except Exception as exc:
                failures += 1
                print(f"batch failed {canonical_type}: {exc}", file=sys.stderr)
                rows = []
            for row in rows:
                raw = str(row.get("raw", "")); canonical = str(row.get("canonical", "other"))
                confidence = float(row.get("confidence", 0))
                if raw in valid and canonical != "other" and confidence >= 0.85:
                    if normalized_token(raw) != canonical:
                        llm_map[canonical_type][raw] = canonical
            print(f"batches={done}/{len(tasks)} failures={failures}")

    value_map: dict[str, dict[str, str]] = {}
    for canonical_type in VALUE_ONTOLOGY:
        merged = {**rule_map.get(canonical_type, {}), **llm_map.get(canonical_type, {})}
        for raw in VALUE_DENY.get(canonical_type, set()):
            merged.pop(raw, None)
        merged.update(VALUE_OVERRIDES.get(canonical_type, {}))
        if merged:
            value_map[canonical_type] = merged
    output = {
        **attr_data,
        "value_ontology": VALUE_ONTOLOGY,
        "value_map": value_map,
        "value_normalization": {
            "rule_mappings": sum(map(len, rule_map.values())),
            "llm_mappings": sum(map(len, llm_map.values())),
            "batches": len(tasks),
            "failures": failures,
        },
    }
    args.output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={args.output_path} value_mappings={sum(map(len, value_map.values()))}")


if __name__ == "__main__":
    main()
