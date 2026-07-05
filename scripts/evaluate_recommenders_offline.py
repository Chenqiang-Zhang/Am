from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np
from neo4j import GraphDatabase

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.models import AttributeFilter, SearchIntent
from api.recommender import Recommender, _load_env

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "your", "you", "are", "was", "were",
    "of", "to", "in", "on", "a", "an", "is", "it", "as", "by", "or", "at", "be", "skin",
    "hair", "beauty", "product", "products", "pack", "set", "new",
}
METRIC_KEYS = [
    "hit_rate",
    "recall",
    "precision",
    "mrr",
    "ndcg",
    "semantic_recall",
    "semantic_ndcg",
    "title_overlap",
    "attribute_overlap",
    "diversity",
    "sellable_rate",
    "price_coverage",
    "reason_coverage",
    "catalog_coverage",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run offline recommender comparisons with readiness checks, intermediates, and charts."
    )
    parser.add_argument("--sample-users", type=int, default=30)
    parser.add_argument("--min-reviews", type=int, default=4)
    parser.add_argument("--history-size", type=int, default=3)
    parser.add_argument("--holdout-size", type=int, default=2)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--candidate-catalog-limit", type=int, default=8000)
    parser.add_argument("--max-attrs", type=int, default=8)
    parser.add_argument("--min-quality-score", type=float, default=0.6)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/evaluation/offline_comparison"))
    parser.add_argument("--output-path", type=Path, default=None, help="Optional compatibility alias for summary JSON output.")
    parser.add_argument("--strict-readiness", action="store_true", help="Abort if readiness checks raise warnings.")
    parser.add_argument("--skip-catalog-snapshot", action="store_true", help="Do not save the BM25 candidate catalog JSONL.")
    parser.add_argument(
        "--ground-truth-scope",
        choices=["recommendable", "all"],
        default="recommendable",
        help="Use only recommendable products for history/holdout by default so ground truth matches the recommendation pool.",
    )
    return parser.parse_args()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def scalar(session: Any, q: str, **params: Any) -> Any:
    record = session.run(q, **params).single()
    return record[0] if record else None


def relationship_exists(session: Any, relationship_type: str) -> bool:
    record = session.run(
        """
        CALL db.relationshipTypes() YIELD relationshipType
        WHERE relationshipType = $relationship_type
        RETURN count(*) AS count
        """,
        relationship_type=relationship_type,
    ).single()
    return bool(record and int(record["count"] or 0) > 0)


def product_pool_filter(alias: str = "p") -> str:
    return (
        f"coalesce({alias}.sellable_status, CASE WHEN {alias}.price IS NOT NULL THEN \"available\" ELSE \"currently_unavailable\" END) = \"available\" "
        f"AND coalesce(toFloat({alias}.data_quality_score), CASE WHEN {alias}.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score"
    )


def readiness_check(driver: Any, database: str, min_reviews: int, min_quality_score: float) -> dict[str, Any]:
    with driver.session(database=database) as session:
        has_mentions = relationship_exists(session, "MENTIONS")
        stats = {
            "products_total": scalar(session, "MATCH (p:Product) RETURN count(p)"),
            "reviews_total": scalar(session, "MATCH (r:Review) RETURN count(r)"),
            "users_total": scalar(session, "MATCH (u:User) RETURN count(u)"),
            "rated_edges": scalar(session, "MATCH (:User)-[r:RATED]->(:Product) RETURN count(r)"),
            "products_with_price": scalar(session, "MATCH (p:Product) WHERE p.price IS NOT NULL RETURN count(p)"),
            "products_with_title": scalar(session, "MATCH (p:Product) WHERE p.title IS NOT NULL AND trim(p.title) <> '' RETURN count(p)"),
            "products_with_image": scalar(session, "MATCH (p:Product) WHERE p.image_url IS NOT NULL AND trim(p.image_url) <> '' RETURN count(p)"),
            "products_with_features": scalar(session, "MATCH (p:Product)-[:HAS_FEATURE]->(:Feature) RETURN count(DISTINCT p)"),
            "products_with_attributes": scalar(session, "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(:Attribute) RETURN count(DISTINCT p)"),
            "attributes_total": scalar(session, "MATCH (a:Attribute) RETURN count(a)"),
            "mentions_total": scalar(session, "MATCH ()-[m:MENTIONS]->() RETURN count(m)") if has_mentions else 0,
            "recommendable_products": scalar(
                session,
                """
                MATCH (p:Product)
                WHERE coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
                  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
                RETURN count(p)
                """,
                min_quality_score=min_quality_score,
            ),
            "recommendable_with_attributes": scalar(
                session,
                """
                MATCH (p:Product)-[:HAS_ATTRIBUTE]->(:Attribute)
                WHERE coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
                  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
                RETURN count(DISTINCT p)
                """,
                min_quality_score=min_quality_score,
            ),
            "eligible_eval_users": scalar(
                session,
                f"""
                MATCH (u:User)-[r:RATED]->(p:Product)
                WHERE r.timestamp IS NOT NULL
                  AND {product_pool_filter("p")}
                WITH u, count(r) AS review_count
                WHERE review_count >= $min_reviews
                RETURN count(u)
                """,
                min_reviews=min_reviews,
                min_quality_score=min_quality_score,
            ),
        }
    ratios = {
        "price_coverage": safe_div(stats["products_with_price"], stats["products_total"]),
        "feature_coverage": safe_div(stats["products_with_features"], stats["products_total"]),
        "attribute_coverage": safe_div(stats["products_with_attributes"], stats["products_total"]),
        "recommendable_rate": safe_div(stats["recommendable_products"], stats["products_total"]),
        "recommendable_attribute_coverage": safe_div(stats["recommendable_with_attributes"], stats["recommendable_products"]),
    }
    warnings: list[str] = []
    if stats["recommendable_products"] < 1000:
        warnings.append("recommendable_products_below_1000")
    if stats["eligible_eval_users"] < 20:
        warnings.append("eligible_eval_users_below_20")
    if ratios["price_coverage"] < 0.1:
        warnings.append("price_coverage_below_10_percent")
    if ratios["recommendable_attribute_coverage"] < 0.2:
        warnings.append("recommendable_attribute_coverage_below_20_percent")
    return {"stats": stats, "ratios": ratios, "warnings": warnings, "ready_for_experiment": not warnings}


def safe_div(num: Any, den: Any) -> float:
    return round(float(num or 0) / float(den or 1), 6)


def normalize_fact_row(row: dict[str, Any]) -> dict[str, Any]:
    row["features"] = [value for value in row.get("features") or [] if value]
    row["attributes"] = [
        attr
        for attr in row.get("attributes") or []
        if attr and attr.get("attribute_type") and attr.get("value")
    ]
    return row


def write_readiness_markdown(path: Path, readiness: dict[str, Any]) -> None:
    lines = ["# Offline Experiment Data Readiness", ""]
    lines.append(f"Ready for experiment: `{readiness['ready_for_experiment']}`")
    lines.append("")
    if readiness["warnings"]:
        lines.append("## Warnings")
        lines.extend(f"- `{warning}`" for warning in readiness["warnings"])
        lines.append("")
    lines.append("## Counts")
    lines.append("| Item | Count |")
    lines.append("|---|---:|")
    for key, value in readiness["stats"].items():
        lines.append(f"| `{key}` | {value:,} |" if isinstance(value, int) else f"| `{key}` | {value} |")
    lines.append("")
    lines.append("## Ratios")
    lines.append("| Item | Ratio |")
    lines.append("|---|---:|")
    for key, value in readiness["ratios"].items():
        lines.append(f"| `{key}` | {value:.2%} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_users(
    driver: Any,
    database: str,
    sample_users: int,
    min_reviews: int,
    seed: int,
    min_quality_score: float,
    ground_truth_scope: str,
) -> list[str]:
    pool_clause = f"AND {product_pool_filter('p')}" if ground_truth_scope == "recommendable" else ""
    cypher = f"""
MATCH (u:User)-[r:RATED]->(p:Product)
WHERE r.timestamp IS NOT NULL
  {pool_clause}
WITH u, count(DISTINCT p) AS product_count
WHERE product_count >= $min_reviews
RETURN u.user_id AS user_id
ORDER BY u.user_id
LIMIT $sample_users
"""
    # Keep the offline sample reproducible; broader random folds can be added once the dataset is larger.
    with driver.session(database=database) as session:
        return [
            record["user_id"]
            for record in session.run(
                cypher,
                min_reviews=min_reviews,
                sample_users=sample_users,
                min_quality_score=min_quality_score,
            )
        ]


def user_review_sequence(
    driver: Any,
    database: str,
    user_id: str,
    min_quality_score: float,
    ground_truth_scope: str,
) -> list[dict[str, Any]]:
    pool_clause = f"AND {product_pool_filter('p')}" if ground_truth_scope == "recommendable" else ""
    cypher = f"""
MATCH (:User {{user_id: $user_id}})-[r:RATED]->(p:Product)
WHERE r.timestamp IS NOT NULL
  {pool_clause}
RETURN p.product_id AS product_id,
       p.title AS title,
       r.timestamp AS timestamp,
       toFloat(r.rating) AS rating
ORDER BY r.timestamp ASC
"""
    with driver.session(database=database) as session:
        return [
            dict(record)
            for record in session.run(
                cypher,
                user_id=user_id,
                min_quality_score=min_quality_score,
            )
        ]


def history_intent(driver: Any, database: str, product_ids: list[str], max_attrs: int = 8) -> SearchIntent:
    cypher = """
MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE p.product_id IN $product_ids
WITH a.attribute_type AS attribute_type, a.value AS value, count(*) AS freq
ORDER BY freq DESC
LIMIT $max_attrs
RETURN attribute_type, value, freq
"""
    with driver.session(database=database) as session:
        rows = [dict(record) for record in session.run(cypher, product_ids=product_ids, max_attrs=max_attrs)]
    filters = [
        AttributeFilter(attribute_type=row["attribute_type"], value=row["value"], weight=0.7)
        for row in rows
        if row.get("attribute_type") and row.get("value")
    ]
    keywords = [row["value"] for row in rows if row.get("value")]
    return SearchIntent(attribute_filters=filters, keywords=keywords[:max_attrs], price_max=None, min_rating=None)


def semantic_history_intent(
    driver: Any,
    database: str,
    history: list[dict[str, Any]],
    max_attrs: int = 8,
) -> SearchIntent:
    attribute_intent = history_intent(
        driver,
        database,
        [row["product_id"] for row in history if row.get("product_id")],
        max_attrs=max_attrs,
    )
    title_intent = title_profile_intent(history, max_terms=max_attrs)
    keywords: list[str] = []
    for value in attribute_intent.keywords + title_intent.keywords:
        if value and value not in keywords:
            keywords.append(value)
    return SearchIntent(
        attribute_filters=attribute_intent.attribute_filters,
        keywords=keywords[: max_attrs * 2],
        price_max=None,
        min_rating=None,
    )


def title_profile_intent(history: list[dict[str, Any]], max_terms: int = 8) -> SearchIntent:
    counter: Counter[str] = Counter()
    for row in history:
        for token in tokenize(row.get("title") or ""):
            counter[token] += 1
    keywords = [token for token, _ in counter.most_common(max_terms)]
    return SearchIntent(attribute_filters=[], keywords=keywords, price_max=None, min_rating=None)


def fetch_catalog(driver: Any, database: str, limit: int, min_quality_score: float) -> list[dict[str, Any]]:
    cypher = """
MATCH (p:Product)
WHERE coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
OPTIONAL MATCH (p)-[:HAS_FEATURE]->(f:Feature)
OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH p,
     collect(DISTINCT coalesce(f.normalized_text, f.text))[0..4] AS features,
     collect(DISTINCT {attribute_type: a.attribute_type, value: a.value})[0..12] AS attributes
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       p.sellable_status AS sellable_status,
       p.data_quality_score AS data_quality_score,
       features AS features,
       attributes AS attributes
ORDER BY coalesce(toFloat(p.average_rating), 3.8) DESC, coalesce(toInteger(p.rating_number), 0) DESC
LIMIT $limit
"""
    with driver.session(database=database) as session:
        return [normalize_fact_row(dict(record)) for record in session.run(cypher, limit=limit, min_quality_score=min_quality_score)]


def fetch_product_facts(driver: Any, database: str, product_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not product_ids:
        return {}
    cypher = """
MATCH (p:Product)
WHERE p.product_id IN $product_ids
OPTIONAL MATCH (p)-[:HAS_FEATURE]->(f:Feature)
OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH p,
     collect(DISTINCT coalesce(f.normalized_text, f.text))[0..4] AS features,
     collect(DISTINCT {attribute_type: a.attribute_type, value: a.value})[0..12] AS attributes
RETURN p.product_id AS product_id,
       p.title AS title,
       p.price AS price,
       p.average_rating AS average_rating,
       p.rating_number AS rating_number,
       coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) AS sellable_status,
       p.data_quality_score AS data_quality_score,
       features AS features,
       attributes AS attributes
"""
    with driver.session(database=database) as session:
        return {
            record["product_id"]: normalize_fact_row(dict(record))
            for record in session.run(cypher, product_ids=product_ids)
        }


def popularity_baseline(catalog: list[dict[str, Any]], k: int, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    return [row["product_id"] for row in catalog if row["product_id"] not in exclude][:k]


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall((text or "").lower()) if len(token) > 2 and token not in STOPWORDS]


def build_bm25_index(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    df: Counter[str] = Counter()
    total_len = 0
    for row in catalog:
        text = " ".join([str(row.get("title") or "")] + [str(x or "") for x in row.get("features") or []])
        tokens = tokenize(text)
        tf = Counter(tokens)
        docs.append({"product_id": row["product_id"], "tf": tf, "length": len(tokens)})
        total_len += len(tokens)
        for token in tf:
            df[token] += 1
    return {
        "docs": docs,
        "df": df,
        "avgdl": total_len / max(len(docs), 1),
        "doc_count": len(docs),
    }


def bm25_recommend(index: dict[str, Any], query_terms: list[str], k: int, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    query_terms = [term for term in query_terms if term]
    if not query_terms:
        return []
    k1 = 1.5
    b = 0.75
    scored: list[tuple[float, str]] = []
    doc_count = index["doc_count"]
    avgdl = index["avgdl"] or 1.0
    df = index["df"]
    for doc in index["docs"]:
        product_id = doc["product_id"]
        if product_id in exclude:
            continue
        score = 0.0
        for term in query_terms:
            freq = doc["tf"].get(term, 0)
            if freq <= 0:
                continue
            idf = math.log(1 + (doc_count - df[term] + 0.5) / (df[term] + 0.5))
            denom = freq + k1 * (1 - b + b * doc["length"] / avgdl)
            score += idf * freq * (k1 + 1) / denom
        if score > 0:
            scored.append((score, product_id))
    scored.sort(reverse=True)
    return [product_id for _, product_id in scored[:k]]


def reciprocal_rank_fusion(lists: list[list[str]], k: int, exclude: set[str] | None = None, constant: int = 60) -> list[str]:
    exclude = exclude or set()
    scores: defaultdict[str, float] = defaultdict(float)
    for ranking in lists:
        for index, product_id in enumerate(ranking, start=1):
            if product_id in exclude:
                continue
            scores[product_id] += 1.0 / (constant + index)
    return [product_id for product_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]]


def title_jaccard(left: str, right: str) -> float:
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def attr_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for attr in row.get("attributes") or []:
        attr_type = str(attr.get("attribute_type") or "").lower()
        value = str(attr.get("value") or "").lower()
        if attr_type and value:
            keys.add(f"{attr_type}:{value}")
    return keys


def profile_tokens(row: dict[str, Any]) -> set[str]:
    parts = [str(row.get("title") or "")]
    parts.extend(str(value or "") for value in row.get("features") or [])
    for attr in row.get("attributes") or []:
        parts.append(str(attr.get("value") or ""))
    return set(tokenize(" ".join(parts)))


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def pairwise_diversity(rows: list[dict[str, Any]]) -> float:
    token_sets = [profile_tokens(row) for row in rows if row]
    if len(token_sets) < 2:
        return 0.0
    similarities: list[float] = []
    for left_index, left in enumerate(token_sets):
        for right in token_sets[left_index + 1 :]:
            similarities.append(jaccard(left, right))
    return 1.0 - (sum(similarities) / max(len(similarities), 1))


def semantic_similarity(candidate: dict[str, Any], target: dict[str, Any]) -> dict[str, float]:
    title_score = title_jaccard(str(candidate.get("title") or ""), str(target.get("title") or ""))
    token_score = jaccard(profile_tokens(candidate), profile_tokens(target))
    attribute_score = jaccard(attr_keys(candidate), attr_keys(target))
    score = max(title_score, (0.45 * title_score) + (0.30 * token_score) + (0.25 * attribute_score))
    return {
        "semantic": min(score, 1.0),
        "title": title_score,
        "attribute": attribute_score,
    }


def metrics(
    recommended: list[str],
    relevant: set[str],
    holdout: list[dict[str, Any]],
    k: int,
    catalog_by_id: dict[str, dict[str, Any]],
) -> dict[str, float]:
    top_k = recommended[:k]
    hits = [1 if product_id in relevant else 0 for product_id in top_k]
    hit_rate = 1.0 if any(hits) else 0.0
    recall = sum(hits) / max(len(relevant), 1)
    precision = sum(hits) / max(k, 1)
    reciprocal_rank = 0.0
    dcg = 0.0
    for index, hit in enumerate(hits, start=1):
        if hit and reciprocal_rank == 0.0:
            reciprocal_rank = 1.0 / index
        if hit:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    ndcg = dcg / idcg if idcg else 0.0
    known_rows = [catalog_by_id.get(product_id, {}) for product_id in top_k]
    sellable = [row for row in known_rows if row.get("sellable_status") == "available"]
    priced = [row for row in known_rows if row.get("price") is not None]
    reasoned = [product_id for product_id in top_k if product_id]
    holdout_by_id = {row["product_id"]: row for row in holdout if row.get("product_id")}
    missing_holdout = [product_id for product_id in relevant if product_id not in catalog_by_id]
    if missing_holdout:
        # The caller normally prefetches these rows; keep a title-only fallback for older intermediates.
        for row in holdout:
            catalog_by_id.setdefault(row["product_id"], row)
    holdout_rows = [catalog_by_id.get(product_id) or holdout_by_id.get(product_id, {}) for product_id in relevant]
    semantic_scores: list[float] = []
    title_scores: list[float] = []
    attribute_scores: list[float] = []
    for row in known_rows:
        pair_scores = [semantic_similarity(row, holdout_row) for holdout_row in holdout_rows if holdout_row]
        semantic_scores.append(max((score["semantic"] for score in pair_scores), default=0.0))
        title_scores.append(max((score["title"] for score in pair_scores), default=0.0))
        attribute_scores.append(max((score["attribute"] for score in pair_scores), default=0.0))
    semantic_dcg = sum(score / math.log2(index + 1) for index, score in enumerate(semantic_scores, start=1))
    semantic_idcg = sum(1.0 / math.log2(index + 1) for index in range(1, len(semantic_scores) + 1))
    denom = max(len(top_k), 1)
    return {
        "hit_rate": hit_rate,
        "recall": recall,
        "precision": precision,
        "mrr": reciprocal_rank,
        "ndcg": ndcg,
        "semantic_recall": min(sum(semantic_scores) / max(len(relevant), 1), 1.0),
        "semantic_ndcg": semantic_dcg / semantic_idcg if semantic_idcg else 0.0,
        "title_overlap": sum(title_scores) / denom,
        "attribute_overlap": sum(attribute_scores) / denom,
        "diversity": pairwise_diversity(known_rows),
        "sellable_rate": len(sellable) / denom,
        "price_coverage": len(priced) / denom,
        "reason_coverage": len(reasoned) / denom,
        "catalog_coverage": 0.0,
    }


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {key: 0.0 for key in METRIC_KEYS}
    return {key: round(mean(row.get(key, 0.0) for row in rows), 4) for key in METRIC_KEYS}


def rec_ids(recs: Any) -> list[str]:
    return [rec.product_id for rec in recs]


def exclude_seen(product_ids: list[str], exclude: set[str], k: int) -> list[str]:
    filtered: list[str] = []
    for product_id in product_ids:
        if product_id in exclude or product_id in filtered:
            continue
        filtered.append(product_id)
        if len(filtered) >= k:
            break
    return filtered


def plot_metric_group(
    summary: dict[str, Any],
    path: Path,
    metrics_to_plot: list[str],
    title: str,
    ylabel: str = "Score",
    fixed_ylim: bool = False,
) -> None:
    methods = list(summary["methods"])
    all_values = [
        summary["methods"][method][metric]
        for method in methods
        for metric in metrics_to_plot
    ]
    x = np.arange(len(methods))
    width = min(0.82 / max(len(metrics_to_plot), 1), 0.22)
    fig, ax = plt.subplots(figsize=(max(10, len(methods) * 1.5), 5.2))
    for i, metric in enumerate(metrics_to_plot):
        values = [summary["methods"][method][metric] for method in methods]
        offset = (i - (len(metrics_to_plot) - 1) / 2) * width
        ax.bar(x + offset, values, width, label=metric)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_xticks(x)
    ax.set_xticklabels(methods, rotation=25, ha="right")
    if fixed_ylim:
        ax.set_ylim(0, 1.0)
    else:
        upper = min(1.0, max(0.05, max(all_values or [0.0]) * 1.25 + 0.01))
        ax.set_ylim(0, upper)
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_summary_markdown(summary: dict[str, Any], path: Path) -> None:
    readiness = summary["data_readiness"]
    has_exact_hits = any(row.get("hit_rate", 0.0) > 0.0 for row in summary["methods"].values())
    lines = [
        "# Offline Recommender Comparison",
        "",
        f"Created at: `{summary['created_at']}`",
        f"Evaluated users: `{summary['evaluated_users']}`",
        f"Ground-truth scope: `{summary['config']['ground_truth_scope']}`",
        "",
        "## Data Readiness",
        "",
        f"Ready for experiment: `{readiness['ready_for_experiment']}`",
        "",
    ]
    if readiness["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- `{warning}`" for warning in readiness["warnings"])
        lines.append("")
    lines.extend(
        [
            "| Ratio | Value |",
            "|---|---:|",
        ]
    )
    for key, value in readiness["ratios"].items():
        lines.append(f"| `{key}` | {value:.2%} |")
    lines.extend(
        [
            "",
            "## Method Metrics",
            "",
            "| Method | Recall@K | HitRate@K | NDCG@K | MRR@K | Precision@K | SemanticNDCG@K | SemanticRecall@K | TitleOverlap@K | AttributeOverlap@K | Diversity@K | CatalogCoverage@K | SellableRate@K | PriceCoverage@K |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for method, metrics_row in summary["methods"].items():
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{method}`",
                    f"{metrics_row['recall']:.4f}",
                    f"{metrics_row['hit_rate']:.4f}",
                    f"{metrics_row['ndcg']:.4f}",
                    f"{metrics_row['mrr']:.4f}",
                    f"{metrics_row['precision']:.4f}",
                    f"{metrics_row['semantic_ndcg']:.4f}",
                    f"{metrics_row['semantic_recall']:.4f}",
                    f"{metrics_row['title_overlap']:.4f}",
                    f"{metrics_row['attribute_overlap']:.4f}",
                    f"{metrics_row['diversity']:.4f}",
                    f"{metrics_row['catalog_coverage']:.4f}",
                    f"{metrics_row['sellable_rate']:.4f}",
                    f"{metrics_row['price_coverage']:.4f}",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Notes",
            "",
            "- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.",
            "- `SemanticNDCG@K`, `SemanticRecall@K`, `TitleOverlap@K`, and `AttributeOverlap@K` are softer discovery metrics for cases where the correct answer is a similar product rather than the exact future ASIN.",
            "- `Diversity@K` and `CatalogCoverage@K` check whether a method collapses to the same narrow set of products.",
            "- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.",
            "- `SellableRate@K` and `PriceCoverage@K` verify that comparison methods are not winning by recommending unusable products.",
            "",
            "## Output Files",
            "",
        ]
    )
    if has_exact_hits:
        lines.insert(
            lines.index("## Output Files") - 1,
            "- Non-zero exact-hit scores show that the evaluation target is now aligned with the recommendable product pool.",
        )
    for label, file_path in summary["intermediate_files"].items():
        if file_path:
            lines.append(f"- `{label}`: `{file_path}`")
    for label, file_path in summary["charts"].items():
        lines.append(f"- `{label}` chart: `{file_path}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_readiness(readiness: dict[str, Any], path: Path) -> None:
    keys = ["price_coverage", "feature_coverage", "attribute_coverage", "recommendable_rate", "recommendable_attribute_coverage"]
    values = [readiness["ratios"].get(key, 0.0) for key in keys]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.bar(keys, values, color=["#4d7c8a", "#b08d57", "#7b5ea7", "#5f8f5f", "#a75858"])
    ax.set_ylabel("Ratio")
    ax.set_title("Graph data readiness ratios")
    ax.set_ylim(0, 1.0)
    ax.set_xticks(range(len(keys)))
    ax.set_xticklabels(keys, rotation=25, ha="right")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    _load_env()
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    effective_min_reviews = max(args.min_reviews, args.history_size + args.holdout_size)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    intermediates_dir = output_dir / "intermediates"
    charts_dir = output_dir / "charts"

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    recommender = Recommender()

    readiness = readiness_check(driver, database, effective_min_reviews, args.min_quality_score)
    write_json(output_dir / "data_readiness.json", readiness)
    write_readiness_markdown(output_dir / "data_readiness.md", readiness)
    plot_readiness(readiness, charts_dir / "data_readiness.png")
    if args.strict_readiness and readiness["warnings"]:
        raise SystemExit(f"Readiness check failed: {readiness['warnings']}")

    users = pick_users(
        driver,
        database,
        args.sample_users,
        effective_min_reviews,
        args.random_seed,
        args.min_quality_score,
        args.ground_truth_scope,
    )
    append_jsonl(intermediates_dir / "sampled_users.jsonl", ({"user_id": user_id} for user_id in users))

    catalog = fetch_catalog(driver, database, args.candidate_catalog_limit, args.min_quality_score)
    catalog_by_id = {row["product_id"]: row for row in catalog}
    if not args.skip_catalog_snapshot:
        append_jsonl(intermediates_dir / "candidate_catalog.jsonl", catalog)
    bm25_index = build_bm25_index(catalog)
    no_history_home_ids = popularity_baseline(catalog, args.k * 3)

    method_metrics: dict[str, list[dict[str, float]]] = defaultdict(list)
    method_unique_recommendations: dict[str, set[str]] = defaultdict(set)
    per_user_rows: list[dict[str, Any]] = []
    recommendation_rows: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []

    for user_id in users:
        sequence = user_review_sequence(driver, database, user_id, args.min_quality_score, args.ground_truth_scope)
        if len(sequence) < args.history_size + args.holdout_size:
            continue
        history = sequence[: args.history_size]
        holdout = sequence[args.history_size : args.history_size + args.holdout_size]
        history_ids = {row["product_id"] for row in history}
        relevant = {row["product_id"] for row in holdout}

        kg_intent = history_intent(driver, database, list(history_ids), args.max_attrs)
        kg_attribute_only_intent = SearchIntent(
            attribute_filters=kg_intent.attribute_filters,
            keywords=[],
            price_max=None,
            min_rating=None,
        )
        kg_semantic_intent = semantic_history_intent(driver, database, history, args.max_attrs)
        title_intent = title_profile_intent(history, args.max_attrs)
        kg_plan = recommender.make_query_plan("[offline_kg_history]", kg_attribute_only_intent, user_id=None)

        kg_recs = exclude_seen(
            rec_ids(
                recommender.search_products(
                    kg_attribute_only_intent,
                    args.k * 3,
                    "en",
                    user_id=None,
                    query_plan=kg_plan,
                )
            ),
            history_ids,
            args.k,
        )
        bm25_terms = title_intent.keywords + kg_semantic_intent.keywords + [f.value for f in kg_semantic_intent.attribute_filters]
        bm25_recs = bm25_recommend(bm25_index, bm25_terms, args.k, exclude=history_ids)
        bm25_semantic_pool = bm25_recommend(bm25_index, bm25_terms, args.k * 3, exclude=history_ids)
        kg_semantic_recs = reciprocal_rank_fusion([kg_recs, bm25_semantic_pool], args.k, exclude=history_ids)
        title_recs = bm25_recommend(bm25_index, title_intent.keywords, args.k, exclude=history_ids)
        pop_recs = popularity_baseline(catalog, args.k, exclude=history_ids)
        kg_no_history_recs = exclude_seen(no_history_home_ids, history_ids, args.k)
        hybrid_recs = reciprocal_rank_fusion(
            [bm25_recs, bm25_recs, title_recs, title_recs, kg_semantic_recs, kg_recs],
            args.k,
            exclude=history_ids,
        )

        methods = {
            "popularity_baseline": pop_recs,
            "bm25_history_profile": bm25_recs,
            "kg_no_history_home": kg_no_history_recs,
            "kg_attribute_history": kg_recs,
            "kg_semantic_history": kg_semantic_recs,
            "title_keyword_profile": title_recs,
            "hybrid_rrf": hybrid_recs,
        }
        missing_facts = sorted(
            {
                product_id
                for recommendations in methods.values()
                for product_id in recommendations
                if product_id not in catalog_by_id
            }
        )
        missing_facts.extend(product_id for product_id in relevant if product_id not in catalog_by_id)
        if missing_facts:
            catalog_by_id.update(fetch_product_facts(driver, database, missing_facts))
        user_metrics: dict[str, Any] = {
            "user_id": user_id,
            "history_products": sorted(history_ids),
            "holdout_products": sorted(relevant),
            "kg_intent": kg_intent.model_dump(),
            "kg_semantic_intent": kg_semantic_intent.model_dump(),
            "title_intent": title_intent.model_dump(),
        }
        for method, recommendations in methods.items():
            row_metrics = metrics(recommendations, relevant, holdout, args.k, catalog_by_id)
            method_metrics[method].append(row_metrics)
            method_unique_recommendations[method].update(recommendations[: args.k])
            user_metrics[method] = row_metrics
            for rank, product_id in enumerate(recommendations, start=1):
                recommendation_rows.append(
                    {
                        "user_id": user_id,
                        "method": method,
                        "rank": rank,
                        "product_id": product_id,
                        "is_relevant": product_id in relevant,
                    }
                )
        per_user_rows.append(user_metrics)
        if len(examples) < 8:
            examples.append({**user_metrics, "recommendations": methods})

    append_jsonl(intermediates_dir / "per_user_metrics.jsonl", per_user_rows)
    append_jsonl(intermediates_dir / "method_recommendations.jsonl", recommendation_rows)

    method_summary = {method: aggregate(rows) for method, rows in sorted(method_metrics.items())}
    catalog_size = max(len(catalog), 1)
    for method, row in method_summary.items():
        row["catalog_coverage"] = round(len(method_unique_recommendations.get(method, set())) / catalog_size, 4)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "effective_min_reviews": effective_min_reviews,
        "data_readiness": readiness,
        "evaluated_users": len(per_user_rows),
        "methods": method_summary,
        "intermediate_files": {
            "sampled_users": str(intermediates_dir / "sampled_users.jsonl"),
            "candidate_catalog": None if args.skip_catalog_snapshot else str(intermediates_dir / "candidate_catalog.jsonl"),
            "per_user_metrics": str(intermediates_dir / "per_user_metrics.jsonl"),
            "method_recommendations": str(intermediates_dir / "method_recommendations.jsonl"),
        },
        "charts": {
            "ranking_metrics": str(charts_dir / "ranking_metrics.png"),
            "semantic_metrics": str(charts_dir / "kg_semantic_metrics.png"),
            "operational_metrics": str(charts_dir / "operational_metrics.png"),
            "data_readiness": str(charts_dir / "data_readiness.png"),
        },
        "examples": examples,
    }
    write_json(output_dir / "summary.json", summary)
    if args.output_path:
        write_json(args.output_path, summary)
    write_summary_markdown(summary, output_dir / "summary.md")
    plot_metric_group(
        summary,
        charts_dir / "ranking_metrics.png",
        ["recall", "hit_rate", "ndcg", "mrr", "precision"],
        "Top-K Ranking Metrics",
    )
    plot_metric_group(
        summary,
        charts_dir / "kg_semantic_metrics.png",
        ["semantic_ndcg", "semantic_recall", "title_overlap", "attribute_overlap"],
        "KG and Semantic Relevance Metrics",
    )
    plot_metric_group(
        summary,
        charts_dir / "operational_metrics.png",
        ["sellable_rate", "price_coverage", "reason_coverage", "diversity", "catalog_coverage"],
        "Operational Quality Metrics",
        fixed_ylim=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

    recommender.close()
    driver.close()


if __name__ == "__main__":
    main()
