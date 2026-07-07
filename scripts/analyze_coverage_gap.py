from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
from neo4j import GraphDatabase


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


PRODUCT_FACTS_QUERY = """
UNWIND $product_ids AS product_id
OPTIONAL MATCH (p:Product {product_id: product_id})
OPTIONAL MATCH (p)-[:HAS_FEATURE]->(f:Feature)
WITH product_id, p, count(DISTINCT f) AS feature_count
OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH product_id, p, feature_count, count(DISTINCT a) AS attribute_count
OPTIONAL MATCH (r:Review)-[:REVIEWS]->(p)
RETURN product_id,
       p IS NOT NULL AS in_graph,
       p.title AS title,
       p.price AS price,
       properties(p).image_url AS image_url,
       p.main_category AS main_category,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) AS sellable_status,
       coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) AS data_quality_score,
       p.quality_flags AS quality_flags,
       p.title_duplicate_key AS title_duplicate_key,
       p.title_duplicate_count AS title_duplicate_count,
       feature_count,
       attribute_count,
       count(DISTINCT r) AS review_count
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze why offline holdout products are not covered by the current recommendable catalog."
    )
    parser.add_argument("--evaluation-dir", type=Path, default=Path("reports/evaluation/offline_comparison_200_users"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/evaluation/coverage_gap"))
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--min-quality-score", type=float, default=0.6)
    parser.add_argument("--priority-limit", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--skip-meta-export", action="store_true")
    return parser.parse_args()


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


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def valid_title(title: str) -> bool:
    title = clean_text(title)
    if len(title) < 8:
        return False
    if title.lower() in {"unknown", "none", "null", "n/a", "na"}:
        return False
    return len(re.findall(r"[A-Za-z0-9]", title)) >= 5


def load_holdout_counts(per_user_path: Path) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in iter_jsonl(per_user_path):
        for product_id in row.get("holdout_products", []):
            if product_id:
                counter[str(product_id)] += 1
    return counter


def load_candidate_ids(candidate_catalog_path: Path) -> set[str]:
    return {
        str(row["product_id"])
        for row in iter_jsonl(candidate_catalog_path)
        if row.get("product_id")
    }


def batched(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fetch_product_facts(driver: Any, database: str, product_ids: list[str], batch_size: int) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    with driver.session(database=database) as session:
        for batch in batched(product_ids, batch_size):
            for record in session.run(PRODUCT_FACTS_QUERY, product_ids=batch):
                row = dict(record)
                facts[str(row["product_id"])] = row
    return facts


def exclusion_reasons(row: dict[str, Any], in_candidate_catalog: bool, min_quality_score: float) -> list[str]:
    if in_candidate_catalog:
        return []
    if not row.get("in_graph"):
        return ["not_in_graph"]

    reasons: list[str] = []
    title = clean_text(row.get("title"))
    price = optional_float(row.get("price"))
    image_url = clean_text(row.get("image_url"))
    average_rating = optional_float(row.get("average_rating"))
    rating_number = optional_int(row.get("rating_number")) or 0
    data_quality_score = optional_float(row.get("data_quality_score")) or 0.0
    sellable_status = clean_text(row.get("sellable_status")) or "unknown"
    feature_count = optional_int(row.get("feature_count")) or 0
    attribute_count = optional_int(row.get("attribute_count")) or 0
    review_count = optional_int(row.get("review_count")) or 0
    duplicate_count = optional_int(row.get("title_duplicate_count")) or 0
    quality_flags = row.get("quality_flags") if isinstance(row.get("quality_flags"), list) else []

    if sellable_status != "available":
        reasons.append(f"status_{sellable_status}")
    if price is None or price <= 0:
        reasons.append("missing_price")
    if data_quality_score < min_quality_score:
        reasons.append("low_quality_score")
    if not valid_title(title):
        reasons.append("missing_or_short_title")
    if not image_url:
        reasons.append("missing_image")
    if attribute_count <= 0:
        reasons.append("missing_attributes")
    if feature_count <= 0:
        reasons.append("missing_features")
    if review_count <= 0:
        reasons.append("missing_reviews")
    if average_rating is None or rating_number <= 0:
        reasons.append("missing_rating")
    if duplicate_count > 1 or sellable_status == "duplicate_suspect" or "duplicate_suspect" in quality_flags:
        reasons.append("duplicate_suspect")

    return sorted(dict.fromkeys(reasons)) or ["not_in_candidate_catalog"]


def pool_label(row: dict[str, Any], in_candidate_catalog: bool, min_quality_score: float) -> str:
    if in_candidate_catalog:
        return "available_pool"
    if not row.get("in_graph") or not valid_title(clean_text(row.get("title"))):
        return "excluded_pool"
    data_quality_score = optional_float(row.get("data_quality_score")) or 0.0
    image_url = clean_text(row.get("image_url"))
    rating_number = optional_int(row.get("rating_number")) or 0
    attribute_count = optional_int(row.get("attribute_count")) or 0
    feature_count = optional_int(row.get("feature_count")) or 0
    has_useful_content = attribute_count > 0 or feature_count > 0 or rating_number > 0 or bool(image_url)
    if has_useful_content and data_quality_score >= min_quality_score * 0.75:
        return "discoverable_pool"
    return "research_pool"


def repair_actions(reasons: list[str]) -> list[str]:
    actions: list[str] = []
    if "missing_attributes" in reasons:
        actions.append("extract_attributes")
    if "missing_image" in reasons:
        actions.append("enrich_image")
    if "missing_price" in reasons or any(reason.startswith("status_") for reason in reasons):
        actions.append("keep_unavailable_or_source_price")
    if "low_quality_score" in reasons:
        actions.append("reaudit_quality_after_enrichment")
    if "duplicate_suspect" in reasons:
        actions.append("deduplicate_or_keep_best_variant")
    return actions or ["no_action_needed"]


def priority_score(row: dict[str, Any], holdout_count: int, reasons: list[str], pool: str) -> float:
    if pool == "available_pool":
        return 0.0
    if not row.get("in_graph") or "missing_or_short_title" in reasons:
        return 0.0
    rating_number = optional_int(row.get("rating_number")) or 0
    review_count = optional_int(row.get("review_count")) or 0
    average_rating = optional_float(row.get("average_rating")) or 0.0
    score = holdout_count * 4.0
    score += min(math.log1p(rating_number) / math.log1p(1000), 1.0) * 2.0
    score += min(math.log1p(review_count) / math.log1p(50), 1.0) * 1.5
    score += max(average_rating - 3.0, 0.0) * 0.5
    if "missing_attributes" in reasons:
        score += 3.0
    if "low_quality_score" in reasons:
        score += 1.0
    if "missing_price" in reasons:
        score += 0.5
    if pool == "discoverable_pool":
        score += 1.5
    return round(score, 4)


def export_meta_subset(meta_path: Path, product_ids: set[str], output_path: Path) -> int:
    if not meta_path.exists() or not product_ids:
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8") as out:
        for row in iter_jsonl(meta_path):
            product_id = clean_text(row.get("parent_asin"))
            if product_id in product_ids:
                out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                written += 1
                if written >= len(product_ids):
                    break
    return written


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Coverage Gap Analysis",
        "",
        f"Created at: `{report['created_at']}`",
        f"Evaluation source: `{report['evaluation_dir']}`",
        "",
        "## Summary",
        "",
        f"- Unique holdout products: `{report['summary']['unique_holdouts']:,}`",
        f"- Weighted holdout events: `{report['summary']['weighted_holdouts']:,}`",
        f"- Candidate catalog size: `{report['summary']['candidate_catalog_size']:,}`",
        f"- Unique holdouts in candidate catalog: `{report['summary']['unique_holdouts_in_candidate_catalog']:,}`",
        f"- Unique holdouts outside candidate catalog: `{report['summary']['unique_holdouts_outside_candidate_catalog']:,}`",
        f"- Priority repair candidates: `{report['summary']['priority_candidate_count']:,}`",
        f"- Attribute-extraction priority meta rows: `{report['summary']['attribute_priority_meta_rows_written']:,}`",
        "",
        "## Pool Split",
        "",
        "| Pool | Unique Products | Weighted Events |",
        "|---|---:|---:|",
    ]
    for pool, row in report["pool_counts"].items():
        lines.append(f"| `{pool}` | {row['unique_products']} | {row['weighted_events']} |")

    lines.extend(["", "## Top Exclusion Reasons", "", "| Reason | Unique Products | Weighted Events |", "|---|---:|---:|"])
    for reason, row in report["reason_counts"].items():
        lines.append(f"| `{reason}` | {row['unique_products']} | {row['weighted_events']} |")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action, count in report["repair_action_counts"].items():
        lines.append(f"- `{action}`: {count} priority candidates")

    lines.extend(["", "## Top Priority Products", "", "| Product | Count | Score | Pool | Reasons | Title |", "|---|---:|---:|---|---|---|"])
    for row in report["priority_candidates"][:20]:
        title = clean_text(row.get("title"))[:80].replace("|", " ")
        reasons = ", ".join(row.get("reasons", []))
        lines.append(
            f"| `{row['product_id']}` | {row['holdout_count']} | {row['priority_score']:.2f} | "
            f"`{row['pool']}` | {reasons} | {title} |"
        )

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- JSON report: `{report['output_files']['json_report']}`",
            f"- Product rows: `{report['output_files']['product_rows']}`",
            f"- Priority candidates: `{report['output_files']['priority_candidates']}`",
            f"- Priority meta subset: `{report['output_files']['priority_meta_subset']}`",
            f"- Attribute priority meta subset: `{report['output_files']['attribute_priority_meta_subset']}`",
            f"- Chart: `{report['output_files']['chart']}`",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_report(report: dict[str, Any], path: Path) -> None:
    top_reasons = list(report["reason_counts"].items())[:12]
    pools = list(report["pool_counts"].items())
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].bar([key for key, _ in top_reasons], [row["unique_products"] for _, row in top_reasons])
    axes[0].set_title("Top Coverage Gap Reasons")
    axes[0].set_ylabel("Unique Holdout Products")
    axes[0].tick_params(axis="x", rotation=45)

    axes[1].bar([key for key, _ in pools], [row["unique_products"] for _, row in pools])
    axes[1].set_title("Holdout Pool Split")
    axes[1].set_ylabel("Unique Holdout Products")
    axes[1].tick_params(axis="x", rotation=25)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    load_env_file()

    per_user_path = args.evaluation_dir / "intermediates/per_user_metrics.jsonl"
    candidate_catalog_path = args.evaluation_dir / "intermediates/candidate_catalog.jsonl"
    if not per_user_path.exists():
        raise SystemExit(f"Missing per-user metrics: {per_user_path}")
    if not candidate_catalog_path.exists():
        raise SystemExit(f"Missing candidate catalog: {candidate_catalog_path}")

    holdout_counts = load_holdout_counts(per_user_path)
    candidate_ids = load_candidate_ids(candidate_catalog_path)
    product_ids = sorted(holdout_counts)

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    try:
        facts = fetch_product_facts(driver, database, product_ids, args.batch_size)
    finally:
        driver.close()

    product_rows: list[dict[str, Any]] = []
    reason_unique: Counter[str] = Counter()
    reason_weighted: Counter[str] = Counter()
    pool_unique: Counter[str] = Counter()
    pool_weighted: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()

    for product_id in product_ids:
        fact = facts.get(product_id, {"product_id": product_id, "in_graph": False})
        holdout_count = holdout_counts[product_id]
        in_candidate_catalog = product_id in candidate_ids
        reasons = exclusion_reasons(fact, in_candidate_catalog, args.min_quality_score)
        pool = pool_label(fact, in_candidate_catalog, args.min_quality_score)
        actions = repair_actions(reasons)
        score = priority_score(fact, holdout_count, reasons, pool)
        row = {
            **fact,
            "product_id": product_id,
            "holdout_count": holdout_count,
            "in_candidate_catalog": in_candidate_catalog,
            "pool": pool,
            "reasons": reasons,
            "repair_actions": actions,
            "priority_score": score,
        }
        product_rows.append(row)
        pool_unique[pool] += 1
        pool_weighted[pool] += holdout_count
        for reason in reasons or ["in_candidate_catalog"]:
            reason_unique[reason] += 1
            reason_weighted[reason] += holdout_count
        if score > 0:
            for action in actions:
                action_counter[action] += 1

    product_rows.sort(key=lambda row: (row["priority_score"], row["holdout_count"]), reverse=True)
    priority_candidates = [row for row in product_rows if row["priority_score"] > 0][: args.priority_limit]

    json_report_path = args.output_dir / "coverage_gap.json"
    markdown_path = args.output_dir / "coverage_gap.md"
    product_rows_path = args.output_dir / "coverage_gap_products.jsonl"
    priority_path = args.output_dir / "priority_candidates.jsonl"
    priority_meta_path = args.output_dir / "priority_candidates_meta.jsonl"
    attribute_meta_path = args.output_dir / "attribute_priority_candidates_meta.jsonl"
    chart_path = args.output_dir / "coverage_gap_by_reason.png"

    write_jsonl(product_rows_path, product_rows)
    write_jsonl(priority_path, priority_candidates)
    meta_written = 0
    if not args.skip_meta_export:
        priority_ids = {row["product_id"] for row in priority_candidates}
        meta_written = export_meta_subset(args.meta_path, priority_ids, priority_meta_path)
        attribute_ids = {
            row["product_id"]
            for row in priority_candidates
            if "extract_attributes" in row.get("repair_actions", [])
        }
        attribute_meta_written = export_meta_subset(args.meta_path, attribute_ids, attribute_meta_path)
    else:
        attribute_meta_written = 0

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_dir": str(args.evaluation_dir),
        "config": {
            "min_quality_score": args.min_quality_score,
            "priority_limit": args.priority_limit,
            "meta_path": str(args.meta_path),
        },
        "summary": {
            "unique_holdouts": len(product_ids),
            "weighted_holdouts": sum(holdout_counts.values()),
            "candidate_catalog_size": len(candidate_ids),
            "unique_holdouts_in_candidate_catalog": sum(1 for product_id in product_ids if product_id in candidate_ids),
            "unique_holdouts_outside_candidate_catalog": sum(1 for product_id in product_ids if product_id not in candidate_ids),
            "priority_candidate_count": len(priority_candidates),
            "priority_meta_rows_written": meta_written,
            "attribute_priority_meta_rows_written": attribute_meta_written,
        },
        "pool_counts": {
            key: {"unique_products": pool_unique[key], "weighted_events": pool_weighted[key]}
            for key, _ in pool_unique.most_common()
        },
        "reason_counts": {
            key: {"unique_products": reason_unique[key], "weighted_events": reason_weighted[key]}
            for key, _ in reason_unique.most_common()
        },
        "repair_action_counts": dict(action_counter.most_common()),
        "priority_candidates": priority_candidates[:100],
        "output_files": {
            "json_report": str(json_report_path),
            "markdown_report": str(markdown_path),
            "product_rows": str(product_rows_path),
            "priority_candidates": str(priority_path),
            "priority_meta_subset": str(priority_meta_path) if meta_written else None,
            "attribute_priority_meta_subset": str(attribute_meta_path) if attribute_meta_written else None,
            "chart": str(chart_path),
        },
    }

    write_json(json_report_path, report)
    write_markdown(markdown_path, report)
    plot_report(report, chart_path)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
