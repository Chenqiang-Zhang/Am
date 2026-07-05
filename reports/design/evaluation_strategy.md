# Evaluation Strategy for Demo and Presentation

## Immediate Evaluation Scope

The first evaluation target is history-aware recommendation for users who already have review history in the graph. This avoids relying on online click/purchase data that the project does not yet have.

## Offline Split

For each user with enough reviews:

```text
earlier reviewed products -> history profile
later reviewed products   -> held-out ground truth
```

The KG-history method extracts attributes from the history products, recommends products from the graph, and checks whether held-out products appear in the top K.

## Methods to Compare First

| Method | Purpose |
|---|---|
| Popularity baseline | Checks whether the system beats simple popular/high-rating recommendation |
| KG history profile | Checks whether review history improves recommendation relevance |

Later comparison methods can add BM25, vector search, KG without history, and full hybrid ranking.

## Metrics

| Metric | Meaning |
|---|---|
| `HitRate@K` | Whether at least one held-out product appears in Top-K |
| `Precision@K` | Fraction of Top-K recommendations that are held-out products |
| `MRR@K` | How early the first held-out product appears |
| `NDCG@K` | Whether relevant products rank higher |
| `SellableRate@K` | Fraction of Top-K products that are available and high-quality |
| `ReasonCoverage@K` | Fraction of recommendations with graph-backed reasons |

The initial script implements HitRate, Precision, MRR, and NDCG. SellableRate and ReasonCoverage can be added once the comparison set expands.

## Command

```bash
conda run -n py312 python scripts/evaluate_recommenders_offline.py \
  --sample-users 30 \
  --min-reviews 4 \
  --history-size 3 \
  --holdout-size 2 \
  --k 10
```

Output:

```text
reports/evaluation/offline_history_eval.json
```

## How to Present the Value

The system should not be framed as "more accurate than every mature recommender." A stronger claim is:

> The system improves explainable product discovery by combining natural-language understanding, KG evidence, product quality controls, user history, and review-derived signals.

For new users and multi-turn conversation without history, use qualitative demo cases for now and describe full online evaluation as future work.
