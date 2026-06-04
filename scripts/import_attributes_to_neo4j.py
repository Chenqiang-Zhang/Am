from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any


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


def clean_float(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def import_attributes(driver: Any, database: str | None, input_dir: Path) -> None:
    attr_rows = read_csv(input_dir / "nodes_attributes.csv")
    rel_rows = [
        {
            "product_id": row["product_id"],
            "attribute_id": row["attribute_id"],
            "confidence": clean_float(row.get("confidence")),
            "evidence": row.get("evidence", ""),
            "model": row.get("model", ""),
        }
        for row in read_csv(input_dir / "rel_product_attribute.csv")
    ]

    with driver.session(database=database) as session:
        session.run("CREATE CONSTRAINT attribute_id IF NOT EXISTS FOR (a:Attribute) REQUIRE a.attribute_id IS UNIQUE").consume()
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MERGE (a:Attribute {attribute_id: row.attribute_id})
                SET a.name = row.name,
                    a.value = row.value,
                    a.attribute_type = row.attribute_type
                """,
                rows=attr_rows,
            ).consume()
        )
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MATCH (p:Product {product_id: row.product_id})
                MATCH (a:Attribute {attribute_id: row.attribute_id})
                MERGE (p)-[rel:HAS_ATTRIBUTE]->(a)
                SET rel.confidence = row.confidence,
                    rel.evidence = row.evidence,
                    rel.model = row.model
                """,
                rows=rel_rows,
            ).consume()
        )

        print(f"attributes: {len(attr_rows):,}")
        print(f"rel_product_attribute: {len(rel_rows):,}")
        print("\nAttribute counts by type")
        for record in session.run("MATCH (a:Attribute) RETURN a.attribute_type AS type, count(*) AS count ORDER BY count DESC"):
            print(f"{record['type']}: {record['count']:,}")


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(description="Import LLM-extracted attribute CSV files into Neo4j or Neo4j Aura.")
    parser.add_argument("--input-dir", type=Path, default=Path("kg_output/all_beauty"))
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER") or os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
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
        import_attributes(driver, args.database, args.input_dir)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
