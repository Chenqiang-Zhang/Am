"""
Extract attribute mentions from review text using LLM.

Reads:  nodes_reviews.csv  (review_id, text, rating, ...)
        rel_about.csv      (review_id, product_id)
Writes: review_mentions.jsonl  (one line per review)

Output schema per line:
  {"review_id": "...", "product_id": "...", "mentions": [
    {"attr_type": "build_quality", "value": "sturdy", "sentiment": "positive", "confidence": 0.9}
  ]}

Attribute vocabulary is shared with extract_product_attributes.py via the
runtime-growable (attr_type, value) database in ontology/attribute_vocab.yaml
(see utils/attribute_vocab_store.py). The LLM is shown the current known pairs
and instructed to reuse one whenever it fits. It may propose a brand-new pair
only when nothing fits; such a proposal is kept only if the LLM's own
"necessity" score clears NEW_PAIR_MIN_NECESSITY, in which case it is persisted
to the YAML immediately — later batches (this run and future runs, including
extract_product_attributes.py runs) then see it as known. Proposals that
don't clear the gate are dropped from the output entirely.

Run build_attribute_graph.py afterwards to produce Neo4j import CSVs.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import yaml

from utils.attribute_vocab_store import AttributeVocabStore, canonicalize_and_gate, format_vocab_listing
from utils.csv_io import load_done_ids
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import batch_extract_with_fallback
from utils.text_utils import clean_text


# ── LLM schema / prompts ───────────────────────────────────────────────────────

MENTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "review_id": {"type": "string"},
                    "mentions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "attr_type": {"type": "string"},
                                "value": {"type": "string"},
                                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "is_new": {"type": "boolean"},
                                "necessity": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["attr_type", "value", "sentiment", "confidence", "is_new", "necessity"],
                        },
                    },
                },
                "required": ["review_id", "mentions"],
            },
        }
    },
    "required": ["reviews"],
}

def build_system_prompt(genre: str, vocab_listing: str) -> str:
    """Build the LLM system prompt. vocab_listing is a fresh snapshot of the
    shared attribute vocabulary (see utils/attribute_vocab_store.py), rebuilt
    for every batch so growth within the same run — including growth from
    extract_product_attributes.py — is reflected immediately."""
    return f"""\
Extract product attribute mentions from customer reviews of {genre}.

Return valid JSON only. Output shape:
{{"reviews":[{{"review_id":"...","mentions":[{{"attr_type":"...","value":"...","sentiment":"positive|negative|neutral","confidence":0.0,"is_new":false,"necessity":0.0}}]}}]}}

KNOWN (attr_type: values) already used in this knowledge graph — reuse an existing
pair whenever a fact fits one, even if the exact wording in the review differs
(normalize to the known value):
{vocab_listing}

For facts that fit none of the known pairs above, you may propose a new one. Set
is_new=true and "necessity" (0.0-1.0) to how strongly this fact needs its own new
canonical (attr_type, value) — not how confident you are about extracting it
correctly. Reserve values above 0.9 for cases where dropping this fact would
clearly lose important, recurring, recommendation-relevant information. Most new
proposals should score well below that. For facts that DO reuse a known pair, set
is_new=false and necessity=0.0 (confidence carries the extraction certainty instead).

FORBIDDEN attr_type — never use:
  brand           (brand is a separate node; do not extract it)
  feature, other  (too generic)

Rules for value:
- Short, lowercase, normalized (e.g. "wireless", "co_op")
- Do not repeat the attr_type in the value

Rules for sentiment:
- positive: reviewer views this attribute favorably ("battery lasts forever", "great build quality")
- negative: reviewer views this attribute unfavorably ("battery dies fast", "cheap plastic feel")
- neutral: mentioned without clear opinion

General rules:
- Only extract attributes explicitly stated in the review text
- Skip generic comments about shipping, packaging condition, seller, or price
- Skip subjective overall opinions ("great product", "love it") unless tied to a specific attribute
- Avoid duplicating attr_type+value pairs in the same review
"""


# ── API helpers ────────────────────────────────────────────────────────────────

def extract_with_fallback(
    client: Any, model: str, reviews: list[dict], system_prompt: str,
    max_output_tokens: int, retries: int, use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    def build_messages(batch: list[dict]) -> list[dict]:
        user_content = json.dumps(
            {"task": "Extract attribute mentions from reviews.", "reviews": batch},
            ensure_ascii=False,
        )
        return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]

    def parse_result(parsed: dict) -> dict[str, list]:
        return {r["review_id"]: r.get("mentions", []) for r in parsed.get("reviews", [])}

    return batch_extract_with_fallback(
        client, model, reviews, item_id=lambda r: r["review_id"],
        build_messages=build_messages, parse_result=parse_result,
        max_output_tokens=max_output_tokens, retries=retries, use_responses_api=use_responses_api,
        response_schema=MENTIONS_SCHEMA, schema_name="mentions", label="review",
    )


# ── normalization ──────────────────────────────────────────────────────────────

VALID_SENTIMENTS = {"positive", "negative", "neutral"}


def normalize_mentions(raw: list[dict], min_confidence: float, store: AttributeVocabStore) -> list[dict]:
    """LLM が返した mention のうち、共有語彙のゲートを通ったものだけを残す
    （正規化・ゲート判定自体は canonicalize_and_gate に共通化）。"""
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for m in raw:
        resolved = canonicalize_and_gate(m, store, min_confidence)
        if resolved is None:
            continue
        t, v, confidence = resolved
        sentiment = str(m.get("sentiment", "neutral")).lower()
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        key = (t, v)
        if key in seen:
            continue
        seen.add(key)
        result.append({"attr_type": t, "value": v, "sentiment": sentiment, "confidence": confidence})
    return result


# ── data loading ───────────────────────────────────────────────────────────────

def load_reviews(reviews_csv: Path, min_text_len: int) -> dict[str, dict]:
    """Load review_id → {text, rating} for reviews with sufficient text."""
    result: dict[str, dict] = {}
    with reviews_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            text = clean_text(row.get("text", ""))
            if len(text) < min_text_len:
                continue
            result[row["review_id"]] = {
                "review_id": row["review_id"],
                "text": text,
                "rating": row.get("rating", ""),
            }
    return result


def load_about(about_csv: Path) -> dict[str, str]:
    """Load review_id → product_id mapping."""
    result: dict[str, str] = {}
    with about_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row["review_id"]] = row["product_id"]
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract review attribute mentions via LLM.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--reviews-csv", type=Path, help="Path to nodes_reviews.csv")
    parser.add_argument("--about-csv", type=Path, help="Path to rel_about.csv")
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=-1, help="-1 = all")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=2000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--min-text-len", type=int, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
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

    out_dir = args.config.resolve().parent / data_cfg.get("output_dir", "kg_output/video_games")
    reviews_csv = args.reviews_csv or (out_dir / "nodes_reviews.csv")
    about_csv = args.about_csv or (out_dir / "rel_about.csv")
    output_path = args.output_path or (out_dir / "attributes" / "review_mentions.jsonl")

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    min_text_len = args.min_text_len if args.min_text_len is not None else int(llm_cfg.get("min_review_text_len", 30))
    min_confidence = args.min_confidence if args.min_confidence is not None else float(llm_cfg.get("min_confidence", 0.6))

    client, model = build_client(provider, model_arg, cfg_base_url)
    use_responses_api = False  # use chat.completions for all providers

    # 語彙は extract_product_attributes.py と同じ attribute_vocab.yaml を共有する。
    # 既に商品属性抽出が生成/成長させた (attr_type, value) をそのまま引き継ぐ。
    genre = cfg.get("genre", "products")
    vocab_store = AttributeVocabStore()
    print(f"Prompt genre={genre!r}; shared attribute vocab loaded with {len(vocab_store.snapshot())} attr_types")

    print(f"Loading reviews from {reviews_csv}...")
    reviews_by_id = load_reviews(reviews_csv, min_text_len)
    print(f"  {len(reviews_by_id):,} reviews with text >= {min_text_len} chars")

    print(f"Loading about edges from {about_csv}...")
    about = load_about(about_csv)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(output_path, "review_id") if args.resume else set()
    limit = None if args.limit < 0 else args.limit

    all_review_ids = [rid for rid in reviews_by_id if rid not in done_ids and rid in about]
    if limit is not None:
        all_review_ids = all_review_ids[:limit]

    print(f"Processing {len(all_review_ids):,} reviews (batch_size={args.batch_size}, workers={args.workers})...")

    processed = 0
    pending: list[dict] = []
    futures: set[Future] = set()

    def process_batch(batch: list[dict]) -> list[dict]:
        payloads = [{"review_id": item["review_id"], "text": item["text"]} for item in batch]
        vocab_listing = format_vocab_listing(
            vocab_store.snapshot(), args.max_vocab_types_shown, args.max_vocab_values_shown,
        )
        system_prompt = build_system_prompt(genre, vocab_listing)
        try:
            mention_map, _usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)
        except Exception as exc:
            print(f"Batch extraction failed: {exc}", file=sys.stderr)
            mention_map = {}

        records: list[dict] = []
        for item in batch:
            rid = item["review_id"]
            mentions = normalize_mentions(mention_map.get(rid, []), min_confidence, vocab_store)
            records.append({
                "review_id": rid,
                "product_id": item["product_id"],
                "mentions": mentions,
            })
        return records

    def write_records(out: Any, records: list[dict]) -> None:
        for rec in records:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {rec['review_id']}  mentions={len(rec['mentions'])}")
        out.flush()

    with output_path.open("a", encoding="utf-8") as out:
        executor = ThreadPoolExecutor(max_workers=args.workers) if args.workers > 1 else None
        try:
            for rid in all_review_ids:
                rev = reviews_by_id[rid]
                pending.append({
                    "review_id": rid,
                    "product_id": about[rid],
                    "text": rev["text"],
                })

                if len(pending) >= args.batch_size:
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

            # flush remaining
            if pending:
                processed += len(pending)
                if executor is None:
                    write_records(out, process_batch(pending))
                else:
                    futures.add(executor.submit(process_batch, pending))

            if futures:
                done, _ = wait(futures)
                for f in done:
                    write_records(out, f.result())
        finally:
            if executor:
                executor.shutdown(wait=True)

    print(f"\nWrote {processed} review mention records to {output_path}")


if __name__ == "__main__":
    main()
