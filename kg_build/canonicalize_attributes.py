"""
Canonicalize attr_type / value spelling drift after extraction.

Rule-based extraction (details keys) and LLM extraction (free text) run
independently and get merged by build_attribute_graph.py with exact-string
dedup only. That leaves near-duplicates on the table, e.g.:
  attr_type: "platform" vs "system"            (same concept, different name)
  value:     "long lasting" vs "long-lasting"  (same value, different spelling)

This script scans the extraction outputs, asks the LLM once for an attr_type
merge map, then once per batch of attr_types for a value merge map, and
writes both to a single canonicalization file. build_attribute_graph.py
applies it (if present) before computing attribute_id, so synonyms collapse
into one Attribute node instead of staying split.

Reads:
  product_attributes.jsonl  (from extract_product_attributes.py)
  review_mentions.jsonl     (from extract_review_mentions.py)

Writes:
  attribute_canonical_map.json
    {"attr_type_map": {"raw": "canonical", ...},
     "value_map": {"canonical_attr_type": {"raw_value": "canonical_value", ...}}}

Usage:
    python3 kg_build/canonicalize_attributes.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from utils.csv_io import read_jsonl
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call


def collect_counts(product_attrs_path: Path, review_mentions_path: Path) -> dict[str, dict[str, int]]:
    """Return {attr_type: {value: count}} tallied across both extraction outputs."""
    counts: dict[str, dict[str, int]] = {}

    def bump(t: str, v: str) -> None:
        if not t or not v:
            return
        counts.setdefault(t, {})
        counts[t][v] = counts[t].get(v, 0) + 1

    for rec in read_jsonl(product_attrs_path):
        for a in rec.get("attributes", []):
            bump(str(a.get("attr_type", "")), str(a.get("value", "")))
    for rec in read_jsonl(review_mentions_path):
        for m in rec.get("mentions", []):
            bump(str(m.get("attr_type", "")), str(m.get("value", "")))
    return counts


# ── LLM helpers ────────────────────────────────────────────────────────────────

ATTR_TYPE_MAP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "map": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        }
    },
    "required": ["map"],
}

VALUE_MAP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "maps": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        }
    },
    "required": ["maps"],
}


def call_llm(
    client: Any,
    model: str,
    system: str,
    user: str,
    max_output_tokens: int = 3500,
    *,
    response_schema: dict[str, Any] | None = None,
    schema_name: str = "canonicalization",
) -> dict[str, Any]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    parsed, _usage = chat_json_call(
        client,
        model,
        messages,
        max_output_tokens=max_output_tokens,
        retries=2,
        response_schema=response_schema,
        schema_name=schema_name,
    )
    return parsed


ATTR_TYPE_SYSTEM_PROMPT = """\
You are cleaning up the attribute taxonomy of a product knowledge graph for {genre}.
Below is every attr_type name currently in use, with how many attributes use it.
Some names are synonyms or near-duplicates created by independent extraction passes
(rule-based + LLM), e.g. "platform" and "system" mean the same thing.

Return valid JSON only: {{"map": {{"raw_name": "canonical_name", ...}}}}
- Only include an entry for a name that should be MERGED into a different name.
- Omit any name that is already fine as-is — it stays unchanged.
- When merging, prefer the more frequent / more descriptive name as canonical.
- Do not invent a canonical name that isn't already in the input list.
"""

VALUE_SYSTEM_PROMPT = """\
You are cleaning up attribute values in a product knowledge graph.
For each attr_type below, you are given its distinct values with counts.
Values that mean the same thing but differ in spelling/spacing/punctuation/
singular-plural (e.g. "long lasting" vs "long-lasting") should be merged.

Return valid JSON only: {{"maps": {{"attr_type_name": {{"raw_value": "canonical_value", ...}}}}}}
- Only include an entry for a value that should be MERGED into a different value.
- Omit any value that is already fine as-is — it stays unchanged.
- When merging, prefer the more frequent spelling as canonical.
- Do not merge values that are genuinely different (e.g. "pc" and "console").
"""


def build_attr_type_map(
    client: Any, model: str, genre: str, counts: dict[str, dict[str, int]], batch_size: int = 30
) -> dict[str, str]:
    """attr_type一覧をLLMに1回で渡すと、種類数が多いジャンルではプロンプトが
    コンテキスト長を超える（元は単発呼び出しだった）。batch_sizeごとに分割して
    複数回呼び出し、結果をマージする。1バッチ内でしか同義語判定はできないが、
    build_value_map（値の正規化）も同じ方式でバッチ化されており、それに合わせる。"""
    totals = sorted(
        ({"attr_type": t, "count": sum(vc.values())} for t, vc in counts.items()),
        key=lambda x: -x["count"],
    )
    if len(totals) <= 1:
        return {}
    system = ATTR_TYPE_SYSTEM_PROMPT.format(genre=genre)
    valid_targets = set(counts.keys())
    frequencies = {t: sum(values.values()) for t, values in counts.items()}
    merged_map: dict[str, str] = {}
    for batch in chunked(totals, batch_size):
        user = json.dumps({"attr_types": batch}, ensure_ascii=False)
        try:
            data = call_llm(
                client,
                model,
                system,
                user,
                response_schema=ATTR_TYPE_MAP_SCHEMA,
                schema_name="attr_type_map",
            )
        except Exception as exc:
            print(f"  attr_type canonicalization batch failed, skipping: {exc}", file=sys.stderr)
            continue
        raw_map = data.get("map", {}) if isinstance(data, dict) else {}
        for k, v in raw_map.items():
            source, target = str(k), str(v)
            # Only accept existing names and direct every merge toward a strict
            # total order (frequency, then name). This prevents invented targets
            # and reciprocal/cyclic mappings even if the model proposes them.
            if (
                source != target
                and source in valid_targets
                and target in valid_targets
                and (frequencies[target], target) > (frequencies[source], source)
            ):
                merged_map[source] = target
        print(f"  processed {len(batch)} attr_types")
    return merged_map


def apply_attr_type_map(counts: dict[str, dict[str, int]], attr_type_map: dict[str, str]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for t, values in counts.items():
        canonical = attr_type_map.get(t, t)
        bucket = merged.setdefault(canonical, {})
        for v, c in values.items():
            bucket[v] = bucket.get(v, 0) + c
    return merged


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_value_map(
    client: Any, model: str, counts: dict[str, dict[str, int]], batch_size: int, min_distinct: int,
    max_values_per_type: int = 60, max_values_per_batch: int = 200,
) -> dict[str, dict[str, str]]:
    # 1 attr_type が数千の値を持つと、attr_type を batch_size 個束ねただけでは
    # プロンプトがコンテキスト長を超える。そこで (1) 各 attr_type は頻出上位
    # max_values_per_type 件だけを正規化対象にし、(2) バッチは「含まれる値の総数」が
    # max_values_per_batch を超えないように詰める。正規化は表記揺れ吸収が目的なので、
    # 低頻度のロングテール値まで完全網羅する必要はない。
    candidates = [
        {
            "attr_type": t,
            "values": [
                {"value": v, "count": c}
                for v, c in sorted(vc.items(), key=lambda x: -x[1])[:max_values_per_type]
            ],
        }
        for t, vc in counts.items()
        if len(vc) >= min_distinct
    ]
    if not candidates:
        return {}

    # 値総数ベースで動的にバッチを作る（attr_type 数上限 batch_size も併用）
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_values = 0
    for cand in candidates:
        n = len(cand["values"])
        if cur and (cur_values + n > max_values_per_batch or len(cur) >= batch_size):
            batches.append(cur)
            cur, cur_values = [], 0
        cur.append(cand)
        cur_values += n
    if cur:
        batches.append(cur)

    value_map: dict[str, dict[str, str]] = {}
    for batch in batches:
        user = json.dumps({"attr_types": batch}, ensure_ascii=False)
        try:
            data = call_llm(
                client,
                model,
                VALUE_SYSTEM_PROMPT,
                user,
                response_schema=VALUE_MAP_SCHEMA,
                schema_name="value_map",
            )
        except Exception as exc:
            print(f"  value canonicalization batch failed, skipping: {exc}", file=sys.stderr)
            continue
        maps = data.get("maps", {}) if isinstance(data, dict) else {}
        valid_types = {b["attr_type"] for b in batch}
        for t, m in maps.items():
            if t not in valid_types or not isinstance(m, dict):
                continue
            valid_values = set(counts[t].keys())
            cleaned = {str(k): str(v) for k, v in m.items() if str(k) != str(v) and str(k) in valid_values}
            if cleaned:
                value_map[t] = cleaned
        print(f"  processed {len(batch)} attr_types ({sum(len(b['values']) for b in batch)} values)")
    return value_map


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonicalize attr_type/value spelling drift via LLM.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--product-attrs", type=Path)
    parser.add_argument("--review-mentions", type=Path)
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--value-batch-size", type=int, default=15, help="attr_types per value-canonicalization call")
    parser.add_argument("--min-distinct-values", type=int, default=2, help="skip value canonicalization for attr_types with fewer distinct values")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})
    config_dir = args.config.resolve().parent
    out_dir = config_dir / data_cfg.get("output_dir", "kg_output/video_games")
    attrs_dir = out_dir / "attributes"

    product_attrs_path = args.product_attrs or (attrs_dir / "product_attributes.jsonl")
    review_mentions_path = args.review_mentions or (attrs_dir / "review_mentions.jsonl")
    output_path = args.output_path or (attrs_dir / "attribute_canonical_map.json")

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    client, model = build_client(provider, model_arg, cfg_base_url)
    genre = cfg.get("genre", "products")

    print(f"Collecting attr_type/value counts from {product_attrs_path.name} + {review_mentions_path.name}...")
    counts = collect_counts(product_attrs_path, review_mentions_path)
    print(f"  {len(counts)} distinct attr_types, {sum(len(v) for v in counts.values())} distinct (attr_type, value) pairs")

    print("Canonicalizing attr_type names...")
    attr_type_map = build_attr_type_map(client, model, genre, counts)
    print(f"  {len(attr_type_map)} attr_type merges: {attr_type_map}")

    merged_counts = apply_attr_type_map(counts, attr_type_map)

    print("Canonicalizing values per attr_type...")
    value_map = build_value_map(client, model, merged_counts, args.value_batch_size, args.min_distinct_values)
    total_value_merges = sum(len(m) for m in value_map.values())
    print(f"  {total_value_merges} value merges across {len(value_map)} attr_types")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"attr_type_map": attr_type_map, "value_map": value_map}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote canonicalization map to {output_path}")


if __name__ == "__main__":
    main()
