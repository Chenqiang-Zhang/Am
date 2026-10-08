"""
Extract product attributes from meta JSONL using LLM.

Output (JSONL, one line per product):
  {"product_id": "...", "model": "...", "attributes": [
    {"attr_type": "platform", "value": "pc", "evidence": "...", "confidence": 0.9}
  ]}

All attributes come from the LLM reading title/features/description — there is
no rule-based extraction from structured metadata (`details`) here; that path
turned out to need constant hand-maintained exceptions (ignored keys, key
aliases, value collapsing) for little benefit, since a well-stocked shared
vocabulary lets the LLM recover the same facts from free text anyway.

Attribute vocabulary is not free-form: extraction consults the shared, runtime-
growable (attr_type, value) database in ontology/attribute_vocab.yaml (see
utils/attribute_vocab_store.py). The LLM is shown the current known pairs and
instructed to reuse one whenever it fits. It may propose a brand-new pair only
when nothing fits; such a proposal is kept only if the LLM's own "necessity"
score clears NEW_PAIR_MIN_NECESSITY, in which case it is persisted to the YAML
immediately — later batches (this run and future runs) then see it as known.
Proposals that don't clear the gate are dropped from the output entirely.

Run build_attribute_graph.py afterwards to produce Neo4j import CSVs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import time
from pathlib import Path
from typing import Any

import yaml

from utils.attribute_vocab_store import AttributeVocabStore, canonicalize_and_gate, format_vocab_listing
from utils.csv_io import load_done_ids, read_jsonl_gz
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import batch_extract_with_fallback
from utils.text_utils import as_list, clean_text


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
                                "is_new": {"type": "boolean"},
                                "necessity": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["attr_type", "value", "evidence", "confidence", "is_new", "necessity"],
                        },
                    },
                },
                "required": ["product_id", "attributes"],
            },
        }
    },
    "required": ["products"],
}

def build_system_prompt(genre: str, vocab_listing: str) -> str:
    """Build the LLM system prompt. vocab_listing is a fresh snapshot of the
    shared attribute vocabulary (see utils/attribute_vocab_store.py), rebuilt
    for every batch so growth within the same run is reflected immediately."""
    return f"""\
Extract product attributes for a knowledge graph of {genre}, from the product's
title/features/description.

Return valid JSON only. Output shape:
{{"products":[{{"product_id":"...","attributes":[{{"attr_type":"...","value":"...","evidence":"short source phrase","confidence":0.0,"is_new":false,"necessity":0.0}}]}}]}}

KNOWN (attr_type: values) already used in this knowledge graph — reuse an existing
pair whenever a fact fits one, even if the exact wording in the product text differs
(normalize to the known value):
{vocab_listing}

For facts that fit none of the known pairs above, you may propose a new one. Set
is_new=true and "necessity" (0.0-1.0) to how strongly this fact needs its own new
canonical (attr_type, value) — not how confident you are about extracting it
correctly. Reserve values above 0.9 for cases where dropping this fact would
clearly lose important, recurring, recommendation-relevant information. Most new
proposals should score well below that. For facts that DO reuse a known pair, set
is_new=false and necessity=0.0 (confidence carries the extraction certainty instead).

FORBIDDEN attr_type — never use these:
  brand           (brand is a separate node in the graph; do not extract it)
  feature, other  (too generic)

Rules for value:
- Short, lowercase, normalized (e.g. "wireless", "co_op", "steel")
- Do not repeat the attr_type in the value (attr_type="platform", value="pc" not "pc platform")

General rules:
- Return only attributes supported by the product record
- Do not infer medical claims or sensitive traits
- If a product is sparse, return an empty attributes list
- Avoid duplicate attr_type+value pairs for the same product
"""


# ── product payload builder ────────────────────────────────────────────────────

def product_payload(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    budget = max(120, max_chars // 4)
    return {
        "product_id": row.get("parent_asin"),
        "title": clean_text(row.get("title"))[:budget],
        "store": clean_text(row.get("store")),
        "features": [clean_text(x)[:budget] for x in as_list(row.get("features"))[:5] if clean_text(x)],
        "description": [clean_text(x)[:budget] for x in as_list(row.get("description"))[:3] if clean_text(x)],
    }


def is_sparse(payload: dict[str, Any]) -> bool:
    return bool(payload.get("title")) and not payload.get("features") and not payload.get("description")


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


# ── gating post-processing ──────────────────────────────────────────────────────

def gate_llm_attrs(
    attrs: list[dict[str, Any]], store: AttributeVocabStore, min_confidence: float,
) -> list[dict[str, Any]]:
    """LLM が返した attribute のうち、共有語彙のゲートを通ったものだけを残す
    （正規化・ゲート判定自体は canonicalize_and_gate に共通化 — 通らなかった
    ものは出力から除外する）。"""
    seen: set[tuple[str, str]] = set()
    kept: list[dict[str, Any]] = []
    for a in attrs:
        resolved = canonicalize_and_gate(a, store, min_confidence)
        if resolved is None:
            continue
        t, v, confidence = resolved
        key = (t, v)
        if key in seen:
            continue
        seen.add(key)
        kept.append({
            "attr_type": t,
            "value": v,
            "evidence": clean_text(a.get("evidence", ""))[:120],
            "confidence": confidence,
        })
    return kept


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
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument(
        "--product-ids-file", type=Path, default=None,
        help="CSV with a 'product_id' column (e.g. nodes_products.csv). "
             "Only products listed here will be processed.",
    )
    parser.add_argument("--max-vocab-types-shown", type=int, default=60)
    parser.add_argument("--max-vocab-values-shown", type=int, default=12)
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

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    min_confidence = args.min_confidence if args.min_confidence is not None else llm_cfg.get("min_confidence", 0.6)

    client, model = build_client(provider, model_arg, cfg_base_url)
    use_responses_api = False  # use chat.completions for all providers

    genre = cfg.get("genre", "products")
    vocab_store = AttributeVocabStore()
    print(f"Prompt genre={genre!r}; shared attribute vocab loaded with {len(vocab_store.snapshot())} attr_types")

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
        vocab_listing = format_vocab_listing(
            vocab_store.snapshot(), args.max_vocab_types_shown, args.max_vocab_values_shown,
        )
        system_prompt = build_system_prompt(genre, vocab_listing)
        llm_map, _usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)

        records: list[dict] = []
        for item in batch:
            pid = item["product_id"]
            attrs = gate_llm_attrs(llm_map.get(pid, []), vocab_store, min_confidence)
            records.append({
                "product_id": pid,
                "model": model,
                "attributes": attrs,
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

    with output_path.open("a", encoding="utf-8") as out:
        executor = ThreadPoolExecutor(max_workers=args.workers) if args.workers > 1 else None
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

                payload = product_payload(row, args.max_input_chars)

                if args.skip_sparse and is_sparse(payload):
                    with output_path.open("a", encoding="utf-8") as _out:
                        _out.write(json.dumps({
                            "product_id": pid, "model": model, "attributes": [],
                        }, ensure_ascii=False) + "\n")
                    processed += 1
                    continue

                pending.append({"product_id": pid, "payload": payload})
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

    print(f"\nWrote {processed} products to {output_path}")


if __name__ == "__main__":
    main()
