# Offline Recommender Comparison

Created at: `2026-07-05T19:49:19.492980+00:00`
Evaluated users: `30`
Ground-truth scope: `recommendable`

## Data Readiness

Ready for experiment: `False`

Warnings:
- `recommendable_attribute_coverage_below_20_percent`

| Ratio | Value |
|---|---:|
| `attribute_coverage` | 1.58% |
| `feature_coverage` | 21.00% |
| `price_coverage` | 15.72% |
| `recommendable_attribute_coverage` | 2.76% |
| `recommendable_rate` | 15.35% |

## Method Metrics

| Method | HitRate@K | NDCG@K | MRR@K | Precision@K | TitleOverlap@K | SellableRate@K | PriceCoverage@K |
|---|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0333 | 0.0064 | 0.0042 | 0.0033 | 0.0894 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0333 | 0.0088 | 0.0083 | 0.0033 | 0.0523 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0019 | 0.1667 | 0.1667 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0139 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0086 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0667 | 0.0283 | 0.0400 | 0.0067 | 0.0618 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
- `title_overlap` is a softer objective proxy for similar-product discovery and is useful while exact behavioral ground truth is sparse.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- `SellableRate@K` and `PriceCoverage@K` verify that comparison methods are not winning by recommending unusable products.
- Non-zero exact-hit scores show that the evaluation target is now aligned with the recommendable product pool.

## Output Files

- `candidate_catalog`: `reports/evaluation/offline_comparison/intermediates/candidate_catalog.jsonl`
- `method_recommendations`: `reports/evaluation/offline_comparison/intermediates/method_recommendations.jsonl`
- `per_user_metrics`: `reports/evaluation/offline_comparison/intermediates/per_user_metrics.jsonl`
- `sampled_users`: `reports/evaluation/offline_comparison/intermediates/sampled_users.jsonl`
- `data_readiness` chart: `reports/evaluation/offline_comparison/charts/data_readiness.png`
- `metrics` chart: `reports/evaluation/offline_comparison/charts/metrics_comparison.png`
