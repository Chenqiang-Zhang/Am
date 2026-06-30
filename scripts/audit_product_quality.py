from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_QUERY = """
MATCH (p:Product)
CALL (p) {
  OPTIONAL MATCH (p)-[:HAS_FEATURE]->(f:Feature)
  RETURN count(f) AS feature_count
}
CALL (p) {
  OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
  RETURN count(a) AS attribute_count
}
CALL (p) {
  OPTIONAL MATCH (r:Review)-[:REVIEWS]->(p)
  RETURN count(r) AS review_count
}
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       properties(p).image_url AS image_url,
       p.main_category AS main_category,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       feature_count,
       attribute_count,
       review_count
"""


UPDATE_QUERY = """
UNWIND $rows AS row
MATCH (p:Product {product_id: row.product_id})
SET p.sellable_status = row.sellable_status,
    p.data_quality_score = row.data_quality_score,
    p.quality_flags = row.quality_flags,
    p.quality_audited_at = $audited_at,
    p.title_duplicate_key = row.title_duplicate_key,
    p.title_duplicate_count = row.title_duplicate_count,
    p.has_valid_price = row.has_valid_price,
    p.has_image = row.has_image,
    p.has_useful_content = row.has_useful_content
"""


INDEX_STATEMENTS = [
    "CREATE INDEX product_sellable_status IF NOT EXISTS FOR (p:Product) ON (p.sellable_status)",
    "CREATE INDEX product_quality_score IF NOT EXISTS FOR (p:Product) ON (p.data_quality_score)",
]


@dataclass
class AuditConfig:
    min_quality_score: float
    min_title_len: int
    min_content_count: int
    duplicate_sample_limit: int


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def title_duplicate_key(title: str) -> str:
    key = title.lower()
    key = re.sub(r"[^a-z0-9]+", " ", key)
    key = re.sub(r"\b(pack|pcs|piece|pieces|set|new)\b", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def valid_title(title: str, min_title_len: int) -> bool:
    if len(title) < min_title_len:
        return False
    alnum_count = len(re.findall(r"[A-Za-z0-9]", title))
    if alnum_count < max(5, min_title_len // 2):
        return False
    bad_values = {"unknown", "none", "null", "n/a", "na"}
    return title.lower() not in bad_values


def product_score_and_status(
    row: dict[str, Any],
    duplicate_count: int,
    config: AuditConfig,
) -> dict[str, Any]:
    title = clean_text(row.get("title"))
    price = optional_float(row.get("price"))
    image_url = clean_text(row.get("image_url"))
    category = clean_text(row.get("main_category"))
    average_rating = optional_float(row.get("average_rating"))
    rating_number = optional_int(row.get("rating_number")) or 0
    feature_count = optional_int(row.get("feature_count")) or 0
    attribute_count = optional_int(row.get("attribute_count")) or 0
    review_count = optional_int(row.get("review_count")) or 0
    content_count = feature_count + attribute_count

    has_valid_price = price is not None and price > 0
    has_image = bool(image_url)
    has_title = valid_title(title, config.min_title_len)
    has_useful_content = content_count >= config.min_content_count
    has_rating = average_rating is not None and rating_number > 0
    has_category = bool(category)
    duplicate_suspect = bool(title and duplicate_count > 1)

    content_score = min(content_count / max(config.min_content_count * 3, 1), 1.0)
    rating_count_score = min(math.log1p(rating_number) / math.log1p(1000), 1.0) if rating_number else 0.0
    rating_score = 0.7 + 0.3 * rating_count_score if has_rating else 0.0

    score = (
        (0.25 if has_valid_price else 0.0)
        + (0.15 if has_image else 0.0)
        + (0.20 if has_title else 0.0)
        + (0.20 * content_score)
        + (0.10 * rating_score)
        + (0.05 if has_category else 0.0)
        + (0.05 if not duplicate_suspect else 0.0)
    )
    score = round(min(max(score, 0.0), 1.0), 4)

    flags: list[str] = []
    if not has_valid_price:
        flags.append("missing_price")
    if not has_image:
        flags.append("missing_image")
    if not has_title:
        flags.append("missing_or_short_title")
    if not has_useful_content:
        flags.append("missing_useful_content")
    if not has_rating:
        flags.append("missing_rating")
    if not has_category:
        flags.append("missing_category")
    if duplicate_suspect:
        flags.append("duplicate_suspect")
    if score < config.min_quality_score:
        flags.append("low_quality")

    if not has_title or score < config.min_quality_score:
        status = "low_quality"
    elif duplicate_suspect:
        status = "duplicate_suspect"
    elif not has_valid_price:
        status = "currently_unavailable"
    elif not has_image:
        status = "missing_image"
    else:
        status = "available"

    return {
        "product_id": row["product_id"],
        "title": title,
        "data_quality_score": score,
        "sellable_status": status,
        "quality_flags": flags,
        "title_duplicate_key": title_duplicate_key(title),
        "title_duplicate_count": duplicate_count,
        "has_valid_price": has_valid_price,
        "has_image": has_image,
        "has_useful_content": has_useful_content,
        "feature_count": feature_count,
        "attribute_count": attribute_count,
        "review_count": review_count,
    }


def fetch_products(driver: Any, database: str | None) -> list[dict[str, Any]]:
    with driver.session(database=database) as session:
        result = session.run(PRODUCT_QUERY)
        return [dict(record) for record in result]


def write_batches(
    driver: Any,
    database: str | None,
    rows: list[dict[str, Any]],
    batch_size: int,
    audited_at: str,
) -> None:
    with driver.session(database=database) as session:
        for statement in INDEX_STATEMENTS:
            session.run(statement).consume()
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            session.execute_write(
                lambda tx, payload: tx.run(UPDATE_QUERY, rows=payload, audited_at=audited_at).consume(),
                batch,
            )
            print(f"updated {min(start + batch_size, len(rows)):,}/{len(rows):,}")


def build_report(
    rows: list[dict[str, Any]],
    audited_at: str,
    config: AuditConfig,
    duplicate_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts = Counter(row["sellable_status"] for row in rows)
    flag_counts: Counter[str] = Counter()
    for row in rows:
        flag_counts.update(row["quality_flags"])

    buckets = Counter()
    for row in rows:
        score = row["data_quality_score"]
        bucket = f"{math.floor(score * 10) / 10:.1f}-{math.floor(score * 10) / 10 + 0.1:.1f}"
        if score == 1.0:
            bucket = "1.0"
        buckets[bucket] += 1

    available_high_quality = sum(
        1
        for row in rows
        if row["sellable_status"] == "available" and row["data_quality_score"] >= config.min_quality_score
    )

    return {
        "audited_at": audited_at,
        "thresholds": {
            "min_quality_score": config.min_quality_score,
            "min_title_len": config.min_title_len,
            "min_content_count": config.min_content_count,
        },
        "summary": {
            "total_products": len(rows),
            "available_high_quality": available_high_quality,
            "available_high_quality_rate": round(available_high_quality / len(rows), 4) if rows else 0,
        },
        "sellable_status_counts": dict(status_counts.most_common()),
        "quality_flag_counts": dict(flag_counts.most_common()),
        "score_buckets": dict(sorted(buckets.items())),
        "duplicate_samples": duplicate_samples,
    }


def write_reports(report: dict[str, Any], output_dir: Path, prefix: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{prefix}.json"
    md_path = output_dir / f"{prefix}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Product Quality Audit",
        "",
        f"- audited_at: `{report['audited_at']}`",
        f"- total_products: `{report['summary']['total_products']:,}`",
        f"- available_high_quality: `{report['summary']['available_high_quality']:,}`",
        f"- available_high_quality_rate: `{report['summary']['available_high_quality_rate']:.2%}`",
        "",
        "## Sellable Status Counts",
        "",
        "| status | count |",
        "| --- | ---: |",
    ]
    for key, value in report["sellable_status_counts"].items():
        lines.append(f"| {key} | {value:,} |")

    lines.extend(["", "## Quality Flag Counts", "", "| flag | count |", "| --- | ---: |"])
    for key, value in report["quality_flag_counts"].items():
        lines.append(f"| {key} | {value:,} |")

    lines.extend(["", "## Score Buckets", "", "| score | count |", "| --- | ---: |"])
    for key, value in report["score_buckets"].items():
        lines.append(f"| {key} | {value:,} |")

    if report["duplicate_samples"]:
        lines.extend(["", "## Duplicate Samples", ""])
        for sample in report["duplicate_samples"]:
            lines.append(f"- key: `{sample['title_duplicate_key']}` ({sample['count']} products)")
            for item in sample["products"]:
                lines.append(f"  - `{item['product_id']}` {item['title'][:140]}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def duplicate_samples(
    rows: list[dict[str, Any]],
    duplicate_counts: Counter[str],
    limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    duplicate_keys = {key for key, count in duplicate_counts.items() if key and count > 1}
    for row in rows:
        key = title_duplicate_key(clean_text(row.get("title")))
        if key in duplicate_keys and len(grouped[key]) < 5:
            grouped[key].append({"product_id": row["product_id"], "title": clean_text(row.get("title"))})

    top_keys = [key for key, _ in duplicate_counts.most_common() if key in duplicate_keys][:limit]
    return [
        {
            "title_duplicate_key": key,
            "count": duplicate_counts[key],
            "products": grouped[key],
        }
        for key in top_keys
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Product node quality and write sellability fields to Neo4j.")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER") or os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--min-quality-score", type=float, default=0.6)
    parser.add_argument("--min-title-len", type=int, default=8)
    parser.add_argument("--min-content-count", type=int, default=1)
    parser.add_argument("--duplicate-sample-limit", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/product_quality"))
    parser.add_argument("--dry-run", action="store_true", help="Generate reports without writing fields to Neo4j.")
    return parser.parse_args()


def main() -> int:
    load_env_file()
    args = parse_args()
    if not args.uri or not args.password:
        print("Set NEO4J_URI and NEO4J_PASSWORD in .env or pass --uri/--password.", file=sys.stderr)
        return 2

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("Install neo4j: pip install neo4j", file=sys.stderr)
        return 2

    config = AuditConfig(
        min_quality_score=args.min_quality_score,
        min_title_len=args.min_title_len,
        min_content_count=args.min_content_count,
        duplicate_sample_limit=args.duplicate_sample_limit,
    )
    audited_at = datetime.now(timezone.utc).isoformat()
    prefix = "product_quality_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        print("fetching products...")
        products = fetch_products(driver, args.database)
        duplicate_counts = Counter(title_duplicate_key(clean_text(row.get("title"))) for row in products)
        audited_rows = [
            product_score_and_status(
                row,
                duplicate_counts[title_duplicate_key(clean_text(row.get("title")))],
                config,
            )
            for row in products
        ]
        samples = duplicate_samples(products, duplicate_counts, args.duplicate_sample_limit)
        report = build_report(audited_rows, audited_at, config, samples)
        json_path, md_path = write_reports(report, args.output_dir, prefix)
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")

        if args.dry_run:
            print("dry-run: skipped Neo4j updates")
        else:
            write_batches(driver, args.database, audited_rows, args.batch_size, audited_at)

        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
