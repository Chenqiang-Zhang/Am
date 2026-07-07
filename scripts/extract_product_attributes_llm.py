from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


ATTRIBUTE_TYPES = [
    "benefit",
    "skin_type",
    "scent",
    "texture",
    "ingredient",
    "material",
    "color",
    "size",
    "target_area",
    "usage",
    "brand",
    "product_type",
    "other",
]

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
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                                "attribute_type": {"type": "string", "enum": ATTRIBUTE_TYPES},
                                "evidence": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["name", "value", "attribute_type", "evidence", "confidence"],
                        },
                    },
                },
                "required": ["product_id", "attributes"],
            },
        }
    },
    "required": ["products"],
}

SYSTEM_PROMPT = """Extract product attributes for a beauty product knowledge graph.

Return valid JSON only. Output shape:
{"products":[{"product_id":"...","attributes":[{"name":"...","value":"...","attribute_type":"benefit|skin_type|scent|texture|ingredient|material|color|size|target_area|usage|brand|product_type|other","evidence":"short source phrase","confidence":0.0}]}]}

Rules:
- Return only attributes supported by the product record.
- Prefer short normalized values.
- Skip generic labels such as "features", "specification", "package dimensions", and UPC.
- Do not infer medical claims or sensitive traits.
- If a product is sparse or unrelated, return an empty attributes list.
- Avoid duplicate attributes with the same name and value.
"""

DETAIL_KEY_MAP = {
    "Brand": ("brand", "brand"),
    "Skin Type": ("skin_type", "skin_type"),
    "Scent": ("scent", "scent"),
    "Item Form": ("texture", "texture"),
    "Product Benefits": ("benefit", "benefit"),
    "Material": ("material", "material"),
    "Material Type": ("material", "material"),
    "Color": ("color", "color"),
    "Hair Type": ("hair_type", "other"),
    "Unit Count": ("size", "size"),
    "Size": ("size", "size"),
    "Target Audience": ("usage", "usage"),
}

COLOR_WORDS = {
    "black",
    "white",
    "brown",
    "blonde",
    "red",
    "pink",
    "blue",
    "green",
    "gold",
    "silver",
    "purple",
    "clear",
}

SIZE_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|oz|ounce|ounces|ml|g|gram|grams|inch|inches|mm|cm|pcs|pack)\b", re.I)


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def read_jsonl(path: Path):
    open_fn = gzip.open if str(path).endswith(".gz") else open
    with open_fn(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split()).strip()


def compact_text(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return clean_text(text)[:max_chars]


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def first_words(text: str, max_words: int = 18) -> str:
    return " ".join(text.split()[:max_words])


def normalize_attribute_value(value: Any, max_words: int = 12, max_chars: int = 96) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*see\s+more\s*$", "", text, flags=re.I).strip()
    text = re.sub(r"(.{20,}?)\1+", r"\1", text)
    if len(text) > max_chars:
        text = first_words(text, max_words)
    return text[:max_chars].strip(" ,.;:")


def add_attr(attrs: list[dict[str, Any]], seen: set[tuple[str, str]], name: str, value: Any, attr_type: str, evidence: str, confidence: float) -> None:
    value_text = normalize_attribute_value(value)
    if not value_text:
        return
    key = (name.lower(), value_text.lower())
    if key in seen:
        return
    seen.add(key)
    attrs.append(
        {
            "name": name,
            "value": value_text,
            "attribute_type": attr_type if attr_type in ATTRIBUTE_TYPES else "other",
            "evidence": first_words(clean_text(evidence), 14),
            "confidence": confidence,
        }
    )


def rule_attributes(row: dict[str, Any]) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    store = clean_text(row.get("store"))
    if store:
        add_attr(attrs, seen, "brand", store, "brand", f"store: {store}", 0.95)

    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    for key, (name, attr_type) in DETAIL_KEY_MAP.items():
        if key in details:
            add_attr(attrs, seen, name, details[key], attr_type, f"{key}: {details[key]}", 0.9)

    title = clean_text(row.get("title"))
    for match in SIZE_PATTERN.findall(title):
        add_attr(attrs, seen, "size", match, "size", f"title: {match}", 0.75)
        break

    title_lower = title.lower()
    for color in COLOR_WORDS:
        if re.search(rf"\b{re.escape(color)}\b", title_lower):
            add_attr(attrs, seen, "color", color, "color", f"title: {color}", 0.65)
            break

    return attrs


def product_payload(row: dict[str, Any], max_input_chars: int, compact_input: bool) -> dict[str, Any]:
    text_budget = max(120, max_input_chars // 4)
    if compact_input:
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        detail_payload = {
            key: details[key]
            for key in DETAIL_KEY_MAP
            if key in details and clean_text(details[key])
        }
        payload = {
            "id": row.get("parent_asin"),
            "title": clean_text(row.get("title"))[:text_budget],
            "store": clean_text(row.get("store")),
            "features": [clean_text(x)[:text_budget] for x in as_list(row.get("features"))[:5] if clean_text(x)],
            "description": [clean_text(x)[:text_budget] for x in as_list(row.get("description"))[:3] if clean_text(x)],
            "details": detail_payload,
        }
    else:
        keys = [
            "parent_asin",
            "title",
            "main_category",
            "store",
            "categories",
            "features",
            "description",
            "details",
        ]
        payload = {key: row.get(key) for key in keys if key in row}

    return payload


def is_sparse(product: dict[str, Any]) -> bool:
    title = clean_text(product.get("title") or product.get("parent_asin") or product.get("id"))
    features = as_list(product.get("features"))
    description = as_list(product.get("description"))
    details = product.get("details") if isinstance(product.get("details"), dict) else {}
    useful_details = [k for k, v in details.items() if clean_text(v)]
    return bool(title) and not features and not description and len(useful_details) <= 1


def usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {}


def normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }


def split_usage(usage: dict[str, Any], parts: int) -> dict[str, int]:
    if parts <= 1:
        return {key: int(value or 0) for key, value in usage.items()}
    return {key: int(round((value or 0) / parts)) for key, value in usage.items()}


def output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) == "output_text":
                chunks.append(getattr(content, "text", ""))
    return "".join(chunks)


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def load_done_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()

    done: set[str] = set()
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            product_id = row.get("product_id")
            if product_id:
                done.add(str(product_id))
    return done


def merge_attributes(rule_attrs: list[dict[str, Any]], llm_attrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for attr in rule_attrs + llm_attrs:
        name = clean_text(attr.get("name"))
        value = clean_text(attr.get("value"))
        if not name or not value:
            continue
        key = (name.lower(), value.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "name": name,
                "value": value,
                "attribute_type": attr.get("attribute_type") if attr.get("attribute_type") in ATTRIBUTE_TYPES else "other",
                "evidence": clean_text(attr.get("evidence")),
                "confidence": attr.get("confidence", 0.7),
            }
        )
    return merged


def extract_batch_openai_responses(
    client: Any,
    model: str,
    products: list[dict[str, Any]],
    max_output_tokens: int,
    retries: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    user_input = {"task": "Extract normalized product attributes.", "products": products}
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_input, ensure_ascii=False, separators=(",", ":"))},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "product_attribute_extraction",
                        "schema": ATTRIBUTE_SCHEMA,
                        "strict": True,
                    }
                },
                temperature=0,
                max_output_tokens=max_output_tokens,
            )
            parsed = parse_json_text(output_text(response))
            return {p["product_id"]: p.get("attributes", []) for p in parsed.get("products", [])}, normalize_usage(usage_dict(response))
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2**attempt, 30))

    raise RuntimeError(f"OpenAI extraction failed: {last_error}") from last_error


def extract_batch_chat_json(
    client: Any,
    model: str,
    products: list[dict[str, Any]],
    max_output_tokens: int,
    retries: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    user_input = {"task": "Extract normalized product attributes.", "products": products}
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_input, ensure_ascii=False, separators=(",", ":"))},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=max_output_tokens,
            )
            content = response.choices[0].message.content or "{}"
            parsed = parse_json_text(content)
            return {p["product_id"]: p.get("attributes", []) for p in parsed.get("products", [])}, normalize_usage(usage_dict(response))
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2**attempt, 30))

    raise RuntimeError(f"Chat JSON extraction failed: {last_error}") from last_error


def extract_chat_with_fallback(
    client: Any,
    model: str,
    products: list[dict[str, Any]],
    max_output_tokens: int,
    retries: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    try:
        return extract_batch_chat_json(client, model, products, max_output_tokens, retries)
    except Exception as batch_error:
        if len(products) <= 1:
            product_id = products[0].get("product_id") if products else ""
            print(f"Single-product JSON failed; using rules only for {product_id}: {batch_error}", file=sys.stderr)
            return {str(product_id): []}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        print(f"Batch JSON failed; retrying {len(products)} products one by one: {batch_error}", file=sys.stderr)
        merged: dict[str, list[dict[str, Any]]] = {}
        total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for product in products:
            try:
                one_map, one_usage = extract_batch_chat_json(client, model, [product], max_output_tokens, retries)
                merged.update(one_map)
                for key in total_usage:
                    total_usage[key] += int(one_usage.get(key) or 0)
            except Exception as exc:
                print(f"Single-product JSON failed; using rules only for {product.get('product_id')}: {exc}", file=sys.stderr)
                merged[str(product.get("product_id", ""))] = []
        return merged, total_usage


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Extract product attributes with OpenAI or DeepSeek-compatible APIs.")
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--output-path", type=Path, default=Path("kg_output/attributes/product_attributes_llm.jsonl"))
    parser.add_argument("--provider", choices=["openai", "deepseek"], default=os.environ.get("LLM_PROVIDER", "openai"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=20, help="Number of products to process. Use -1 for all rows.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between API calls.")
    parser.add_argument("--max-input-chars", type=int, default=1800)
    parser.add_argument("--max-output-tokens", type=int, default=1800)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--resume", action="store_true", help="Skip product_ids already present in the output JSONL.")
    parser.add_argument("--compact-input", action="store_true", help="Send only high-value compact fields to the LLM.")
    parser.add_argument("--skip-sparse", action="store_true", help="Use rule-only attributes for sparse products instead of calling the LLM.")
    parser.add_argument("--batch-size", type=int, default=1, help="Products per LLM request. Use 5-10 for cheaper bulk extraction.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent LLM batches. Start with 3-5 to avoid rate limits.")
    parser.add_argument("--rule-only", action="store_true", help="Do not call an LLM; only use deterministic metadata rules.")
    parser.add_argument("--timeout", type=float, default=45.0, help="Per-request API timeout in seconds.")
    return parser.parse_args()


def build_client(args: argparse.Namespace) -> tuple[Any | None, str, str]:
    if args.provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key and not args.rule_only:
            print("DEEPSEEK_API_KEY is not set.", file=sys.stderr)
            sys.exit(2)
        model = args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if args.rule_only:
            return None, model, base_url
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key and not args.rule_only:
            print("OPENAI_API_KEY is not set.", file=sys.stderr)
            sys.exit(2)
        model = args.model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if args.rule_only:
            return None, model, base_url or ""

    try:
        from openai import OpenAI
    except ImportError:
        print("The openai package is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)

    if args.provider == "deepseek":
        return OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=base_url, timeout=args.timeout), model, base_url
    return (
        OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=base_url, timeout=args.timeout)
        if base_url
        else OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=args.timeout)
    ), model, base_url or ""


def write_records(out: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
    out.flush()


def main() -> None:
    args = parse_args()
    if args.batch_size < 1:
        print("--batch-size must be >= 1", file=sys.stderr)
        sys.exit(2)
    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        sys.exit(2)

    client, model, _base_url = build_client(args)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(args.output_path) if args.resume else set()
    limit = None if args.limit < 0 else args.limit
    processed = 0
    seen = 0
    pending: list[dict[str, Any]] = []
    futures: set[Future[list[dict[str, Any]]]] = set()

    def process_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        products = [item["payload"] for item in batch]
        if args.rule_only:
            llm_map: dict[str, list[dict[str, Any]]] = {}
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        elif args.provider == "deepseek":
            llm_map, usage = extract_chat_with_fallback(client, model, products, args.max_output_tokens, args.retries)
        else:
            llm_map, usage = extract_batch_openai_responses(client, model, products, args.max_output_tokens, args.retries)

        records: list[dict[str, Any]] = []
        per_record_usage = split_usage(usage, len(batch))
        for item in batch:
            product_id = item["product_id"]
            attributes = merge_attributes(item["rule_attributes"], llm_map.get(product_id, []))
            records.append(
                {
                    "product_id": product_id,
                    "title": item["title"],
                    "provider": "rules" if args.rule_only else args.provider,
                    "model": "rules" if args.rule_only else model,
                    "attributes": attributes,
                    "usage": per_record_usage,
                    "batch_usage": usage,
                    "batch_size": len(batch),
                }
            )
        return records

    def write_completed(out: Any, records: list[dict[str, Any]]) -> None:
        write_records(out, records)
        for record in records:
            print(f"saved: {record['product_id']} attributes={len(record['attributes'])}")

    def drain_futures(out: Any, block: bool) -> None:
        nonlocal futures
        if not futures:
            return
        if block:
            done, futures = wait(futures)
        else:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
        for future in done:
            write_completed(out, future.result())

    def flush_pending(out: Any, executor: ThreadPoolExecutor | None) -> None:
        nonlocal processed, pending, futures
        if not pending:
            return

        batch = pending
        pending = []
        processed += len(batch)
        if executor is None:
            write_completed(out, process_batch(batch))
        else:
            futures.add(executor.submit(process_batch, batch))
            if len(futures) >= args.workers * 2:
                drain_futures(out, block=False)
        if args.sleep:
            time.sleep(args.sleep)

    with args.output_path.open("a", encoding="utf-8") as out:
        executor = ThreadPoolExecutor(max_workers=args.workers) if args.workers > 1 and not args.rule_only else None
        try:
            for row in read_jsonl(args.meta_path):
                if seen < args.offset:
                    seen += 1
                    continue
                if limit is not None and processed + len(pending) >= limit:
                    break

                product_id = clean_text(row.get("parent_asin"))
                seen += 1
                if not product_id or product_id in done_ids:
                    continue

                rule_attrs = rule_attributes(row)
                payload = product_payload(row, args.max_input_chars, args.compact_input)
                payload["product_id"] = product_id

                if args.skip_sparse and is_sparse(payload):
                    record = {
                        "product_id": product_id,
                        "title": row.get("title", ""),
                        "provider": "rules",
                        "model": "rules",
                        "attributes": rule_attrs,
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    }
                    write_records(out, [record])
                    processed += 1
                    print(f"{processed}: {product_id} attributes={len(rule_attrs)} rule-only")
                    continue

                pending.append(
                    {
                        "product_id": product_id,
                        "title": row.get("title", ""),
                        "payload": payload,
                        "rule_attributes": rule_attrs,
                    }
                )
                if len(pending) >= args.batch_size:
                    flush_pending(out, executor)

            flush_pending(out, executor)
            drain_futures(out, block=True)
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

    print(f"Wrote {processed} product attribute rows to {args.output_path}")


if __name__ == "__main__":
    main()
