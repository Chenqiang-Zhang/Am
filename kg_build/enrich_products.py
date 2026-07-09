"""
Post-import Product enrichment: adds optional properties to existing Product
nodes without rebuilding the graph. Two independent modes — run either or
both in a single call (a shared Neo4j connection is reused for both):

  --images     Set Product.image_url from the metadata JSONL (no LLM).
  --titles-ja  Translate Product.title -> Product.title_ja via LLM.
               Only untranslated products (title_ja IS NULL) are picked up,
               so it's safe to re-run after scaling up the catalog.

Usage:
    python3 kg_build/enrich_products.py --images
    python3 kg_build/enrich_products.py --titles-ja
    python3 kg_build/enrich_products.py --images --titles-ja
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from utils.csv_io import read_jsonl_gz
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call
from utils.neo4j_io import connect, load_env_file, resolve_neo4j_conn


# ── --images ─────────────────────────────────────────────────────────────────

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


def run_images(driver: Any, database: str | None, meta_path: Path, batch_size: int) -> None:
    query = (
        "UNWIND $rows AS row "
        "MATCH (p:Product {product_id: row.product_id}) "
        "SET p.image_url = row.image_url"
    )
    total = 0
    batch: list[dict] = []
    with driver.session(database=database) as session:
        for rec in read_jsonl_gz(meta_path):
            pid = rec.get("parent_asin")
            url = pick_image(rec.get("images") or [])
            if not pid or not url:
                continue
            batch.append({"product_id": pid, "image_url": url})
            if len(batch) >= batch_size:
                session.execute_write(lambda tx, rows: tx.run(query, rows=rows).consume(), batch)
                total += len(batch)
                print(f"  image_url set: {total:,}")
                batch = []
        if batch:
            session.execute_write(lambda tx, rows: tx.run(query, rows=rows).consume(), batch)
            total += len(batch)
    print(f"Done. {total:,} products enriched with image_url.")


# ── --titles-ja ────────────────────────────────────────────────────────────────

TRANSLATE_SYSTEM_PROMPT = """\
Translate the following e-commerce product titles into natural, concise Japanese.
Preserve brand names, model numbers, sizes, and quantities as-is (do not translate
proper nouns or units). Return valid JSON only:
{"translations": [{"product_id": "...", "title_ja": "..."}]}
"""


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def translate_batch(client: Any, model: str, items: list[dict[str, str]], retries: int) -> dict[str, str]:
    user_content = json.dumps({"titles": items}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    data, _usage = chat_json_call(client, model, messages, max_output_tokens=2000, retries=retries)
    return {
        str(t.get("product_id", "")): str(t.get("title_ja", "")).strip()
        for t in data.get("translations", [])
        if t.get("product_id") and t.get("title_ja")
    }


def fetch_untranslated(driver: Any, database: str | None, limit: int | None) -> list[dict[str, str]]:
    query = (
        "MATCH (p:Product) "
        "WHERE p.title_ja IS NULL AND p.title IS NOT NULL AND p.title <> '' "
        "RETURN p.product_id AS product_id, p.title AS title"
    )
    if limit is not None:
        query += " LIMIT $limit"
    with driver.session(database=database) as session:
        res = session.run(query, limit=limit) if limit is not None else session.run(query)
        return [{"product_id": r["product_id"], "title": r["title"]} for r in res]


def write_translations(driver: Any, database: str | None, rows: list[dict[str, str]]) -> None:
    query = (
        "UNWIND $rows AS row "
        "MATCH (p:Product {product_id: row.product_id}) "
        "SET p.title_ja = row.title_ja"
    )
    with driver.session(database=database) as session:
        session.execute_write(lambda tx, rs: tx.run(query, rows=rs).consume(), rows)


def run_titles_ja(
    driver: Any, database: str | None, client: Any, model: str,
    batch_size: int, limit: int | None, retries: int,
) -> None:
    print("Fetching untranslated products...")
    rows = fetch_untranslated(driver, database, limit)
    print(f"  {len(rows):,} products need translation")

    total = 0
    for batch in chunked(rows, batch_size):
        items = [{"product_id": r["product_id"], "title": r["title"]} for r in batch]
        try:
            translated = translate_batch(client, model, items, retries)
        except Exception as exc:
            print(f"  batch failed, skipping: {exc}", file=sys.stderr)
            continue
        write_rows = [{"product_id": pid, "title_ja": t} for pid, t in translated.items()]
        if write_rows:
            write_translations(driver, database, write_rows)
            total += len(write_rows)
        print(f"  translated: {total:,}/{len(rows):,}")
    print(f"Done. {total:,} products translated to title_ja.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-import Product enrichment (image_url / title_ja).")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--images", action="store_true", help="Set Product.image_url from metadata.")
    parser.add_argument("--titles-ja", action="store_true", help="Translate Product.title to Product.title_ja.")
    parser.add_argument("--meta-path", type=Path, help="[--images] Path to meta_*.jsonl.gz (default: config.yaml data.meta_path)")
    parser.add_argument("--images-batch-size", type=int, default=5000)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None, help="[--titles-ja]")
    parser.add_argument("--model", default=None, help="[--titles-ja]")
    parser.add_argument("--titles-batch-size", type=int, default=20)
    parser.add_argument("--titles-limit", type=int, default=-1, help="[--titles-ja] -1 = translate all untranslated products")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--uri", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--database", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.images and not args.titles_ja:
        print("Nothing to do: pass --images and/or --titles-ja.", file=sys.stderr)
        sys.exit(2)

    config_dir = args.config.resolve().parent
    load_env_file(config_dir / ".env")
    load_env_file()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    data_cfg = cfg.get("data", {})

    conn = resolve_neo4j_conn(args, cfg)
    driver = connect(conn["uri"], conn["user"], conn["password"])
    database = conn["database"]

    try:
        driver.verify_connectivity()
        print(f"Connected to {conn['uri']}")

        if args.images:
            meta_path = args.meta_path or (config_dir / data_cfg.get("meta_path", "data/meta_Video_Games.jsonl.gz"))
            print(f"\n[images] Reading {meta_path}...")
            run_images(driver, database, meta_path, args.images_batch_size)

        if args.titles_ja:
            llm_cfg = cfg.get("llm", {})
            cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
            provider = args.provider or cfg_provider
            model_arg = args.model or cfg_model
            client, model = build_client(provider, model_arg, cfg_base_url)
            titles_limit = None if args.titles_limit < 0 else args.titles_limit
            print("\n[titles-ja]")
            run_titles_ja(driver, database, client, model, args.titles_batch_size, titles_limit, args.retries)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
