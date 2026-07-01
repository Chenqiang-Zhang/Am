"""
Build Neo4j-ready CSV files for the knowledge graph.

Nodes: Product / User / Review / Category / Brand
Edges: RATED, WROTE, ABOUT, BELONGS_TO, SUBCATEGORY_OF, MADE_BY

HAS_ATTRIBUTE (Product→Attribute) is built by extract_attributes.py.
MENTIONS (Review→Attribute) is built by extract_mentions.py.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml


# ── text cleaning ──────────────────────────────────────────────────────────────

_HTML_TAG = re.compile(r"<[^>]+>")
_TEXT_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    text = _HTML_TAG.sub(" ", text)
    return html.unescape(text)


def clean_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).replace("\x00", " ")
    text = _strip_html(text)
    return _TEXT_WS.sub(" ", text).strip()


def clean_id(value: Any) -> str:
    return clean_text(value)


def sha1_id(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [value]


# ── I/O helpers ────────────────────────────────────────────────────────────────

def read_jsonl_gz(path: Path, max_rows: int | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_rows is not None and i >= max_rows:
                break
            rows.append(json.loads(line))
    return pd.DataFrame(rows)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
            count += 1
    return count


# ── category hierarchy ─────────────────────────────────────────────────────────

def _iter_category_paths(row: pd.Series) -> list[list[str]]:
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


def build_category_nodes_and_edges(
    all_paths: list[list[str]],
) -> tuple[dict[str, dict], list[dict]]:
    """
    Build Category node rows and SUBCATEGORY_OF edges from collected paths.
    Category.level = depth in the hierarchy (0 = root).
    SUBCATEGORY_OF direction: child → parent.
    """
    category_rows: dict[str, dict] = {}
    subcategory_edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()

    for path in all_paths:
        for i, name in enumerate(path):
            cat_id = sha1_id("cat", name.lower())
            if cat_id not in category_rows:
                category_rows[cat_id] = {
                    "category_id": cat_id,
                    "name": name,
                    "level": i,
                }
            if i > 0:
                parent_id = sha1_id("cat", path[i - 1].lower())
                edge = (cat_id, parent_id)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    subcategory_edges.append(
                        {"child_category_id": cat_id, "parent_category_id": parent_id}
                    )

    return category_rows, subcategory_edges


# ── product description ────────────────────────────────────────────────────────

def build_description(row: pd.Series, min_len: int = 8) -> str:
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


# ── main build ─────────────────────────────────────────────────────────────────

def build_kg(
    review_path: Path,
    meta_path: Path,
    output_dir: Path,
    max_reviews: int | None,
    max_meta: int | None,
    min_feature_len: int,
) -> dict[str, int]:
    print("Loading JSONL data...")

    # ── meta: read all, sort by review count desc, take top max_meta ──────────
    meta_all = read_jsonl_gz(meta_path, None)
    meta_all = meta_all.dropna(subset=["parent_asin"]).copy()
    meta_all["price"] = pd.to_numeric(
        meta_all["price"] if "price" in meta_all.columns else pd.Series(dtype=float),
        errors="coerce",
    )
    meta_all = (
        meta_all.sort_values("rating_number", ascending=False, na_position="last")
        .drop_duplicates("parent_asin")
    )
    meta = meta_all.head(max_meta) if max_meta is not None else meta_all
    selected_ids: set[str] = set(meta["parent_asin"].astype(str))
    print(f"  Selected {len(selected_ids):,} products (top by rating_number)")

    # ── reviews: read all, keep only reviews for selected products ─────────────
    # max_reviews is ignored — we use ALL reviews for the chosen products.
    reviews_all = read_jsonl_gz(review_path, None)
    reviews_all = reviews_all.dropna(subset=["user_id", "parent_asin"]).copy()
    reviews = reviews_all[reviews_all["parent_asin"].astype(str).isin(selected_ids)].copy()
    print(f"  Found {len(reviews):,} reviews for selected products")

    reviews["rating"] = pd.to_numeric(reviews.get("rating"), errors="coerce")
    reviews["timestamp"] = (
        pd.to_numeric(reviews.get("timestamp"), errors="coerce").astype("Int64")
    )
    reviews["helpful_vote"] = (
        pd.to_numeric(reviews.get("helpful_vote"), errors="coerce").fillna(0).astype(int)
    )

    # Keep latest review per (user_id, parent_asin) — dedup
    reviews = (
        reviews.sort_values("timestamp", ascending=False, na_position="last")
        .drop_duplicates(subset=["user_id", "parent_asin"], keep="first")
    )
    # Cap reviews per product (most recent first) to avoid popular products dominating
    if max_reviews is not None:
        reviews = (
            reviews.groupby("parent_asin", group_keys=False)
            .head(max_reviews)
        )
        print(f"  Capped to {max_reviews:,} reviews per product → {len(reviews):,} total")

    # ── product / brand / category ─────────────────────────────────────────────
    product_rows: dict[str, dict] = {}
    brand_rows: dict[str, dict] = {}
    product_brand_edges: set[tuple[str, str]] = set()
    product_category_edges: list[dict] = []
    all_category_paths: list[list[str]] = []

    print("Processing product metadata...")
    for _, row in meta.iterrows():
        product_id = clean_id(row["parent_asin"])
        if not product_id:
            continue

        price_val = row.get("price")
        price_out = "" if (price_val is None or (isinstance(price_val, float) and pd.isna(price_val))) else price_val

        product_rows[product_id] = {
            "product_id": product_id,
            "title": clean_text(row.get("title")),
            "price": price_out,
            "avg_rating": row.get("average_rating", ""),
            "rating_count": row.get("rating_number", ""),
            "description": build_description(row, min_feature_len),
        }

        # MADE_BY edge target
        brand_name = clean_text(row.get("store"))
        if brand_name:
            brand_id = sha1_id("brand", brand_name.lower())
            brand_rows[brand_id] = {"brand_id": brand_id, "name": brand_name}
            product_brand_edges.add((product_id, brand_id))

        # BELONGS_TO edges (leaf of each path) + collect paths for hierarchy
        paths = _iter_category_paths(row)
        all_category_paths.extend(paths)
        seen_leaf: set[str] = set()
        for path in paths:
            if path:
                leaf_id = sha1_id("cat", path[-1].lower())
                if leaf_id not in seen_leaf:
                    seen_leaf.add(leaf_id)
                    product_category_edges.append(
                        {"product_id": product_id, "category_id": leaf_id}
                    )

    category_rows, subcategory_edges = build_category_nodes_and_edges(all_category_paths)

    # ── reviews / users ────────────────────────────────────────────────────────
    review_rows: list[dict] = []
    user_rows: dict[str, dict] = {}
    wrote_edges: list[dict] = []
    about_edges: list[dict] = []
    rated_edges: list[dict] = []

    print("Processing reviews...")
    for _, row in reviews.iterrows():
        user_id = clean_id(row["user_id"])
        product_id = clean_id(row["parent_asin"])
        if not user_id or not product_id:
            continue

        timestamp = row.get("timestamp", "")
        rating = row.get("rating", "")
        review_id = sha1_id("review", f"{user_id}|{product_id}|{timestamp}")

        user_rows[user_id] = {"user_id": user_id}

        if product_id not in product_rows:
            product_rows[product_id] = {
                "product_id": product_id,
                "title": "", "price": "", "avg_rating": "",
                "rating_count": "", "description": "",
            }

        review_rows.append({
            "review_id": review_id,
            "rating": rating,
            "timestamp": timestamp,
            "helpful_vote": row.get("helpful_vote", 0),
            "verified": row.get("verified_purchase", ""),
            "title": clean_text(row.get("title")),
            "text": clean_text(row.get("text")),
        })
        wrote_edges.append({"user_id": user_id, "review_id": review_id})
        about_edges.append({"review_id": review_id, "product_id": product_id})
        rated_edges.append({
            "user_id": user_id,
            "product_id": product_id,
            "rating": rating,
            "timestamp": timestamp,
        })

    # ── write CSVs ─────────────────────────────────────────────────────────────
    print("Writing CSV files...")
    counts: dict[str, int] = {
        "products": write_csv(
            output_dir / "nodes_products.csv", product_rows.values(),
            ["product_id", "title", "price", "avg_rating", "rating_count", "description"],
        ),
        "users": write_csv(
            output_dir / "nodes_users.csv", user_rows.values(), ["user_id"],
        ),
        "reviews": write_csv(
            output_dir / "nodes_reviews.csv", review_rows,
            ["review_id", "rating", "timestamp", "helpful_vote", "verified", "title", "text"],
        ),
        "brands": write_csv(
            output_dir / "nodes_brands.csv", brand_rows.values(), ["brand_id", "name"],
        ),
        "categories": write_csv(
            output_dir / "nodes_categories.csv", category_rows.values(),
            ["category_id", "name", "level"],
        ),
        "rel_wrote": write_csv(
            output_dir / "rel_wrote.csv", wrote_edges, ["user_id", "review_id"],
        ),
        "rel_about": write_csv(
            output_dir / "rel_about.csv", about_edges, ["review_id", "product_id"],
        ),
        "rel_rated": write_csv(
            output_dir / "rel_rated.csv", rated_edges,
            ["user_id", "product_id", "rating", "timestamp"],
        ),
        "rel_made_by": write_csv(
            output_dir / "rel_made_by.csv",
            ({"product_id": p, "brand_id": b} for p, b in sorted(product_brand_edges)),
            ["product_id", "brand_id"],
        ),
        "rel_belongs_to": write_csv(
            output_dir / "rel_belongs_to.csv", product_category_edges,
            ["product_id", "category_id"],
        ),
        "rel_subcategory_of": write_csv(
            output_dir / "rel_subcategory_of.csv", subcategory_edges,
            ["child_category_id", "parent_category_id"],
        ),
    }

    (output_dir / "build_summary.json").write_text(
        json.dumps(counts, indent=2), encoding="utf-8"
    )
    return counts


# ── CLI ────────────────────────────────────────────────────────────────────────

def _load_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Neo4j-ready CSV files for the Amazon Reviews'23 knowledge graph."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("../config.yaml"),
        help="Path to config.yaml (default: ../../config.yaml relative to this script)",
    )
    parser.add_argument("--review-path", type=Path)
    parser.add_argument("--meta-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-reviews", type=int, help="Use -1 to load all reviews.")
    parser.add_argument("--max-meta", type=int, help="Use -1 to load all metadata rows.")
    parser.add_argument("--min-feature-len", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        cfg = _load_config(args.config)
        print(f"Config loaded from: {args.config}")
    else:
        print(f"Config not found at {args.config}, using CLI/defaults only.")

    data_cfg = cfg.get("data", {})
    scale_cfg = cfg.get("scale", {})

    review_path = args.review_path or Path(data_cfg.get("review_path", "data/All_Beauty.jsonl.gz"))
    meta_path = args.meta_path or Path(data_cfg.get("meta_path", "data/meta_All_Beauty.jsonl.gz"))
    output_dir = args.output_dir or Path(data_cfg.get("output_dir", "kg_output/all_beauty"))

    max_reviews_raw = args.max_reviews if args.max_reviews is not None else scale_cfg.get("max_reviews", 50000)
    max_meta_raw = args.max_meta if args.max_meta is not None else scale_cfg.get("max_meta", -1)
    min_feature_len = args.min_feature_len if args.min_feature_len is not None else scale_cfg.get("min_feature_len", 8)

    max_reviews = None if max_reviews_raw < 0 else max_reviews_raw
    max_meta = None if max_meta_raw < 0 else max_meta_raw

    counts = build_kg(
        review_path=review_path,
        meta_path=meta_path,
        output_dir=output_dir,
        max_reviews=max_reviews,
        max_meta=max_meta,
        min_feature_len=min_feature_len,
    )

    print()
    for name, count in counts.items():
        print(f"  {name}: {count:,}")
    print(f"\nCSV files written to: {output_dir}")


if __name__ == "__main__":
    main()
