"""
Convert LLM extraction outputs to Neo4j import CSVs.

Reads:
  product_attributes.jsonl  (from extract_product_attributes_llm.py)
  review_mentions.jsonl     (from extract_mentions.py)

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
import hashlib
import json
from pathlib import Path
from typing import Any


def sha1_id(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def attr_id(attr_type: str, value: str) -> str:
    return sha1_id("attr", f"{attr_type}|{value}")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return len(rows)


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def build_attribute_csvs(
    product_attrs_path: Path,
    review_mentions_path: Path,
    output_dir: Path,
    min_confidence: float,
) -> dict[str, int]:
    attribute_nodes: dict[str, dict] = {}
    has_attribute_edges: list[dict] = []
    mentions_edges: list[dict] = []

    # ── product attributes → HAS_ATTRIBUTE ────────────────────────────────────
    if product_attrs_path.exists():
        for record in read_jsonl(product_attrs_path):
            product_id = str(record.get("product_id", ""))
            model = str(record.get("model", ""))
            if not product_id:
                continue
            for attr in record.get("attributes", []):
                t = str(attr.get("attr_type", "")).strip()
                v = str(attr.get("value", "")).strip()
                confidence = float(attr.get("confidence", 0.0))
                if not t or not v or confidence < min_confidence:
                    continue
                aid = attr_id(t, v)
                attribute_nodes.setdefault(aid, {"attribute_id": aid, "attr_type": t, "value": v})
                has_attribute_edges.append({
                    "product_id": product_id,
                    "attribute_id": aid,
                    "confidence": confidence,
                    "evidence": str(attr.get("evidence", ""))[:200],
                    "source": "product_desc",
                    "model": model,
                })
        print(f"Loaded {len(has_attribute_edges):,} HAS_ATTRIBUTE edges from {product_attrs_path.name}")
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
                aid = attr_id(t, v)
                attribute_nodes.setdefault(aid, {"attribute_id": aid, "attr_type": t, "value": v})
                mentions_edges.append({
                    "review_id": review_id,
                    "attribute_id": aid,
                    "sentiment": sentiment,
                    "confidence": confidence,
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
            ["product_id", "attribute_id", "confidence", "evidence", "source", "model"],
        ),
        "rel_mentions": write_csv(
            output_dir / "rel_mentions.csv",
            mentions_edges,
            ["review_id", "attribute_id", "sentiment", "confidence"],
        ),
    }
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Attribute node and edge CSVs from LLM extraction outputs.")
    parser.add_argument("--config", type=Path, default=Path("../config.yaml"))
    parser.add_argument("--product-attrs", type=Path, help="Path to product_attributes.jsonl")
    parser.add_argument("--review-mentions", type=Path, help="Path to review_mentions.jsonl")
    parser.add_argument("--output-dir", type=Path, help="Directory to write CSVs")
    parser.add_argument("--min-confidence", type=float, default=None)
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

    out_dir = args.output_dir or Path(data_cfg.get("output_dir", "kg_output/all_beauty"))
    attrs_dir = out_dir / "attributes"

    product_attrs_path = args.product_attrs or (attrs_dir / "product_attributes.jsonl")
    review_mentions_path = args.review_mentions or (attrs_dir / "review_mentions.jsonl")
    min_confidence = args.min_confidence if args.min_confidence is not None else float(llm_cfg.get("min_confidence", 0.6))

    print(f"Building attribute CSVs in {out_dir}...")
    counts = build_attribute_csvs(product_attrs_path, review_mentions_path, out_dir, min_confidence)

    print()
    for name, count in counts.items():
        print(f"  {name}: {count:,}")
    print(f"\nDone. CSV files written to: {out_dir}")


if __name__ == "__main__":
    main()
