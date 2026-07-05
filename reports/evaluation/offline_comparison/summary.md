# Offline Recommender Comparison

Created at: `2026-07-05T20:23:52.264671+00:00`
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

## Method Metrics

| Method | Recall@K | HitRate@K | NDCG@K | MRR@K | Precision@K | SemanticNDCG@K | SemanticRecall@K | TitleOverlap@K | AttributeOverlap@K | Diversity@K | CatalogCoverage@K | SellableRate@K | PriceCoverage@K |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0794 | 0.1993 | 0.0719 | 0.0753 | 0.8592 | 0.0331 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0167 | 0.0333 | 0.0064 | 0.0042 | 0.0033 | 0.0828 | 0.2209 | 0.0761 | 0.0788 | 0.8674 | 0.0331 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0500 | 0.1000 | 0.0365 | 0.0472 | 0.0100 | 0.0805 | 0.2438 | 0.0661 | 0.0766 | 0.8886 | 0.0300 | 0.9667 | 0.9667 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.0013 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0333 | 0.0667 | 0.0273 | 0.0381 | 0.0067 | 0.0830 | 0.2346 | 0.0709 | 0.0728 | 0.9099 | 0.0329 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.0013 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0824 | 0.2013 | 0.0755 | 0.0769 | 0.8429 | 0.0326 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
- `SemanticNDCG@K`, `SemanticRecall@K`, `TitleOverlap@K`, and `AttributeOverlap@K` are softer discovery metrics for cases where the correct answer is a similar product rather than the exact future ASIN.
- `Diversity@K` and `CatalogCoverage@K` check whether a method collapses to the same narrow set of products.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- `SellableRate@K` and `PriceCoverage@K` verify that comparison methods are not winning by recommending unusable products.
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
