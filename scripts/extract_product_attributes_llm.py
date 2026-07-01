"""
Extract product attributes from meta JSONL using LLM.

Output (JSONL, one line per product):
  {"product_id": "...", "model": "...", "attributes": [
    {"attr_type": "skin_type", "value": "dry", "evidence": "...", "confidence": 0.9}
  ]}

attr_type is LLM-defined in snake_case. Post-normalization merges spelling variants.
Run build_attribute_csvs.py afterwards to produce Neo4j import CSVs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import gzip
import html
import json
import re
import sys
import time
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
    if value is None or (isinstance(value, float) and _is_nan(value)):
        return ""
    text = str(value).replace("\x00", " ")
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return _TEXT_WS.sub(" ", text).strip()


def _is_nan(v: float) -> bool:
    try:
        return v != v
    except Exception:
        return False


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def normalize_attr_type(raw: str) -> str:
    """Normalize LLM-generated attr_type to consistent snake_case."""
    lower = raw.lower().strip()
    snaked = _ATTR_WS.sub("_", lower)
    cleaned = _ATTR_NON_ALPHA.sub("", snaked)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "other"


def normalize_value(raw: str) -> str:
    return clean_text(raw).lower()


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

def build_system_prompt(genre: str, rule_attr_types: list[str]) -> str:
    """
    Build the LLM system prompt dynamically.

    rule_attr_types: attr_types already confirmed by rule-based extraction
    (derived from DETAIL_KEY_MAP.values()). These become the "prefer these"
    list so the LLM reuses the same names and the prompt adapts to each genre.
    """
    known = "\n".join(f"  {t}" for t in sorted(rule_attr_types))
    return f"""\
Extract product attributes for a knowledge graph of {genre}.

Return valid JSON only. Output shape:
{{"products":[{{"product_id":"...","attributes":[{{"attr_type":"...","value":"...","evidence":"short source phrase","confidence":0.0}}]}}]}}

RULE-CONFIRMED attr_type names (already extracted from structured fields — reuse these exactly when the attribute fits):
{known}

ADDITIONAL attr_types for free-text content (use when the above don't apply):
  ingredient   specific actives only: vitamin c, retinol, hyaluronic acid, niacinamide
  target_area  e.g. face, hair, eyes, lips, body, nails
  product_type e.g. moisturizer, shampoo, serum, toner, cleanser, mask, conditioner
  (You may invent new snake_case names only if none of the above truly fit)

FORBIDDEN attr_type — never use these:
  brand           (brand is a separate node in the graph; do not extract it)
  item_form       (→ use texture)
  product_benefit (→ use benefit)
  feature, other  (too generic)

Rules for value:
- Short, lowercase, normalized (e.g. "dry", "floral", "vitamin c")
- Do not repeat the attr_type in the value (attr_type="skin_type", value="dry" not "dry skin")

General rules:
- Return only attributes supported by the product record
- Skip generic labels like "UPC", "package dimensions", "ASIN"
- Do not infer medical claims or sensitive traits
- If a product is sparse, return an empty attributes list
- Avoid duplicate attr_type+value pairs for the same product
"""

# ── structured fields → rule-based attributes ─────────────────────────────────

DETAIL_KEY_MAP: dict[str, str] = {
    # "Brand" excluded: brand is already a first-class KG node (MADE_BY relationship)
    "Skin Type": "skin_type",
    "Scent": "scent",
    "Item Form": "texture",
    "Product Benefits": "benefit",
    "Material": "material",
    "Material Type": "material",
    "Color": "color",
    "Hair Type": "hair_type",
    "Unit Count": "size",
    "Size": "size",
    "Target Audience": "usage",
}

_SIZE_PAT = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|oz|ounce|ounces|ml|g|gram|grams|inch|inches|mm|cm|pcs|pack)\b",
    re.I,
)
_COLOR_WORDS = {"black", "white", "brown", "blonde", "red", "pink", "blue", "green", "gold", "silver", "purple", "clear"}


def rule_attributes(row: dict[str, Any]) -> list[dict[str, Any]]:
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
    for key, attr_type in DETAIL_KEY_MAP.items():
        if key in details:
            add(attr_type, details[key], f"{key}: {details[key]}", 0.9)

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

def product_payload(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    budget = max(120, max_chars // 4)
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    return {
        "product_id": row.get("parent_asin"),
        "title": clean_text(row.get("title"))[:budget],
        "store": clean_text(row.get("store")),
        "features": [clean_text(x)[:budget] for x in as_list(row.get("features"))[:5] if clean_text(x)],
        "description": [clean_text(x)[:budget] for x in as_list(row.get("description"))[:3] if clean_text(x)],
        "details": {k: details[k] for k in DETAIL_KEY_MAP if k in details and clean_text(details[k])},
    }


def is_sparse(payload: dict[str, Any]) -> bool:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    return bool(payload.get("title")) and not payload.get("features") and not payload.get("description") and len(details) <= 1


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


def split_usage(usage: dict[str, int], parts: int) -> dict[str, int]:
    if parts <= 1:
        return dict(usage)
    return {k: round(v / parts) for k, v in usage.items()}


def extract_batch(
    client: Any,
    model: str,
    payloads: list[dict[str, Any]],
    system_prompt: str,
    max_output_tokens: int,
    retries: int,
    use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    user_content = json.dumps({"task": "Extract product attributes.", "products": payloads}, ensure_ascii=False)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        try:
            if use_responses_api:
                resp = client.responses.create(
                    model=model,
                    input=messages,
                    text={"format": {"type": "json_schema", "name": "attrs", "schema": ATTRIBUTE_SCHEMA, "strict": True}},
                    temperature=0,
                    max_output_tokens=max_output_tokens,
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
            return {p["product_id"]: p.get("attributes", []) for p in parsed.get("products", [])}, usage
        except Exception as exc:
            last_err = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(f"LLM extraction failed after {retries + 1} attempts: {last_err}") from last_err


def extract_with_fallback(
    client: Any, model: str, payloads: list[dict], system_prompt: str,
    max_output_tokens: int, retries: int, use_responses_api: bool,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    try:
        return extract_batch(client, model, payloads, system_prompt, max_output_tokens, retries, use_responses_api)
    except Exception as batch_err:
        if len(payloads) <= 1:
            raise
        print(f"Batch failed; retrying one-by-one: {batch_err}", file=sys.stderr)
        merged: dict[str, list[dict]] = {}
        total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for p in payloads:
            try:
                m, u = extract_batch(client, model, [p], system_prompt, max_output_tokens, retries, use_responses_api)
                merged.update(m)
                for k in total:
                    total[k] += u.get(k, 0)
            except Exception as exc:
                print(f"Single fallback failed for {p.get('product_id')}: {exc}", file=sys.stderr)
                merged[str(p.get("product_id", ""))] = []
        return merged, total


# ── normalization post-processing ──────────────────────────────────────────────

def normalize_attrs(attrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize attr_type to snake_case and deduplicate."""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for a in attrs:
        t = normalize_attr_type(str(a.get("attr_type", "")))
        v = normalize_value(str(a.get("value", "")))
        if not t or not v:
            continue
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


def merge_attrs(rule: list[dict], llm: list[dict], do_normalize: bool) -> list[dict]:
    combined = rule + llm
    if do_normalize:
        return normalize_attrs(combined)
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for a in combined:
        key = (str(a.get("attr_type", "")).lower(), str(a.get("value", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(a)
    return result


# ── I/O helpers ────────────────────────────────────────────────────────────────

def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_done_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    done: set[str] = set()
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if pid := row.get("product_id"):
                    done.add(str(pid))
            except json.JSONDecodeError:
                continue
    return done


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract product attributes via LLM.")
    parser.add_argument("--config", type=Path, default=Path("../config.yaml"))
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})

    meta_path = args.meta_path or Path(data_cfg.get("meta_path", "data/meta_All_Beauty.jsonl.gz"))
    out_dir = Path(data_cfg.get("output_dir", "kg_output/all_beauty"))
    output_path = args.output_path or (out_dir / "attributes" / "product_attributes.jsonl")

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    do_normalize = llm_cfg.get("attr_type_normalize", True)
    min_confidence = args.min_confidence if args.min_confidence is not None else llm_cfg.get("min_confidence", 0.6)

    if not args.rule_only:
        client, model = build_client(provider, model_arg, cfg_base_url)
    else:
        client, model = None, model_arg or "rules"
    use_responses_api = False  # use chat.completions for all providers

    # Build system prompt dynamically from DETAIL_KEY_MAP (genre-adaptive)
    genre = cfg.get("genre", "products")
    rule_attr_types = sorted(set(DETAIL_KEY_MAP.values()))
    system_prompt = build_system_prompt(genre, rule_attr_types)
    print(f"Prompt built for genre={genre!r}, rule-confirmed types: {rule_attr_types}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(output_path) if args.resume else set()
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
            llm_map, usage = extract_with_fallback(client, model, payloads, system_prompt, args.max_output_tokens, args.retries, use_responses_api)

        records: list[dict] = []
        per_usage = split_usage(usage, len(batch))
        for item in batch:
            pid = item["product_id"]
            llm_attrs = [a for a in llm_map.get(pid, []) if float(a.get("confidence", 0)) >= min_confidence]
            attrs = merge_attrs(item["rule_attrs"], llm_attrs, do_normalize)
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

                rule_attrs = rule_attributes(row)
                payload = product_payload(row, args.max_input_chars)

                if args.skip_sparse and is_sparse(payload):
                    with output_path.open("a", encoding="utf-8") as _out:
                        _out.write(json.dumps({
                            "product_id": pid, "model": "rules",
                            "attributes": normalize_attrs(rule_attrs) if do_normalize else rule_attrs,
                            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        }, ensure_ascii=False) + "\n")
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

    print(f"\nWrote {processed} products to {output_path}")


if __name__ == "__main__":
    main()
