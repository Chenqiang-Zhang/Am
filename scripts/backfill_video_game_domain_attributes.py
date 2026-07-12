#!/usr/bin/env python3
"""Backfill Video Games domain attributes into Neo4j.

This is a deterministic, low-cost enrichment layer for the current Video_Games
graph. It complements LLM-extracted open attributes with normalized fields that
matter for recommendation quality: platform, franchise, and product_type.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "kg_build"))

from utils.neo4j_io import connect, resolve_neo4j_conn  # noqa: E402
from utils.llm_client import load_env_file  # noqa: E402


PLATFORMS: dict[str, list[str]] = {
    "nintendo_switch": ["nintendo switch", "switch", "joy-con", "joy con"],
    "ps5": ["playstation 5", "ps5"],
    "ps4": ["playstation 4", "ps4"],
    "ps3": ["playstation 3", "ps3"],
    "playstation_vita": ["playstation vita", "ps vita", "vita"],
    "xbox_series_x": ["xbox series x", "xbox series s", "series x", "series s"],
    "xbox_one": ["xbox one"],
    "xbox_360": ["xbox 360"],
    "wii_u": ["wii u", "wiiu"],
    "wii": ["nintendo wii", " wii "],
    "nintendo_3ds": ["nintendo 3ds", " 3ds "],
    "nintendo_ds": ["nintendo ds", " ds "],
    "pc": [" pc ", "windows", "steam"],
}

FRANCHISES: dict[str, list[str]] = {
    "mario": ["mario", "super mario", "mario kart", "mario party"],
    "zelda": ["zelda", "legend of zelda"],
    "pokemon": ["pokemon", "pokémon"],
    "sonic": ["sonic"],
    "minecraft": ["minecraft"],
    "call_of_duty": ["call of duty", " cod "],
    "final_fantasy": ["final fantasy"],
    "spider_man": ["spider-man", "spider man", "spiderman"],
    "grand_theft_auto": ["grand theft auto", " gta "],
    "red_dead": ["red dead redemption", "red dead"],
    "assassins_creed": ["assassin's creed", "assassins creed"],
    "lego": ["lego"],
    "star_wars": ["star wars"],
    "madden": ["madden"],
    "nba_2k": ["nba 2k", " nba2k "],
    "fifa": ["fifa"],
    "mlb_the_show": ["mlb the show"],
    "animal_crossing": ["animal crossing"],
    "kirby": ["kirby"],
    "splatoon": ["splatoon"],
    "fire_emblem": ["fire emblem"],
    "metroid": ["metroid"],
    "resident_evil": ["resident evil"],
    "monster_hunter": ["monster hunter"],
    "dragon_quest": ["dragon quest"],
    "kingdom_hearts": ["kingdom hearts"],
    "battlefield": ["battlefield"],
    "halo": ["halo"],
    "forza": ["forza"],
}

PRODUCT_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("storage", ["micro sd", "microsd", "sd card", "memory card", "memory storage"]),
    ("controller", ["controller", "gamepad", "joy-con", "joy con", "dualshock", "dualsense", "remote"]),
    ("headset", ["headset", "headphone", "gaming headset"]),
    ("console", ["console", "playstation 4 slim", "xbox one s", "nintendo switch console"]),
    ("accessory", ["accessory", "case", "charger", "charging", "cable", "stand", "protector", "skin", "amiibo"]),
]

GAME_CATEGORY_TERMS = {
    "games",
    "downloadable content",
    "pc",
    "kids and family",
    "game genre of the month",
    "video games",
}


def attr_id(attr_type: str, value: str) -> str:
    digest = hashlib.sha1(f"{attr_type}|{value}".encode("utf-8")).hexdigest()[:16]
    return f"attr_{digest}"


def norm_text(*parts: Any) -> str:
    text = " ".join(str(p or "") for p in parts)
    text = re.sub(r"[_/,-]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text)
    return f" {text.strip()} "


def contains_any(text: str, needles: list[str]) -> bool:
    return any(n in text for n in needles)


def infer_product_type(text: str, categories: list[str]) -> set[str]:
    values: set[str] = set()
    for product_type, needles in PRODUCT_TYPE_RULES:
        if contains_any(text, needles):
            values.add(product_type)

    category_text = norm_text(*categories)
    if any(c.lower() in GAME_CATEGORY_TERMS for c in categories):
        values.add("video_game")
    elif " games " in category_text and not values:
        values.add("video_game")

    if "video_game" in values and values & {"controller", "headset", "storage", "console", "accessory"}:
        values.discard("video_game")
    return values


def infer_attrs(record: dict[str, Any]) -> list[dict[str, str]]:
    categories = [str(c or "") for c in record.get("categories") or [] if c]
    text = norm_text(record.get("title"), record.get("description"), record.get("brand"), *categories)
    type_text = norm_text(record.get("title"), *categories)
    franchise_text = norm_text(record.get("title"), *categories)
    attrs: list[dict[str, str]] = []

    for value, needles in PLATFORMS.items():
        if contains_any(text, needles):
            attrs.append({"attr_type": "domain_platform", "value": value})

    for value, needles in FRANCHISES.items():
        if contains_any(franchise_text, needles):
            attrs.append({"attr_type": "domain_franchise", "value": value})

    for value in infer_product_type(type_text, categories):
        attrs.append({"attr_type": "domain_product_type", "value": value})

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for attr in attrs:
        unique[(attr["attr_type"], attr["value"])] = attr
    return list(unique.values())


def load_products(driver: Any, database: str | None, limit: int | None) -> list[dict[str, Any]]:
    cypher = """
    MATCH (p:Product)
    OPTIONAL MATCH (p)-[:BELONGS_TO]->(c:Category)
    OPTIONAL MATCH (p)-[:MADE_BY]->(b:Brand)
    RETURN p.product_id AS product_id,
           p.title AS title,
           p.description AS description,
           b.name AS brand,
           collect(DISTINCT c.name) AS categories
    ORDER BY p.product_id
    """
    if limit:
        cypher += " LIMIT $limit"
    with driver.session(database=database) as session:
        return [dict(row) for row in session.run(cypher, limit=limit)]


def write_attrs(driver: Any, database: str | None, rows: list[dict[str, str]], dry_run: bool) -> None:
    if dry_run or not rows:
        return
    cypher = """
    UNWIND $rows AS row
    MATCH (p:Product {product_id: row.product_id})
    MERGE (a:Attribute {attribute_id: row.attribute_id})
    SET a.attr_type = row.attr_type,
        a.value = row.value
    MERGE (p)-[:HAS_ATTRIBUTE]->(a)
    """
    with driver.session(database=database) as session:
        for start in range(0, len(rows), 1000):
            session.run(cypher, rows=rows[start:start + 1000]).consume()


def clear_domain_attrs(driver: Any, database: str | None, dry_run: bool) -> dict[str, int]:
    count_cypher = """
    MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
    WHERE a.attr_type STARTS WITH 'domain_'
    RETURN count(r) AS rels, count(DISTINCT a) AS attrs
    """
    delete_cypher = """
    MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
    WHERE a.attr_type STARTS WITH 'domain_'
    DELETE r
    WITH DISTINCT a
    WHERE NOT (a)<-[:HAS_ATTRIBUTE]-(:Product) AND NOT (a)<-[:MENTIONS]-(:Review)
    DELETE a
    """
    with driver.session(database=database) as session:
        before = session.run(count_cypher).single()
        stats = {"domain_relationships_deleted": int(before["rels"]), "domain_attributes_deleted": int(before["attrs"])}
        if not dry_run:
            session.run(delete_cypher).consume()
    return stats


def write_report(out_dir: Path, summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "video_game_domain_attribute_backfill.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    lines = [
        "# Video Games Domain Attribute Backfill",
        "",
        f"- products_scanned: {summary['products_scanned']}",
        f"- product_attribute_edges_inferred: {summary['product_attribute_edges_inferred']}",
        f"- dry_run: {summary['dry_run']}",
        f"- replace: {summary['replace']}",
        "",
        "## Attribute Counts",
        "",
        "| attr_type | value | count |",
        "|---|---:|---:|",
    ]
    for row in summary["top_values"]:
        lines.append(f"| {row['attr_type']} | {row['value']} | {row['count']} |")
    (out_dir / "video_game_domain_attribute_backfill.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--uri")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--database")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Delete existing domain_* attributes before writing.")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports" / "data_quality")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(args.config.parent / ".env")
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    conn = resolve_neo4j_conn(args, cfg or {})
    driver = connect(str(conn["uri"]), str(conn["user"]), str(conn["password"]))

    try:
        cleanup_stats = clear_domain_attrs(driver, conn["database"], args.dry_run) if args.replace else {}
        products = load_products(driver, conn["database"], args.limit)
        rows: list[dict[str, str]] = []
        counts: Counter[tuple[str, str]] = Counter()
        per_type_products: dict[str, set[str]] = defaultdict(set)
        for product in products:
            for attr in infer_attrs(product):
                row = {
                    "product_id": product["product_id"],
                    "attribute_id": attr_id(attr["attr_type"], attr["value"]),
                    "attr_type": attr["attr_type"],
                    "value": attr["value"],
                }
                rows.append(row)
                counts[(attr["attr_type"], attr["value"])] += 1
                per_type_products[attr["attr_type"]].add(product["product_id"])

        write_attrs(driver, conn["database"], rows, args.dry_run)
    finally:
        driver.close()

    summary = {
        "products_scanned": len(products),
        "product_attribute_edges_inferred": len(rows),
        "dry_run": args.dry_run,
        "replace": args.replace,
        **cleanup_stats,
        "products_with_attr_type": {k: len(v) for k, v in sorted(per_type_products.items())},
        "top_values": [
            {"attr_type": k[0], "value": k[1], "count": v}
            for k, v in counts.most_common(80)
        ],
    }
    write_report(args.out_dir, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
