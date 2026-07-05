from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Any

from neo4j import GraphDatabase

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.models import AttributeFilter, SearchIntent
from api.recommender import Recommender, _load_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline evaluation for history-aware recommendation using review history."
    )
    parser.add_argument("--sample-users", type=int, default=30)
    parser.add_argument("--min-reviews", type=int, default=4)
    parser.add_argument("--history-size", type=int, default=3)
    parser.add_argument("--holdout-size", type=int, default=2)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--output-path", type=Path, default=Path("reports/evaluation/offline_history_eval.json"))
    parser.add_argument("--min-quality-score", type=float, default=0.6)
    return parser.parse_args()


def pick_users(driver: Any, database: str, sample_users: int, min_reviews: int) -> list[str]:
    cypher = """
MATCH (u:User)-[r:RATED]->(p:Product)
WHERE r.timestamp IS NOT NULL
WITH u, count(DISTINCT p) AS product_count
WHERE product_count >= $min_reviews
RETURN u.user_id AS user_id
ORDER BY rand()
LIMIT $sample_users
"""
    with driver.session(database=database) as session:
        return [record["user_id"] for record in session.run(cypher, min_reviews=min_reviews, sample_users=sample_users)]


def user_review_sequence(driver: Any, database: str, user_id: str) -> list[dict[str, Any]]:
    cypher = """
MATCH (:User {user_id: $user_id})-[r:RATED]->(p:Product)
RETURN p.product_id AS product_id,
       p.title AS title,
       r.timestamp AS timestamp,
       toFloat(r.rating) AS rating
ORDER BY r.timestamp ASC
"""
    with driver.session(database=database) as session:
        return [dict(record) for record in session.run(cypher, user_id=user_id)]


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


def popularity_baseline(driver: Any, database: str, k: int, min_quality_score: float) -> list[str]:
    cypher = """
MATCH (p:Product)
WHERE coalesce(p.sellable_status, CASE WHEN p.price IS NOT NULL THEN "available" ELSE "currently_unavailable" END) = "available"
  AND coalesce(toFloat(p.data_quality_score), CASE WHEN p.price IS NOT NULL THEN 0.6 ELSE 0.0 END) >= $min_quality_score
RETURN p.product_id AS product_id
ORDER BY coalesce(toFloat(p.average_rating), 3.8) DESC, coalesce(toInteger(p.rating_number), 0) DESC
LIMIT $k
"""
    with driver.session(database=database) as session:
        return [record["product_id"] for record in session.run(cypher, k=k, min_quality_score=min_quality_score)]


def metrics(recommended: list[str], relevant: set[str], k: int) -> dict[str, float]:
    top_k = recommended[:k]
    hits = [1 if product_id in relevant else 0 for product_id in top_k]
    hit_rate = 1.0 if any(hits) else 0.0
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
    return {
        "hit_rate": hit_rate,
        "precision": precision,
        "mrr": reciprocal_rank,
        "ndcg": ndcg,
    }


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {"hit_rate": 0.0, "precision": 0.0, "mrr": 0.0, "ndcg": 0.0}
    return {key: round(mean(row[key] for row in rows), 4) for key in rows[0]}


def main() -> None:
    args = parse_args()
    _load_env()
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
    )
    recommender = Recommender()

    users = pick_users(driver, database, args.sample_users, args.min_reviews)
    popularity = popularity_baseline(driver, database, args.k, args.min_quality_score)
    baseline_rows: list[dict[str, float]] = []
    history_rows: list[dict[str, float]] = []
    examples: list[dict[str, Any]] = []

    for user_id in users:
        sequence = user_review_sequence(driver, database, user_id)
        if len(sequence) < args.history_size + 1:
            continue
        history = sequence[: args.history_size]
        holdout = sequence[args.history_size : args.history_size + args.holdout_size]
        relevant = {row["product_id"] for row in holdout}
        intent = history_intent(driver, database, [row["product_id"] for row in history])
        plan = recommender.make_query_plan("[offline_history_eval]", intent, user_id=None)
        recs = recommender.search_products(intent, args.k, "en", user_id=None, query_plan=plan)
        history_product_ids = [rec.product_id for rec in recs]

        baseline_metric = metrics(popularity, relevant, args.k)
        history_metric = metrics(history_product_ids, relevant, args.k)
        baseline_rows.append(baseline_metric)
        history_rows.append(history_metric)
        if len(examples) < 5:
            examples.append(
                {
                    "user_id": user_id,
                    "history_products": [row["product_id"] for row in history],
                    "holdout_products": sorted(relevant),
                    "history_intent": intent.model_dump(),
                    "history_recommendations": history_product_ids,
                    "baseline_metrics": baseline_metric,
                    "history_metrics": history_metric,
                }
            )

    report = {
        "config": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "evaluated_users": len(history_rows),
        "methods": {
            "popularity_baseline": aggregate(baseline_rows),
            "kg_history_profile": aggregate(history_rows),
        },
        "examples": examples,
    }
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    recommender.close()
    driver.close()


if __name__ == "__main__":
    main()
