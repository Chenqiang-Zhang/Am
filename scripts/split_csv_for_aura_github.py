from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path


MAX_BYTES_DEFAULT = 20 * 1024 * 1024


NODE_IMPORTS = {
    "nodes_users": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (:User {{user_id: row.user_id}});
""",
    "nodes_products": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (p:Product {{product_id: row.product_id}})
SET p.title = row.title,
    p.main_category = row.main_category,
    p.price = CASE row.price WHEN '' THEN null ELSE toFloat(row.price) END,
    p.average_rating = CASE row.average_rating WHEN '' THEN null ELSE toFloat(row.average_rating) END,
    p.rating_number = CASE row.rating_number WHEN '' THEN null ELSE toInteger(row.rating_number) END;
""",
    "nodes_reviews": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (r:Review {{review_id: row.review_id}})
SET r.title = row.title,
    r.text = row.text,
    r.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    r.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    r.helpful_vote = CASE row.helpful_vote WHEN '' THEN 0 ELSE toInteger(row.helpful_vote) END,
    r.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;
""",
    "nodes_categories": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (c:Category {{category_id: row.category_id}})
SET c.name = row.name;
""",
    "nodes_stores": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (s:Store {{store_id: row.store_id}})
SET s.name = row.name;
""",
    "nodes_features": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MERGE (f:Feature {{feature_id: row.feature_id}})
SET f.text = row.text,
    f.normalized_text = row.normalized_text;
""",
}


REL_IMPORTS = {
    "rel_wrote": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (u:User {{user_id: row.user_id}})
MATCH (r:Review {{review_id: row.review_id}})
MERGE (u)-[:WROTE]->(r);
""",
    "rel_reviews": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (r:Review {{review_id: row.review_id}})
MATCH (p:Product {{product_id: row.product_id}})
MERGE (r)-[:REVIEWS]->(p);
""",
    "rel_rated": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (u:User {{user_id: row.user_id}})
MATCH (p:Product {{product_id: row.product_id}})
MERGE (u)-[rel:RATED]->(p)
SET rel.rating = CASE row.rating WHEN '' THEN null ELSE toFloat(row.rating) END,
    rel.timestamp = CASE row.timestamp WHEN '' THEN null ELSE toInteger(row.timestamp) END,
    rel.verified_purchase = CASE row.verified_purchase WHEN 'True' THEN true WHEN 'False' THEN false ELSE null END;
""",
    "rel_product_category": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (p:Product {{product_id: row.product_id}})
MATCH (c:Category {{category_id: row.category_id}})
MERGE (p)-[:BELONGS_TO]->(c);
""",
    "rel_product_store": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (p:Product {{product_id: row.product_id}})
MATCH (s:Store {{store_id: row.store_id}})
MERGE (p)-[:SOLD_BY]->(s);
""",
    "rel_product_feature": """LOAD CSV WITH HEADERS FROM '{url}' AS row
MATCH (p:Product {{product_id: row.product_id}})
MATCH (f:Feature {{feature_id: row.feature_id}})
MERGE (p)-[:HAS_FEATURE]->(f);
""",
}


CONSTRAINTS = """CREATE CONSTRAINT user_id IF NOT EXISTS
FOR (u:User) REQUIRE u.user_id IS UNIQUE;

CREATE CONSTRAINT product_id IF NOT EXISTS
FOR (p:Product) REQUIRE p.product_id IS UNIQUE;

CREATE CONSTRAINT review_id IF NOT EXISTS
FOR (r:Review) REQUIRE r.review_id IS UNIQUE;

CREATE CONSTRAINT category_id IF NOT EXISTS
FOR (c:Category) REQUIRE c.category_id IS UNIQUE;

CREATE CONSTRAINT store_id IF NOT EXISTS
FOR (s:Store) REQUIRE s.store_id IS UNIQUE;

CREATE CONSTRAINT feature_id IF NOT EXISTS
FOR (f:Feature) REQUIRE f.feature_id IS UNIQUE;
"""


CHECKS = """MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY labels;

MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS count
ORDER BY relationship;
"""


def split_csv(path: Path, output_dir: Path, max_bytes: int) -> list[Path]:
    if path.stat().st_size <= max_bytes:
        target = output_dir / path.name
        shutil.copy2(path, target)
        return [target]

    output_paths: list[Path] = []
    stem = path.stem

    with path.open("r", newline="", encoding="utf-8") as src:
        reader = csv.reader(src)
        header = next(reader)

        part_index = 1
        current_path = output_dir / f"{stem}_part{part_index:03d}.csv"
        current_file = current_path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(current_file)
        writer.writerow(header)
        output_paths.append(current_path)

        for row in reader:
            writer.writerow(row)
            if current_file.tell() >= max_bytes:
                current_file.close()
                part_index += 1
                current_path = output_dir / f"{stem}_part{part_index:03d}.csv"
                current_file = current_path.open("w", newline="", encoding="utf-8")
                writer = csv.writer(current_file)
                writer.writerow(header)
                output_paths.append(current_path)

        current_file.close()

    return output_paths


def make_url(base_url: str, path: Path) -> str:
    return f"{base_url.rstrip('/')}/all_beauty/{path.name}"


def generate_cypher(chunks: dict[str, list[Path]], base_url: str) -> str:
    lines = [
        "// Neo4j Aura import script for GitHub/raw HTTPS CSV chunks.",
        "// Replace YOUR_GITHUB_RAW_BASE_URL with your real raw GitHub base URL.",
        "// Example: https://raw.githubusercontent.com/USER/REPO/main",
        "",
        CONSTRAINTS,
        "",
    ]

    for key in NODE_IMPORTS:
        for path in chunks.get(key, []):
            lines.append(NODE_IMPORTS[key].format(url=make_url(base_url, path)))
            lines.append("")

    for key in REL_IMPORTS:
        for path in chunks.get(key, []):
            lines.append(REL_IMPORTS[key].format(url=make_url(base_url, path)))
            lines.append("")

    lines.append("// Quick checks")
    lines.append(CHECKS)
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split KG CSV files into GitHub-web-upload friendly chunks and generate Aura Cypher.")
    parser.add_argument("--input-dir", type=Path, default=Path("kg_output/all_beauty"))
    parser.add_argument("--output-dir", type=Path, default=Path("kg_output/all_beauty_github/all_beauty"))
    parser.add_argument("--max-mb", type=int, default=20)
    parser.add_argument("--base-url", default="https://raw.githubusercontent.com/USER/REPO/main")
    parser.add_argument("--cypher-out", type=Path, default=Path("neo4j/all_beauty_import_github_chunks.cypher"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = args.max_mb * 1024 * 1024

    chunks: dict[str, list[Path]] = {}
    for csv_path in sorted(args.input_dir.glob("*.csv")):
        key = csv_path.stem
        chunk_paths = split_csv(csv_path, args.output_dir, max_bytes)
        chunks[key] = chunk_paths
        print(f"{csv_path.name}: {len(chunk_paths)} file(s)")

    args.cypher_out.write_text(generate_cypher(chunks, args.base_url), encoding="utf-8")
    print(f"Chunked CSV files written to: {args.output_dir}")
    print(f"Cypher script written to: {args.cypher_out}")


if __name__ == "__main__":
    main()
