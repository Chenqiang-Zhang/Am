"""
Build Neo4j-ready CSV files for the knowledge graph.

Nodes: Product / User / Review / Category / Brand
Edges: RATED, WROTE, ABOUT, BELONGS_TO, SUBCATEGORY_OF, MADE_BY

HAS_ATTRIBUTE (Product→Attribute) is built by extract_product_attributes.py
+ build_attribute_graph.py. MENTIONS (Review→Attribute) is built by
extract_review_mentions.py + build_attribute_graph.py.

商品・ユーザーの選定は常に select_kcore.py が確定した k-core を使う
（全ユーザー・全商品が最低k件の相互作用を持つことを保証するため）。
先に以下を実行してから本スクリプトを実行すること:
    python kg_build/select_kcore.py --config config.yaml --k 3
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from utils.csv_io import write_csv
from utils.text_utils import as_list, clean_text, sha1_id

clean_id = clean_text


def _dataframe_from_jsonl_gz(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    return pd.DataFrame(rows)


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
    min_feature_len: int,
    selected_user_ids: set[str],
    selected_product_ids: set[str],
) -> dict[str, int]:
    """selected_user_ids/selected_product_ids（select_kcore.py が確定したk-core）
    との一致だけで商品・レビューを絞り込む。件数ベースの間引きは一切行わない
    （k-coreで保証された「全ノードが最低k件の相互作用を持つ」性質を崩さないため）。"""
    print("Loading JSONL data...")

    # ── meta: keep only k-core selected products ────────────────────────────
    meta_all = _dataframe_from_jsonl_gz(meta_path)
    meta_all = meta_all.dropna(subset=["parent_asin"]).copy()
    meta_all["price"] = pd.to_numeric(
        meta_all["price"] if "price" in meta_all.columns else pd.Series(dtype=float),
        errors="coerce",
    )
    meta_all = (
        meta_all.sort_values("rating_number", ascending=False, na_position="last")
        .drop_duplicates("parent_asin")
    )
    meta = meta_all[meta_all["parent_asin"].astype(str).isin(selected_product_ids)]
    print(f"  Selected {len(meta):,} products (from k-core selection file)")
    selected_ids: set[str] = set(meta["parent_asin"].astype(str))

    # ── reviews: keep only k-core selected (user, product) pairs ───────────────
    reviews_all = _dataframe_from_jsonl_gz(review_path)
    reviews_all = reviews_all.dropna(subset=["user_id", "parent_asin"]).copy()
    reviews = reviews_all[reviews_all["parent_asin"].astype(str).isin(selected_ids)].copy()
    reviews = reviews[reviews["user_id"].astype(str).isin(selected_user_ids)].copy()
    print(f"  Found {len(reviews):,} reviews for selected (product, user) k-core pairs")

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
        "--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml",
        help="Path to config.yaml (default: Am/config.yaml, resolved from this script's location)",
    )
    parser.add_argument("--review-path", type=Path)
    parser.add_argument("--meta-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--min-feature-len", type=int)
    parser.add_argument(
        "--kcore-selection-dir", type=Path, default=None,
        help="select_kcore.py の出力ディレクトリ（selected_user_ids.txt / "
             "selected_product_ids.txt を含む）。省略時は <output_dir>/kcore_selection "
             "（select_kcore.py 自体のデフォルト出力先と同じ）を使う。",
    )
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
    config_dir = args.config.resolve().parent

    review_path = args.review_path or (config_dir / data_cfg.get("review_path", "data/Video_Games.jsonl.gz"))
    meta_path = args.meta_path or (config_dir / data_cfg.get("meta_path", "data/meta_Video_Games.jsonl.gz"))
    output_dir = args.output_dir or (config_dir / data_cfg.get("output_dir", "kg_output/video_games_kcore3"))

    min_feature_len = args.min_feature_len if args.min_feature_len is not None else scale_cfg.get("min_feature_len", 8)

    kcore_selection_dir = args.kcore_selection_dir or (output_dir / "kcore_selection")
    users_file = kcore_selection_dir / "selected_user_ids.txt"
    items_file = kcore_selection_dir / "selected_product_ids.txt"
    if not users_file.exists() or not items_file.exists():
        raise SystemExit(
            f"k-core selection not found at {kcore_selection_dir}. Run first:\n"
            f"  python kg_build/select_kcore.py --config {args.config} --k <k>"
        )
    selected_user_ids = set(users_file.read_text(encoding="utf-8").splitlines())
    selected_product_ids = set(items_file.read_text(encoding="utf-8").splitlines())
    print(
        f"Using k-core selection from {kcore_selection_dir}: "
        f"{len(selected_user_ids):,} users, {len(selected_product_ids):,} products"
    )

    counts = build_kg(
        review_path=review_path,
        meta_path=meta_path,
        output_dir=output_dir,
        min_feature_len=min_feature_len,
        selected_user_ids=selected_user_ids,
        selected_product_ids=selected_product_ids,
    )

    print()
    for name, count in counts.items():
        print(f"  {name}: {count:,}")
    print(f"\nCSV files written to: {output_dir}")


if __name__ == "__main__":
    main()
