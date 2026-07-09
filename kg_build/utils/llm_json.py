"""
Shared helpers for calling an OpenAI-compatible chat/responses endpoint and
parsing its JSON output, used by the attribute-extraction, canonicalization, and
translation scripts (extract_product_attributes.py, extract_review_mentions.py,
canonicalize_attributes.py, backfill_display_fields.py).
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable

_CODE_FENCE_OPEN = re.compile(r"^```(?:json)?")
_CODE_FENCE_CLOSE = re.compile(r"```$")


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = _CODE_FENCE_OPEN.sub("", text).strip()
        text = _CODE_FENCE_CLOSE.sub("", text).strip()
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


def chat_json_call(
    client: Any,
    model: str,
    messages: list[dict],
    max_output_tokens: int,
    *,
    temperature: float = 0,
    retries: int = 0,
    use_responses_api: bool = False,
    response_schema: dict | None = None,
    schema_name: str = "response",
) -> tuple[dict[str, Any], dict[str, int]]:
    """Call a chat/responses endpoint, parse the JSON reply, retrying with
    exponential backoff on failure. Returns (parsed_json, usage)."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if use_responses_api:
                resp = client.responses.create(
                    model=model, input=messages,
                    text={"format": {"type": "json_schema", "name": schema_name, "schema": response_schema, "strict": True}},
                    temperature=temperature, max_output_tokens=max_output_tokens,
                )
                raw = getattr(resp, "output_text", None) or "".join(
                    c.text for item in (getattr(resp, "output", []) or [])
                    for c in (getattr(item, "content", []) or [])
                    if getattr(c, "type", "") == "output_text"
                )
                usage = normalize_usage(getattr(resp, "usage", {}))
            else:
                resp = client.chat.completions.create(
                    model=model, messages=messages,
                    response_format={"type": "json_object"},
                    temperature=temperature, max_tokens=max_output_tokens,
                )
                raw = resp.choices[0].message.content or "{}"
                usage = normalize_usage(getattr(resp, "usage", {}))
            return parse_json_text(raw), usage
        except Exception as exc:
            last_err = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_err}") from last_err


def batch_extract_with_fallback(
    client: Any,
    model: str,
    items: list[dict[str, Any]],
    item_id: Callable[[dict], str],
    build_messages: Callable[[list[dict]], list[dict]],
    parse_result: Callable[[dict], dict[str, list]],
    max_output_tokens: int,
    retries: int,
    use_responses_api: bool,
    *,
    response_schema: dict | None = None,
    schema_name: str = "response",
    label: str = "item",
) -> tuple[dict[str, list], dict[str, int]]:
    """Run one batched LLM call; on failure, retry the batch one item at a
    time so a single malformed item doesn't sink the whole batch."""

    def call(batch: list[dict]) -> tuple[dict[str, list], dict[str, int]]:
        parsed, usage = chat_json_call(
            client, model, build_messages(batch), max_output_tokens,
            retries=retries, use_responses_api=use_responses_api,
            response_schema=response_schema, schema_name=schema_name,
        )
        return parse_result(parsed), usage

    try:
        return call(items)
    except Exception as batch_err:
        if len(items) <= 1:
            raise
        print(f"Batch failed; retrying one-by-one: {batch_err}", file=sys.stderr)
        merged: dict[str, list] = {}
        total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for item in items:
            try:
                m, u = call([item])
                merged.update(m)
                for k in total:
                    total[k] += u.get(k, 0)
            except Exception as exc:
                print(f"Single fallback failed for {label} {item_id(item)}: {exc}", file=sys.stderr)
                merged[str(item_id(item))] = []
        return merged, total
