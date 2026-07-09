"""
Shared text/ID normalization utilities used across the KG build pipeline
(build_base_graph.py, extract_product_attributes.py, extract_review_mentions.py,
canonicalize_attributes.py, build_attribute_graph.py).
"""
from __future__ import annotations

import hashlib
import html
import re
from typing import Any

_HTML_TAG = re.compile(r"<[^>]+>")
_TEXT_WS = re.compile(r"\s+")
_ATTR_WS = re.compile(r"[\s\-]+")
_ATTR_NON_ALPHA = re.compile(r"[^a-z0-9_]")


def _is_nan(value: Any) -> bool:
    try:
        return value != value  # true only for float('nan') / pandas NaN
    except Exception:
        return False


def clean_text(value: Any) -> str:
    if value is None or _is_nan(value):
        return ""
    text = str(value).replace("\x00", " ")
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return _TEXT_WS.sub(" ", text).strip()


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or _is_nan(value):
        return []
    return [value]


def sha1_id(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def normalize_attr_type(raw: str) -> str:
    """Normalize an LLM- or rule-generated attr_type to consistent snake_case."""
    lower = raw.lower().strip()
    snaked = _ATTR_WS.sub("_", lower)
    cleaned = _ATTR_NON_ALPHA.sub("", snaked)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "other"


def normalize_value(raw: str) -> str:
    return clean_text(raw).lower()
