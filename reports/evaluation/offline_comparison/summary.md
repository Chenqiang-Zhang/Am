# Offline Recommender Comparison

Created at: `2026-07-06T22:59:53.537449+00:00`
Evaluated users: `45`
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
Holdout products in candidate catalog: `90` / `90`
Unique holdout products in candidate catalog: `80` / `80`

| Method | Exact Hits@10 | Exact Hits@20 | Exact Hits@50 | Users With Hit@50 |
|---|---:|---:|---:|---:|
| `bm25_history_profile` | 1 | 4 | 8 | 6 |
| `hybrid_rrf` | 8 | 18 | 29 | 23 |
| `item_cf_history` | 7 | 10 | 20 | 16 |
| `kg_attribute_history` | 4 | 7 | 11 | 9 |
| `kg_no_history_home` | 0 | 0 | 0 | 0 |
| `kg_semantic_history` | 2 | 8 | 11 | 8 |
| `popularity_baseline` | 0 | 0 | 0 | 0 |
| `title_keyword_profile` | 1 | 3 | 7 | 5 |
| `transition_history` | 7 | 14 | 21 | 17 |

## Method Metrics

| Method | Recall@10 | Recall@20 | Recall@50 | HitRate@10 | HitRate@20 | HitRate@50 | NDCG@10 | NDCG@20 | NDCG@50 | MRR@10 | Precision@10 | SemanticNDCG@10 | SemanticRecall@10 | TitleOverlap@10 | AttributeOverlap@10 | Diversity@10 | Novelty@10 | Quality@10 | Rating@10 | CatalogCoverage@10 | SellableRate@10 | PriceCoverage@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0111 | 0.0444 | 0.0889 | 0.0222 | 0.0667 | 0.1333 | 0.0086 | 0.0188 | 0.0296 | 0.0111 | 0.0022 | 0.0835 | 0.2312 | 0.0745 | 0.0670 | 0.8448 | 0.6878 | 0.8594 | 0.8371 | 0.0240 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0889 | 0.2000 | 0.3222 | 0.1778 | 0.3556 | 0.5111 | 0.0602 | 0.0936 | 0.1241 | 0.0745 | 0.0178 | 0.1039 | 0.3197 | 0.0915 | 0.0862 | 0.9100 | 0.5748 | 0.8938 | 0.8475 | 0.0137 | 1.0000 | 1.0000 |
| `item_cf_history` | 0.0778 | 0.1111 | 0.2222 | 0.1556 | 0.2222 | 0.3556 | 0.0568 | 0.0678 | 0.0965 | 0.0735 | 0.0156 | 0.0523 | 0.2117 | 0.0418 | 0.0362 | 0.9737 | 0.5452 | 0.8943 | 0.8846 | 0.0097 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0444 | 0.0778 | 0.1222 | 0.0889 | 0.1333 | 0.2000 | 0.0292 | 0.0396 | 0.0507 | 0.0352 | 0.0089 | 0.0853 | 0.2439 | 0.0748 | 0.0730 | 0.8923 | 0.5268 | 0.9164 | 0.8641 | 0.0191 | 0.9778 | 0.9778 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0824 | 0.0090 | 0.0212 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0222 | 0.0889 | 0.1222 | 0.0444 | 0.1333 | 0.1778 | 0.0154 | 0.0358 | 0.0438 | 0.0185 | 0.0044 | 0.0889 | 0.2562 | 0.0791 | 0.0726 | 0.8780 | 0.6103 | 0.8989 | 0.8612 | 0.0219 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0824 | 0.0090 | 0.0212 | 0.9888 | 0.6572 | 0.8520 | 1.0000 | 0.0006 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0111 | 0.0333 | 0.0778 | 0.0222 | 0.0444 | 0.1111 | 0.0049 | 0.0114 | 0.0226 | 0.0037 | 0.0022 | 0.0833 | 0.2173 | 0.0763 | 0.0648 | 0.8247 | 0.6840 | 0.8566 | 0.8363 | 0.0239 | 1.0000 | 1.0000 |
| `transition_history` | 0.0778 | 0.1556 | 0.2333 | 0.1556 | 0.2889 | 0.3778 | 0.0608 | 0.0837 | 0.1033 | 0.0828 | 0.0156 | 0.0515 | 0.2184 | 0.0365 | 0.0347 | 0.9765 | 0.5779 | 0.8967 | 0.8659 | 0.0097 | 1.0000 | 1.0000 |

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
