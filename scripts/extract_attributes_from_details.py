"""
details フィールドから Attribute ノードを生成する。

LLM 不使用・ゼロコスト。
出力先（--output-dir）に nodes_attributes.csv と rel_product_attribute.csv を書き出す。
その後 import_attributes_to_neo4j.py でそのままインポート可能。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# details キー → attribute_type マッピング
# 推薦に意味があるものだけ残す。パッケージサイズ・UPC 等は除外。
# ---------------------------------------------------------------------------
KEY_MAP: dict[str, str] = {
    "Skin Type":                    "skin_type",
    "Hair Type":                    "hair_type",
    "Item Form":                    "product_type",
    "Scent":                        "scent",
    "Product Benefits":             "benefit",
    "Special Feature":              "benefit",
    "Material Feature":             "ingredient",
    "Active Ingredients":           "ingredient",
    "Finish Type":                  "texture",
    "Color":                        "color",
    "Size":                         "size",
    "Material":                     "material",
    "Specific Uses For Product":    "benefit",
    "Age Range (Description)":      "other",
    "Style":                        "other",
    "Target Gender":                "other",
}

# 値として不要なもの（ノイズ除去）
SKIP_VALUES = {
    "n/a", "na", "none", "unknown", "other", "not applicable",
    "no", "yes",  # "Is Discontinued" 等の残滓
}


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def normalize_value(v: str) -> str:
    return " ".join(v.strip().split()).lower()


def iter_records(meta_path: Path) -> Iterator[dict]:
    open_fn = gzip.open if str(meta_path).endswith(".gz") else open
    with open_fn(meta_path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract(meta_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    attr_rows: dict[str, dict] = {}       # attribute_id → node row
    rel_rows: list[dict] = []

    skipped_values = 0
    total_products = 0
    products_with_attrs = 0

    for item in iter_records(meta_path):
        product_id = item.get("parent_asin") or item.get("asin")
        if not product_id:
            continue
        total_products += 1

        details = item.get("details")
        if not isinstance(details, dict):
            continue

        added = 0
        for raw_key, raw_val in details.items():
            attr_type = KEY_MAP.get(raw_key)
            if not attr_type:
                continue

            val_str = normalize_value(str(raw_val))
            if not val_str or val_str in SKIP_VALUES:
                skipped_values += 1
                continue

            attr_id = stable_id(attr_type, val_str)

            if attr_id not in attr_rows:
                attr_rows[attr_id] = {
                    "attribute_id":   attr_id,
                    "name":           raw_key,
                    "value":          val_str,
                    "attribute_type": attr_type,
                }

            rel_rows.append({
                "product_id":   product_id,
                "attribute_id": attr_id,
                "confidence":   "1.0",
                "evidence":     f"{raw_key}: {raw_val}",
                "model":        "details",
            })
            added += 1

        if added > 0:
            products_with_attrs += 1

    # CSV 書き出し
    attr_path = output_dir / "nodes_attributes.csv"
    rel_path  = output_dir / "rel_product_attribute.csv"

    with attr_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["attribute_id", "name", "value", "attribute_type"])
        w.writeheader()
        w.writerows(attr_rows.values())

    with rel_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "attribute_id", "confidence", "evidence", "model"])
        w.writeheader()
        w.writerows(rel_rows)

    print(f"完了")
    print(f"  処理商品数      : {total_products:,}")
    print(f"  属性付き商品数  : {products_with_attrs:,} ({products_with_attrs/total_products*100:.1f}%)")
    print(f"  ユニーク属性数  : {len(attr_rows):,}")
    print(f"  エッジ数        : {len(rel_rows):,}")
    print(f"  スキップ値      : {skipped_values:,}")
    print(f"  出力: {attr_path}")
    print(f"  出力: {rel_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Attribute nodes from product details field.")
    parser.add_argument("--meta",       default="data/meta_All_Beauty.jsonl.gz")
    parser.add_argument("--output-dir", default="kg_output/all_beauty")
    args = parser.parse_args()

    extract(Path(args.meta), Path(args.output_dir))


if __name__ == "__main__":
    main()
