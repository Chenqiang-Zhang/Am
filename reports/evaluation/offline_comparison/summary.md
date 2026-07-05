# Offline Recommender Comparison

Created at: `2026-07-05T19:32:28.454885+00:00`
Evaluated users: `30`

## Data Readiness

Ready for experiment: `False`

Warnings:
- `recommendable_attribute_coverage_below_20_percent`

| Ratio | Value |
|---|---:|
| `price_coverage` | 15.72% |
| `feature_coverage` | 21.00% |
| `attribute_coverage` | 1.58% |
| `recommendable_rate` | 15.35% |
| `recommendable_attribute_coverage` | 2.76% |

## Method Metrics

| Method | HitRate@K | NDCG@K | MRR@K | Precision@K | TitleOverlap@K | SellableRate@K | PriceCoverage@K |
|---|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0531 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0371 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0119 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0061 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0436 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; zero exact-hit scores mean the experiment did not recover the same future reviewed product in Top-K.
- `title_overlap` is a softer objective proxy for similar-product discovery and is useful while exact behavioral ground truth is sparse.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- `SellableRate@K` and `PriceCoverage@K` verify that comparison methods are not winning by recommending unusable products.

## Output Files

- `sampled_users`: `reports/evaluation/offline_comparison/intermediates/sampled_users.jsonl`
- `candidate_catalog`: `reports/evaluation/offline_comparison/intermediates/candidate_catalog.jsonl`
- `per_user_metrics`: `reports/evaluation/offline_comparison/intermediates/per_user_metrics.jsonl`
- `method_recommendations`: `reports/evaluation/offline_comparison/intermediates/method_recommendations.jsonl`
- `metrics` chart: `reports/evaluation/offline_comparison/charts/metrics_comparison.png`
- `data_readiness` chart: `reports/evaluation/offline_comparison/charts/data_readiness.png`
