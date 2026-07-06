# Offline Recommender Comparison

Created at: `2026-07-06T09:03:03.436473+00:00`
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

| Method | Exact Hits@10 | Exact Hits@20 | Exact Hits@50 | Users With Hit@50 |
|---|---:|---:|---:|---:|
| `bm25_history_profile` | 1 | 2 | 5 | 4 |
| `hybrid_rrf` | 4 | 10 | 18 | 14 |
| `item_cf_history` | 4 | 6 | 13 | 10 |
| `kg_attribute_history` | 4 | 4 | 6 | 5 |
| `kg_no_history_home` | 0 | 0 | 0 | 0 |
| `kg_semantic_history` | 4 | 5 | 8 | 6 |
| `popularity_baseline` | 0 | 0 | 0 | 0 |
| `title_keyword_profile` | 1 | 1 | 4 | 3 |
| `transition_history` | 3 | 9 | 14 | 11 |

## Method Metrics

| Method | Recall@10 | Recall@20 | Recall@50 | HitRate@10 | HitRate@20 | HitRate@50 | NDCG@10 | NDCG@20 | NDCG@50 | MRR@10 | Precision@10 | SemanticNDCG@10 | SemanticRecall@10 | TitleOverlap@10 | AttributeOverlap@10 | Diversity@10 | Novelty@10 | Quality@10 | Rating@10 | CatalogCoverage@10 | SellableRate@10 | PriceCoverage@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0167 | 0.0333 | 0.0833 | 0.0333 | 0.0667 | 0.1333 | 0.0129 | 0.0183 | 0.0302 | 0.0167 | 0.0033 | 0.0805 | 0.2238 | 0.0690 | 0.0754 | 0.8381 | 0.6994 | 0.8567 | 0.8319 | 0.0166 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0667 | 0.1667 | 0.3000 | 0.1333 | 0.3000 | 0.4667 | 0.0626 | 0.0926 | 0.1249 | 0.0917 | 0.0133 | 0.1003 | 0.2907 | 0.0825 | 0.0906 | 0.9108 | 0.5719 | 0.8896 | 0.8446 | 0.0098 | 1.0000 | 1.0000 |
| `item_cf_history` | 0.0667 | 0.1000 | 0.2167 | 0.1333 | 0.2000 | 0.3333 | 0.0626 | 0.0736 | 0.1036 | 0.0917 | 0.0133 | 0.0572 | 0.2050 | 0.0429 | 0.0387 | 0.9695 | 0.5399 | 0.8957 | 0.8781 | 0.0080 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0667 | 0.0667 | 0.1000 | 0.1333 | 0.1333 | 0.1667 | 0.0444 | 0.0444 | 0.0526 | 0.0539 | 0.0133 | 0.0846 | 0.2622 | 0.0701 | 0.0810 | 0.8855 | 0.4880 | 0.9016 | 0.8636 | 0.0125 | 0.9667 | 0.9667 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0155 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0667 | 0.0833 | 0.1333 | 0.1333 | 0.1333 | 0.2000 | 0.0320 | 0.0374 | 0.0494 | 0.0289 | 0.0133 | 0.0904 | 0.2783 | 0.0821 | 0.0923 | 0.8802 | 0.5999 | 0.8961 | 0.8587 | 0.0149 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0155 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0167 | 0.0167 | 0.0667 | 0.0333 | 0.0333 | 0.1000 | 0.0073 | 0.0073 | 0.0196 | 0.0056 | 0.0033 | 0.0787 | 0.1987 | 0.0710 | 0.0702 | 0.8065 | 0.6912 | 0.8508 | 0.8341 | 0.0166 | 1.0000 | 1.0000 |
| `transition_history` | 0.0500 | 0.1500 | 0.2333 | 0.1000 | 0.2667 | 0.3667 | 0.0468 | 0.0763 | 0.0975 | 0.0700 | 0.0100 | 0.0470 | 0.1913 | 0.0308 | 0.0311 | 0.9763 | 0.5723 | 0.8956 | 0.8653 | 0.0076 | 1.0000 | 1.0000 |

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
