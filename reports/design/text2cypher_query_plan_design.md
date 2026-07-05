# Controlled Query Plan Design

## Problem

The previous discussion described "using past behavior and search input to let an LLM generate Cypher." That design is flexible, but it is too coarse for a research demo because raw LLM-generated Cypher is hard to audit, difficult to keep compatible with the frontend, and risky when the graph schema changes.

## Updated Design

The backend now uses a controlled query-plan layer:

```text
Natural language + optional user history
-> LLM / heuristic SearchIntent extraction
-> backend-owned QueryPlan
-> allow-listed query actions
-> fixed Cypher templates
-> candidate merge, filtering, ranking, explanation
```

The LLM is allowed to extract intent, but it is not allowed to execute or write arbitrary Cypher. The API response includes `query_plan`, so demos can show which backend actions were selected.

## API Action Design

Allowed actions are:

| Action | Purpose | Cypher Ownership |
|---|---|---|
| `attribute_recall` | Match `Product-[:HAS_ATTRIBUTE]->Attribute` | Fixed backend template |
| `feature_text_recall` | Match product feature/description text | Fixed backend template |
| `field_recall` | Match title/category/store fallback fields | Fixed backend template |
| `filter_available` | Keep only available products | Embedded backend filter |
| `filter_quality` | Keep products above `data_quality_score` threshold | Embedded backend filter |
| `apply_price_ceiling` | Apply user max price | Parameterized filter |
| `apply_min_rating` | Apply user min rating | Parameterized filter |
| `apply_user_history_boost` | Boost products sharing attributes with positive behavior history | Fixed backend template |
| `apply_review_mention_ranking` | Use review sentiment mentions when available | Fixed backend template |
| `deduplicate_products` | Remove duplicate IDs/titles | Python post-processing |
| `rerank_hybrid` | Merge final ranking signals | Python ranking |

## Why This Is Better

- Safer: raw LLM Cypher is never executed.
- More reproducible: every action maps to a stable backend template.
- Easier to evaluate: the `query_plan` records recall, filter, and ranking decisions.
- Easier to explain: recommendation reasons can be tied to selected actions and graph evidence.
- Frontend-compatible: the response shape is explicit and can be contract-tested.

## Current Implementation

- `api/query_plan.py` defines the allow-listed actions and plan builder.
- `/recommend`, `/recommend/home`, and search-mode `/chat` responses now include `query_plan`.
- `api/recommender.py` uses the plan to decide whether to run attribute, feature-text, field, user-history, and review-mention paths.

## Future Work

- Add a dedicated `/query-plan/preview` endpoint for demo/debug mode.
- Add saved query-plan logs for offline error analysis.
- Extend action weights so evaluation can compare rule-only, KG-only, history-only, and hybrid plans.
