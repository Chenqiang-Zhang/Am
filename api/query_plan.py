from __future__ import annotations

from typing import Iterable

from .models import QueryAction, QueryPlan, SearchIntent


ALLOWED_QUERY_ACTIONS = {
    "attribute_recall",
    "feature_text_recall",
    "field_recall",
    "filter_available",
    "filter_quality",
    "apply_price_ceiling",
    "apply_min_rating",
    "apply_user_history_boost",
    "apply_review_mention_ranking",
    "deduplicate_products",
    "rerank_hybrid",
}


ACTION_CYPHER_TEMPLATES = {
    "attribute_recall": "ATTRIBUTE_SEARCH_CYPHER",
    "feature_text_recall": "FEATURE_SEARCH_CYPHER",
    "field_recall": "FIELD_SEARCH_CYPHER",
    "filter_available": "embedded_in_recall_where_clause",
    "filter_quality": "embedded_in_recall_where_clause",
    "apply_user_history_boost": "USER_BEHAVIOR_ATTRIBUTE_CONTEXT",
    "apply_review_mention_ranking": "REVIEW_MENTIONS_CONTEXT",
}


def build_controlled_query_plan(
    user_input: str,
    intent: SearchIntent,
    user_id: str | None = None,
    history_terms: Iterable[str] | None = None,
    min_quality_score: float = 0.6,
) -> QueryPlan:
    """Build a backend-owned plan instead of asking an LLM to write Cypher.

    The LLM may produce SearchIntent, but Cypher execution is restricted to the
    action names in ALLOWED_QUERY_ACTIONS and the fixed templates they map to.
    This makes natural-language and history-driven recommendation auditable.
    """
    actions: list[QueryAction] = []
    constraints: dict[str, str | int | float | bool | None | list[str]] = {
        "sellable_status": "available",
        "min_quality_score": min_quality_score,
        "price_max": intent.price_max,
        "min_rating": intent.min_rating,
        "attribute_filter_count": len(intent.attribute_filters),
        "keyword_count": len(intent.keywords),
    }
    history_terms = [term for term in (history_terms or []) if term]
    if history_terms:
        constraints["history_terms"] = list(dict.fromkeys(history_terms))[:10]

    if intent.attribute_filters:
        actions.append(
            _action(
                "attribute_recall",
                "Use structured Attribute nodes extracted from product metadata for precise matches.",
            )
        )

    if intent.keywords or intent.attribute_filters:
        actions.append(
            _action(
                "feature_text_recall",
                "Use product feature and description text as a broader lexical recall path.",
            )
        )
        actions.append(
            _action(
                "field_recall",
                "Use title, category, and store-name matches as a cheap fallback recall path.",
            )
        )

    actions.append(
        _action(
            "filter_available",
            "Exclude products that are unavailable or missing the minimum sellability signal.",
        )
    )
    actions.append(
        _action(
            "filter_quality",
            "Require graph-level data_quality_score before products can enter the default recommendation pool.",
        )
    )

    if intent.price_max is not None:
        actions.append(_action("apply_price_ceiling", "Apply the user-specified maximum price constraint."))
    if intent.min_rating is not None:
        actions.append(_action("apply_min_rating", "Apply the user-specified minimum rating constraint."))
    if user_id:
        actions.append(
            _action(
                "apply_user_history_boost",
                "Boost candidates that share graph attributes with products from the user's positive behavior history.",
            )
        )

    actions.append(
        _action(
            "apply_review_mention_ranking",
            "Use Review-[:MENTIONS]->Attribute sentiment signals when they exist in the graph.",
        )
    )
    actions.append(_action("deduplicate_products", "Remove duplicate product IDs and near-duplicate titles."))
    actions.append(_action("rerank_hybrid", "Rank merged candidates with match, quality, popularity, history, and review signals."))

    return QueryPlan(
        user_input=user_input,
        history_policy="positive_behavior_attributes" if user_id else "none",
        constraints=constraints,
        actions=actions,
        safety_notes=[
            "LLM output is parsed only as SearchIntent; raw Cypher from the LLM is never executed.",
            "Only allow-listed backend actions can choose recall paths or ranking signals.",
            "Product availability and data-quality filters are applied before ranking.",
        ],
    )


def enabled_action_names(plan: QueryPlan | None) -> set[str]:
    if plan is None:
        return set(ALLOWED_QUERY_ACTIONS)
    return {action.name for action in plan.actions if action.enabled and action.name in ALLOWED_QUERY_ACTIONS}


def _action(name: str, reason: str) -> QueryAction:
    return QueryAction(
        name=name,
        enabled=True,
        reason=reason,
        cypher_template=ACTION_CYPHER_TEMPLATES.get(name),
    )
