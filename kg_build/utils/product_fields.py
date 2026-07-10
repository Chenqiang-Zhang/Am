"""
Shared product-metadata field helpers used by both build_base_graph.py (KG構築の
Product/Category抽出) と select_kcore.py（k-core計算前のメタデータ完全性フィルタ）。
どちらも「1商品分のmetaレコードから何が取り出せるか」を同じ基準で判定する必要があるため、
このモジュールを唯一の参照実装にして二重管理を避ける。
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from .text_utils import as_list, clean_text


def iter_category_paths(row: dict[str, Any]) -> list[list[str]]:
    """
    Extract category paths from a meta row.
    Each path is a list of names from root (level 0) to leaf.

    Amazon Reviews'23 `categories` field is typically:
      [["Root", "Sub", "Leaf"]]   – list of one or more paths
    or just a flat list treated as a single path.
    """
    main = clean_text(row.get("main_category"))
    raw = as_list(row.get("categories"))

    paths: list[list[str]] = []
    if not raw:
        if main:
            paths.append([main])
        return paths

    first = raw[0]
    if isinstance(first, list):
        for item in raw:
            if isinstance(item, list):
                cleaned = [clean_text(x) for x in item if clean_text(x)]
                if cleaned:
                    paths.append(cleaned)
    else:
        cleaned = [clean_text(x) for x in raw if clean_text(x)]
        if cleaned:
            paths.append(cleaned)

    # Ensure main_category is always the root of each path
    result: list[list[str]] = []
    for path in paths:
        if main and (not path or path[0].lower() != main.lower()):
            result.append([main] + path)
        else:
            result.append(path)

    return result if result else ([main] if main else [])


def build_description(row: dict[str, Any], min_len: int = 8) -> str:
    """
    Concatenate `features` and `description` fields into one raw text block.
    Each part is newline-separated. Used for LLM attribute extraction and
    Neo4j FULLTEXT indexing.
    """
    parts: list[str] = []
    for col in ("features", "description"):
        for item in as_list(row.get(col)):
            text = clean_text(item)
            if len(text) >= min_len:
                parts.append(text)
    return "\n".join(parts)


def is_complete_metadata(row: dict[str, Any], min_feature_len: int) -> bool:
    """price/avg_rating/rating_count/description/brand/categoryが全て揃っているか判定する。
    select_kcore.pyがk-core計算前に商品を絞り込む基準として使う。"""
    price = row.get("price")
    if price is None or price == "":
        return False
    if row.get("average_rating") is None or row.get("rating_number") is None:
        return False
    if not build_description(row, min_feature_len):
        return False
    if not clean_text(row.get("store")):
        return False
    if not iter_category_paths(row):
        return False
    return True


def complete_product_ids(meta_path: Path, min_feature_len: int) -> set[str]:
    """price/avg_rating/rating_count/description/brand/categoryが全て揃っている
    product_id の集合を、meta_*.jsonl.gz 全件から返す。"""
    complete: set[str] = set()
    with gzip.open(meta_path, "rt", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            pid = row.get("parent_asin")
            if pid and is_complete_metadata(row, min_feature_len):
                complete.add(pid)
    return complete
