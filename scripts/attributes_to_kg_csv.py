from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split()).strip()


def normalize(value: Any) -> str:
    return clean_text(value).lower()


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return len(rows)


def convert(input_path: Path, output_dir: Path) -> dict[str, int]:
    attributes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    for record in read_jsonl(input_path):
        product_id = clean_text(record.get("product_id"))
        if not product_id:
            continue

        model = clean_text(record.get("model"))
        for attr in record.get("attributes", []):
            name = normalize(attr.get("name"))
            value = normalize(attr.get("value"))
            attr_type = normalize(attr.get("attribute_type")) or "other"
            if not name or not value:
                continue

            attribute_id = stable_id("attribute", f"{attr_type}|{name}|{value}")
            attributes[attribute_id] = {
                "attribute_id": attribute_id,
                "name": name,
                "value": value,
                "attribute_type": attr_type,
            }

            edge_key = (product_id, attribute_id)
            confidence = attr.get("confidence", "")
            edges[edge_key] = {
                "product_id": product_id,
                "attribute_id": attribute_id,
                "confidence": confidence,
                "evidence": clean_text(attr.get("evidence")),
                "model": model,
            }

    counts = {
        "attributes": write_csv(
            output_dir / "nodes_attributes.csv",
            list(attributes.values()),
            ["attribute_id", "name", "value", "attribute_type"],
        ),
        "rel_product_attribute": write_csv(
            output_dir / "rel_product_attribute.csv",
            list(edges.values()),
            ["product_id", "attribute_id", "confidence", "evidence", "model"],
        ),
    }
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert LLM product attribute JSONL to Neo4j-ready CSV files.")
    parser.add_argument("--input-path", type=Path, default=Path("kg_output/attributes/product_attributes_llm.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("kg_output/all_beauty"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    counts = convert(args.input_path, args.output_dir)
    for name, count in counts.items():
        print(f"{name}: {count:,}")
    print(f"CSV files written to: {args.output_dir}")


if __name__ == "__main__":
    main()
