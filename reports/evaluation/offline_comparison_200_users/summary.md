# Offline Recommender Comparison

Created at: `2026-07-06T23:07:41.680381+00:00`
Evaluated users: `200`
Ground-truth scope: `all`

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
Holdout products in candidate catalog: `70` / `400`
Unique holdout products in candidate catalog: `69` / `386`

| Method | Exact Hits@10 | Exact Hits@20 | Exact Hits@50 | Users With Hit@50 |
|---|---:|---:|---:|---:|
| `bm25_history_profile` | 0 | 1 | 3 | 2 |
| `hybrid_rrf` | 4 | 4 | 7 | 6 |
| `item_cf_history` | 3 | 3 | 5 | 5 |
| `kg_attribute_history` | 1 | 3 | 3 | 3 |
| `kg_no_history_home` | 0 | 0 | 0 | 0 |
| `kg_semantic_history` | 1 | 2 | 4 | 3 |
| `popularity_baseline` | 0 | 0 | 0 | 0 |
| `title_keyword_profile` | 1 | 1 | 3 | 3 |
| `transition_history` | 4 | 4 | 5 | 5 |

## Method Metrics

| Method | Recall@10 | Recall@20 | Recall@50 | HitRate@10 | HitRate@20 | HitRate@50 | NDCG@10 | NDCG@20 | NDCG@50 | MRR@10 | Precision@10 | SemanticNDCG@10 | SemanticRecall@10 | TitleOverlap@10 | AttributeOverlap@10 | Diversity@10 | Novelty@10 | Quality@10 | Rating@10 | CatalogCoverage@10 | SellableRate@10 | PriceCoverage@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0000 | 0.0025 | 0.0075 | 0.0000 | 0.0050 | 0.0100 | 0.0000 | 0.0008 | 0.0021 | 0.0000 | 0.0000 | 0.0617 | 0.2652 | 0.0564 | 0.0320 | 0.8838 | 0.6812 | 0.8617 | 0.8028 | 0.0856 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0100 | 0.0100 | 0.0175 | 0.0200 | 0.0200 | 0.0300 | 0.0064 | 0.0064 | 0.0082 | 0.0075 | 0.0020 | 0.0500 | 0.2236 | 0.0456 | 0.0291 | 0.9446 | 0.5886 | 0.9004 | 0.8287 | 0.0627 | 1.0000 | 1.0000 |
| `item_cf_history` | 0.0075 | 0.0075 | 0.0125 | 0.0150 | 0.0150 | 0.0250 | 0.0033 | 0.0033 | 0.0046 | 0.0026 | 0.0015 | 0.0252 | 0.1190 | 0.0209 | 0.0135 | 0.9804 | 0.5626 | 0.8952 | 0.9027 | 0.0317 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0025 | 0.0075 | 0.0075 | 0.0050 | 0.0150 | 0.0150 | 0.0031 | 0.0046 | 0.0046 | 0.0050 | 0.0005 | 0.0469 | 0.2077 | 0.0395 | 0.0315 | 0.9351 | 0.5253 | 0.9530 | 0.8722 | 0.0602 | 1.0000 | 1.0000 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0134 | 0.0657 | 0.0075 | 0.0132 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0025 | 0.0050 | 0.0100 | 0.0050 | 0.0100 | 0.0150 | 0.0013 | 0.0021 | 0.0034 | 0.0013 | 0.0005 | 0.0570 | 0.2565 | 0.0507 | 0.0341 | 0.9286 | 0.5946 | 0.9120 | 0.8407 | 0.0799 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0134 | 0.0657 | 0.0075 | 0.0132 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0025 | 0.0025 | 0.0075 | 0.0050 | 0.0050 | 0.0150 | 0.0011 | 0.0011 | 0.0023 | 0.0008 | 0.0005 | 0.0608 | 0.2603 | 0.0563 | 0.0312 | 0.8815 | 0.6813 | 0.8634 | 0.8014 | 0.0860 | 1.0000 | 1.0000 |
| `transition_history` | 0.0100 | 0.0100 | 0.0125 | 0.0200 | 0.0200 | 0.0250 | 0.0086 | 0.0086 | 0.0092 | 0.0123 | 0.0020 | 0.0258 | 0.1149 | 0.0186 | 0.0133 | 0.9818 | 0.5861 | 0.8868 | 0.9000 | 0.0267 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
- If `Holdout products in candidate catalog` is below 100%, local candidate-based methods cannot possibly hit every exact target.
- `SemanticNDCG@K`, `SemanticRecall@K`, `TitleOverlap@K`, and `AttributeOverlap@K` are softer discovery metrics for cases where the correct answer is a similar product rather than the exact future ASIN.
- `Diversity@K`, `Novelty@K`, and `CatalogCoverage@K` check whether a method collapses to the same narrow set of popular products.
- `SellableRate@K` and `PriceCoverage@K` are constraint checks. They are expected to be near 1.0 because the candidate pool is filtered to recommendable products.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- Non-zero exact-hit scores show that the evaluation target is now aligned with the recommendable product pool.

## Output Files

- `sampled_users`: `reports/evaluation/offline_comparison_200_users/intermediates/sampled_users.jsonl`
- `candidate_catalog`: `reports/evaluation/offline_comparison_200_users/intermediates/candidate_catalog.jsonl`
- `per_user_metrics`: `reports/evaluation/offline_comparison_200_users/intermediates/per_user_metrics.jsonl`
- `method_recommendations`: `reports/evaluation/offline_comparison_200_users/intermediates/method_recommendations.jsonl`
- `ranking_metrics` chart: `reports/evaluation/offline_comparison_200_users/charts/ranking_metrics.png`
- `semantic_metrics` chart: `reports/evaluation/offline_comparison_200_users/charts/kg_semantic_metrics.png`
- `operational_metrics` chart: `reports/evaluation/offline_comparison_200_users/charts/operational_metrics.png`
- `data_readiness` chart: `reports/evaluation/offline_comparison_200_users/charts/data_readiness.png`
