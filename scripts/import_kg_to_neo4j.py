from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any, Callable


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


def clean_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def clean_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def clean_bool(value: str) -> bool | None:
    value = (value or "").strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def read_csv_batches(
    path: Path,
    batch_size: int,
    transform: Callable[[dict[str, str]], dict[str, Any]],
):
    with path.open("r", newline="", encoding="utf-8") as f:
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
        for batch in read_csv_batches(path, batch_size, transform):
            session.execute_write(lambda tx, rows: tx.run(query, rows=rows).consume(), batch)
            total += len(batch)
            print(f"{label}: {total:,}")
    return total


def create_constraints(driver: Any, database: str | None) -> None:
    statements = [
        "CREATE CONSTRAINT user_id IF NOT EXISTS FOR (u:User) REQUIRE u.user_id IS UNIQUE",
        "CREATE CONSTRAINT product_id IF NOT EXISTS FOR (p:Product) REQUIRE p.product_id IS UNIQUE",
        "CREATE CONSTRAINT review_id IF NOT EXISTS FOR (r:Review) REQUIRE r.review_id IS UNIQUE",
        "CREATE CONSTRAINT category_id IF NOT EXISTS FOR (c:Category) REQUIRE c.category_id IS UNIQUE",
        "CREATE CONSTRAINT store_id IF NOT EXISTS FOR (s:Store) REQUIRE s.store_id IS UNIQUE",
        "CREATE CONSTRAINT feature_id IF NOT EXISTS FOR (f:Feature) REQUIRE f.feature_id IS UNIQUE",
    ]
    with driver.session(database=database) as session:
        for statement in statements:
            session.run(statement).consume()


def import_graph(driver: Any, database: str | None, input_dir: Path, batch_size: int) -> dict[str, int]:
    counts: dict[str, int] = {}

    node_jobs = [
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
            SET p.title = row.title,
                p.main_category = row.main_category,
                p.price = row.price,
                p.average_rating = row.average_rating,
                p.rating_number = row.rating_number
            """,
            lambda r: {
                "product_id": r["product_id"],
                "title": r.get("title", ""),
                "main_category": r.get("main_category", ""),
                "price": clean_float(r.get("price", "")),
                "average_rating": clean_float(r.get("average_rating", "")),
                "rating_number": clean_int(r.get("rating_number", "")),
            },
        ),
        (
            "reviews",
            input_dir / "nodes_reviews.csv",
            """
            UNWIND $rows AS row
            MERGE (r:Review {review_id: row.review_id})
            SET r.title = row.title,
                r.text = row.text,
                r.rating = row.rating,
                r.timestamp = row.timestamp,
                r.helpful_vote = row.helpful_vote,
                r.verified_purchase = row.verified_purchase
            """,
            lambda r: {
                "review_id": r["review_id"],
                "title": r.get("title", ""),
                "text": r.get("text", ""),
                "rating": clean_float(r.get("rating", "")),
                "timestamp": clean_int(r.get("timestamp", "")),
                "helpful_vote": clean_int(r.get("helpful_vote", "")) or 0,
                "verified_purchase": clean_bool(r.get("verified_purchase", "")),
            },
        ),
        (
            "categories",
            input_dir / "nodes_categories.csv",
            "UNWIND $rows AS row MERGE (c:Category {category_id: row.category_id}) SET c.name = row.name",
            lambda r: {"category_id": r["category_id"], "name": r.get("name", "")},
        ),
        (
            "stores",
            input_dir / "nodes_stores.csv",
            "UNWIND $rows AS row MERGE (s:Store {store_id: row.store_id}) SET s.name = row.name",
            lambda r: {"store_id": r["store_id"], "name": r.get("name", "")},
        ),
        (
            "features",
            input_dir / "nodes_features.csv",
            """
            UNWIND $rows AS row
            MERGE (f:Feature {feature_id: row.feature_id})
            SET f.text = row.text,
                f.normalized_text = row.normalized_text
            """,
            lambda r: {"feature_id": r["feature_id"], "text": r.get("text", ""), "normalized_text": r.get("normalized_text", "")},
        ),
    ]

    rel_jobs = [
        (
            "rel_wrote",
            input_dir / "rel_wrote.csv",
            """
            UNWIND $rows AS row
            MATCH (u:User {user_id: row.user_id})
            MATCH (r:Review {review_id: row.review_id})
            MERGE (u)-[:WROTE]->(r)
            """,
            lambda r: {"user_id": r["user_id"], "review_id": r["review_id"]},
        ),
        (
            "rel_reviews",
            input_dir / "rel_reviews.csv",
            """
            UNWIND $rows AS row
            MATCH (r:Review {review_id: row.review_id})
            MATCH (p:Product {product_id: row.product_id})
            MERGE (r)-[:REVIEWS]->(p)
            """,
            lambda r: {"review_id": r["review_id"], "product_id": r["product_id"]},
        ),
        (
            "rel_rated",
            input_dir / "rel_rated.csv",
            """
            UNWIND $rows AS row
            MATCH (u:User {user_id: row.user_id})
            MATCH (p:Product {product_id: row.product_id})
            MERGE (u)-[rel:RATED]->(p)
            SET rel.rating = row.rating,
                rel.timestamp = row.timestamp,
                rel.verified_purchase = row.verified_purchase
            """,
            lambda r: {
                "user_id": r["user_id"],
                "product_id": r["product_id"],
                "rating": clean_float(r.get("rating", "")),
                "timestamp": clean_int(r.get("timestamp", "")),
                "verified_purchase": clean_bool(r.get("verified_purchase", "")),
            },
        ),
        (
            "rel_product_category",
            input_dir / "rel_product_category.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product {product_id: row.product_id})
            MATCH (c:Category {category_id: row.category_id})
            MERGE (p)-[:BELONGS_TO]->(c)
            """,
            lambda r: {"product_id": r["product_id"], "category_id": r["category_id"]},
        ),
        (
            "rel_product_store",
            input_dir / "rel_product_store.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product {product_id: row.product_id})
            MATCH (s:Store {store_id: row.store_id})
            MERGE (p)-[:SOLD_BY]->(s)
            """,
            lambda r: {"product_id": r["product_id"], "store_id": r["store_id"]},
        ),
        (
            "rel_product_feature",
            input_dir / "rel_product_feature.csv",
            """
            UNWIND $rows AS row
            MATCH (p:Product {product_id: row.product_id})
            MATCH (f:Feature {feature_id: row.feature_id})
            MERGE (p)-[:HAS_FEATURE]->(f)
            """,
            lambda r: {"product_id": r["product_id"], "feature_id": r["feature_id"]},
        ),
    ]

    for label, path, query, transform in node_jobs + rel_jobs:
        if not path.exists():
            print(f"Skipping missing file: {path}")
            continue
        counts[label] = run_batches(driver, database, label, path, batch_size, query, transform)

    return counts


def print_counts(driver: Any, database: str | None) -> None:
    with driver.session(database=database) as session:
        print("\nNode counts")
        for record in session.run("MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY labels"):
            print(f"{record['labels']}: {record['count']:,}")

        print("\nRelationship counts")
        for record in session.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type"):
            print(f"{record['type']}: {record['count']:,}")


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Import local KG CSV files into Neo4j or Neo4j Aura over Bolt.")
    parser.add_argument("--input-dir", type=Path, default=Path("kg_output/all_beauty"))
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER") or os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--skip-import", action="store_true", help="Only print current database counts.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.uri or not args.password:
        print("Set NEO4J_URI and NEO4J_PASSWORD in .env or pass --uri/--password.", file=sys.stderr)
        sys.exit(2)

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("The neo4j package is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
        if not args.skip_import:
            create_constraints(driver, args.database)
            import_graph(driver, args.database, args.input_dir, args.batch_size)
        print_counts(driver, args.database)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
