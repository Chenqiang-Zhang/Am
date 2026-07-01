"""
Extract attribute mentions from review text using LLM.

Reads:  nodes_reviews.csv  (review_id, text, rating, ...)
        rel_about.csv      (review_id, product_id)
Writes: review_mentions.jsonl  (one line per review)

Output schema per line:
  {"review_id": "...", "product_id": "...", "mentions": [
    {"attr_type": "scent", "value": "floral", "sentiment": "positive", "confidence": 0.9}
  ]}

Run build_attribute_csvs.py afterwards to produce Neo4j import CSVs.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import yaml
from llm_client import build_client, provider_from_config


# ── text utilities ─────────────────────────────────────────────────────────────

_HTML_TAG = re.compile(r"<[^>]+>")
_TEXT_WS = re.compile(r"\s+")
_ATTR_WS = re.compile(r"[\s\-]+")
_ATTR_NON_ALPHA = re.compile(r"[^a-z0-9_]")


def clean_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value).replace("\x00", " ")
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return _TEXT_WS.sub(" ", text).strip()


def normalize_attr_type(raw: str) -> str:
    lower = raw.lower().strip()
    snaked = _ATTR_WS.sub("_", lower)
    cleaned = _ATTR_NON_ALPHA.sub("", snaked)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "other"


def normalize_value(raw: str) -> str:
    return clean_text(raw).lower()


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

def build_system_prompt(genre: str, known_attr_types: list[str]) -> str:
    """
    Build the LLM system prompt dynamically.

    known_attr_types: attr_types already in use (collected from product_attributes.jsonl
    or DETAIL_KEY_MAP). The LLM is told to reuse these names for consistency.
    """
    known = "\n".join(f"  {t}" for t in sorted(known_attr_types))
    return f"""\
Extract product attribute mentions from customer reviews of {genre}.

Return valid JSON only. Output shape:
{{"reviews":[{{"review_id":"...","mentions":[{{"attr_type":"...","value":"...","sentiment":"positive|negative|neutral","confidence":0.0}}]}}]}}

KNOWN attr_type names (already used in this knowledge graph — reuse these exactly when the attribute fits):
{known}
  (You may invent new snake_case names only if none of the above truly fit)

FORBIDDEN attr_type — never use:
  brand           (brand is a separate node; do not extract it)
  item_form       (→ use texture)
  product_benefit (→ use benefit)
  feature, other  (too generic)

Rules for value:
- Short, lowercase, normalized (e.g. "floral", "vitamin c")
- Do not repeat the attr_type in the value

Rules for sentiment:
- positive: reviewer views this attribute favorably ("smells amazing", "great texture")
- negative: reviewer views this attribute unfavorably ("terrible scent", "too thick")
- neutral: mentioned without clear opinion

General rules:
- Only extract attributes explicitly stated in the review text
- Skip generic comments about shipping, packaging condition, seller, or price
- Skip subjective overall opinions ("great product", "love it") unless tied to a specific attribute
- Avoid duplicating attr_type+value pairs in the same review
"""


# ── API helpers ────────────────────────────────────────────────────────────────

def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def normalize_usage(usage: Any) -> dict[str, int]:
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def extract_batch(
    client: Any,
    model: str,
    reviews: list[dict[str, Any]],
    system_prompt: str,
    max_output_tokens: int,
    retries: int,
    use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    user_content = json.dumps(
        {"task": "Extract attribute mentions from reviews.", "reviews": reviews},
        ensure_ascii=False,
    )
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        try:
            if use_responses_api:
                resp = client.responses.create(
                    model=model, input=messages,
                    text={"format": {"type": "json_schema", "name": "mentions", "schema": MENTIONS_SCHEMA, "strict": True}},
                    temperature=0, max_output_tokens=max_output_tokens,
                )
                raw = getattr(resp, "output_text", None) or "".join(
                    c.text for item in (getattr(resp, "output", []) or []) for c in (getattr(item, "content", []) or []) if getattr(c, "type", "") == "output_text"
                )
                usage = normalize_usage(getattr(resp, "usage", {}))
            else:
                resp = client.chat.completions.create(
                    model=model, messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0, max_tokens=max_output_tokens,
                )
                raw = resp.choices[0].message.content or "{}"
                usage = normalize_usage(getattr(resp, "usage", {}))

            parsed = parse_json_text(raw)
            return {r["review_id"]: r.get("mentions", []) for r in parsed.get("reviews", [])}, usage
        except Exception as exc:
            last_err = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(f"LLM mention extraction failed: {last_err}") from last_err


def extract_with_fallback(
    client: Any, model: str, reviews: list[dict], system_prompt: str,
    max_output_tokens: int, retries: int, use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    try:
        return extract_batch(client, model, reviews, system_prompt, max_output_tokens, retries, use_responses_api)
    except Exception as batch_err:
        if len(reviews) <= 1:
            raise
        print(f"Batch failed; retrying one-by-one: {batch_err}", file=sys.stderr)
        merged: dict[str, list[dict]] = {}
        total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for r in reviews:
            try:
                m, u = extract_batch(client, model, [r], system_prompt, max_output_tokens, retries, use_responses_api)
                merged.update(m)
                for k in total:
                    total[k] += u.get(k, 0)
            except Exception as exc:
                print(f"Single fallback failed for {r.get('review_id')}: {exc}", file=sys.stderr)
                merged[str(r.get("review_id", ""))] = []
        return merged, total


# ── normalization ──────────────────────────────────────────────────────────────

VALID_SENTIMENTS = {"positive", "negative", "neutral"}


def normalize_mentions(raw: list[dict], min_confidence: float) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for m in raw:
        t = normalize_attr_type(str(m.get("attr_type", "")))
        v = normalize_value(str(m.get("value", "")))
        sentiment = str(m.get("sentiment", "neutral")).lower()
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        confidence = float(m.get("confidence", 0.0))
        if not t or not v or confidence < min_confidence:
            continue
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


def load_done_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    done: set[str] = set()
    with output_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if rid := row.get("review_id"):
                    done.add(str(rid))
            except json.JSONDecodeError:
                continue
    return done


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract review attribute mentions via LLM.")
    parser.add_argument("--config", type=Path, default=Path("../config.yaml"))
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})

    out_dir = Path(data_cfg.get("output_dir", "kg_output/all_beauty"))
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

    # Build system prompt: collect known attr_types from product_attributes.jsonl
    # (already produced by extract_product_attributes_llm.py) for consistency
    genre = cfg.get("genre", "products")
    attr_path = out_dir / "attributes" / "product_attributes.jsonl"
    known_attr_types: set[str] = set()
    if attr_path.exists():
        with attr_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    for a in json.loads(line).get("attributes", []):
                        if t := a.get("attr_type"):
                            known_attr_types.add(t)
                except json.JSONDecodeError:
                    pass
    if not known_attr_types:
        # Fallback: default types if attributes file not yet available
        known_attr_types = {
            "skin_type", "hair_type", "scent", "texture", "benefit",
            "ingredient", "material", "color", "size", "target_area",
            "usage", "product_type",
        }
    system_prompt = build_system_prompt(genre, sorted(known_attr_types))
    print(f"Prompt built for genre={genre!r}, known attr_types ({len(known_attr_types)}): {sorted(known_attr_types)}")

    print(f"Loading reviews from {reviews_csv}...")
    reviews_by_id = load_reviews(reviews_csv, min_text_len)
    print(f"  {len(reviews_by_id):,} reviews with text >= {min_text_len} chars")

    print(f"Loading about edges from {about_csv}...")
    about = load_about(about_csv)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(output_path) if args.resume else set()
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
        try:
            mention_map, usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)
        except Exception as exc:
            print(f"Batch extraction failed: {exc}", file=sys.stderr)
            mention_map = {}
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        records: list[dict] = []
        per_usage = {k: round(v / len(batch)) for k, v in usage.items()}
        for item in batch:
            rid = item["review_id"]
            mentions = normalize_mentions(mention_map.get(rid, []), min_confidence)
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
