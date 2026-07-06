# Offline Recommender Comparison

Created at: `2026-07-06T00:33:29.732179+00:00`
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

| Method | Recall@K | HitRate@K | NDCG@K | MRR@K | Precision@K | SemanticNDCG@K | SemanticRecall@K | TitleOverlap@K | AttributeOverlap@K | Diversity@K | Novelty@K | Quality@K | Rating@K | CatalogCoverage@K | SellableRate@K | PriceCoverage@K |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0794 | 0.1993 | 0.0719 | 0.0753 | 0.8592 | 0.6763 | 0.8590 | 0.9371 | 0.0331 | 1.0000 | 1.0000 |
| `hybrid_rrf` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0804 | 0.2041 | 0.0731 | 0.0750 | 0.8673 | 0.6348 | 0.8742 | 0.9275 | 0.0325 | 1.0000 | 1.0000 |
| `kg_attribute_history` | 0.0333 | 0.0667 | 0.0161 | 0.0139 | 0.0067 | 0.0699 | 0.2049 | 0.0597 | 0.0676 | 0.8924 | 0.4526 | 0.8959 | 0.8589 | 0.0283 | 0.9667 | 0.9667 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6287 | 0.8520 | 1.0000 | 0.0013 | 1.0000 | 1.0000 |
| `kg_semantic_history` | 0.0167 | 0.0333 | 0.0068 | 0.0048 | 0.0033 | 0.0744 | 0.2115 | 0.0665 | 0.0697 | 0.9100 | 0.5631 | 0.9010 | 0.9111 | 0.0316 | 1.0000 | 1.0000 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0154 | 0.0843 | 0.0085 | 0.0240 | 0.9888 | 0.6287 | 0.8520 | 1.0000 | 0.0013 | 1.0000 | 1.0000 |
| `title_keyword_profile` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0824 | 0.2013 | 0.0755 | 0.0769 | 0.8429 | 0.6590 | 0.8608 | 0.9342 | 0.0326 | 1.0000 | 1.0000 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
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
