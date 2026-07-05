"""Add an image_url property to existing Product nodes from the Amazon metadata.

Lightweight enrichment: does NOT rebuild the graph. Reads the meta JSONL,
picks the best image URL per product, and SETs Product.image_url over Bolt.

Usage:
    python3 scripts/enrich_product_images.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def pick_image(images: list[dict]) -> str | None:
    """Prefer the MAIN variant, then large > hi_res > thumb."""
    if not images:
        return None
    main = [i for i in images if i.get("variant") == "MAIN"] or images
    for key in ("large", "hi_res", "thumb"):
        for img in main:
            if img.get(key):
                return img[key]
    return None


def iter_image_rows(meta_path: Path):
    open_fn = gzip.open if meta_path.suffix == ".gz" else open
    with open_fn(meta_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            pid = rec.get("parent_asin")
            url = pick_image(rec.get("images") or [])
            if pid and url:
                yield {"product_id": pid, "image_url": url}


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Set Product.image_url from metadata.")
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER") or os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    if not args.uri or not args.password:
        print("Set NEO4J_URI and NEO4J_PASSWORD in .env.", file=sys.stderr)
        sys.exit(2)

    from neo4j import GraphDatabase

    query = (
        "UNWIND $rows AS row "
        "MATCH (p:Product {product_id: row.product_id}) "
        "SET p.image_url = row.image_url"
    )

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    total = 0
    try:
        driver.verify_connectivity()
        batch: list[dict] = []
        with driver.session(database=args.database) as session:
            for row in iter_image_rows(args.meta_path):
                batch.append(row)
                if len(batch) >= args.batch_size:
                    session.execute_write(lambda tx, rows: tx.run(query, rows=rows).consume(), batch)
                    total += len(batch)
                    print(f"image_url set: {total:,}")
                    batch = []
            if batch:
                session.execute_write(lambda tx, rows: tx.run(query, rows=rows).consume(), batch)
                total += len(batch)
                print(f"image_url set: {total:,}")
    finally:
        driver.close()
    print(f"Done. {total:,} products enriched with image_url.")


if __name__ == "__main__":
    main()
