"""
Extract product attributes from meta JSONL using LLM.

Output (JSONL, one line per product):
  {"product_id": "...", "model": "...", "attributes": [
    {"attr_type": "platform", "value": "pc", "evidence": "...", "confidence": 0.9}
  ]}

attr_type/value are drawn from a GROWABLE vocabulary (attr_vocab.yaml, seeded by
propose_attr_vocab.py and reviewed by hand): the LLM is told to reuse an existing
(attr_type, value) pair whenever one fits, and may introduce a new one only when
truly necessary — when that happens it's added to the same database (see
utils/attr_vocab.py's GrowableVocab), not dropped, so later batches in this same run
see and can reuse it too. rule_attributes() maps `details` dict keys through
detail_key_map.yaml (same directory, static) instead of auto-slugifying them.
Run build_attribute_graph.py afterwards to produce Neo4j import CSVs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from utils.attr_vocab import GrowableVocab, load_detail_key_map
from utils.csv_io import load_done_ids, read_jsonl_gz
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import batch_extract_with_fallback, split_usage
from utils.text_utils import as_list, clean_text, normalize_attr_type, normalize_value


# ── LLM schema / prompts ───────────────────────────────────────────────────────

ATTRIBUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_id": {"type": "string"},
                    "attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "attr_type": {"type": "string"},
                                "value": {"type": "string"},
                                "evidence": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["attr_type", "value", "evidence", "confidence"],
                        },
                    },
                },
                "required": ["product_id", "attributes"],
            },
        }
    },
    "required": ["products"],
}

def build_system_prompt(genre: str, vocab_text: str) -> str:
    """Build the LLM system prompt. vocab_text is the CURRENT state of the growable
    attr_type/value vocabulary (GrowableVocab.prompt_text() — call this fresh for
    every LLM call, not once per run, so growth from earlier batches is visible).
    Per-product known attributes are passed in the user payload (see
    product_payload()), not here."""
    return f"""\
Extract product attributes for a knowledge graph of {genre}.

Return valid JSON only. Output shape:
{{"products":[{{"product_id":"...","attributes":[{{"attr_type":"...","value":"...","evidence":"short source phrase","confidence":0.0}}]}}]}}

Each product in the input includes a "known_attributes" list — attr_type/value pairs
already extracted from structured metadata fields (zero-cost, rule-based). Do NOT
re-extract these facts. Only return attributes for NEW information found in
title/features/description that is not already covered by known_attributes.

EXISTING attr_type/value vocabulary — STRONGLY prefer reusing one of these exact
(attr_type, value) combinations when it fits. When an entry lists "allowed values",
treat those as the known values for that attr_type. Only introduce a new attr_type,
or a new value under an existing attr_type, when you are confident nothing existing
truly fits — new entries become part of the shared vocabulary, so invent one only
when genuinely necessary, not as a default:
{vocab_text}

FORBIDDEN attr_type — never use these:
  brand           (brand is a separate node in the graph; do not extract it)
  feature, other  (too generic)

Rules for value:
- Short, lowercase, normalized (e.g. "wireless", "co_op", "steel")
- Do not repeat the attr_type in the value (attr_type="platform", value="pc" not "pc platform")

General rules:
- Return only attributes supported by the product record
- Skip generic labels like "UPC", "package dimensions", "ASIN"
- Do not infer medical claims or sensitive traits
- If a product is sparse, return an empty attributes list
- Avoid duplicate attr_type+value pairs for the same product
"""

# ── structured fields → rule-based attributes ─────────────────────────────────

# details キーのうち、ジャンルを問わず属性として無意味なものは共通で除外する
# （propose_attr_vocab.py もこのリストを再利用し、LLMにこれらのキーを尋ねない）。
# "Brand" は既に一級のKGノード（MADE_BY関係）なので Attribute としては不要。
IGNORED_DETAIL_KEYS = {
    "Brand",
    "UPC", "EAN", "ASIN", "Item model number", "Model Number", "Manufacturer",
    "Item Weight", "Product Dimensions", "Package Dimensions", "Item Dimensions LxWxH",
    "Date First Available", "Customer Reviews", "Best Sellers Rank",
    "Is Discontinued By Manufacturer", "Batteries Required?", "Batteries Included?",
    "Warranty Description", "Country of Origin", "Domestic Shipping",
    "International Shipping", "Included Components", "Number of Items",
}

# 残りの details キーは detail_key_map.yaml（propose_attr_vocab.py が生成し、人手で
# レビュー済みの allowlist）を通してのみ attr_type になる。マップに無い/null のキーは
# 破棄する（以前の _key_to_attr_type() による無条件slugifyは廃止 — 閉じた語彙という
# 前提が崩れるため）。

_SIZE_PAT = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|oz|ounce|ounces|ml|g|gram|grams|inch|inches|mm|cm|pcs|pack)\b",
    re.I,
)
_COLOR_WORDS = {"black", "white", "brown", "blonde", "red", "pink", "blue", "green", "gold", "silver", "purple", "clear"}


def rule_attributes(row: dict[str, Any], detail_key_map: dict[str, str | None]) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(attr_type: str, value: Any, evidence: str, confidence: float) -> None:
        t = normalize_attr_type(attr_type)
        v = normalize_value(str(value))
        if not v:
            return
        key = (t, v)
        if key in seen:
            return
        seen.add(key)
        attrs.append({"attr_type": t, "value": v, "evidence": evidence[:120], "confidence": confidence})

    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    for key, raw_val in details.items():
        if key in IGNORED_DETAIL_KEYS:
            continue
        canonical = detail_key_map.get(key)
        if not canonical:
            continue
        add(canonical, raw_val, f"{key}: {raw_val}", 0.9)

    title = clean_text(row.get("title"))
    for match in _SIZE_PAT.findall(title):
        add("size", match, f"title: {match}", 0.75)
        break

    title_lower = title.lower()
    for color in _COLOR_WORDS:
        if re.search(rf"\b{re.escape(color)}\b", title_lower):
            add("color", color, f"title: {color}", 0.65)
            break

    return attrs


# ── product payload builder ────────────────────────────────────────────────────

def product_payload(row: dict[str, Any], max_chars: int, rule_attrs: list[dict[str, Any]]) -> dict[str, Any]:
    budget = max(120, max_chars // 4)
    return {
        "product_id": row.get("parent_asin"),
        "title": clean_text(row.get("title"))[:budget],
        "store": clean_text(row.get("store")),
        "features": [clean_text(x)[:budget] for x in as_list(row.get("features"))[:5] if clean_text(x)],
        "description": [clean_text(x)[:budget] for x in as_list(row.get("description"))[:3] if clean_text(x)],
        "known_attributes": [{"attr_type": a["attr_type"], "value": a["value"]} for a in rule_attrs],
    }


def is_sparse(payload: dict[str, Any]) -> bool:
    known = payload.get("known_attributes") or []
    return bool(payload.get("title")) and not payload.get("features") and not payload.get("description") and len(known) <= 1


# ── API helpers ────────────────────────────────────────────────────────────────

def extract_with_fallback(
    client: Any, model: str, payloads: list[dict], system_prompt: str,
    max_output_tokens: int, retries: int, use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    def build_messages(batch: list[dict]) -> list[dict]:
        user_content = json.dumps({"task": "Extract product attributes.", "products": batch}, ensure_ascii=False)
        return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]

    def parse_result(parsed: dict) -> dict[str, list]:
        return {p["product_id"]: p.get("attributes", []) for p in parsed.get("products", [])}

    return batch_extract_with_fallback(
        client, model, payloads, item_id=lambda p: p["product_id"],
        build_messages=build_messages, parse_result=parse_result,
        max_output_tokens=max_output_tokens, retries=retries, use_responses_api=use_responses_api,
        response_schema=ATTRIBUTE_SCHEMA, schema_name="attrs", label="product",
    )


# ── normalization post-processing ──────────────────────────────────────────────

def normalize_attrs(attrs: list[dict[str, Any]], vocab: GrowableVocab) -> list[dict[str, Any]]:
    """Normalize attr_type/value to snake_case and deduplicate. Every pair is resolved
    against vocab with growth allowed — reused if already known, added to the shared
    database if not (see GrowableVocab.resolve()). Only empty/malformed input is
    actually skipped."""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for a in attrs:
        raw_t = str(a.get("attr_type", ""))
        raw_v = str(a.get("value", ""))
        if not raw_t or not raw_v:
            continue
        resolved = vocab.resolve(raw_t, raw_v, allow_growth=True)
        if resolved is None:
            continue
        t, v = resolved
        key = (t, v)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "attr_type": t,
            "value": v,
            "evidence": clean_text(a.get("evidence", ""))[:120],
            "confidence": float(a.get("confidence", 0.7)),
        })
    return result


def merge_attrs(rule: list[dict], llm: list[dict], vocab: GrowableVocab) -> list[dict]:
    return normalize_attrs(rule + llm, vocab)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract product attributes via LLM.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--meta-path", type=Path)
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=-1, help="-1 = all")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-input-chars", type=int, default=2000)
    parser.add_argument("--max-output-tokens", type=int, default=2000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-sparse", action="store_true")
    parser.add_argument("--rule-only", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument(
        "--product-ids-file", type=Path, default=None,
        help="CSV with a 'product_id' column (e.g. nodes_products.csv). "
             "Only products listed here will be processed.",
    )
    parser.add_argument("--attr-vocab-path", type=Path, default=None,
                         help="Path to attr_vocab.yaml (default: <output_dir>/attributes/attr_vocab.yaml)")
    parser.add_argument("--detail-key-map-path", type=Path, default=None,
                         help="Path to detail_key_map.yaml (default: <output_dir>/attributes/detail_key_map.yaml)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})
    config_dir = args.config.resolve().parent

    meta_path = args.meta_path or (config_dir / data_cfg.get("meta_path", "data/meta_Video_Games.jsonl.gz"))
    out_dir = config_dir / data_cfg.get("output_dir", "kg_output/video_games")
    output_path = args.output_path or (out_dir / "attributes" / "product_attributes.jsonl")
    attrs_dir = out_dir / "attributes"

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    min_confidence = args.min_confidence if args.min_confidence is not None else llm_cfg.get("min_confidence", 0.6)

    if not args.rule_only:
        client, model = build_client(provider, model_arg, cfg_base_url)
    else:
        client, model = None, model_arg or "rules"
    use_responses_api = False  # use chat.completions for all providers

    genre = cfg.get("genre", "products")
    vocab_path = args.attr_vocab_path or (attrs_dir / "attr_vocab.yaml")
    detail_key_map_path = args.detail_key_map_path or (attrs_dir / "detail_key_map.yaml")
    vocab = GrowableVocab(vocab_path)
    detail_key_map = load_detail_key_map(detail_key_map_path)
    if vocab.type_count() == 0:
        print(
            f"WARNING: {vocab_path} not found or empty — starting from an empty vocabulary "
            f"(every attribute will be a 'new' addition). Run kg_build/propose_attr_vocab.py "
            f"first for a better starting point.",
            file=sys.stderr,
        )
    print(f"Prompt will draw from genre={genre!r}, {vocab.type_count()} attr_types currently in vocabulary")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(output_path, "product_id") if args.resume else set()
    limit = None if args.limit < 0 else args.limit

    allowed_ids: set[str] | None = None
    if args.product_ids_file:
        import csv as _csv
        with args.product_ids_file.open(encoding="utf-8") as _f:
            allowed_ids = {row["product_id"] for row in _csv.DictReader(_f)}
        print(f"Filtering to {len(allowed_ids):,} products from {args.product_ids_file.name}")

    processed = 0
    seen_rows = 0
    pending: list[dict] = []
    futures: set[Future] = set()

    def process_batch(batch: list[dict]) -> list[dict]:
        payloads = [item["payload"] for item in batch]
        if args.rule_only:
            llm_map: dict[str, list[dict]] = {}
            usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        else:
            # built fresh from the live vocab so growth from earlier batches (this run,
            # possibly on another worker thread) is visible to this call
            system_prompt = build_system_prompt(genre, vocab.prompt_text())
            llm_map, usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)

        records: list[dict] = []
        per_usage = split_usage(usage, len(batch))
        for item in batch:
            pid = item["product_id"]
            llm_attrs = [a for a in llm_map.get(pid, []) if float(a.get("confidence", 0)) >= min_confidence]
            attrs = merge_attrs(item["rule_attrs"], llm_attrs, vocab)
            records.append({
                "product_id": pid,
                "model": "rules" if args.rule_only else model,
                "attributes": attrs,
                "usage": per_usage,
            })
        return records

    def flush_pending(out: Any, executor: ThreadPoolExecutor | None) -> None:
        nonlocal processed, pending, futures
        if not pending:
            return
        batch, pending = pending, []
        processed += len(batch)
        if executor is None:
            write_records(out, process_batch(batch))
        else:
            futures.add(executor.submit(process_batch, batch))
            while len(futures) >= args.workers * 2:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for f in done:
                    write_records(out, f.result())
        if args.sleep:
            time.sleep(args.sleep)

    def write_records(out: Any, records: list[dict]) -> None:
        for rec in records:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {rec['product_id']}  attrs={len(rec['attributes'])}")
        out.flush()
        vocab.save()  # persist any growth from this batch immediately (crash-safe,
                       # and visible to later batches via process_batch's fresh prompt_text())

    with output_path.open("a", encoding="utf-8") as out:
        executor = ThreadPoolExecutor(max_workers=args.workers) if args.workers > 1 and not args.rule_only else None
        try:
            for row in read_jsonl_gz(meta_path):
                if seen_rows < args.offset:
                    seen_rows += 1
                    continue
                if limit is not None and processed + len(pending) >= limit:
                    break
                pid = clean_text(row.get("parent_asin"))
                seen_rows += 1
                if not pid or pid in done_ids:
                    continue
                if allowed_ids is not None and pid not in allowed_ids:
                    continue

                rule_attrs = rule_attributes(row, detail_key_map)
                payload = product_payload(row, args.max_input_chars, rule_attrs)

                if args.skip_sparse and is_sparse(payload):
                    with output_path.open("a", encoding="utf-8") as _out:
                        _out.write(json.dumps({
                            "product_id": pid, "model": "rules",
                            "attributes": normalize_attrs(rule_attrs, vocab),
                            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        }, ensure_ascii=False) + "\n")
                    vocab.save()
                    processed += 1
                    continue

                pending.append({"product_id": pid, "payload": payload, "rule_attrs": rule_attrs})
                if len(pending) >= args.batch_size:
                    flush_pending(out, executor)

            flush_pending(out, executor)
            if futures:
                done, _ = wait(futures)
                for f in done:
                    write_records(out, f.result())
        finally:
            if executor:
                executor.shutdown(wait=True)

    vocab.save()
    print(f"\nWrote {processed} products to {output_path}")
    growth = vocab.growth_summary()
    if growth:
        print(f"Vocabulary grew this run — {growth}\n  -> review {vocab_path} by hand if this looks noisy.")
    else:
        print(f"No vocabulary growth this run — {vocab.type_count()} attr_types, all reused.")


if __name__ == "__main__":
    main()
