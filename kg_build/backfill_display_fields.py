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
  --values-ja  Translate Attribute.value -> Attribute.value_ja via LLM (attr_type
               is sent along as context). Only untranslated attributes
               (value_ja IS NULL) are picked up. Text2Cypher's few-shot
               examples always collect value_ja alongside value in matched_attrs,
               so once this has run, recommendation responses show the
               translated value when lang="ja" and a translation exists.
  --descriptions-ja
               Translate Product.description (truncated to the first N chars,
               matching what GET /products/{id}/description shows) to
               Product.description_ja. Only untranslated products
               (description_ja IS NULL) are picked up.

Usage:
    python3 kg_build/backfill_display_fields.py --images
    python3 kg_build/backfill_display_fields.py --titles-ja
    python3 kg_build/backfill_display_fields.py --reviews-ja --reviews-limit 2000
    python3 kg_build/backfill_display_fields.py --values-ja
    python3 kg_build/backfill_display_fields.py --descriptions-ja
    python3 kg_build/backfill_display_fields.py --images --titles-ja --reviews-ja --values-ja --descriptions-ja
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import yaml

from utils.csv_io import read_jsonl_gz
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call
from utils.neo4j_io import connect, load_env_file, resolve_neo4j_conn


# ── 共有ヘルパー ────────────────────────────────────────────────────────────────

_SENTENCE_ENDS = ".!?。！？"


def cut_at_sentence(text: str, max_chars: int) -> str:
    """max_charsを超えるテキストを直近の文末（. ! ? 。等）で切り詰める。

    翻訳・表示対象のテキストが文の途中でぶつ切りになるのを防ぐ。max_chars以内なら
    そのまま返す。前半に文末が全く見つからない場合のみ語境界+「…」に落とす。"""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last = max(cut.rfind(ch) for ch in _SENTENCE_ENDS)
    if last >= max_chars // 2:
        return cut[: last + 1].rstrip()
    trimmed = cut.rsplit(" ", 1)[0].rstrip()
    return (trimmed or cut.rstrip()) + "…"


def translation_token_budget(
    total_chars: int, cap: int = 8000, context_limit: int = 15000,
) -> int:
    """英文total_chars分の日本語訳を返すのに必要な出力トークン数の見積もり。

    日本語訳はおおむね原文の6〜8割の文字数・1文字≈1トークンになることが多いが、
    JSONラッパー分と安全マージンを載せて原文文字数×1.0+600とする。

    さらにprompt側の消費(システムプロンプト+JSON入力、英文≈3.5字/トークン)を
    差し引き、prompt+max_tokensがモデルのコンテキスト長(16Kサーバー想定、余裕を
    みて15K)を超えないようにクランプする。超えるとvLLM/LM Studioは400エラーを
    返すため、バッチサイズを大きくし過ぎた場合でも呼び出し自体は失敗しない。
    ただしクランプされた分は訳が途中で切れる恐れがあるので、1バッチの原文合計は
    8,000字程度(バッチサイズ2×4,000字)までに抑えるのが前提。"""
    prompt_tokens = 500 + total_chars // 3
    budget = min(cap, 600 + int(total_chars * 1.0))
    return max(800, min(budget, context_limit - prompt_tokens))


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
Translate the following e-commerce product titles into natural, concise Japanese,
the way Japanese online game/electronics stores actually display them.

- Game and media titles (franchise/movie/anime names) must be transliterated into
  natural Japanese katakana, using the official Japanese title if you know it
  (e.g. "Super Mario Odyssey" -> "スーパーマリオ オデッセイ", "The Legend of Zelda:
  Breath of the Wild" -> "ゼルダの伝説 ブレス オブ ザ ワイルド"). Do not leave a
  well-known title untranslated just because it is a proper noun — that defeats the
  purpose of this translation.
- Platform/hardware brand names (Nintendo Switch, PlayStation, Xbox, PC, etc.),
  company names, and alphanumeric model/SKU codes are commonly kept in Roman
  letters as-is on Japanese storefronts — do not transliterate these.
- Preserve sizes, quantities, and units as-is.

Return valid JSON only:
{"translations": [{"product_id": "...", "title_ja": "..."}]}
"""


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def run_batches_parallel(
    rows: list,
    batch_size: int,
    process_batch: Callable[[list], list[dict]],
    write_rows: Callable[[list[dict]], None],
    label: str,
    workers: int,
) -> int:
    """バッチを ThreadPoolExecutor で並列に LLM 投入する共通ループ。逐次だと
    vLLM の継続バッチングの恩恵が出ず単発 10 秒/件レベルになるため、翻訳系の
    3 モード（titles/reviews/values）で共有する。process_batch は 1 バッチを
    翻訳して書き込み用 dict のリストを返す純粋関数、write_rows は Neo4j 書き込み。
    Neo4j セッションはスレッド安全でないので、書き込みだけはロックで直列化する。"""
    batches = list(chunked(rows, batch_size))
    total = 0
    write_lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_batch, b): b for b in batches}
        for fut in as_completed(futures):
            done += 1
            try:
                out_rows = fut.result()
            except Exception as exc:
                print(f"  [{label}] batch failed, skipping: {exc}", file=sys.stderr)
                continue
            if out_rows:
                with write_lock:
                    write_rows(out_rows)
                total += len(out_rows)
            if done % 10 == 0 or done == len(batches):
                print(f"  [{label}] {done:,}/{len(batches):,} batches, {total:,} rows written")
    return total


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
    batch_size: int, limit: int | None, retries: int, workers: int = 16,
) -> None:
    print("Fetching untranslated products...")
    rows = fetch_untranslated(driver, database, limit)
    print(f"  {len(rows):,} products need translation")

    def process(batch: list) -> list[dict]:
        items = [{"product_id": r["product_id"], "title": r["title"]} for r in batch]
        translated = translate_batch(client, model, items, retries)
        return [{"product_id": pid, "title_ja": t} for pid, t in translated.items()]

    total = run_batches_parallel(
        rows, batch_size, process,
        lambda wr: write_translations(driver, database, wr),
        "titles-ja", workers,
    )
    print(f"Done. {total:,} products translated to title_ja.")


# ── --reviews-ja ───────────────────────────────────────────────────────────────

# UIに出るレビュー本文の翻訳上限。表示対象(各商品の上位5件)の84%はこの範囲に全文が
# 収まる。旧上限500字は表示対象の72%を文の途中で切っており、全文が読めなかった。
REVIEW_MAX_CHARS = 4000

TRANSLATE_REVIEWS_SYSTEM_PROMPT = """\
Translate the following e-commerce product review titles and bodies into natural,
concise Japanese. Preserve brand names, model numbers, sizes, and quantities as-is
(do not translate proper nouns or units). If a title is empty, leave title_ja empty.
Return valid JSON only:
{"translations": [{"review_id": "...", "title_ja": "...", "text_ja": "..."}]}
"""


def _translations_schema(item_props: dict[str, dict], n_items: int) -> dict:
    """vLLMのguided decoding用スキーマ。Qwen3.5はjson_objectモードだとバッチ内の
    項目を黙って省略することがある（2件渡して1件しか返さない）ため、minItems/
    maxItemsで件数まで強制する。fast_normalize_values.pyと同じ対策。"""
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "minItems": n_items,
                "maxItems": n_items,
                "items": {
                    "type": "object",
                    "properties": item_props,
                    "required": list(item_props),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def translate_reviews_batch(
    client: Any, model: str, items: list[dict[str, str]], retries: int,
) -> dict[str, dict[str, str]]:
    user_content = json.dumps({"reviews": items}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": TRANSLATE_REVIEWS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    # 出力トークンはバッチ内の原文量に応じて動的に確保する（固定1800だと
    # 長文レビューの訳が途中で切れ、UIに全文が出ない問題があった）。
    total_chars = sum(len(i.get("text", "")) + len(i.get("title", "")) for i in items)
    schema = _translations_schema(
        {
            "review_id": {"type": "string"},
            "title_ja": {"type": "string"},
            "text_ja": {"type": "string"},
        },
        len(items),
    )
    data, _usage = chat_json_call(
        client, model, messages,
        max_output_tokens=translation_token_budget(total_chars), retries=retries,
        response_schema=schema, schema_name="review_translations",
    )
    return {
        str(t.get("review_id", "")): {
            "title_ja": str(t.get("title_ja", "")).strip(),
            "text_ja": str(t.get("text_ja", "")).strip(),
        }
        for t in data.get("translations", [])
        if t.get("review_id") and t.get("text_ja")
    }


def fetch_untranslated_reviews(
    driver: Any, database: str | None, limit: int | None, per_product: int | None = 5,
    shard_index: int = 0, shard_count: int = 1,
) -> list[dict[str, str]]:
    """未翻訳（text_ja IS NULL）のレビューを翻訳対象として返す。

    per_product が指定されているときは「各商品ごとに helpful_vote 降順で上位
    per_product 件」だけを対象にする。これは get_reviews()（UIが商品詳細で表示する
    レビュー: (Review)-[:ABOUT]->(Product) を helpful_vote DESC, rating DESC で上位5件）と
    同じ絞り込みで、UIに実際に出るレビューを過不足なくカバーするため。
    グローバルな helpful_vote 上位 N 件（旧実装）だと、人気商品にレビューが偏り、
    helpful_vote が低い商品の「表示される上位5件」が翻訳から漏れていた。

    per_product が None のときは従来どおり全未翻訳レビューを helpful_vote 降順で返す
    （limit と併用可）。"""
    if per_product is not None:
        query = (
            "MATCH (p:Product)<-[:ABOUT]-(r:Review) "
            "WHERE id(p) % $shard_count = $shard_index "
            "AND r.text IS NOT NULL "
            "AND size(coalesce(r.text, '')) > 10 "
            "WITH p, r ORDER BY r.helpful_vote DESC, r.rating DESC, r.review_id ASC "
            "WITH p, collect(r)[0..$per_product] AS top "
            "UNWIND top AS r "
            "WITH DISTINCT r "
            "WHERE r.text_ja IS NULL "
            "RETURN r.review_id AS review_id, r.title AS title, r.text AS text"
        )
        if limit is not None:
            query += " LIMIT $limit"
        with driver.session(database=database) as session:
            params = {
                "per_product": per_product,
                "shard_index": shard_index,
                "shard_count": shard_count,
            }
            if limit is not None:
                params["limit"] = limit
            res = session.run(query, **params)
            return [{"review_id": r["review_id"], "title": r["title"] or "", "text": r["text"]} for r in res]

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
    batch_size: int, limit: int | None, retries: int, workers: int = 16,
    per_product: int | None = 5,
    shard_index: int = 0, shard_count: int = 1,
) -> None:
    if per_product is not None:
        print(f"Fetching untranslated reviews (per-product top {per_product}, matches UI get_reviews)...")
    else:
        print("Fetching untranslated reviews (global helpful_vote DESC)...")
    rows = fetch_untranslated_reviews(
        driver, database, limit, per_product, shard_index, shard_count,
    )
    print(f"  {len(rows):,} reviews need translation")

    def process(batch: list) -> list[dict]:
        items = [
            {
                "review_id": r["review_id"],
                "title": r["title"],
                # プロンプト暴走防止の上限は残しつつ、UIに出るレビューはほぼ全文
                # (表示対象の84%がREVIEW_MAX_CHARS以内)を翻訳する。上限超過分も
                # 文末で切るため、訳が文の途中でぶつ切りになることはない。
                "text": cut_at_sentence(r["text"], REVIEW_MAX_CHARS),
            }
            for r in batch
        ]
        translated = translate_reviews_batch(client, model, items, retries)
        return [
            {"review_id": rid, "title_ja": t["title_ja"], "text_ja": t["text_ja"]}
            for rid, t in translated.items()
        ]

    total = run_batches_parallel(
        rows, batch_size, process,
        lambda wr: write_review_translations(driver, database, wr),
        "reviews-ja", workers,
    )
    print(f"Done. {total:,} reviews translated to title_ja/text_ja.")


# ── --values-ja ────────────────────────────────────────────────────────────────

TRANSLATE_VALUES_SYSTEM_PROMPT = """\
Translate the following product attribute values into short, natural Japanese.
Each value comes with its attr_type for context (e.g. attr_type="color", value="black"
-> value_ja="黒"). Keep translations short — a word or short phrase, not a sentence.
Preserve brand/model names as-is. Return valid JSON only:
{"translations": [{"attribute_id": "...", "value_ja": "..."}]}
"""


def translate_values_batch(
    client: Any, model: str, items: list[dict[str, str]], retries: int,
) -> dict[str, str]:
    user_content = json.dumps({"attributes": items}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": TRANSLATE_VALUES_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    data, _usage = chat_json_call(client, model, messages, max_output_tokens=2000, retries=retries)
    return {
        str(t.get("attribute_id", "")): str(t.get("value_ja", "")).strip()
        for t in data.get("translations", [])
        if t.get("attribute_id") and t.get("value_ja")
    }


def fetch_untranslated_values(driver: Any, database: str | None, limit: int | None) -> list[dict[str, str]]:
    """未翻訳の属性値を、グラフ内での被参照数（HAS_ATTRIBUTE + MENTIONS の合計）が
    多い順に返す。--values-limit で予算を絞ったとき、実際にUIで表示されやすい
    頻出属性から優先的に翻訳されるようにする（属性値は13万件超あり全件翻訳は非現実的）。"""
    query = (
        "MATCH (a:Attribute) "
        "WHERE a.value_ja IS NULL AND a.value IS NOT NULL AND a.value <> '' "
        "OPTIONAL MATCH (a)<-[rel]-() "
        "WITH a, count(rel) AS deg "
        "RETURN a.attribute_id AS attribute_id, a.attr_type AS attr_type, a.value AS value "
        "ORDER BY deg DESC"
    )
    if limit is not None:
        query += " LIMIT $limit"
    with driver.session(database=database) as session:
        res = session.run(query, limit=limit) if limit is not None else session.run(query)
        return [{"attribute_id": r["attribute_id"], "attr_type": r["attr_type"], "value": r["value"]} for r in res]


def write_value_translations(driver: Any, database: str | None, rows: list[dict[str, str]]) -> None:
    query = (
        "UNWIND $rows AS row "
        "MATCH (a:Attribute {attribute_id: row.attribute_id}) "
        "SET a.value_ja = row.value_ja"
    )
    with driver.session(database=database) as session:
        session.execute_write(lambda tx, rs: tx.run(query, rows=rs).consume(), rows)


def run_values_ja(
    driver: Any, database: str | None, client: Any, model: str,
    batch_size: int, limit: int | None, retries: int, workers: int = 16,
) -> None:
    print("Fetching untranslated attribute values...")
    rows = fetch_untranslated_values(driver, database, limit)
    print(f"  {len(rows):,} attribute values need translation")

    def process(batch: list) -> list[dict]:
        items = [{"attribute_id": r["attribute_id"], "attr_type": r["attr_type"], "value": r["value"]} for r in batch]
        translated = translate_values_batch(client, model, items, retries)
        return [{"attribute_id": aid, "value_ja": v} for aid, v in translated.items()]

    total = run_batches_parallel(
        rows, batch_size, process,
        lambda wr: write_value_translations(driver, database, wr),
        "values-ja", workers,
    )
    print(f"Done. {total:,} attribute values translated to value_ja.")


# ── --descriptions-ja ────────────────────────────────────────────────────────────

# GET /products/{id}/description が表示する文字数と揃える
# (app/api/recommender.py の Recommender._DESCRIPTION_MAX_CHARS と同じ値)。
# 説明文の80%はこの範囲に全文が収まる。超過分（末尾は定型文・法務文が多い）は
# cut_at_sentence()で文末まで含めて切る。旧上限800字は中央値1640字の半分しか
# カバーできず、大半の説明文が文の途中で切れていた。
DESCRIPTION_MAX_CHARS = 4000

TRANSLATE_DESCRIPTIONS_SYSTEM_PROMPT = """\
Translate the following e-commerce product descriptions into natural, concise
Japanese, the way Japanese online game/electronics stores actually display them.

- Game and media titles (franchise/movie/anime names) mentioned inside the
  description must be transliterated into natural Japanese katakana, using the
  official Japanese title if you know it (e.g. "Super Mario Odyssey" ->
  "スーパーマリオ オデッセイ"). Do not leave a well-known title untranslated just
  because it is a proper noun.
- Platform/hardware brand names (Nintendo Switch, PlayStation, Xbox, PC, etc.),
  company names, and alphanumeric model/SKU codes are commonly kept in Roman
  letters as-is on Japanese storefronts — do not transliterate these.
- Translate the FULL text you are given. Do not summarize, shorten, or drop
  sentences — the whole description is shown to the user.

Return valid JSON only:
{"translations": [{"product_id": "...", "description_ja": "..."}]}
"""


def translate_descriptions_batch(
    client: Any, model: str, items: list[dict[str, str]], retries: int,
) -> dict[str, str]:
    user_content = json.dumps({"descriptions": items}, ensure_ascii=False)
    messages = [
        {"role": "system", "content": TRANSLATE_DESCRIPTIONS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    # 出力トークンはバッチ内の原文量に応じて動的に確保（固定値だと長文の訳が切れる）
    total_chars = sum(len(i.get("description", "")) for i in items)
    schema = _translations_schema(
        {
            "product_id": {"type": "string"},
            "description_ja": {"type": "string"},
        },
        len(items),
    )
    data, _usage = chat_json_call(
        client, model, messages,
        max_output_tokens=translation_token_budget(total_chars), retries=retries,
        response_schema=schema, schema_name="description_translations",
    )
    return {
        str(t.get("product_id", "")): str(t.get("description_ja", "")).strip()
        for t in data.get("translations", [])
        if t.get("product_id") and t.get("description_ja")
    }


def fetch_untranslated_descriptions(driver: Any, database: str | None, limit: int | None) -> list[dict[str, str]]:
    # substringはDESCRIPTION_MAX_CHARSより余分に取り、Python側のcut_at_sentence()で
    # 文末まで含めて切り詰める（Cypherの固定長カットだと文の途中で切れるため）。
    query = (
        "MATCH (p:Product) "
        "WHERE p.description_ja IS NULL AND p.description IS NOT NULL AND p.description <> '' "
        "RETURN p.product_id AS product_id, substring(p.description, 0, $max_chars) AS description"
    )
    if limit is not None:
        query += " LIMIT $limit"
    with driver.session(database=database) as session:
        params = {"max_chars": DESCRIPTION_MAX_CHARS + 800}
        if limit is not None:
            params["limit"] = limit
        res = session.run(query, **params)
        return [
            {
                "product_id": r["product_id"],
                "description": cut_at_sentence(r["description"], DESCRIPTION_MAX_CHARS),
            }
            for r in res
        ]


def write_description_translations(driver: Any, database: str | None, rows: list[dict[str, str]]) -> None:
    query = (
        "UNWIND $rows AS row "
        "MATCH (p:Product {product_id: row.product_id}) "
        "SET p.description_ja = row.description_ja"
    )
    with driver.session(database=database) as session:
        session.execute_write(lambda tx, rs: tx.run(query, rows=rs).consume(), rows)


def run_descriptions_ja(
    driver: Any, database: str | None, client: Any, model: str,
    batch_size: int, limit: int | None, retries: int, workers: int = 16,
) -> None:
    print("Fetching untranslated product descriptions...")
    rows = fetch_untranslated_descriptions(driver, database, limit)
    print(f"  {len(rows):,} product descriptions need translation")

    def process(batch: list) -> list[dict]:
        items = [{"product_id": r["product_id"], "description": r["description"]} for r in batch]
        translated = translate_descriptions_batch(client, model, items, retries)
        return [{"product_id": pid, "description_ja": d} for pid, d in translated.items()]

    total = run_batches_parallel(
        rows, batch_size, process,
        lambda wr: write_description_translations(driver, database, wr),
        "descriptions-ja", workers,
    )
    print(f"Done. {total:,} product descriptions translated to description_ja.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-import Product/Review/Attribute enrichment (image_url / title_ja / review title_ja+text_ja / attribute value_ja).")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--images", action="store_true", help="Set Product.image_url from metadata.")
    parser.add_argument("--titles-ja", action="store_true", help="Translate Product.title to Product.title_ja.")
    parser.add_argument("--reviews-ja", action="store_true", help="Translate Review.title/text to Review.title_ja/text_ja.")
    parser.add_argument("--values-ja", action="store_true", help="Translate Attribute.value to Attribute.value_ja.")
    parser.add_argument("--descriptions-ja", action="store_true", help="Translate Product.description (truncated) to Product.description_ja.")
    parser.add_argument("--meta-path", type=Path, help="[--images] Path to meta_*.jsonl.gz (default: config.yaml data.meta_path)")
    parser.add_argument("--images-batch-size", type=int, default=5000)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None, help="[--titles-ja/--reviews-ja/--values-ja]")
    parser.add_argument("--model", default=None, help="[--titles-ja/--reviews-ja/--values-ja]")
    parser.add_argument("--base-url", default=None, help="OpenAI互換サーバーのbase_url。config.yamlのllm.base_urlを上書きする（例: kuberaのvLLMを使う場合）")
    parser.add_argument("--titles-batch-size", type=int, default=20)
    parser.add_argument("--titles-limit", type=int, default=-1, help="[--titles-ja] -1 = translate all untranslated products")
    parser.add_argument("--reviews-batch-size", type=int, default=10, help="[--reviews-ja] smaller default than titles since review text is longer")
    parser.add_argument("--reviews-limit", type=int, default=-1, help="[--reviews-ja] -1 = no global cap; set a budget cap otherwise")
    parser.add_argument("--reviews-per-product", type=int, default=5, help="[--reviews-ja] translate each product's top-N reviews by helpful_vote (matches UI get_reviews). 0 = global helpful_vote order instead")
    parser.add_argument("--reviews-shard-index", type=int, default=0, help="[--reviews-ja] zero-based Product shard index")
    parser.add_argument("--reviews-shard-count", type=int, default=1, help="[--reviews-ja] number of disjoint Product shards")
    parser.add_argument("--values-batch-size", type=int, default=30, help="[--values-ja] larger default since attribute values are short")
    parser.add_argument("--values-limit", type=int, default=-1, help="[--values-ja] -1 = translate all untranslated attribute values")
    parser.add_argument("--descriptions-batch-size", type=int, default=8, help="[--descriptions-ja] smaller default since descriptions are long")
    parser.add_argument("--descriptions-limit", type=int, default=-1, help="[--descriptions-ja] -1 = translate all untranslated descriptions")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--workers", type=int, default=16, help="[--titles-ja/--reviews-ja/--values-ja] parallel LLM request workers")
    parser.add_argument("--uri", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--database", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.reviews_shard_count < 1 or not 0 <= args.reviews_shard_index < args.reviews_shard_count:
        parser_error = "--reviews-shard-index must be in [0, --reviews-shard-count)"
        raise SystemExit(parser_error)
    if not args.images and not args.titles_ja and not args.reviews_ja and not args.values_ja and not args.descriptions_ja:
        print("Nothing to do: pass --images and/or --titles-ja and/or --reviews-ja and/or --values-ja and/or --descriptions-ja.", file=sys.stderr)
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
        if args.titles_ja or args.reviews_ja or args.values_ja or args.descriptions_ja:
            llm_cfg = cfg.get("llm", {})
            cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
            provider = args.provider or cfg_provider
            model_arg = args.model or cfg_model
            # --base-url指定時はconfig.yamlのbase_urlより優先する（config.yamlが
            # ローカルLM Studioを向いたまま、別サーバー(例: kuberaのvLLM)で
            # 翻訳バッチだけ回したいケース用）
            client, model = build_client(provider, model_arg, args.base_url or cfg_base_url)

        if args.titles_ja:
            titles_limit = None if args.titles_limit < 0 else args.titles_limit
            print("\n[titles-ja]")
            run_titles_ja(driver, database, client, model, args.titles_batch_size, titles_limit, args.retries, args.workers)

        if args.reviews_ja:
            reviews_limit = None if args.reviews_limit < 0 else args.reviews_limit
            per_product = None if args.reviews_per_product <= 0 else args.reviews_per_product
            print("\n[reviews-ja]")
            run_reviews_ja(
                driver, database, client, model,
                args.reviews_batch_size, reviews_limit, args.retries, args.workers,
                per_product, args.reviews_shard_index, args.reviews_shard_count,
            )

        if args.values_ja:
            values_limit = None if args.values_limit < 0 else args.values_limit
            print("\n[values-ja]")
            run_values_ja(driver, database, client, model, args.values_batch_size, values_limit, args.retries, args.workers)

        if args.descriptions_ja:
            descriptions_limit = None if args.descriptions_limit < 0 else args.descriptions_limit
            print("\n[descriptions-ja]")
            run_descriptions_ja(driver, database, client, model, args.descriptions_batch_size, descriptions_limit, args.retries, args.workers)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
