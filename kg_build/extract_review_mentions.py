"""
Extract attribute mentions from review text using LLM.

Reads:  nodes_reviews.csv  (review_id, text, rating, ...)
        rel_about.csv      (review_id, product_id)
Writes: review_mentions.jsonl  (one line per review)

Output schema per line:
  {"review_id": "...", "product_id": "...", "mentions": [
    {"attr_type": "build_quality", "value": "sturdy", "sentiment": "positive", "confidence": 0.9}
  ]}

attr_type/value are drawn from the same GROWABLE vocabulary as extract_product_attributes.py
(attr_vocab.yaml, seeded by propose_attr_vocab.py and reviewed by hand) — this keeps
HAS_ATTRIBUTE and MENTIONS sharing one attr_type/value namespace. The LLM is told to
reuse an existing (attr_type, value) pair whenever one fits, and may introduce a new
one only when truly necessary; when that happens it's added to the same database (see
utils/attr_vocab.py's GrowableVocab), not dropped, so later batches in this same run
see and can reuse it too.

By default, reviews are capped to --max-reviews-per-product (50) per product, keeping
the ones ranked highest by helpful_vote then rating (same tiebreaker as get_reviews()
in app/api/recommender.py) — this avoids paying full LLM cost on products with a very
long tail of reviews. Pass -1 to disable the cap.

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

from utils.attr_vocab import GrowableVocab
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
                            },
                            "required": ["attr_type", "value", "sentiment", "confidence"],
                        },
                    },
                },
                "required": ["review_id", "mentions"],
            },
        }
    },
    "required": ["reviews"],
}

def build_system_prompt(genre: str, vocab_text: str) -> str:
    """Build the LLM system prompt. vocab_text is the CURRENT state of the growable
    attr_type/value vocabulary (GrowableVocab.prompt_text(), shared with
    extract_product_attributes.py) — call this fresh for every LLM call, not once
    per run, so growth from earlier batches is visible."""
    return f"""\
Extract product attribute mentions from customer reviews of {genre}.

Return valid JSON only. Output shape:
{{"reviews":[{{"review_id":"...","mentions":[{{"attr_type":"...","value":"...","sentiment":"positive|negative|neutral","confidence":0.0}}]}}]}}

EXISTING attr_type/value vocabulary — STRONGLY prefer reusing one of these exact
(attr_type, value) combinations when it fits. When an entry lists "allowed values",
treat those as the known values for that attr_type. Only introduce a new attr_type,
or a new value under an existing attr_type, when you are confident nothing existing
truly fits — new entries become part of the shared vocabulary, so invent one only
when genuinely necessary, not as a default:
{vocab_text}

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


def normalize_mentions(raw: list[dict], min_confidence: float, vocab: GrowableVocab) -> list[dict]:
    """Every mention is resolved against vocab with growth allowed — reused if
    already known, added to the shared database if not (see GrowableVocab.resolve())."""
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for m in raw:
        raw_t = str(m.get("attr_type", ""))
        raw_v = str(m.get("value", ""))
        sentiment = str(m.get("sentiment", "neutral")).lower()
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        confidence = float(m.get("confidence", 0.0))
        if not raw_t or not raw_v or confidence < min_confidence:
            continue
        resolved = vocab.resolve(raw_t, raw_v, allow_growth=True)
        if resolved is None:
            continue
        t, v = resolved
        key = (t, v)
        if key in seen:
            continue
        seen.add(key)
        result.append({"attr_type": t, "value": v, "sentiment": sentiment, "confidence": confidence})
    return result


# ── data loading ───────────────────────────────────────────────────────────────

def load_reviews(reviews_csv: Path, min_text_len: int) -> dict[str, dict]:
    """Load review_id → {text, rating, helpful_vote} for reviews with sufficient text."""
    result: dict[str, dict] = {}
    with reviews_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            text = clean_text(row.get("text", ""))
            if len(text) < min_text_len:
                continue
            try:
                helpful_vote = int(float(row.get("helpful_vote") or 0))
            except ValueError:
                helpful_vote = 0
            try:
                rating = float(row.get("rating") or 0)
            except ValueError:
                rating = 0.0
            result[row["review_id"]] = {
                "review_id": row["review_id"],
                "text": text,
                "rating": row.get("rating", ""),
                "helpful_vote": helpful_vote,
                "_rating_num": rating,
            }
    return result


def load_about(about_csv: Path) -> dict[str, str]:
    """Load review_id → product_id mapping."""
    result: dict[str, str] = {}
    with about_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row["review_id"]] = row["product_id"]
    return result


def cap_reviews_per_product(
    review_ids: list[str], reviews_by_id: dict[str, dict], about: dict[str, str], max_per_product: int,
) -> list[str]:
    """Keep only the top max_per_product reviews per product, ranked by
    helpful_vote desc then rating desc (same tiebreaker as get_reviews() in
    app/api/recommender.py) — mirrors what the app already treats as
    'most informative reviews first'."""
    by_product: dict[str, list[str]] = {}
    for rid in review_ids:
        pid = about.get(rid, "")
        by_product.setdefault(pid, []).append(rid)

    kept: list[str] = []
    for pid, rids in by_product.items():
        rids.sort(
            key=lambda rid: (-reviews_by_id[rid]["helpful_vote"], -reviews_by_id[rid]["_rating_num"], rid),
        )
        kept.extend(rids[:max_per_product])
    return kept


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
    parser.add_argument(
        "--max-reviews-per-product", type=int, default=50,
        help="Cap reviews processed per product, ranked by helpful_vote desc then rating desc "
             "(same tiebreaker as get_reviews()). -1 = no cap.",
    )
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=2000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--min-text-len", type=int, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--attr-vocab-path", type=Path, default=None,
                         help="Path to attr_vocab.yaml (default: <output_dir>/attributes/attr_vocab.yaml)")
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

    genre = cfg.get("genre", "products")
    attrs_dir = out_dir / "attributes"
    vocab_path = args.attr_vocab_path or (attrs_dir / "attr_vocab.yaml")
    vocab = GrowableVocab(vocab_path)
    if vocab.type_count() == 0:
        print(
            f"WARNING: {vocab_path} not found or empty — starting from an empty vocabulary "
            f"(every mention will be a 'new' addition). Run kg_build/propose_attr_vocab.py "
            f"first for a better starting point.",
            file=sys.stderr,
        )
    print(f"Prompt will draw from genre={genre!r}, {vocab.type_count()} attr_types currently in vocabulary")

    print(f"Loading reviews from {reviews_csv}...")
    reviews_by_id = load_reviews(reviews_csv, min_text_len)
    print(f"  {len(reviews_by_id):,} reviews with text >= {min_text_len} chars")

    print(f"Loading about edges from {about_csv}...")
    about = load_about(about_csv)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(output_path, "review_id") if args.resume else set()
    limit = None if args.limit < 0 else args.limit

    all_review_ids = [rid for rid in reviews_by_id if rid not in done_ids and rid in about]
    if args.max_reviews_per_product >= 0:
        before = len(all_review_ids)
        all_review_ids = cap_reviews_per_product(all_review_ids, reviews_by_id, about, args.max_reviews_per_product)
        print(f"  capped to {args.max_reviews_per_product} reviews/product: {before:,} -> {len(all_review_ids):,}")
    if limit is not None:
        all_review_ids = all_review_ids[:limit]

    print(f"Processing {len(all_review_ids):,} reviews (batch_size={args.batch_size}, workers={args.workers})...")

    processed = 0
    pending: list[dict] = []
    futures: set[Future] = set()

    def process_batch(batch: list[dict]) -> list[dict]:
        payloads = [{"review_id": item["review_id"], "text": item["text"]} for item in batch]
        try:
            # built fresh from the live vocab so growth from earlier batches (this run,
            # possibly on another worker thread) is visible to this call
            system_prompt = build_system_prompt(genre, vocab.prompt_text())
            mention_map, usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)
        except Exception as exc:
            print(f"Batch extraction failed: {exc}", file=sys.stderr)
            mention_map = {}
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        records: list[dict] = []
        per_usage = {k: round(v / len(batch)) for k, v in usage.items()}
        for item in batch:
            rid = item["review_id"]
            mentions = normalize_mentions(mention_map.get(rid, []), min_confidence, vocab)
            records.append({
                "review_id": rid,
                "product_id": item["product_id"],
                "mentions": mentions,
                "usage": per_usage,
            })
        return records

    def write_records(out: Any, records: list[dict]) -> None:
        for rec in records:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {rec['review_id']}  mentions={len(rec['mentions'])}")
        out.flush()
        vocab.save()  # persist any growth from this batch immediately (crash-safe,
                       # and visible to later batches via process_batch's fresh prompt_text())

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

    vocab.save()
    print(f"\nWrote {processed} review mention records to {output_path}")
    growth = vocab.growth_summary()
    if growth:
        print(f"Vocabulary grew this run — {growth}\n  -> review {vocab_path} by hand if this looks noisy.")
    else:
        print(f"No vocabulary growth this run — {vocab.type_count()} attr_types, all reused.")


if __name__ == "__main__":
    main()
