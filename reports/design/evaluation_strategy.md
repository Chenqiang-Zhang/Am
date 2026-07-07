# Evaluation Strategy for Demo and Presentation

## Immediate Evaluation Scope

The first evaluation target is history-aware recommendation for users who already have review history in the graph. This avoids relying on online click/purchase data that the project does not yet have.

## Offline Split

For each user with enough reviews:

```text
earlier reviewed products -> history profile
later reviewed products   -> held-out ground truth
```

By default, the split uses only products that are also eligible for the recommendation pool:

```text
sellable_status = available
data_quality_score >= 0.6
```

This matters because using all reviewed products as ground truth can make exact-hit metrics artificially zero: many reviewed products are missing price, marked low-quality, or otherwise filtered out by the production recommender.

The KG-history method extracts attributes from the history products, recommends products from the graph, and checks whether held-out products appear in the top K.

## Methods Compared

| Method | Purpose |
|---|---|
| Popularity baseline | Checks whether the system beats simple popular/high-rating recommendation |
| KG no-history home | Uses the current backend home recommender without user history |
| Title keyword profile | Builds a simple keyword profile from the user's historical product titles |
| BM25 history profile | Runs lexical BM25 over a sellable/high-quality candidate catalog |
| KG attribute history | Extracts graph attributes from historical products and recalls related products |
| Hybrid RRF | Combines BM25, title keywords, KG history, KG no-history, and popularity via reciprocal rank fusion |

Vector search is still a future comparison once product embeddings are available.

## Metrics

| Metric | Meaning |
|---|---|
| `HitRate@K` | Whether at least one held-out product appears in Top-K |
| `Precision@K` | Fraction of Top-K recommendations that are held-out products |
| `MRR@K` | How early the first held-out product appears |
| `NDCG@K` | Whether relevant products rank higher |
| `TitleOverlap@K` | Average token overlap between recommended titles and held-out product titles |
| `SellableRate@K` | Fraction of Top-K products that are available and high-quality |
| `ReasonCoverage@K` | Fraction of recommendations with graph-backed reasons |

The script implements HitRate, Precision, MRR, NDCG, TitleOverlap, SellableRate, PriceCoverage, and ReasonCoverage. Exact held-out ASIN prediction is intentionally kept as a strict metric; TitleOverlap is a softer objective proxy for similar-product discovery.

## Data Readiness Check

Before running the comparison, `scripts/evaluate_recommenders_offline.py` checks whether the current graph is suitable for the experiment. It records:

- total products, users, reviews, and rating edges
- products with price, title, image, features, and LLM attributes
- recommendable products after sellability and quality filters
- eligible users with enough review history
- review mention relationship coverage

The current local graph is usable for baseline/BM25/history-title experiments, but KG attribute history is data-limited: only a small fraction of recommendable products currently have LLM attributes. This should be reported as a data limitation, not hidden.

## Command

```bash
conda run -n py312 python scripts/evaluate_recommenders_offline.py \
  --sample-users 30 \
  --min-reviews 4 \
  --history-size 3 \
  --holdout-size 2 \
  --k 10 \
  --candidate-catalog-limit 8000 \
  --ground-truth-scope recommendable
```

Output:

```text
reports/evaluation/offline_comparison/
├── data_readiness.json
├── data_readiness.md
├── summary.json
├── summary.md
├── charts/
│   ├── data_readiness.png
│   └── metrics_comparison.png
└── intermediates/
    ├── sampled_users.jsonl
    ├── candidate_catalog.jsonl
    ├── per_user_metrics.jsonl
    └── method_recommendations.jsonl
```

All visualization text is in English for presentation reuse.

## How to Present the Value

The system should not be framed as "more accurate than every mature recommender." A stronger claim is:

> The system improves explainable product discovery by combining natural-language understanding, KG evidence, product quality controls, user history, and review-derived signals.

For new users and multi-turn conversation without history, use qualitative demo cases for now and describe full online evaluation as future work.
