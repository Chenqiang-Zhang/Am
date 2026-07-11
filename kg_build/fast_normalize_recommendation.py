"""Fast, recommendation-focused attr_type normalization.

Unlike canonicalize_attributes.py, this does not attempt an all-pairs merge
across the full open-ended taxonomy. It selects high-impact attr_types, applies
safe rules first, then asks the LLM to classify only the ambiguous remainder
into a small fixed ontology. The existing canonical map is never overwritten.
"""
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

from utils.csv_io import read_jsonl
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call


ONTOLOGY = [
    "platform",
    "product_kind",
    "genre",
    "franchise",
    "play_mode",
    "multiplayer_type",
    "player_count",
    "online_support",
    "difficulty",
    "gameplay_style",
    "graphics",
    "story",
    "language",
    "release_date",
    "price",
    "other",
]

RELEVANCE_TERMS = (
    "platform", "console", "system", "genre", "category", "franchise",
    "mode", "player", "multi", "coop", "co_op", "online", "difficulty",
    "gameplay", "graphic", "visual", "story", "narrative", "language",
    "release", "price", "pricing", "product_type", "product_kind",
)

EXACT_RULES = {
    "platform": "platform",
    "platforms": "platform",
    "hardware_platform": "platform",
    "computer_platform": "platform",
    "game_platform": "platform",
    "console": "platform",
    "game_genre": "genre",
    "genre": "genre",
    "genres": "genre",
    "franchise": "franchise",
    "series": "franchise",
    "game_mode": "play_mode",
    "game_modes": "play_mode",
    "gameplay_mode": "play_mode",
    "gameplay_modes": "play_mode",
    "play_mode": "play_mode",
    "multiplayer": "multiplayer_type",
    "multiplayer_mode": "multiplayer_type",
    "multiplayer_modes": "multiplayer_type",
    "coop": "multiplayer_type",
    "co_op": "multiplayer_type",
    "online_co_op": "multiplayer_type",
    "local_multiplayer": "multiplayer_type",
    "online_multiplayer": "multiplayer_type",
    "number_of_players": "player_count",
    "player_count": "player_count",
    "max_players": "player_count",
    "online_support": "online_support",
    "online_features": "online_support",
    "difficulty": "difficulty",
    "gameplay_style": "gameplay_style",
    "gameplay": "gameplay_style",
    "graphics": "graphics",
    "visual_style": "graphics",
    "narrative": "story",
    "story": "story",
    "storyline": "story",
    "language": "language",
    "release_date": "release_date",
    "pricing": "price",
    "price": "price",
    "product_type": "product_kind",
    "product_kind": "product_kind",
}

# High-confidence LLM output can still be ontologically wrong when the target
# set is deliberately small. Keep known neighboring concepts separate rather
# than forcing them into a recommendation facet.
DENY_MAPPINGS = {
    ("format", "platform"),
    ("media_format", "platform"),
    ("controller_type", "platform"),
    ("controller_support", "platform"),
    ("control_scheme", "play_mode"),
    ("source_material", "franchise"),
    ("value", "price"),
}

SYSTEM_PROMPT = """\
Classify product-knowledge-graph attr_types into a fixed recommendation ontology.
Use the attr_type name AND its example values. Do not classify by name similarity alone.
For example, battle_system is gameplay-related, not platform; operating_system may be
platform only when its values are actual target platforms/OS names. Use other when the
concept is not useful for product recommendation or does not cleanly fit.

Allowed canonical types:
platform, product_kind, genre, franchise, play_mode, multiplayer_type,
player_count, online_support, difficulty, gameplay_style, graphics, story,
language, release_date, price, other.

Return one result for every input item. Confidence is 0.0 to 1.0.
"""


def classification_schema() -> dict[str, Any]:
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
                        "canonical": {"type": "string", "enum": ONTOLOGY},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["raw", "canonical", "confidence"],
                },
            }
        },
        "required": ["classifications"],
    }


def collect_stats(product_path: Path, review_path: Path) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"product_uses": 0, "review_uses": 0, "values": Counter()}
    )
    for rec in read_jsonl(product_path):
        for attr in rec.get("attributes", []):
            raw = str(attr.get("attr_type", "")).strip()
            value = str(attr.get("value", "")).strip()
            if raw and value:
                stats[raw]["product_uses"] += 1
                stats[raw]["values"][value] += 1
    for rec in read_jsonl(review_path):
        for attr in rec.get("mentions", []):
            raw = str(attr.get("attr_type", "")).strip()
            value = str(attr.get("value", "")).strip()
            if raw and value:
                stats[raw]["review_uses"] += 1
                stats[raw]["values"][value] += 1
    return dict(stats)


def priority(item: tuple[str, dict[str, Any]]) -> int:
    _, stat = item
    return stat["product_uses"] * 10 + stat["review_uses"]


def select_candidates(
    stats: dict[str, dict[str, Any]], min_usage: int, max_candidates: int,
) -> list[str]:
    eligible = {
        raw for raw, stat in stats.items()
        if stat["product_uses"] + stat["review_uses"] >= min_usage
    }
    top = {
        raw for raw, _ in sorted(stats.items(), key=priority, reverse=True)[:max_candidates]
        if raw in eligible
    }
    relevant = {
        raw for raw in eligible
        if any(term in raw.lower() for term in RELEVANCE_TERMS)
    }
    selected = top | relevant
    return sorted(selected, key=lambda raw: priority((raw, stats[raw])), reverse=True)[:max_candidates]


def safe_rule(raw: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if normalized in EXACT_RULES:
        return EXACT_RULES[normalized]
    if normalized.endswith("_platform") or normalized.endswith("_platforms"):
        return "platform"
    if normalized in {"players", "player_number", "maximum_players"}:
        return "player_count"
    return None


def item_payload(raw: str, stat: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw": raw,
        "product_uses": stat["product_uses"],
        "review_uses": stat["review_uses"],
        "example_values": [value for value, _ in stat["values"].most_common(5)],
    }


def chunks(items: list[Any], size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def classify_batch(client: Any, model: str, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data, _ = chat_json_call(
        client,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)},
        ],
        max_output_tokens=1600,
        retries=2,
        response_schema=classification_schema(),
        schema_name="recommendation_attr_classification",
    )
    return data.get("classifications", []) if isinstance(data, dict) else []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--product-attrs", type=Path)
    parser.add_argument("--review-mentions", type=Path)
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"])
    parser.add_argument("--model")
    parser.add_argument("--min-usage", type=int, default=4)
    parser.add_argument("--max-candidates", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    config_dir = args.config.resolve().parent
    out_dir = config_dir / cfg.get("data", {}).get("output_dir", "kg_output/video_games")
    attrs_dir = out_dir / "attributes"
    product_path = args.product_attrs or attrs_dir / "product_attributes.jsonl"
    review_path = args.review_mentions or attrs_dir / "review_mentions.jsonl"
    output_path = args.output_path or attrs_dir / "recommendation_canonical_map.json"

    stats = collect_stats(product_path, review_path)
    selected = select_candidates(stats, args.min_usage, args.max_candidates)
    rule_results: dict[str, dict[str, Any]] = {}
    ambiguous: list[str] = []
    for raw in selected:
        canonical = safe_rule(raw)
        if canonical:
            rule_results[raw] = {"canonical": canonical, "confidence": 1.0, "method": "rule"}
        else:
            ambiguous.append(raw)

    print(
        f"attr_types={len(stats)} selected={len(selected)} "
        f"rules={len(rule_results)} llm={len(ambiguous)}"
    )
    if args.dry_run:
        print(json.dumps({"selected": selected, "ambiguous": ambiguous}, ensure_ascii=False))
        return

    llm_cfg = cfg.get("llm", {})
    cfg_provider, cfg_model, base_url = provider_from_config(llm_cfg)
    client, model = build_client(args.provider or cfg_provider, args.model or cfg_model, base_url)
    payloads = [item_payload(raw, stats[raw]) for raw in ambiguous]
    llm_results: dict[str, dict[str, Any]] = {}
    batches = list(chunks(payloads, args.batch_size))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {pool.submit(classify_batch, client, model, batch): batch for batch in batches}
        for done, future in enumerate(as_completed(future_map), 1):
            batch = future_map[future]
            valid_raw = {item["raw"] for item in batch}
            try:
                rows = future.result()
            except Exception as exc:
                print(f"batch failed: {exc}", file=sys.stderr)
                rows = []
            for row in rows:
                raw = str(row.get("raw", ""))
                canonical = str(row.get("canonical", "other"))
                if raw in valid_raw and canonical in ONTOLOGY:
                    llm_results[raw] = {
                        "canonical": canonical,
                        "confidence": float(row.get("confidence", 0)),
                        "method": "llm",
                    }
            print(f"batches={done}/{len(batches)} classified={len(llm_results)}/{len(ambiguous)}")

    missing = [raw for raw in ambiguous if raw not in llm_results]
    if missing:
        print(f"warning: {len(missing)} missing classifications -> other", file=sys.stderr)
        for raw in missing:
            llm_results[raw] = {"canonical": "other", "confidence": 0.0, "method": "fallback"}

    classifications = {**rule_results, **llm_results}
    attr_type_map = {
        raw: result["canonical"]
        for raw, result in classifications.items()
        if result["canonical"] != "other"
        and result["confidence"] >= 0.85
        and (raw, result["canonical"]) not in DENY_MAPPINGS
    }
    output = {
        "ontology": ONTOLOGY,
        "selection": {
            "min_usage": args.min_usage,
            "max_candidates": args.max_candidates,
            "selected": len(selected),
            "rule_classified": len(rule_results),
            "llm_classified": len(llm_results),
        },
        "attr_type_map": attr_type_map,
        "classifications": classifications,
        "value_map": {},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={output_path} mapped={len(attr_type_map)}")


if __name__ == "__main__":
    main()
