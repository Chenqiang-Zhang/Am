"""
GPUサーバー（kubera）で生成した以下をNeo4jに反映する:
  ② MENTIONS 関係（レビュー言及）
  ③ LLM抽出属性（HAS_ATTRIBUTE の追加、既存detailsベース属性とはtype+valueでMERGE）
  ④ title_ja / text_ja（商品タイトル・レビュー本文の日本語訳）
  ⑤ value_ja（属性値の日本語訳）

既存の attribute_id 生成方式（extract_attributes_from_details.py）と完全一致させることで、
detailsベースとLLMベースの同一属性（type+value）を同じノードにマージする。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from neo4j import GraphDatabase

GPU_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "kg_output" / "gpu_output"


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def normalize_value(v: str) -> str:
    return " ".join(str(v).strip().split()).lower()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_dedup_by_id(paths: list[Path], id_key: str = "id") -> dict[str, dict]:
    """複数ファイルをidでデデュープしてマージ（先勝ち）。"""
    merged: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            continue
        for row in iter_jsonl(path):
            rid = row.get(id_key)
            if rid and rid not in merged:
                merged[rid] = row
    return merged


def import_features_ja(session, feature_paths: list[Path], batch_size: int) -> int:
    merged = load_dedup_by_id(feature_paths)
    rows = [{"feature_id": k, "text_ja": v["ja"]} for k, v in merged.items() if v.get("ja")]
    print(f"[feature text_ja] {len(rows)} features to set")
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            "UNWIND $rows AS row MATCH (f:Feature {feature_id: row.feature_id}) SET f.text_ja = row.text_ja",
            rows=batch,
        )
        print(f"  feature text_ja progress: {min(i+batch_size, len(rows))}/{len(rows)}")
    return len(rows)


def import_review_titles_ja(session, title_paths: list[Path], batch_size: int) -> int:
    merged = load_dedup_by_id(title_paths)
    rows = [{"review_id": k, "title_ja": v["ja"]} for k, v in merged.items() if v.get("ja")]
    print(f"[review title_ja] {len(rows)} reviews to set")
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            "UNWIND $rows AS row MATCH (r:Review {review_id: row.review_id}) SET r.title_ja = row.title_ja",
            rows=batch,
        )
        print(f"  review title_ja progress: {min(i+batch_size, len(rows))}/{len(rows)}")
    return len(rows)


def import_attribute_values_ja(session, attr_values_path: Path, batch_size: int) -> int:
    ja_map: dict[str, str] = {}
    for row in iter_jsonl(attr_values_path):
        key = normalize_value(row["id"])
        ja = (row.get("ja") or "").strip()
        if ja:
            ja_map[key] = ja
    rows = [{"value": k, "value_ja": v} for k, v in ja_map.items()]
    print(f"[value_ja] {len(rows)} unique values to set")
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            "UNWIND $rows AS row MATCH (a:Attribute {value: row.value}) SET a.value_ja = row.value_ja",
            rows=batch,
        )
        print(f"  value_ja progress: {min(i+batch_size, len(rows))}/{len(rows)}")
    return len(rows)


def import_titles_ja(session, title_paths: list[Path], batch_size: int) -> int:
    merged = load_dedup_by_id(title_paths)
    rows = [{"product_id": k, "title_ja": v["ja"]} for k, v in merged.items() if v.get("ja")]
    print(f"[title_ja] {len(rows)} products to set")
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            "UNWIND $rows AS row MATCH (p:Product {product_id: row.product_id}) SET p.title_ja = row.title_ja",
            rows=batch,
        )
        print(f"  title_ja progress: {min(i+batch_size, len(rows))}/{len(rows)}")
    return len(rows)


def import_reviews_ja(session, review_paths: list[Path], batch_size: int) -> int:
    merged = load_dedup_by_id(review_paths)
    rows = [{"review_id": k, "text_ja": v["ja"]} for k, v in merged.items() if v.get("ja")]
    print(f"[text_ja] {len(rows)} reviews to set")
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            "UNWIND $rows AS row MATCH (r:Review {review_id: row.review_id}) SET r.text_ja = row.text_ja",
            rows=batch,
        )
        print(f"  text_ja progress: {min(i+batch_size, len(rows))}/{len(rows)}")
    return len(rows)


def import_llm_attributes(session, attrs_path: Path, batch_size: int) -> tuple[int, int]:
    rows = []
    seen_nodes: dict[str, dict] = {}
    for record in iter_jsonl(attrs_path):
        product_id = record.get("product_id")
        if not product_id:
            continue
        for attr in record.get("attributes", []):
            attr_type = normalize_value(attr.get("attribute_type") or "other")
            raw_value = attr.get("value")
            if not raw_value:
                continue
            value = normalize_value(raw_value)
            if not value:
                continue
            attribute_id = stable_id(attr_type, value)
            seen_nodes[attribute_id] = {
                "attribute_id": attribute_id,
                "name": attr.get("name") or attr_type,
                "value": value,
                "attribute_type": attr_type,
            }
            rows.append({
                "product_id": product_id,
                "attribute_id": attribute_id,
                "confidence": float(attr.get("confidence") or 0.7),
                "evidence": (attr.get("evidence") or "")[:300],
            })

    node_rows = list(seen_nodes.values())
    print(f"[llm attributes] {len(node_rows)} unique attribute nodes (merge), {len(rows)} product edges")

    for i in range(0, len(node_rows), batch_size):
        batch = node_rows[i:i + batch_size]
        session.run(
            """
            UNWIND $rows AS row
            MERGE (a:Attribute {attribute_id: row.attribute_id})
            ON CREATE SET a.name = row.name, a.value = row.value, a.attribute_type = row.attribute_type
            """,
            rows=batch,
        )
        print(f"  attribute nodes progress: {min(i+batch_size, len(node_rows))}/{len(node_rows)}")

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        session.run(
            """
            UNWIND $rows AS row
            MATCH (p:Product {product_id: row.product_id})
            MATCH (a:Attribute {attribute_id: row.attribute_id})
            MERGE (p)-[rel:HAS_ATTRIBUTE]->(a)
            ON CREATE SET rel.confidence = row.confidence, rel.evidence = row.evidence, rel.model = 'llm'
            """,
            rows=batch,
        )
        print(f"  HAS_ATTRIBUTE progress: {min(i+batch_size, len(rows))}/{len(rows)}")

    return len(node_rows), len(rows)


def import_mentions(session, mentions_paths: list[Path], batch_size: int) -> tuple[int, int]:
    seen_ids: set[str] = set()
    node_rows_map: dict[str, dict] = {}
    edge_rows = []
    for path in mentions_paths:
        if not path.exists():
            continue
        for record in iter_jsonl(path):
            review_id = record.get("review_id")
            if not review_id or review_id in seen_ids:
                continue
            seen_ids.add(review_id)
            for m in record.get("mentions", []):
                attr_type = normalize_value(m.get("attr_type") or "other")
                raw_value = m.get("value")
                if not raw_value:
                    continue
                value = normalize_value(raw_value)
                if not value:
                    continue
                attribute_id = stable_id(attr_type, value)
                node_rows_map[attribute_id] = {
                    "attribute_id": attribute_id,
                    "name": attr_type,
                    "value": value,
                    "attribute_type": attr_type,
                }
                edge_rows.append({
                    "review_id": review_id,
                    "attribute_id": attribute_id,
                    "sentiment": m.get("sentiment") or "neutral",
                    "confidence": float(m.get("confidence") or 0.7),
                })

    node_rows = list(node_rows_map.values())
    print(f"[mentions] {len(seen_ids)} reviews, {len(node_rows)} unique attribute nodes (merge), {len(edge_rows)} MENTIONS edges")

    for i in range(0, len(node_rows), batch_size):
        batch = node_rows[i:i + batch_size]
        session.run(
            """
            UNWIND $rows AS row
            MERGE (a:Attribute {attribute_id: row.attribute_id})
            ON CREATE SET a.name = row.name, a.value = row.value, a.attribute_type = row.attribute_type
            """,
            rows=batch,
        )
        print(f"  mention attribute nodes progress: {min(i+batch_size, len(node_rows))}/{len(node_rows)}")

    for i in range(0, len(edge_rows), batch_size):
        batch = edge_rows[i:i + batch_size]
        session.run(
            """
            UNWIND $rows AS row
            MATCH (r:Review {review_id: row.review_id})
            MATCH (a:Attribute {attribute_id: row.attribute_id})
            MERGE (r)-[rel:MENTIONS]->(a)
            ON CREATE SET rel.sentiment = row.sentiment, rel.confidence = row.confidence
            """,
            rows=batch,
        )
        print(f"  MENTIONS progress: {min(i+batch_size, len(edge_rows))}/{len(edge_rows)}")

    return len(node_rows), len(edge_rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="bolt://localhost:7688")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="password123")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--skip", nargs="*", default=[], help="skip steps: mentions, attributes, titles, reviews, values")
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    d = GPU_OUTPUT_DIR

    with driver.session(database=args.database) as session:
        if "mentions" not in args.skip:
            import_mentions(session, [d / "mentions_part1.jsonl", d / "mentions_part2.jsonl", d / "mentions_part3_gpu3.jsonl"], args.batch_size)
        if "attributes" not in args.skip:
            import_llm_attributes(session, d / "attributes_llm_output.jsonl", args.batch_size)
        if "titles" not in args.skip:
            import_titles_ja(session, [d / "titles_ja_gpu4.jsonl", d / "titles_ja_gpu5.jsonl"], args.batch_size)
        if "reviews" not in args.skip:
            import_reviews_ja(session, [
                d / "reviews_ja_gpu3.jsonl", d / "reviews_ja_gpu3b.jsonl",
                d / "reviews_ja_gpu4.jsonl", d / "reviews_ja_gpu5.jsonl",
                d / "reviews_ja_final.jsonl",
            ], args.batch_size)
        if "values" not in args.skip:
            import_attribute_values_ja(session, d / "attr_values_ja.jsonl", args.batch_size)
        if "features" not in args.skip:
            import_features_ja(session, [
                d / "features_ja_gpu3.jsonl", d / "features_ja_gpu5.jsonl",
                d / "features_ja_gpu4help.jsonl", d / "features_ja_gpu3final.jsonl",
            ], args.batch_size)
        if "review_titles" not in args.skip:
            import_review_titles_ja(session, [d / "review_titles_ja_gpu4.jsonl"], args.batch_size)

    driver.close()
    print("done")


if __name__ == "__main__":
    main()
