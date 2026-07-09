# Offline Recommender Comparison

Created at: `2026-07-07T17:40:57.057591+00:00`
Evaluated users: `200`
Ground-truth scope: `discoverable`
Effective candidate pool: `discoverable`

## Data Readiness

Ready for experiment: `True`

| Ratio | Value |
|---|---:|
| `price_coverage` | 15.72% |
| `feature_coverage` | 21.00% |
| `attribute_coverage` | 97.18% |
| `recommendable_rate` | 15.35% |
| `discoverable_pool_rate` | 96.70% |
| `recommendable_attribute_coverage` | 98.95% |

## Ranking Diagnostics

Candidate catalog size: `20,000`
Holdout products in candidate catalog: `31` / `400`
Unique holdout products in candidate catalog: `31` / `385`

| Method | Exact Hits@10 | Exact Hits@20 | Exact Hits@50 | Users With Hit@50 |
|---|---:|---:|---:|---:|
| `bm25_history_profile` | 1 | 1 | 1 | 1 |
| `hybrid_rrf` | 10 | 15 | 27 | 27 |
| `item_cf_history` | 7 | 13 | 21 | 19 |
| `kg_attribute_history` | 2 | 3 | 4 | 3 |
| `kg_no_history_home` | 0 | 0 | 0 | 0 |
| `kg_semantic_history` | 3 | 3 | 5 | 4 |
| `popularity_baseline` | 0 | 0 | 0 | 0 |
| `title_keyword_profile` | 1 | 1 | 1 | 1 |
| `transition_history` | 13 | 19 | 27 | 27 |

## Method Metrics

| Method | Recall@10 | Recall@20 | Recall@50 | HitRate@10 | HitRate@20 | HitRate@50 | NDCG@10 | NDCG@20 | NDCG@50 | MRR@10 | Precision@10 | SemanticNDCG@10 | SemanticRecall@10 | TitleOverlap@10 | AttributeOverlap@10 | Diversity@10 | Novelty@10 | Quality@10 | Rating@10 | CatalogCoverage@10 | DiscoverableRate@10 | SellableRate@10 | PriceCoverage@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bm25_history_profile` | 0.0025 | 0.0025 | 0.0025 | 0.0050 | 0.0050 | 0.0050 | 0.0031 | 0.0031 | 0.0031 | 0.0050 | 0.0005 | 0.0701 | 0.2846 | 0.0646 | 0.0345 | 0.8376 | 0.8174 | 0.7219 | 0.9834 | 0.0793 | 0.8960 | 0.1040 | 0.1040 |
| `hybrid_rrf` | 0.0250 | 0.0375 | 0.0675 | 0.0500 | 0.0750 | 0.1350 | 0.0185 | 0.0223 | 0.0293 | 0.0243 | 0.0050 | 0.0601 | 0.2595 | 0.0540 | 0.0342 | 0.9404 | 0.6035 | 0.7653 | 0.8606 | 0.0704 | 0.8080 | 0.1920 | 0.1920 |
| `item_cf_history` | 0.0175 | 0.0325 | 0.0525 | 0.0350 | 0.0600 | 0.0950 | 0.0087 | 0.0135 | 0.0182 | 0.0080 | 0.0035 | 0.0348 | 0.1630 | 0.0304 | 0.0178 | 0.9804 | 0.5430 | 0.7844 | 0.8720 | 0.0522 | 0.7285 | 0.2715 | 0.2715 |
| `kg_attribute_history` | 0.0050 | 0.0075 | 0.0100 | 0.0100 | 0.0150 | 0.0150 | 0.0035 | 0.0043 | 0.0050 | 0.0042 | 0.0010 | 0.0493 | 0.2182 | 0.0421 | 0.0356 | 0.9323 | 0.4896 | 0.9905 | 0.8717 | 0.0548 | 0.0000 | 1.0000 | 1.0000 |
| `kg_no_history_home` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0076 | 0.0431 | 0.0066 | 0.0029 | 0.9875 | 0.5840 | 0.8357 | 1.0000 | 0.0005 | 0.5000 | 0.5000 | 0.5000 |
| `kg_semantic_history` | 0.0075 | 0.0075 | 0.0125 | 0.0150 | 0.0150 | 0.0200 | 0.0042 | 0.0042 | 0.0054 | 0.0043 | 0.0015 | 0.0613 | 0.2760 | 0.0567 | 0.0374 | 0.9237 | 0.6416 | 0.8630 | 0.9303 | 0.0717 | 0.4215 | 0.5785 | 0.5785 |
| `popularity_baseline` | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0076 | 0.0431 | 0.0066 | 0.0029 | 0.9875 | 0.5840 | 0.8357 | 1.0000 | 0.0005 | 0.5000 | 0.5000 | 0.5000 |
| `title_keyword_profile` | 0.0025 | 0.0025 | 0.0025 | 0.0050 | 0.0050 | 0.0050 | 0.0031 | 0.0031 | 0.0031 | 0.0050 | 0.0005 | 0.0693 | 0.2785 | 0.0638 | 0.0332 | 0.8358 | 0.8211 | 0.7147 | 0.9839 | 0.0788 | 0.9005 | 0.0995 | 0.0995 |
| `transition_history` | 0.0325 | 0.0475 | 0.0675 | 0.0650 | 0.0950 | 0.1350 | 0.0208 | 0.0254 | 0.0303 | 0.0243 | 0.0065 | 0.0404 | 0.1870 | 0.0341 | 0.0209 | 0.9799 | 0.5608 | 0.7821 | 0.8382 | 0.0544 | 0.7405 | 0.2595 | 0.2595 |

## Interpretation Notes

- Exact held-out ASIN prediction is intentionally strict; low HitRate/NDCG/MRR means the experiment rarely recovers the same future reviewed product in Top-K.
- If `Holdout products in candidate catalog` is below 100%, local candidate-based methods cannot possibly hit every exact target.
- `SemanticNDCG@K`, `SemanticRecall@K`, `TitleOverlap@K`, and `AttributeOverlap@K` are softer discovery metrics for cases where the correct answer is a similar product rather than the exact future ASIN.
- `Diversity@K`, `Novelty@K`, and `CatalogCoverage@K` check whether a method collapses to the same narrow set of popular products.
- `DiscoverableRate@K` measures how often a method recommends information-complete but currently unavailable products in research runs.
- `SellableRate@K` and `PriceCoverage@K` are constraint checks. They should be near 1.0 for the online recommendable pool and can be lower when `effective_candidate_pool` is `discoverable`.
- Low `recommendable_attribute_coverage` means KG attribute-history methods are limited by current LLM attribute coverage, not only by ranking quality.
- Non-zero exact-hit scores show that the evaluation target is aligned with the selected candidate pool.

## Output Files

- `sampled_users`: `reports/evaluation/offline_comparison_discoverable/intermediates/sampled_users.jsonl`
- `candidate_catalog`: `reports/evaluation/offline_comparison_discoverable/intermediates/candidate_catalog.jsonl`
- `per_user_metrics`: `reports/evaluation/offline_comparison_discoverable/intermediates/per_user_metrics.jsonl`
- `method_recommendations`: `reports/evaluation/offline_comparison_discoverable/intermediates/method_recommendations.jsonl`
- `ranking_metrics` chart: `reports/evaluation/offline_comparison_discoverable/charts/ranking_metrics.png`
- `semantic_metrics` chart: `reports/evaluation/offline_comparison_discoverable/charts/kg_semantic_metrics.png`
- `operational_metrics` chart: `reports/evaluation/offline_comparison_discoverable/charts/operational_metrics.png`
- `data_readiness` chart: `reports/evaluation/offline_comparison_discoverable/charts/data_readiness.png`
