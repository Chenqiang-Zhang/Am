"""
Import all KG CSV files into Neo4j (or Neo4j Aura).

Run order (all under Am/kg_build/):
  1. python select_kcore.py              → decide the k-core user/product selection
  2. python build_base_graph.py          → base graph CSVs
  3. python extract_product_attributes.py + extract_review_mentions.py
  4. python canonicalize_attributes.py   → (optional) attr_type/value canonicalization
  5. python build_attribute_graph.py     → Attribute node + edge CSVs
  6. python import_kg_to_neo4j.py        → this script (imports everything)
  7. python backfill_display_fields.py --images --titles-ja  → (optional) Product.image_url / title_ja

Attribute jobs (nodes_attributes.csv, rel_has_attribute.csv, rel_mentions.csv)
are optional — silently skipped if the files do not exist yet.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Callable, Iterable

from utils.neo4j_io import connect, load_env_file, resolve_neo4j_conn


# ── helpers ────────────────────────────────────────────────────────────────────

def _float(value: str | None) -> float | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def iter_csv_batches(
    path: Path,
    batch_size: int,
    transform: Callable[[dict[str, str]], dict[str, Any]],
) -> Iterable[list[dict[str, Any]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch: list[dict[str, Any]] = []
        for row in reader:
            batch.append(transform(row))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def run_batches(
    driver: Any,
    database: str | None,
    label: str,
    path: Path,
    batch_size: int,
    query: str,
    transform: Callable[[dict[str, str]], dict[str, Any]],
) -> int:
    total = 0
    with driver.session(database=database) as session:
        for batch in iter_csv_batches(path, batch_size, transform):
            session.execute_write(lambda tx, rows=batch: tx.run(query, rows=rows).consume())
            total += len(batch)
            print(f"  {label}: {total:,}", end="\r")
    print(f"  {label}: {total:,}")
    return total


# ── constraints & indexes ──────────────────────────────────────────────────────

def create_schema(driver: Any, database: str | None) -> None:
    statements = [
        # unique constraints (also create implicit indexes)
        "CREATE CONSTRAINT user_id      IF NOT EXISTS FOR (u:User)      REQUIRE u.user_id      IS UNIQUE",
        "CREATE CONSTRAINT product_id   IF NOT EXISTS FOR (p:Product)   REQUIRE p.product_id   IS UNIQUE",
        "CREATE CONSTRAINT review_id    IF NOT EXISTS FOR (r:Review)    REQUIRE r.review_id    IS UNIQUE",
        "CREATE CONSTRAINT category_id  IF NOT EXISTS FOR (c:Category)  REQUIRE c.category_id  IS UNIQUE",
        "CREATE CONSTRAINT brand_id     IF NOT EXISTS FOR (b:Brand)     REQUIRE b.brand_id     IS UNIQUE",
        "CREATE CONSTRAINT attribute_id IF NOT EXISTS FOR (a:Attribute) REQUIRE a.attribute_id IS UNIQUE",
        # additional indexes for Attribute lookup
        "CREATE INDEX attr_type  IF NOT EXISTS FOR (a:Attribute) ON (a.attr_type)",
        "CREATE INDEX attr_value IF NOT EXISTS FOR (a:Attribute) ON (a.value)",
        # fulltext indexes for broader text search (see Graph_rule.md)
        "CREATE FULLTEXT INDEX product_description_ft IF NOT EXISTS FOR (n:Product) ON EACH [n.title, n.description]",
        "CREATE FULLTEXT INDEX review_text_ft IF NOT EXISTS FOR (n:Review) ON EACH [n.title, n.text]",
    ]
    with driver.session(database=database) as session:
        for stmt in statements:
            session.run(stmt).consume()
    print("Schema constraints and indexes applied.")


# ── import jobs ────────────────────────────────────────────────────────────────

def import_graph(
    driver: Any,
    database: str | None,
    input_dir: Path,
    batch_size: int,
) -> dict[str, int]:
    counts: dict[str, int] = {}

    # ── nodes ──────────────────────────────────────────────────────────────────
    node_jobs: list[tuple[str, Path, str, Callable]] = [
        (
            "users",
            input_dir / "nodes_users.csv",
            "UNWIND $rows AS row MERGE (:User {user_id: row.user_id})",
            lambda r: {"user_id": r["user_id"]},
        ),
        (
            "products",
            input_dir / "nodes_products.csv",
            """
            UNWIND $rows AS row
            MERGE (p:Product {product_id: row.product_id})
            SET p.title        = row.title,
                p.price        = row.price,
                p.avg_rating   = row.avg_rating,
                p.rating_count = row.rating_count,
                p.description  = row.description
            """,
            lambda r: {
                "product_id":   r["product_id"],
                "title":        r.get("title", ""),
                "price":        _float(r.get("price")),
                "avg_rating":   _float(r.get("avg_rating")),
                "rating_count": _int(r.get("rating_count")),
                "description":  r.get("description", ""),
            },
        ),
        (
            "reviews",
            input_dir / "nodes_reviews.csv",
            """
            UNWIND $rows AS row
            MERGE (rv:Review {review_id: row.review_id})
            SET rv.rating       = row.rating,
                rv.timestamp    = row.timestamp,
                rv.helpful_vote = row.helpful_vote,
                rv.title        = row.title,
                rv.text         = row.text
            """,
            lambda r: {
                "review_id":    r["review_id"],
                "rating":       _float(r.get("rating")),
                "timestamp":    _int(r.get("timestamp")),
                "helpful_vote": _int(r.get("helpful_vote")) or 0,
                "title":        r.get("title", ""),
                "text":         r.get("text", ""),
            },
        ),
        (
            "categories",
            input_dir / "nodes_categories.csv",
            """
            UNWIND $rows AS row
            MERGE (c:Category {category_id: row.category_id})
            SET c.name  = row.name,
                c.level = row.level
            """,
            lambda r: {
                "category_id": r["category_id"],
                "name":        r.get("name", ""),
                "level":       _int(r.get("level")),
            },
        ),
        (
            "brands",
            input_dir / "nodes_brands.csv",
            """
            UNWIND $rows AS row
            MERGE (b:Brand {brand_id: row.brand_id})
            SET b.name = row.name
            """,
            lambda r: {"brand_id": r["brand_id"], "name": r.get("name", "")},
        ),
        (
            "attributes",
            input_dir / "nodes_attributes.csv",
            """
            UNWIND $rows AS row
            MERGE (a:Attribute {attribute_id: row.attribute_id})
            SET a.attr_type = row.attr_type,
                a.value     = row.value
            """,
            lambda r: {
                "attribute_id": r["attribute_id"],
                "attr_type":    r.get("attr_type", ""),
                "value":        r.get("value", ""),
            },
        ),
    ]

    # ── relationships ──────────────────────────────────────────────────────────
    rel_jobs: list[tuple[str, Path, str, Callable]] = [
        (
            "WROTE (User→Review)",
            input_dir / "rel_wrote.csv",
            """
            UNWIND $rows AS row
            MATCH (u:User    {user_id:   row.user_id})
            MATCH (rv:Review {review_id: row.review_id})
            MERGE (u)-[:WROTE]->(rv)
            """,
            lambda r: {"user_id": r["user_id"], "review_id": r["review_id"]},
        ),
        (
            "ABOUT (Review→Product)",
            input_dir / "rel_about.csv",
            """
            UNWIND $rows AS row
            MATCH (rv:Review  {review_id:  row.review_id})
            MATCH (p:Product  {product_id: row.product_id})
            MERGE (rv)-[:ABOUT]->(p)
            """,
            lambda r: {"review_id": r["review_id"], "product_id": r["product_id"]},
        ),
        (
            "RATED (User→Product)",
            input_dir / "rel_rated.csv",
            """
            UNWIND $rows AS row
            MATCH (u:User   {user_id:   row.user_id})
            MATCH (p:Product {product_id: row.product_id})
            MERGE (u)-[rel:RATED]->(p)
            SET rel.rating    = row.rating,
                rel.timestamp = row.timestamp
            """,
            lambda r: {
                "user_id":    r["user_id"],
                "product_id": r["product_id"],
                "rating":     _float(r.get("rating")),
                "timestamp":  _int(r.get("timestamp")),
            },
        ),
        (
            "MADE_BY (Product→Brand)",
            input_dir / "rel_made_by.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product {product_id: row.product_id})
            MATCH (b:Brand   {brand_id:   row.brand_id})
            MERGE (p)-[:MADE_BY]->(b)
            """,
            lambda r: {"product_id": r["product_id"], "brand_id": r["brand_id"]},
        ),
        (
            "BELONGS_TO (Product→Category)",
            input_dir / "rel_belongs_to.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product  {product_id:  row.product_id})
            MATCH (c:Category {category_id: row.category_id})
            MERGE (p)-[:BELONGS_TO]->(c)
            """,
            lambda r: {"product_id": r["product_id"], "category_id": r["category_id"]},
        ),
        (
            "SUBCATEGORY_OF (Category→Category)",
            input_dir / "rel_subcategory_of.csv",
            """
            UNWIND $rows AS row
            MATCH (child:Category  {category_id: row.child_category_id})
            MATCH (parent:Category {category_id: row.parent_category_id})
            MERGE (child)-[:SUBCATEGORY_OF]->(parent)
            """,
            lambda r: {
                "child_category_id":  r["child_category_id"],
                "parent_category_id": r["parent_category_id"],
            },
        ),
        (
            "HAS_ATTRIBUTE (Product→Attribute)",
            input_dir / "rel_has_attribute.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product  {product_id:  row.product_id})
            MATCH (a:Attribute {attribute_id: row.attribute_id})
            MERGE (p)-[rel:HAS_ATTRIBUTE]->(a)
            """,
            lambda r: {
                "product_id":   r["product_id"],
                "attribute_id": r["attribute_id"],
            },
        ),
        (
            "MENTIONS (Review→Attribute)",
            input_dir / "rel_mentions.csv",
            """
            UNWIND $rows AS row
            MATCH (rv:Review  {review_id:   row.review_id})
            MATCH (a:Attribute {attribute_id: row.attribute_id})
            MERGE (rv)-[rel:MENTIONS]->(a)
            SET rel.sentiment  = row.sentiment
            """,
            lambda r: {
                "review_id":    r["review_id"],
                "attribute_id": r["attribute_id"],
                "sentiment":    r.get("sentiment", "neutral"),
            },
        ),
    ]

    for label, path, query, transform in node_jobs + rel_jobs:
        if not path.exists():
            print(f"  skipping (file not found): {path.name}")
            continue
        counts[label] = run_batches(driver, database, label, path, batch_size, query, transform)

    return counts


# ── summary ────────────────────────────────────────────────────────────────────

def print_summary(driver: Any, database: str | None) -> None:
    with driver.session(database=database) as session:
        print("\nNode counts:")
        for rec in session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY label"
        ):
            print(f"  {rec['label']}: {rec['cnt']:,}")

        print("\nRelationship counts:")
        for rec in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS cnt ORDER BY type"
        ):
            print(f"  {rec['type']}: {rec['cnt']:,}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import KG CSV files into Neo4j / Neo4j Aura."
    )
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--input-dir", type=Path, default=None,
                        help="Directory containing CSV files (default: data.output_dir from config.yaml)")
    parser.add_argument("--uri",      default=None, help="Neo4j URI (overrides config)")
    parser.add_argument("--user",     default=None, help="Neo4j username (overrides config)")
    parser.add_argument("--password", default=None, help="Neo4j password (overrides .env)")
    parser.add_argument("--database", default=None)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--counts-only", action="store_true",
                        help="Skip import; just print current node/rel counts.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config_dir = args.config.resolve().parent

    # load .env from the directory containing config.yaml (Am/)
    load_env_file(config_dir / ".env")
    load_env_file()  # fallback: also try CWD/.env

    cfg: dict = {}
    if args.config.exists():
        import yaml
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    data_cfg: dict = cfg.get("data", {})

    conn = resolve_neo4j_conn(args, cfg)
    uri, user, password, database = conn["uri"], conn["user"], conn["password"], conn["database"]
    input_dir = args.input_dir or (config_dir / data_cfg.get("output_dir", "kg_output/video_games"))

    driver = connect(uri, user, password)
    try:
        driver.verify_connectivity()
        print(f"Connected to {uri}")

        if args.counts_only:
            print_summary(driver, database)
            return

        print("\nApplying schema…")
        create_schema(driver, database)

        print(f"\nImporting from {input_dir}…")
        import_graph(driver, database, input_dir, args.batch_size)

        print_summary(driver, database)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
