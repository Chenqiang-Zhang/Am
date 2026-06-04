from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ATTRIBUTE_SCHEMA: dict[str, Any] = {
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
                    "name": {
                        "type": "string",
                        "description": "Canonical attribute name, for example skin_type, benefit, scent, texture, ingredient, material, target_area, usage.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Short normalized attribute value.",
                    },
                    "attribute_type": {
                        "type": "string",
                        "enum": [
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
                        ],
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Brief source phrase copied or closely paraphrased from the product record.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["name", "value", "attribute_type", "evidence", "confidence"],
            },
        },
    },
    "required": ["product_id", "attributes"],
}


SYSTEM_PROMPT = """You extract product attributes for a beauty product knowledge graph.

Rules:
- Return only attributes supported by the product record.
- Prefer normalized, reusable values over long phrases.
- Do not infer medical claims, demographics, or sensitive personal traits.
- If the record is sparse or unrelated to beauty, return an empty attributes list.
- Keep evidence short and grounded in the provided text.
- Avoid duplicate attributes with the same name and value.
"""


def read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def compact_text(value: Any, max_chars: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text[:max_chars]


def product_payload(row: dict[str, Any], max_input_chars: int) -> dict[str, Any]:
    keys = [
        "parent_asin",
        "title",
        "main_category",
        "store",
        "categories",
        "features",
        "description",
        "details",
        "price",
        "average_rating",
        "rating_number",
    ]
    payload = {key: row.get(key) for key in keys if key in row}
    return json.loads(compact_text(payload, max_input_chars))


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


def usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {}


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


def extract_attributes(
    client: Any,
    model: str,
    product: dict[str, Any],
    max_output_tokens: int,
    retries: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    product_id = str(product.get("parent_asin", ""))
    user_input = {
        "task": "Extract normalized product attributes for Neo4j knowledge graph construction.",
        "product": product,
    }

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_input, ensure_ascii=False)},
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
            parsed = json.loads(output_text(response))
            parsed["product_id"] = parsed.get("product_id") or product_id
            return parsed, usage_dict(response)
        except Exception as exc:  # OpenAI errors vary by SDK version.
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2**attempt, 30))

    raise RuntimeError(f"OpenAI extraction failed for {product_id}: {last_error}") from last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract product attributes with OpenAI Structured Outputs.")
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--output-path", type=Path, default=Path("kg_output/attributes/product_attributes_openai.jsonl"))
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--limit", type=int, default=20, help="Number of products to process. Use -1 for all rows.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between API calls.")
    parser.add_argument("--max-input-chars", type=int, default=4000)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--resume", action="store_true", help="Skip product_ids already present in the output JSONL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        sys.exit(2)

    try:
        from openai import OpenAI
    except ImportError:
        print("The openai package is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)

    client = OpenAI()
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids(args.output_path) if args.resume else set()
    limit = None if args.limit < 0 else args.limit
    processed = 0
    seen = 0

    with args.output_path.open("a", encoding="utf-8") as out:
        for row in read_jsonl_gz(args.meta_path):
            if seen < args.offset:
                seen += 1
                continue
            if limit is not None and processed >= limit:
                break

            product_id = str(row.get("parent_asin", ""))
            seen += 1
            if not product_id or product_id in done_ids:
                continue

            product = product_payload(row, args.max_input_chars)
            result, usage = extract_attributes(
                client=client,
                model=args.model,
                product=product,
                max_output_tokens=args.max_output_tokens,
                retries=args.retries,
            )
            record = {
                "product_id": product_id,
                "title": row.get("title", ""),
                "model": args.model,
                "attributes": result.get("attributes", []),
                "usage": usage,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            processed += 1
            print(f"{processed}: {product_id} attributes={len(record['attributes'])}")
            if args.sleep:
                time.sleep(args.sleep)

    print(f"Wrote {processed} product attribute rows to {args.output_path}")


if __name__ == "__main__":
    main()
