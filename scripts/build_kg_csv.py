from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


TEXT_WS = re.compile(r"\s+")
NON_WORD_EDGE = re.compile(r"^[\W_]+|[\W_]+$")


def read_jsonl_gz(path: Path, max_rows: int | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_rows is not None and i >= max_rows:
                break
            rows.append(json.loads(line))
    return pd.DataFrame(rows)


def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).replace("\x00", " ").replace("\\", " ")
    return TEXT_WS.sub(" ", text).strip()


def clean_id(value: Any) -> str:
    return clean_text(value)


def normalize_feature(value: Any) -> str:
    text = clean_text(value).lower()
    text = NON_WORD_EDGE.sub("", text)
    return TEXT_WS.sub(" ", text).strip()


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    return [value]


def iter_category_names(row: pd.Series) -> Iterable[str]:
    main = clean_text(row.get("main_category"))
    if main:
        yield main

    for item in as_list(row.get("categories")):
        if isinstance(item, list):
            for subitem in item:
                text = clean_text(subitem)
                if text:
                    yield text
        else:
            text = clean_text(item)
            if text:
                yield text


def iter_feature_texts(row: pd.Series) -> Iterable[str]:
    for col in ("features", "description"):
        for item in as_list(row.get(col)):
            text = clean_text(item)
            if text:
                yield text


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
            count += 1
    return count


def build_kg(
    review_path: Path,
    meta_path: Path,
    output_dir: Path,
    max_reviews: int | None,
    max_meta: int | None,
    min_feature_len: int,
    max_features_per_product: int,
) -> dict[str, int]:
    reviews = read_jsonl_gz(review_path, max_reviews)
    meta = read_jsonl_gz(meta_path, max_meta)

    reviews = reviews.dropna(subset=["user_id", "parent_asin"]).copy()
    meta = meta.dropna(subset=["parent_asin"]).copy()
    reviews["rating"] = pd.to_numeric(reviews.get("rating"), errors="coerce")
    reviews["timestamp"] = pd.to_numeric(reviews.get("timestamp"), errors="coerce").astype("Int64")
    reviews["helpful_vote"] = pd.to_numeric(reviews.get("helpful_vote"), errors="coerce").fillna(0).astype(int)

    if "price" in meta.columns:
        meta["price"] = pd.to_numeric(meta["price"], errors="coerce")
    else:
        meta["price"] = pd.NA

    meta = meta.sort_values("rating_number", ascending=False, na_position="last").drop_duplicates("parent_asin")

    product_rows: dict[str, dict[str, Any]] = {}
    store_rows: dict[str, dict[str, Any]] = {}
    category_rows: dict[str, dict[str, Any]] = {}
    feature_rows: dict[str, dict[str, Any]] = {}

    product_category_edges: set[tuple[str, str]] = set()
    product_store_edges: set[tuple[str, str]] = set()
    product_feature_edges: set[tuple[str, str]] = set()

    for _, row in meta.iterrows():
        product_id = clean_id(row["parent_asin"])
        if not product_id:
            continue

        product_rows[product_id] = {
            "product_id": product_id,
            "title": clean_text(row.get("title")),
            "main_category": clean_text(row.get("main_category")),
            "price": "" if pd.isna(row.get("price")) else row.get("price"),
            "average_rating": row.get("average_rating", ""),
            "rating_number": row.get("rating_number", ""),
        }

        store_name = clean_text(row.get("store"))
        if store_name:
            store_id = stable_id("store", store_name.lower())
            store_rows[store_id] = {"store_id": store_id, "name": store_name}
            product_store_edges.add((product_id, store_id))

        for category_name in iter_category_names(row):
            category_id = stable_id("category", category_name.lower())
            category_rows[category_id] = {"category_id": category_id, "name": category_name}
            product_category_edges.add((product_id, category_id))

        seen_features: set[str] = set()
        for feature_text in iter_feature_texts(row):
            normalized = normalize_feature(feature_text)
            if len(normalized) < min_feature_len or normalized in seen_features:
                continue
            seen_features.add(normalized)
            feature_id = stable_id("feature", normalized)
            feature_rows[feature_id] = {
                "feature_id": feature_id,
                "text": feature_text,
                "normalized_text": normalized,
            }
            product_feature_edges.add((product_id, feature_id))
            if len(seen_features) >= max_features_per_product:
                break

    review_rows: list[dict[str, Any]] = []
    user_rows: dict[str, dict[str, Any]] = {}
    wrote_edges: list[dict[str, Any]] = []
    reviews_edges: list[dict[str, Any]] = []
    rated_edges: list[dict[str, Any]] = []

    for idx, row in reviews.reset_index(drop=True).iterrows():
        user_id = clean_id(row["user_id"])
        product_id = clean_id(row["parent_asin"])
        if not user_id or not product_id:
            continue

        review_id = stable_id("review", f"{user_id}|{product_id}|{row.get('timestamp', '')}|{idx}")
        rating = row.get("rating", "")
        timestamp = row.get("timestamp", "")
        helpful_vote = row.get("helpful_vote", 0)

        user_rows[user_id] = {"user_id": user_id}
        if product_id not in product_rows:
            product_rows[product_id] = {
                "product_id": product_id,
                "title": "",
                "main_category": "",
                "price": "",
                "average_rating": "",
                "rating_number": "",
            }

        review_rows.append(
            {
                "review_id": review_id,
                "title": clean_text(row.get("title")),
                "text": clean_text(row.get("text")),
                "rating": rating,
                "timestamp": timestamp,
                "helpful_vote": helpful_vote,
                "verified_purchase": row.get("verified_purchase", ""),
            }
        )
        wrote_edges.append({"user_id": user_id, "review_id": review_id})
        reviews_edges.append({"review_id": review_id, "product_id": product_id})
        rated_edges.append(
            {
                "user_id": user_id,
                "product_id": product_id,
                "rating": rating,
                "timestamp": timestamp,
                "verified_purchase": row.get("verified_purchase", ""),
            }
        )

    counts = {
        "products": write_csv(output_dir / "nodes_products.csv", product_rows.values(), ["product_id", "title", "main_category", "price", "average_rating", "rating_number"]),
        "users": write_csv(output_dir / "nodes_users.csv", user_rows.values(), ["user_id"]),
        "reviews": write_csv(output_dir / "nodes_reviews.csv", review_rows, ["review_id", "title", "text", "rating", "timestamp", "helpful_vote", "verified_purchase"]),
        "stores": write_csv(output_dir / "nodes_stores.csv", store_rows.values(), ["store_id", "name"]),
        "categories": write_csv(output_dir / "nodes_categories.csv", category_rows.values(), ["category_id", "name"]),
        "features": write_csv(output_dir / "nodes_features.csv", feature_rows.values(), ["feature_id", "text", "normalized_text"]),
        "rel_wrote": write_csv(output_dir / "rel_wrote.csv", wrote_edges, ["user_id", "review_id"]),
        "rel_reviews": write_csv(output_dir / "rel_reviews.csv", reviews_edges, ["review_id", "product_id"]),
        "rel_rated": write_csv(output_dir / "rel_rated.csv", rated_edges, ["user_id", "product_id", "rating", "timestamp", "verified_purchase"]),
        "rel_product_store": write_csv(output_dir / "rel_product_store.csv", ({"product_id": p, "store_id": s} for p, s in sorted(product_store_edges)), ["product_id", "store_id"]),
        "rel_product_category": write_csv(output_dir / "rel_product_category.csv", ({"product_id": p, "category_id": c} for p, c in sorted(product_category_edges)), ["product_id", "category_id"]),
        "rel_product_feature": write_csv(output_dir / "rel_product_feature.csv", ({"product_id": p, "feature_id": f} for p, f in sorted(product_feature_edges)), ["product_id", "feature_id"]),
    }

    (output_dir / "build_summary.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Neo4j-ready CSV files for the Amazon Reviews'23 knowledge graph.")
    parser.add_argument("--review-path", type=Path, default=Path("data/All_Beauty.jsonl.gz"))
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--output-dir", type=Path, default=Path("kg_output/all_beauty"))
    parser.add_argument("--max-reviews", type=int, default=200_000, help="Use -1 to load all reviews.")
    parser.add_argument("--max-meta", type=int, default=-1, help="Use -1 to load all metadata rows.")
    parser.add_argument("--min-feature-len", type=int, default=8)
    parser.add_argument("--max-features-per-product", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_reviews = None if args.max_reviews < 0 else args.max_reviews
    max_meta = None if args.max_meta < 0 else args.max_meta
    counts = build_kg(
        review_path=args.review_path,
        meta_path=args.meta_path,
        output_dir=args.output_dir,
        max_reviews=max_reviews,
        max_meta=max_meta,
        min_feature_len=args.min_feature_len,
        max_features_per_product=args.max_features_per_product,
    )
    for name, count in counts.items():
        print(f"{name}: {count:,}")
    print(f"CSV files written to: {args.output_dir}")


if __name__ == "__main__":
    main()
