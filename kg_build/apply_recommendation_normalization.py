"""Apply recommendation normalization as reversible Attribute properties."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def canonical_value(data: dict[str, Any], canonical_type: str, raw_value: str) -> str | None:
    ontology = data.get("value_ontology", {}).get(canonical_type)
    if not ontology:
        return raw_value
    mapped = data.get("value_map", {}).get(canonical_type, {}).get(raw_value)
    if mapped:
        return mapped
    normalized = token(raw_value)
    return normalized if normalized in set(ontology) - {"other"} else None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    p.add_argument("--map", type=Path, required=True)
    p.add_argument("--uri")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--database")
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    root = args.config.resolve().parent
    load_env(root / ".env")
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    neo = cfg.get("neo4j", {})
    uri = args.uri or os.environ.get("NEO4J_URI") or neo.get("uri")
    user = args.user or os.environ.get("NEO4J_USERNAME") or neo.get("username", "neo4j")
    password = args.password or os.environ.get("NEO4J_PASSWORD") or neo.get("password")
    database = args.database or os.environ.get("NEO4J_DATABASE") or neo.get("database", "neo4j")
    if not uri or not password:
        raise SystemExit("Neo4j connection is not configured")

    data = json.loads(args.map.read_text(encoding="utf-8"))
    attr_map: dict[str, str] = data["attr_type_map"]
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            rows = [
                dict(record) for record in session.run(
                    "MATCH (a:Attribute) WHERE a.attr_type IN $types "
                    "RETURN a.attribute_id AS attribute_id, a.attr_type AS attr_type, a.value AS value",
                    types=list(attr_map),
                )
            ]
        updates = []
        counts: dict[str, int] = {}
        recognized = 0
        for row in rows:
            ctype = attr_map[row["attr_type"]]
            cvalue = canonical_value(data, ctype, row["value"])
            recognized += cvalue is not None
            counts[ctype] = counts.get(ctype, 0) + 1
            updates.append({
                "attribute_id": row["attribute_id"],
                "canonical_type": ctype,
                "canonical_value": cvalue,
            })
        print(
            f"matched_attributes={len(updates)} recognized_values={recognized} "
            f"unresolved_values={len(updates)-recognized}"
        )
        print("by_type=" + json.dumps(counts, sort_keys=True))
        if not args.apply:
            print("dry_run=1 (pass --apply to write properties)")
            return
        query = (
            "UNWIND $rows AS row MATCH (a:Attribute {attribute_id:row.attribute_id}) "
            "SET a.canonical_type=row.canonical_type, a.canonical_value=row.canonical_value"
        )
        with driver.session(database=database) as session:
            for index in range(0, len(updates), args.batch_size):
                batch = updates[index:index + args.batch_size]
                session.execute_write(lambda tx, rs: tx.run(query, rows=rs).consume(), batch)
        print(f"applied={len(updates)}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
