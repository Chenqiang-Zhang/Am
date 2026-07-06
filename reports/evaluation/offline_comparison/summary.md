# Offline Recommender Comparison

Created at: `2026-07-06T08:20:32.640888+00:00`
Evaluated users: `30`
Ground-truth scope: `recommendable`

## Data Readiness

Ready for experiment: `True`

| Ratio | Value |
|---|---:|
| `price_coverage` | 15.72% |
| `feature_coverage` | 21.00% |
| `attribute_coverage` | 97.18% |
| `recommendable_rate` | 15.35% |
| `recommendable_attribute_coverage` | 98.95% |

## Ranking Diagnostics

Candidate catalog size: `17,280`
Holdout products in candidate catalog: `60` / `60`
Unique holdout products in candidate catalog: `55` / `55`

| Method | Exact Hits | Users With Hit |
|---|---:|---:|
| `bm25_history_profile` | 1 | 1 |
| `hybrid_rrf` | 1 | 1 |
| `kg_attribute_history` | 3 | 3 |
| `kg_no_history_home` | 0 | 0 |
| `kg_semantic_history` | 2 | 2 |
| `popularity_baseline` | 0 | 0 |
| `title_keyword_profile` | 1 | 1 |

## Method Metrics

| Method | Recall@K | HitRate@K | NDCG@K | MRR@K | Precision@K | SemanticNDCG@K | SemanticRecall@K | TitleOverlap@K | AttributeOverlap@K | Diversity@K | Novelty@K | Quality@K | Rating@K | CatalogCoverage@K | SellableRate@K | PriceCoverage@K |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0167 | 0.0333 | 0.0129 | 0.0167 | 0.0033 | 0.0805 | 0.2238 | 0.0690 | 0.0754 | 0.8381 | 0.6994 | 0.8567 | 0.8319 | 0.0166 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0167 | 0.0333 | 0.0088 | 0.0083 | 0.0033 | 0.0771 | 0.2181 | 0.0680 | 0.0744 | 0.8572 | 0.6601 | 0.8738 | 0.8439 | 0.0163 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0500 | 0.1000 | 0.0220 | 0.0172 | 0.0100 | 0.0749 | 0.2348 | 0.0647 | 0.0771 | 0.8865 | 0.4986 | 0.8956 | 0.8561 | 0.0128 | 0.9667 | 0.9667 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0155 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0333 | 0.0667 | 0.0144 | 0.0108 | 0.0067 | 0.0787 | 0.2414 | 0.0712 | 0.0759 | 0.9019 | 0.6002 | 0.9030 | 0.8597 | 0.0149 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0155 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0167 | 0.0333 | 0.0073 | 0.0056 | 0.0033 | 0.0787 | 0.1987 | 0.0710 | 0.0702 | 0.8065 | 0.6912 | 0.8508 | 0.8341 | 0.0166 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
- If `Holdout products in candidate catalog` is below 100%, local candidate-based methods cannot possibly hit every exact target.
- `SemanticNDCG@K`, `SemanticRecall@K`, `TitleOverlap@K`, and `AttributeOverlap@K` are softer discovery metrics for cases where the correct answer is a similar product rather than the exact future ASIN.
- `Diversity@K`, `Novelty@K`, and `CatalogCoverage@K` check whether a method collapses to the same narrow set of popular products.
- `SellableRate@K` and `PriceCoverage@K` are constraint checks. They are expected to be near 1.0 because the candidate pool is filtered to recommendable products.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- Non-zero exact-hit scores show that the evaluation target is now aligned with the recommendable product pool.

## Output Files

- `sampled_users`: `reports/evaluation/offline_comparison/intermediates/sampled_users.jsonl`
- `candidate_catalog`: `reports/evaluation/offline_comparison/intermediates/candidate_catalog.jsonl`
- `per_user_metrics`: `reports/evaluation/offline_comparison/intermediates/per_user_metrics.jsonl`
- `method_recommendations`: `reports/evaluation/offline_comparison/intermediates/method_recommendations.jsonl`
- `ranking_metrics` chart: `reports/evaluation/offline_comparison/charts/ranking_metrics.png`
- `semantic_metrics` chart: `reports/evaluation/offline_comparison/charts/kg_semantic_metrics.png`
- `operational_metrics` chart: `reports/evaluation/offline_comparison/charts/operational_metrics.png`
- `data_readiness` chart: `reports/evaluation/offline_comparison/charts/data_readiness.png`
