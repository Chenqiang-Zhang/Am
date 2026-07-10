"""
Post-import Product/Review enrichment: adds optional properties to existing
nodes without rebuilding the graph. Independent modes — run any combination
in a single call (a shared Neo4j connection is reused for all):

  --images     Set Product.image_url from the metadata JSONL (no LLM).
  --titles-ja  Translate Product.title -> Product.title_ja via LLM.
               Only untranslated products (title_ja IS NULL) are picked up,
               so it's safe to re-run after scaling up the catalog.
  --reviews-ja Translate Review.title/text -> Review.title_ja/text_ja via LLM.
               Only untranslated reviews (text_ja IS NULL) are picked up.
               Prioritizes by helpful_vote DESC so the reviews most likely to
               actually be shown (get_reviews() picks top-N by helpful_vote)
               get translated first — pair with --reviews-limit on a budget.

Usage:
    python3 kg_build/backfill_display_fields.py --images
    python3 kg_build/backfill_display_fields.py --titles-ja
    python3 kg_build/backfill_display_fields.py --reviews-ja --reviews-limit 2000
    python3 kg_build/backfill_display_fields.py --images --titles-ja --reviews-ja
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


# ── --reviews-ja ───────────────────────────────────────────────────────────────

TRANSLATE_REVIEWS_SYSTEM_PROMPT = """\
Translate the following e-commerce product review titles and bodies into natural,
concise Japanese. Preserve brand names, model numbers, sizes, and quantities as-is
(do not translate proper nouns or units). If a title is empty, leave title_ja empty.
Return valid JSON only:
{"translations": [{"review_id": "...", "title_ja": "...", "text_ja": "..."}]}
"""


def translate_reviews_batch(
    client: Any, model: str, items: list[dict[str, str]], retries: int,
) -> dict[str, dict[str, str]]:
    user_content = json.dumps({"reviews": items}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": TRANSLATE_REVIEWS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    data, _usage = chat_json_call(client, model, messages, max_output_tokens=3000, retries=retries)
    return {
        str(t.get("review_id", "")): {
            "title_ja": str(t.get("title_ja", "")).strip(),
            "text_ja": str(t.get("text_ja", "")).strip(),
        }
        for t in data.get("translations", [])
        if t.get("review_id") and t.get("text_ja")
    }


def fetch_untranslated_reviews(driver: Any, database: str | None, limit: int | None) -> list[dict[str, str]]:
    """未翻訳（text_ja IS NULL）のレビューを、helpful_vote降順（＝get_reviews()で表示
    されやすい順）で返す。予算を絞る場合は--reviews-limitと組み合わせる。"""
    query = (
        "MATCH (r:Review) "
        "WHERE r.text_ja IS NULL AND r.text IS NOT NULL AND r.text <> '' "
        "RETURN r.review_id AS review_id, r.title AS title, r.text AS text "
        "ORDER BY r.helpful_vote DESC"
    )
    if limit is not None:
        query += " LIMIT $limit"
    with driver.session(database=database) as session:
        res = session.run(query, limit=limit) if limit is not None else session.run(query)
        return [{"review_id": r["review_id"], "title": r["title"] or "", "text": r["text"]} for r in res]


def write_review_translations(driver: Any, database: str | None, rows: list[dict[str, str]]) -> None:
    query = (
        "UNWIND $rows AS row "
        "MATCH (r:Review {review_id: row.review_id}) "
        "SET r.title_ja = row.title_ja, r.text_ja = row.text_ja"
    )
    with driver.session(database=database) as session:
        session.execute_write(lambda tx, rs: tx.run(query, rows=rs).consume(), rows)


def run_reviews_ja(
    driver: Any, database: str | None, client: Any, model: str,
    batch_size: int, limit: int | None, retries: int,
) -> None:
    print("Fetching untranslated reviews (helpful_vote DESC)...")
    rows = fetch_untranslated_reviews(driver, database, limit)
    print(f"  {len(rows):,} reviews need translation")

    total = 0
    for batch in chunked(rows, batch_size):
        items = [{"review_id": r["review_id"], "title": r["title"], "text": r["text"]} for r in batch]
        try:
            translated = translate_reviews_batch(client, model, items, retries)
        except Exception as exc:
            print(f"  batch failed, skipping: {exc}", file=sys.stderr)
            continue
        write_rows = [
            {"review_id": rid, "title_ja": t["title_ja"], "text_ja": t["text_ja"]}
            for rid, t in translated.items()
        ]
        if write_rows:
            write_review_translations(driver, database, write_rows)
            total += len(write_rows)
        print(f"  translated: {total:,}/{len(rows):,}")
    print(f"Done. {total:,} reviews translated to title_ja/text_ja.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-import Product/Review enrichment (image_url / title_ja / review title_ja+text_ja).")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--images", action="store_true", help="Set Product.image_url from metadata.")
    parser.add_argument("--titles-ja", action="store_true", help="Translate Product.title to Product.title_ja.")
    parser.add_argument("--reviews-ja", action="store_true", help="Translate Review.title/text to Review.title_ja/text_ja.")
    parser.add_argument("--meta-path", type=Path, help="[--images] Path to meta_*.jsonl.gz (default: config.yaml data.meta_path)")
    parser.add_argument("--images-batch-size", type=int, default=5000)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None, help="[--titles-ja/--reviews-ja]")
    parser.add_argument("--model", default=None, help="[--titles-ja/--reviews-ja]")
    parser.add_argument("--titles-batch-size", type=int, default=20)
    parser.add_argument("--titles-limit", type=int, default=-1, help="[--titles-ja] -1 = translate all untranslated products")
    parser.add_argument("--reviews-batch-size", type=int, default=10, help="[--reviews-ja] smaller default than titles since review text is longer")
    parser.add_argument("--reviews-limit", type=int, default=-1, help="[--reviews-ja] -1 = translate all untranslated reviews; set a budget cap otherwise")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--uri", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--database", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.images and not args.titles_ja and not args.reviews_ja:
        print("Nothing to do: pass --images and/or --titles-ja and/or --reviews-ja.", file=sys.stderr)
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

        client, model = None, None
        if args.titles_ja or args.reviews_ja:
            llm_cfg = cfg.get("llm", {})
            cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
            provider = args.provider or cfg_provider
            model_arg = args.model or cfg_model
            client, model = build_client(provider, model_arg, cfg_base_url)

        if args.titles_ja:
            titles_limit = None if args.titles_limit < 0 else args.titles_limit
            print("\n[titles-ja]")
            run_titles_ja(driver, database, client, model, args.titles_batch_size, titles_limit, args.retries)

        if args.reviews_ja:
            reviews_limit = None if args.reviews_limit < 0 else args.reviews_limit
            print("\n[reviews-ja]")
            run_reviews_ja(driver, database, client, model, args.reviews_batch_size, reviews_limit, args.retries)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
