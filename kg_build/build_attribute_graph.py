"""
Convert LLM extraction outputs to Neo4j import CSVs.

Reads:
  product_attributes.jsonl     (from extract_product_attributes.py)
  review_mentions.jsonl        (from extract_review_mentions.py)
  attribute_canonical_map.json (optional, from canonicalize_attributes.py)
  nodes_products.csv           (from build_base_graph.py; used to drop attributes for
                                 products outside the current scale.max_meta selection)

Writes:
  nodes_attributes.csv          -- Attribute nodes (deduped by attribute_id)
  rel_has_attribute.csv         -- Product -[HAS_ATTRIBUTE]-> Attribute
  rel_mentions.csv              -- Review  -[MENTIONS]->      Attribute

attribute_id = SHA1(attr_type|value), shared across HAS_ATTRIBUTE and MENTIONS
for the same (attr_type, value) pair.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from utils.csv_io import read_jsonl, write_csv
from utils.text_utils import sha1_id


def attr_id(attr_type: str, value: str) -> str:
    return sha1_id("attr", f"{attr_type}|{value}")


def load_canonical_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"attr_type_map": {}, "value_map": {}}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {"attr_type_map": data.get("attr_type_map", {}), "value_map": data.get("value_map", {})}


def canonicalize(t: str, v: str, canon: dict[str, Any]) -> tuple[str, str]:
    t2 = canon["attr_type_map"].get(t, t)
    v2 = canon["value_map"].get(t2, {}).get(v, v)
    return t2, v2


def load_valid_product_ids(nodes_products_path: Path) -> set[str] | None:
    """Products actually selected into the base graph (build_base_graph.py's scale.max_meta cap).

    Extraction scripts may process a broader set (e.g. --rule-only --limit -1 warms a
    cache ahead of a future scale.max_meta increase). Filtering here guarantees we never
    write HAS_ATTRIBUTE edges or Attribute nodes for products that aren't actually in the
    graph — Neo4j's MATCH-based import silently drops such edges, but the orphan Attribute
    nodes would still get created.
    """
    if not nodes_products_path.exists():
        return None
    with nodes_products_path.open(encoding="utf-8") as f:
        return {row["product_id"] for row in csv.DictReader(f)}


def build_attribute_csvs(
    product_attrs_path: Path,
    review_mentions_path: Path,
    output_dir: Path,
    min_confidence: float,
    canon: dict[str, Any],
    valid_product_ids: set[str] | None,
) -> dict[str, int]:
    attribute_nodes: dict[str, dict] = {}
    has_attribute_edges: list[dict] = []
    mentions_edges: list[dict] = []
    skipped_out_of_scope = 0

    # ── product attributes → HAS_ATTRIBUTE ────────────────────────────────────
    # confidenceはここでのフィルタにのみ使う（閾値未満はエッジを作らない）。生き残った
    # エッジ同士では抽出確信度の差は商品の関連性を意味しないため、エッジのプロパティとして
    # 保持したり検索スコアに使ったりはしない。evidence/source/modelも同様に、抽出結果の
    # デバッグにしか使わないため保持しない（必要ならproduct_attributes.jsonlを直接見る）。
    if product_attrs_path.exists():
        for record in read_jsonl(product_attrs_path):
            product_id = str(record.get("product_id", ""))
            if not product_id:
                continue
            if valid_product_ids is not None and product_id not in valid_product_ids:
                skipped_out_of_scope += 1
                continue
            for attr in record.get("attributes", []):
                t = str(attr.get("attr_type", "")).strip()
                v = str(attr.get("value", "")).strip()
                confidence = float(attr.get("confidence", 0.0))
                if not t or not v or confidence < min_confidence:
                    continue
                t, v = canonicalize(t, v, canon)
                aid = attr_id(t, v)
                attribute_nodes.setdefault(aid, {"attribute_id": aid, "attr_type": t, "value": v})
                has_attribute_edges.append({
                    "product_id": product_id,
                    "attribute_id": aid,
                })
        print(f"Loaded {len(has_attribute_edges):,} HAS_ATTRIBUTE edges from {product_attrs_path.name}")
        if skipped_out_of_scope:
            print(f"  Skipped {skipped_out_of_scope:,} products not present in nodes_products.csv")
    else:
        print(f"Warning: {product_attrs_path} not found, skipping HAS_ATTRIBUTE.")

    # ── review mentions → MENTIONS ─────────────────────────────────────────────
    if review_mentions_path.exists():
        for record in read_jsonl(review_mentions_path):
            review_id = str(record.get("review_id", ""))
            if not review_id:
                continue
            for m in record.get("mentions", []):
                t = str(m.get("attr_type", "")).strip()
                v = str(m.get("value", "")).strip()
                sentiment = str(m.get("sentiment", "neutral")).lower()
                confidence = float(m.get("confidence", 0.0))
                if not t or not v or confidence < min_confidence:
                    continue
                if sentiment not in {"positive", "negative", "neutral"}:
                    sentiment = "neutral"
                t, v = canonicalize(t, v, canon)
                aid = attr_id(t, v)
                attribute_nodes.setdefault(aid, {"attribute_id": aid, "attr_type": t, "value": v})
                mentions_edges.append({
                    "review_id": review_id,
                    "attribute_id": aid,
                    "sentiment": sentiment,
                })
        print(f"Loaded {len(mentions_edges):,} MENTIONS edges from {review_mentions_path.name}")
    else:
        print(f"Warning: {review_mentions_path} not found, skipping MENTIONS.")

    # ── write CSVs ─────────────────────────────────────────────────────────────
    counts = {
        "attributes": write_csv(
            output_dir / "nodes_attributes.csv",
            list(attribute_nodes.values()),
            ["attribute_id", "attr_type", "value"],
        ),
        "rel_has_attribute": write_csv(
            output_dir / "rel_has_attribute.csv",
            has_attribute_edges,
            ["product_id", "attribute_id"],
        ),
        "rel_mentions": write_csv(
            output_dir / "rel_mentions.csv",
            mentions_edges,
            ["review_id", "attribute_id", "sentiment"],
        ),
    }
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Attribute node and edge CSVs from LLM extraction outputs.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--product-attrs", type=Path, help="Path to product_attributes.jsonl")
    parser.add_argument("--review-mentions", type=Path, help="Path to review_mentions.jsonl")
    parser.add_argument("--output-dir", type=Path, help="Directory to write CSVs")
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument(
        "--canonical-map", type=Path,
        help="Path to attribute_canonical_map.json (from canonicalize_attributes.py). "
             "Auto-detected in attributes_dir if omitted; skipped entirely if absent.",
    )
    parser.add_argument(
        "--nodes-products", type=Path,
        help="Path to nodes_products.csv (from build_base_graph.py), used to drop attributes for "
             "products outside the current scale.max_meta selection. Auto-detected in output_dir "
             "if omitted; if not found, no filtering is applied.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        import yaml
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})

    out_dir = args.output_dir or (args.config.resolve().parent / data_cfg.get("output_dir", "kg_output/video_games"))
    attrs_dir = out_dir / "attributes"

    product_attrs_path = args.product_attrs or (attrs_dir / "product_attributes.jsonl")
    review_mentions_path = args.review_mentions or (attrs_dir / "review_mentions.jsonl")
    min_confidence = args.min_confidence if args.min_confidence is not None else float(llm_cfg.get("min_confidence", 0.6))
    canonical_map_path = args.canonical_map or (attrs_dir / "attribute_canonical_map.json")
    nodes_products_path = args.nodes_products or (out_dir / "nodes_products.csv")

    canon = load_canonical_map(canonical_map_path)
    if canon["attr_type_map"] or canon["value_map"]:
        print(f"Applying canonicalization map from {canonical_map_path}")

    valid_product_ids = load_valid_product_ids(nodes_products_path)
    if valid_product_ids is not None:
        print(f"Scoping to {len(valid_product_ids):,} products from {nodes_products_path.name}")
    else:
        print(f"Warning: {nodes_products_path} not found — no product scoping applied.")

    print(f"Building attribute CSVs in {out_dir}...")
    counts = build_attribute_csvs(
        product_attrs_path, review_mentions_path, out_dir, min_confidence, canon, valid_product_ids
    )

    print()
    for name, count in counts.items():
        print(f"  {name}: {count:,}")
    print(f"\nDone. CSV files written to: {out_dir}")


if __name__ == "__main__":
    main()
