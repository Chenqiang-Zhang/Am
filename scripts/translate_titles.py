"""
Translate Product.title into Japanese and cache it as Product.title_ja.

Lightweight enrichment: does NOT rebuild the graph. Reads existing Product
nodes over Bolt, batches untranslated titles to the LLM, and SETs
Product.title_ja. Safe to re-run — only products with title_ja IS NULL are
picked up, so scaling up the catalog later only translates the newly added
products.

Usage:
    python3 scripts/translate_titles.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from llm_client import build_client, load_env_file, provider_from_config


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


SYSTEM_PROMPT = """\
Translate the following e-commerce product titles into natural, concise Japanese.
Preserve brand names, model numbers, sizes, and quantities as-is (do not translate
proper nouns or units). Return valid JSON only:
{"translations": [{"product_id": "...", "title_ja": "..."}]}
"""


def translate_batch(client: Any, model: str, items: list[dict[str, str]]) -> dict[str, str]:
    user_content = json.dumps({"titles": items}, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=2000,
    )
    data = parse_json_text(resp.choices[0].message.content or "{}")
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate Product.title to Japanese (Product.title_ja).")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int, default=-1, help="-1 = translate all untranslated products")
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    config_dir = args.config.resolve().parent
    load_env_file(config_dir / ".env")
    load_env_file()

    llm_cfg = cfg.get("llm", {})
    neo4j_cfg = cfg.get("neo4j", {})

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    client, model = build_client(provider, model_arg, cfg_base_url)

    uri = neo4j_cfg.get("uri") or os.environ.get("NEO4J_URI", "")
    user = os.environ.get("NEO4J_USERNAME") or neo4j_cfg.get("username", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD") or neo4j_cfg.get("password", "")
    database = os.environ.get("NEO4J_DATABASE") or neo4j_cfg.get("database")

    if not uri or not password:
        print("Set neo4j.uri in config.yaml and NEO4J_PASSWORD in .env.", file=sys.stderr)
        sys.exit(2)

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))

    total = 0
    try:
        driver.verify_connectivity()
        fetch_limit = None if args.limit < 0 else args.limit
        print("Fetching untranslated products...")
        rows = fetch_untranslated(driver, database, fetch_limit)
        print(f"  {len(rows):,} products need translation")

        for batch in chunked(rows, args.batch_size):
            items = [{"product_id": r["product_id"], "title": r["title"]} for r in batch]
            translated: dict[str, str] = {}
            for attempt in range(args.retries + 1):
                try:
                    translated = translate_batch(client, model, items)
                    break
                except Exception as exc:
                    print(f"  batch failed (attempt {attempt + 1}/{args.retries + 1}): {exc}", file=sys.stderr)
            write_rows = [{"product_id": pid, "title_ja": t} for pid, t in translated.items()]
            if write_rows:
                write_translations(driver, database, write_rows)
                total += len(write_rows)
            print(f"  translated: {total:,}/{len(rows):,}")
    finally:
        driver.close()
    print(f"\nDone. {total:,} products translated to title_ja.")


if __name__ == "__main__":
    main()
