"""
Structured-intent + knowledge-graph recommender.

Endpoints served:
  POST /recommend                        — keyword search with optional personalization
  POST /recommend/home                   — behavior-based recommendations (no query text)
  POST /recommend/home/warm              — fire-and-forget cache warm-up (call on tab close/hide)
  POST /behavior/view                    — log product view to Neo4j
  POST /chat                             — multi-turn conversational recommendation (CRS)
  GET  /products/{product_id}/reviews    — top reviews for a product

Flow (search):
  1. LLM extracts the conversation/query into structured conditions
     (product/category/attribute keywords and optional rating constraints).
  2. Neo4j executes fixed meta-path retrieval queries. Behavior ranking is
     transition-first:
       User -> recent high-rated Product <- similar User -> next high-rated Product,
     with attribute/category meta-paths used for dialogue filtering,
     explanation, and recall backfill.
  3. The strongest matched graph path becomes the recommendation reason.
  4. A strict no-match result is returned when dialogue constraints cannot be
     satisfied. Popular products are used only for empty/home fallback flows.

Flow (home):
  1. Build user context
  2. If the user has no RATED/VIEWED/attribute history, return popular products
     directly (fast path, no personalization).
  3. Otherwise, use the same fixed User -> Product -> Attribute -> Product
     meta-path; fall back to popular products if it is empty.

Flow (chat):
  1. The attr_type vocabulary actually present in the graph (queried once from
     Neo4j and cached) plus config.yaml's genre are injected into the chat
     prompt — no hardcoded categories/slots, so the flow adapts to whatever
     catalog is loaded
  2. The LLM decides itself (via "action"/"filled_slots" in its structured
     response) whether to ask another clarifying question or move to search;
     Python only enforces MAX_QUESTIONS as a hard cap and falls back to
     searching immediately if the LLM call itself fails
  3. Once search is triggered, delegate to the same structured-intent +
     meta-path search used by /recommend
"""
from __future__ import annotations

import html as _html_mod
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase

from .models import MatchedAttr, ReasonMetrics, Recommendation, SearchIntent

# ── graph schema ───────────────────────────────────────────────────────────────

_SCHEMA = """\
Nodes:
  Product   { product_id, title, title_ja (string|null, Japanese translation of title),
               price (float|null), avg_rating (float|null),
               rating_count (int|null), description, image_url (string|null) }
  User      { user_id }
  Review    { review_id, rating (float 1-5), timestamp (int unix-ms),
               helpful_vote (int), verified (bool), title, text }
  Category  { category_id, name, level (int, 0=root) }
  Brand     { brand_id, name }
  Attribute { attribute_id, attr_type, value,
               value_ja (string|null, Japanese translation of value) }
    attr_type is genre-dependent and open-ended (see "Attribute Types Currently
    in the Graph" below for what's actually available in this catalog).

Relationships:
  (User)-[:RATED {rating (float), timestamp}]->(Product)
  (User)-[:WROTE]->(Review)
  (User)-[:VIEWED {timestamp}]->(Product)
  (Review)-[:ABOUT]->(Product)
  (Product)-[:MADE_BY]->(Brand)
  (Product)-[:BELONGS_TO]->(Category)
  (Category)-[:SUBCATEGORY_OF]->(Category)
  (Product)-[:HAS_ATTRIBUTE]->(Attribute)
"""

_FEW_SHOT_EXAMPLES = """\
## Examples (structural patterns only — invent your own approach as needed)

The attr_type/value names below (e.g. "feature", "durable") are placeholders that illustrate
QUERY STRUCTURE only, not a real catalog. They are intentionally generic and not tied to any
specific product category — never use them literally. Always substitute the actual attr_type
names from "Attribute Types Currently in the Graph" above, matched to what the catalog and the
user's query are actually about.

User: "something durable and easy to carry"
{"cypher": "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute) WHERE toLower(a.value) CONTAINS 'durable' OR toLower(a.value) CONTAINS 'lightweight' OR toLower(a.value) CONTAINS 'portable' WITH p, collect({attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(a) AS matches RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(matches)*2+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Durable, easy-to-carry match' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Keyword match across attribute values regardless of attr_type"}

User: "recommend something popular among people whose taste overlaps with mine" (user_id provided as $uid)
{"cypher": "MATCH (u:User {user_id:$uid})-[:RATED]->(seen:Product)<-[:RATED]-(peer:User)-[:RATED]->(rec:Product) WHERE peer<>u AND NOT (u)-[:RATED]->(rec) WITH rec AS p, count(DISTINCT peer) AS support RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(support)*1.2+coalesce(p.avg_rating,3.5)*0.4 AS score, 'Liked by other users with overlapping taste' AS explanation, [] AS matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Peer collaborative filtering — no attributes involved, purely shared-rating overlap"}

User: "recommend something similar to what I've rated highly" (user_id provided as $uid)
{"cypher": "MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(rec:Product) WHERE r.rating>=4 AND NOT (u)-[:RATED]->(rec) WITH rec AS p, collect(DISTINCT {attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(DISTINCT a) AS shared RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(shared)*1.5+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Similar to products you rated highly' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Attribute similarity from user's high-rated products"}

User: "a well-reviewed option with a specific feature the user names"
{"cypher": "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:MENTIONS {sentiment:'positive'}]-(rev:Review)-[:ABOUT]->(p) WHERE a.attr_type='feature' AND toLower(a.value) CONTAINS 'compact' WITH p, collect(DISTINCT {attr_type:a.attr_type,value:a.value,value_ja:a.value_ja}) AS matched_attrs, count(DISTINCT a) AS attr_matches, count(DISTINCT rev) AS confirmations RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(attr_matches)*1.5+toFloat(confirmations)*0.8+coalesce(p.avg_rating,3.5)*0.5 AS score, 'Feature independently confirmed by other users reviews' AS explanation, matched_attrs ORDER BY score DESC LIMIT $limit", "explanation": "Cross-verifies a product-description attribute against positive MENTIONS from separate reviews — not just a description claim"}
"""

_RULES = """\
## Rules
- Use $uid when referencing the user; NEVER hardcode a user_id string
- End every query with: ORDER BY score DESC LIMIT $limit
- Case-insensitive match: toLower(a.value) CONTAINS toLower("keyword")
- Price filter: toFloat(p.price) <= X  AND p.price IS NOT NULL
- NEVER use CREATE, MERGE, DELETE, SET, or any write clause
- Required RETURN aliases (exact names):
    product_id, title, title_ja, price, avg_rating, rating_count, score, explanation, matched_attrs, image_url
- matched_attrs: collect({attr_type: a.attr_type, value: a.value, value_ja: a.value_ja})  — use [] when no attrs
  (always include value_ja alongside value — Python picks whichever fits the requested language)

## Excluding already-rated/viewed products (CRITICAL)
Bind the user node in MATCH first, then filter in WHERE:
  CORRECT:   MATCH (u:User {user_id:$uid}) ... WHERE NOT (u)-[:RATED]->(p) AND NOT (u)-[:VIEWED]->(p)
  CORRECT:   MATCH (u:User {user_id:$uid})-[r:RATED]->(liked:Product) ... WHERE NOT (u)-[:RATED]->(rec)
  WRONG:     WHERE NOT (p)<-[:RATED]-(u:User {user_id:$uid}) AND NOT (p)<-[:VIEWED]-(u)
  — in the WRONG pattern, 'u' in the second NOT is unbound and excludes ALL viewed products

Output — JSON only, no markdown fences:
{"cypher": "<valid Cypher>", "explanation": "<one sentence>"}
"""

# $explanationはパラメータ化されており、LLMを介さずに言語ごとの文言をPython側で決めて
# 渡せる（フォールバック時・パーソナライズ不要時の両方で共有する）。
_FALLBACK_CYPHER = (
    "MATCH (p:Product) "
    "WHERE p.avg_rating IS NOT NULL AND p.rating_count >= 50 "
    "WITH p, "
    "  coalesce(p.avg_rating, 3.5) * 0.5 "
    "  + log(toFloat(p.rating_count) + 1) * 0.2 AS score, "
    "  [] AS matched_attrs, "
    "  {condition_matches: 0, behavior_matches: 0, transition_peers: 0, "
    "   collaborative_peers: 0, shared_rated_attributes: 0, "
    "   shared_viewed_attributes: 0, review_confirmations: 0} AS reason_metrics "
    "RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.description AS description, p.description_ja AS description_ja, p.image_url AS image_url, p.price AS price, "
    "  p.avg_rating AS avg_rating, p.rating_count AS rating_count, score, "
    "  $explanation AS explanation, matched_attrs, reason_metrics "
    "ORDER BY score DESC LIMIT $limit"
)


def _popular_explanation(lang: str) -> str:
    return "評価の高い人気商品" if lang == "ja" else "Popular highly-rated products"


_METAPATH_USER_CYPHER = """\
MATCH (p:Product)
WHERE p.avg_rating IS NOT NULL
  AND p.rating_count IS NOT NULL
  AND toFloat(p.avg_rating) >= $min_rating
  AND (size($candidate_product_ids) = 0 OR p.product_id IN $candidate_product_ids)
CALL (p) {
  OPTIONAL MATCH (p)-[:BELONGS_TO]->(c:Category)
  RETURN collect(DISTINCT c.name) AS categories
}
CALL (p) {
  OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
  RETURN collect(DISTINCT a) AS product_attrs
}
CALL (p) {
  MATCH (u:User {user_id: $uid})
  MATCH (u)-[r:RATED]->(rated_seed:Product)
  WHERE toFloat(r.rating) >= 4
  WITH rated_seed
  ORDER BY toInteger(r.timestamp) DESC
  LIMIT 15
  OPTIONAL MATCH (rated_seed)-[:HAS_ATTRIBUTE]->(ra:Attribute)<-[:HAS_ATTRIBUTE]-(p)
  WHERE NOT ra.attr_type IN $ignored_behavior_attr_types
  WITH p, collect(DISTINCT ra) AS rated_attrs, count(DISTINCT rated_seed) AS rated_seed_count
  OPTIONAL MATCH (p)<-[:ABOUT]-(rev:Review)-[:MENTIONS {sentiment: 'positive'}]->(ma:Attribute)
  WHERE ma IN rated_attrs
  RETURN rated_attrs, rated_seed_count, count(DISTINCT rev) AS confirmed_rated_mentions
}
CALL (p) {
  MATCH (u:User {user_id: $uid})
  OPTIONAL MATCH (u)-[:VIEWED]->(viewed_seed:Product)-[:HAS_ATTRIBUTE]->(va:Attribute)<-[:HAS_ATTRIBUTE]-(p)
  WHERE NOT va.attr_type IN $ignored_behavior_attr_types
  RETURN collect(DISTINCT va) AS viewed_attrs, count(DISTINCT viewed_seed) AS viewed_seed_count
}
CALL (p) {
  MATCH (u:User {user_id: $uid})
  OPTIONAL MATCH (u)-[sr:RATED]->(seed:Product)<-[pr:RATED]-(peer:User)-[cr:RATED]->(p)
  WHERE toFloat(sr.rating) >= 4
    AND toFloat(pr.rating) >= 4
    AND toFloat(cr.rating) >= 4
    AND peer <> u
  RETURN count(DISTINCT peer) AS cf_peer_count,
         count(DISTINCT seed) AS cf_seed_count
}
CALL (p) {
  MATCH (u:User {user_id: $uid})
  MATCH (u)-[sr:RATED]->(seed:Product)
  WHERE toFloat(sr.rating) >= 4
  WITH u, seed, sr
  ORDER BY toInteger(sr.timestamp) DESC
  LIMIT 20
  OPTIONAL MATCH (seed)<-[pr:RATED]-(peer:User)-[tr:RATED]->(p)
  WHERE peer <> u
    AND toFloat(pr.rating) >= 4
    AND toFloat(tr.rating) >= 4
    AND toInteger(tr.timestamp) > toInteger(pr.timestamp)
  RETURN count(DISTINCT peer) AS transition_peer_count,
         count(DISTINCT seed) AS transition_seed_count
}
CALL (p) {
  MATCH (u:User {user_id: $uid})
  OPTIONAL MATCH (u)-[seen:RATED|VIEWED]->(p)
  RETURN count(seen) AS already_seen
}
WITH p, categories,
     [a IN product_attrs WHERE a IS NOT NULL] AS product_attrs,
     [a IN rated_attrs WHERE a IS NOT NULL] AS rated_attrs,
     [a IN viewed_attrs WHERE a IS NOT NULL] AS viewed_attrs,
     rated_seed_count, viewed_seed_count, cf_peer_count, cf_seed_count,
     transition_peer_count, transition_seed_count, already_seen, confirmed_rated_mentions
WITH p, categories, rated_attrs, viewed_attrs, rated_seed_count, viewed_seed_count,
     cf_peer_count, cf_seed_count, transition_peer_count, transition_seed_count, already_seen,
     confirmed_rated_mentions,
     [kw IN $product_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS product_kw_hits,
     [kw IN $category_keywords
      WHERE any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS category_kw_hits,
     [a IN product_attrs
      WHERE any(kw IN $attribute_keywords
        WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
           OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw
           OR replace(toLower(coalesce(a.attr_type, '')), '_', ' ') CONTAINS kw)] AS query_attrs,
     [kw IN $platform_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS platform_text_hits,
     [kw IN $franchise_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS franchise_text_hits,
     [kw IN $product_type_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS product_type_text_hits,
     [a IN product_attrs
      WHERE a.attr_type IN $platform_attr_types
        AND any(kw IN $platform_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS platform_attrs,
     [a IN product_attrs
      WHERE a.attr_type IN $franchise_attr_types
        AND any(kw IN $franchise_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS franchise_attrs,
     [a IN product_attrs
      WHERE a.attr_type IN $product_type_attr_types
        AND any(kw IN $product_type_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS product_type_attrs
     , [kw IN $required_condition_keywords
        WHERE toLower(coalesce(p.title, '')) CONTAINS kw
           OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
           OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)
           OR any(a IN product_attrs WHERE
             replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw
           )] AS required_condition_hits
WITH p, product_kw_hits, category_kw_hits, query_attrs, platform_text_hits,
     franchise_text_hits, product_type_text_hits, platform_attrs, franchise_attrs,
     product_type_attrs, required_condition_hits, rated_attrs, viewed_attrs,
     rated_seed_count, viewed_seed_count, cf_peer_count, cf_seed_count,
     transition_peer_count, transition_seed_count, already_seen, confirmed_rated_mentions,
     (size(product_kw_hits) + size(category_kw_hits) + size(query_attrs)
      + size(platform_text_hits) + size(franchise_text_hits) + size(product_type_text_hits)
      + size(platform_attrs) + size(franchise_attrs) + size(product_type_attrs)) AS condition_hits,
     (size(rated_attrs) + size(viewed_attrs) + cf_peer_count + transition_peer_count) AS behavior_hits
WHERE already_seen = 0
  AND ($has_query = false OR condition_hits > 0)
  AND ($platform_required = false
       OR ($dialogue_soft_preferences = true AND size(platform_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(platform_text_hits) + size(platform_attrs) > 0))
  AND ($franchise_required = false
       OR ($dialogue_soft_preferences = true AND size(franchise_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(franchise_text_hits) + size(franchise_attrs) > 0))
  AND ($product_type_required = false
       OR ($dialogue_soft_preferences = true AND size(product_type_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(product_type_text_hits) + size(product_type_attrs) > 0))
  AND all(kw IN $required_condition_keywords WHERE kw IN required_condition_hits)
  AND behavior_hits > 0
WITH p, product_kw_hits, category_kw_hits, query_attrs, platform_text_hits,
     franchise_text_hits, product_type_text_hits, platform_attrs, franchise_attrs,
     product_type_attrs, required_condition_hits, rated_attrs, viewed_attrs,
     rated_seed_count, viewed_seed_count, cf_peer_count, cf_seed_count,
     transition_peer_count, transition_seed_count, confirmed_rated_mentions,
     condition_hits, behavior_hits,
     (
       log(toFloat(transition_peer_count) + 1) * 5.0
       + log(toFloat(transition_seed_count) + 1) * 1.2
       + log(toFloat(cf_peer_count) + 1) * 1.2
       + log(toFloat(cf_seed_count) + 1) * 0.5
       +
       toFloat(size(query_attrs))
         * CASE WHEN $dialogue_soft_preferences THEN 20.0 ELSE 2.0 END
       + toFloat(size(product_kw_hits)) * 1.5
       + toFloat(size(category_kw_hits)) * 1.0
       + toFloat(size(franchise_text_hits)) * 14.0
       + toFloat(size(platform_text_hits)) * 12.0
       + toFloat(size(product_type_text_hits)) * 8.0
       + toFloat(size(franchise_attrs)) * 5.0
       + toFloat(size(platform_attrs)) * 4.0
       + toFloat(size(product_type_attrs)) * 2.5
       + toFloat(size(rated_attrs)) * 0.85
       + toFloat(size(viewed_attrs)) * 0.7
       + toFloat(rated_seed_count) * 0.2
       + toFloat(viewed_seed_count) * 0.12
       + log(toFloat(confirmed_rated_mentions) + 1) * 1.2
       + coalesce(toFloat(p.avg_rating), 3.5) * 0.45
       + log(toFloat(coalesce(p.rating_count, 1)) + 1) * 0.12
     ) AS score
RETURN p.product_id AS product_id,
       p.title AS title,
       p.title_ja AS title_ja,
       p.description AS description,
       p.description_ja AS description_ja,
       p.image_url AS image_url,
       p.price AS price,
       p.avg_rating AS avg_rating,
       p.rating_count AS rating_count,
       score,
       {condition_matches: condition_hits,
        behavior_matches: behavior_hits,
        transition_peers: transition_peer_count,
        collaborative_peers: cf_peer_count,
        shared_rated_attributes: size(rated_attrs),
        shared_viewed_attributes: size(viewed_attrs),
        review_confirmations: confirmed_rated_mentions} AS reason_metrics,
       CASE
         WHEN transition_peer_count > 0 THEN $transition_explanation
         WHEN cf_peer_count > 0 THEN $peer_explanation
         WHEN size(rated_attrs) > 0 THEN $rated_explanation
         WHEN size(viewed_attrs) > 0 THEN $viewed_explanation
       ELSE $condition_explanation
       END AS explanation,
       CASE
         WHEN $has_query = false THEN 'behavior_only'
         WHEN transition_peer_count > 0 OR cf_peer_count > 0
           OR size(rated_attrs) > 0 OR size(viewed_attrs) > 0 THEN 'dialogue_personalized'
         ELSE 'dialogue_only'
       END AS recommendation_source,
       [a IN (franchise_attrs + platform_attrs + product_type_attrs + query_attrs + rated_attrs + viewed_attrs)[0..8]
        WHERE a IS NOT NULL | {attr_type: a.attr_type, value: a.value, value_ja: a.value_ja}] AS matched_attrs
ORDER BY score DESC LIMIT $limit
"""


_METAPATH_CONDITION_CYPHER = """\
MATCH (p:Product)
WHERE p.avg_rating IS NOT NULL
  AND p.rating_count IS NOT NULL
  AND toFloat(p.avg_rating) >= $min_rating
CALL (p) {
  OPTIONAL MATCH (p)-[:BELONGS_TO]->(c:Category)
  RETURN collect(DISTINCT c.name) AS categories
}
CALL (p) {
  OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
  RETURN collect(DISTINCT a) AS product_attrs
}
WITH p, categories,
     [a IN product_attrs WHERE a IS NOT NULL] AS product_attrs
WITH p, categories, product_attrs,
     [kw IN $product_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS product_kw_hits,
     [kw IN $category_keywords
      WHERE any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS category_kw_hits,
     [a IN product_attrs
      WHERE any(kw IN $attribute_keywords
        WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
           OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw
           OR replace(toLower(coalesce(a.attr_type, '')), '_', ' ') CONTAINS kw)] AS query_attrs,
     [kw IN $platform_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS platform_text_hits,
     [kw IN $franchise_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS franchise_text_hits,
     [kw IN $product_type_keywords
      WHERE toLower(coalesce(p.title, '')) CONTAINS kw
         OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
         OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)] AS product_type_text_hits,
     [a IN product_attrs
      WHERE a.attr_type IN $platform_attr_types
        AND any(kw IN $platform_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS platform_attrs,
     [a IN product_attrs
      WHERE a.attr_type IN $franchise_attr_types
        AND any(kw IN $franchise_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS franchise_attrs,
     [a IN product_attrs
      WHERE a.attr_type IN $product_type_attr_types
        AND any(kw IN $product_type_keywords
          WHERE replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw)] AS product_type_attrs
     , [kw IN $required_condition_keywords
        WHERE toLower(coalesce(p.title, '')) CONTAINS kw
           OR toLower(coalesce(p.title_ja, '')) CONTAINS kw
           OR any(cat IN categories WHERE toLower(coalesce(cat, '')) CONTAINS kw)
           OR any(a IN product_attrs WHERE
             replace(toLower(coalesce(a.value, '')), '_', ' ') CONTAINS kw
             OR replace(toLower(coalesce(a.value_ja, '')), '_', ' ') CONTAINS kw
           )] AS required_condition_hits
WITH p, product_kw_hits, category_kw_hits, query_attrs, platform_text_hits,
     franchise_text_hits, product_type_text_hits, platform_attrs, franchise_attrs,
     product_type_attrs, required_condition_hits,
     (size(product_kw_hits) + size(category_kw_hits) + size(query_attrs)
      + size(platform_text_hits) + size(franchise_text_hits) + size(product_type_text_hits)
      + size(platform_attrs) + size(franchise_attrs) + size(product_type_attrs)) AS condition_hits
WHERE condition_hits > 0
  AND ($platform_required = false
       OR ($dialogue_soft_preferences = true AND size(platform_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(platform_text_hits) + size(platform_attrs) > 0))
  AND ($franchise_required = false
       OR ($dialogue_soft_preferences = true AND size(franchise_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(franchise_text_hits) + size(franchise_attrs) > 0))
  AND ($product_type_required = false
       OR ($dialogue_soft_preferences = true AND size(product_type_attrs) > 0)
       OR ($dialogue_soft_preferences = false AND size(product_type_text_hits) + size(product_type_attrs) > 0))
  AND all(kw IN $required_condition_keywords WHERE kw IN required_condition_hits)
WITH p, product_kw_hits, category_kw_hits, query_attrs, platform_text_hits,
     franchise_text_hits, product_type_text_hits, platform_attrs, franchise_attrs, product_type_attrs,
     (
       toFloat(size(query_attrs))
         * CASE WHEN $dialogue_soft_preferences THEN 20.0 ELSE 2.0 END
       + toFloat(size(product_kw_hits)) * 1.5
       + toFloat(size(category_kw_hits)) * 1.0
       + toFloat(size(franchise_text_hits)) * 14.0
       + toFloat(size(platform_text_hits)) * 12.0
       + toFloat(size(product_type_text_hits)) * 8.0
       + toFloat(size(franchise_attrs)) * 5.0
       + toFloat(size(platform_attrs)) * 4.0
       + toFloat(size(product_type_attrs)) * 2.5
       + coalesce(toFloat(p.avg_rating), 3.5) * 0.45
       + log(toFloat(coalesce(p.rating_count, 1)) + 1) * 0.12
     ) AS score
RETURN p.product_id AS product_id,
       p.title AS title,
       p.title_ja AS title_ja,
       p.description AS description,
       p.description_ja AS description_ja,
       p.image_url AS image_url,
       p.price AS price,
       p.avg_rating AS avg_rating,
       p.rating_count AS rating_count,
       score,
       {condition_matches: size(query_attrs) + size(product_kw_hits) + size(category_kw_hits)
                            + size(franchise_attrs) + size(platform_attrs) + size(product_type_attrs),
        behavior_matches: 0,
        transition_peers: 0,
        collaborative_peers: 0,
        shared_rated_attributes: 0,
        shared_viewed_attributes: 0,
        review_confirmations: 0} AS reason_metrics,
       $condition_explanation AS explanation,
       'dialogue_only' AS recommendation_source,
       [a IN (franchise_attrs + platform_attrs + product_type_attrs + query_attrs)[0..8]
        WHERE a IS NOT NULL | {attr_type: a.attr_type, value: a.value, value_ja: a.value_ja}] AS matched_attrs
ORDER BY score DESC LIMIT $limit
"""


_CONDITION_STOPWORDS = {
    "a", "an", "and", "are", "for", "from", "game", "games", "good", "high",
    "i", "in", "is", "me", "of", "or", "product", "recommend", "reviews",
    "the", "to", "want", "with",
}

# These words identify the catalog rather than a user's distinctive need.  They
# may help recall, but must never be enough on their own to pass the dialogue
# constraint gate.
_GENERIC_CONDITION_TERMS = {
    "game", "games", "video game", "video games", "software", "product",
    "products", "ゲーム", "ビデオゲーム", "ソフト", "商品", "おすすめ",
}

_IGNORED_BEHAVIOR_ATTR_TYPES = [
    "batteries",
    "color",
    "customer_reviews",
    "date_first_available",
    "item_weight",
    "language",
    "package_dimensions",
    "pricing",
    "release_date",
    "return_policy",
    "terms_of_use",
]

_PLATFORM_ATTR_TYPES = [
    "domain_platform",
]

_FRANCHISE_ATTR_TYPES = [
    "domain_franchise",
]

_PRODUCT_TYPE_ATTR_TYPES = [
    "domain_product_type",
]

_DOMAIN_ATTR_TYPES = [
    *_PLATFORM_ATTR_TYPES,
    *_FRANCHISE_ATTR_TYPES,
    *_PRODUCT_TYPE_ATTR_TYPES,
]

_CHAT_PROFILE_ATTR_TYPES = [
    "domain_platform",
    "domain_franchise",
    "genre",
    "gameplay_style",
    "game_mode",
]

_DOMAIN_ALIASES: dict[str, dict[str, list[str]]] = {
    "platform": {
        "nintendo_switch": ["nintendo switch", "switch", "nintendo_switch", "スイッチ"],
        "ps5": ["playstation 5", "ps5", "ps 5"],
        "ps4": ["playstation 4", "ps4", "ps 4"],
        "ps3": ["playstation 3", "ps3", "ps 3"],
        "playstation_vita": ["playstation vita", "ps vita", "vita"],
        "xbox_series_x": ["xbox series x", "xbox series s", "series x", "series s"],
        "xbox_one": ["xbox one"],
        "xbox_360": ["xbox 360", "360"],
        "wii_u": ["wii u", "wiiu"],
        "wii": ["nintendo wii", "wii"],
        "nintendo_3ds": ["nintendo 3ds", "3ds"],
        "nintendo_ds": ["nintendo ds", "ds"],
        "pc": ["pc", "windows", "steam", "computer"],
    },
    "franchise": {
        "mario": ["mario", "super mario", "マリオ"],
        "zelda": ["zelda", "legend of zelda", "ゼルダ"],
        "pokemon": ["pokemon", "pokémon", "ポケモン"],
        "sonic": ["sonic", "ソニック"],
        "minecraft": ["minecraft", "マインクラフト"],
        "call_of_duty": ["call of duty", "cod"],
        "final_fantasy": ["final fantasy", "ff"],
        "spider_man": ["spider-man", "spider man", "spiderman"],
        "grand_theft_auto": ["grand theft auto", "gta"],
        "red_dead": ["red dead redemption", "red dead"],
        "assassins_creed": ["assassin's creed", "assassins creed"],
        "lego": ["lego"],
        "star_wars": ["star wars"],
        "madden": ["madden"],
        "nba_2k": ["nba 2k", "2k"],
        "fifa": ["fifa"],
        "mlb_the_show": ["mlb the show", "baseball"],
        "animal_crossing": ["animal crossing", "どうぶつの森"],
        "kirby": ["kirby", "カービィ"],
        "splatoon": ["splatoon", "スプラトゥーン"],
    },
    "product_type": {
        "video_game": ["game", "games", "video game", "software", "ゲーム"],
        "controller": ["controller", "gamepad", "joy-con", "joy con", "dualshock", "dualsense"],
        "headset": ["headset", "headphone", "gaming headset"],
        "console": ["console", "system", "本体"],
        "storage": ["memory card", "microsd", "micro sd", "sd card", "storage"],
        "accessory": ["accessory", "case", "charger", "cable", "stand", "protector", "skin"],
    },
}


def _metapath_explanations(lang: str) -> dict[str, str]:
    if lang == "ja":
        return {
            "top": "会話条件とユーザ履歴から、商品属性を共有する候補をグラフの元パスで推薦",
            "condition_top": "会話条件を構造化し、商品・カテゴリ・属性一致で候補を推薦",
            "transition": "最近の好みに近い流れで次に選ばれやすい候補です",
            "peer": "好みが近いユーザにも高評価されている候補です",
            "rated": "高評価した商品と共有する属性が強い候補です",
            "viewed": "最近閲覧した商品と共有する属性がある候補です",
            "condition": "会話で指定された条件に一致する候補です",
            "dialogue_soft": "商品種別・機種・シリーズを満たす候補を、その他の希望との一致度で順位付けしました",
        }
    return {
        "top": "Meta-path recommendation using dialogue constraints and user-history attribute links",
        "condition_top": "Structured dialogue constraints matched against product, category, and attribute data",
        "transition": "Often chosen next after games similar to the user's recent likes",
        "peer": "Highly rated by users with overlapping taste",
        "rated": "Shares attributes with products this user rated highly",
        "viewed": "Shares attributes with products this user recently viewed",
        "condition": "Matches the structured dialogue constraints",
        "dialogue_soft": "Candidates satisfy product type, platform, and franchise constraints, then rank by other preferences",
    }


def _build_condition_prompt(genre: str, attr_vocab_text: str, lang: str) -> str:
    target = "Japanese" if lang == "ja" else "English"
    vocab = attr_vocab_text or "(no attribute vocabulary available)"
    return f"""\
Extract structured recommendation conditions for a {genre} catalog.
The output is NOT a database query. Neo4j will perform retrieval with fixed
meta-path Cypher, so only extract compact conditions that can be used as filters.

Use the catalog vocabulary below when possible:
{vocab}

Guidelines:
- product_keywords: concrete product, franchise, platform, device, or title words
  such as "mario", "nintendo switch", "playstation", "controller".
- category_keywords: broad catalog/category words such as "games", "accessories",
  "consoles".
- attribute_keywords: desired properties or gameplay terms such as "family",
  "multiplayer", "party", "sports", "baseball", "rpg", "co-op".
- platform_keywords: normalized device/platform terms such as "nintendo switch",
  "ps5", "ps4", "xbox one", "pc".
- franchise_keywords: named series/IP terms such as "mario", "zelda", "pokemon",
  "minecraft", "call of duty".
- product_type_keywords: product class terms such as "video game", "controller",
  "headset", "console", "storage", "accessory".
- Translate user intent into English keywords when that is likely to match the
  catalog, but keep useful Japanese terms too when the user wrote Japanese.
- min_rating is null unless the user explicitly asks for high-rated/well-reviewed
  products; then use 4.0.
- Return JSON only. User-facing wording is not needed, but if you include any text,
  use {target}.

Schema:
{{
  "product_keywords": [],
  "category_keywords": [],
  "attribute_keywords": [],
  "platform_keywords": [],
  "franchise_keywords": [],
  "product_type_keywords": [],
  "min_rating": null
}}
"""


def _keyword_list(value: Any, max_items: int = 10) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        term = str(item or "").strip().lower()
        term = re.sub(r"\s+", " ", term)
        term = term.strip(" \t\n\r\"'.,;:!?()[]{}")
        if not term or term in _CONDITION_STOPWORDS:
            continue
        if len(term) < 2 and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", term):
            continue
        if term not in cleaned:
            cleaned.append(term)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _required_condition_keywords(conditions: dict[str, Any]) -> list[str]:
    """Return distinctive dialogue terms that every search result must satisfy.

    Platform, franchise, and product type have their own canonical graph gates.
    This list protects the remaining open-vocabulary dialogue conditions (for
    example, ``adventure`` or ``co-op``) from being outweighed by user history.
    """
    terms: list[str] = []
    for key in ("product_keywords", "category_keywords", "attribute_keywords"):
        for term in conditions.get(key, []) or []:
            normalized = str(term).strip().lower().replace("_", " ")
            if normalized and normalized not in _GENERIC_CONDITION_TERMS and normalized not in terms:
                terms.append(normalized)
    # The condition LLM preserves Japanese wording but also emits an English
    # canonical form for catalog lookup.  Requiring both would turn a valid
    # ``adventure`` match into a false zero-result when the graph stores
    # ``冒険`` or ``adventure_game`` rather than the literal Japanese phrase.
    latin_terms = [term for term in terms if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", term)]
    return (latin_terms or terms)[:12]


def _domain_constraints_from_terms(terms: list[str]) -> dict[str, list[str]]:
    text = " ".join(str(t or "").lower().replace("_", " ") for t in terms)
    constraints: dict[str, list[str]] = {
        "platform_keywords": [],
        "franchise_keywords": [],
        "product_type_keywords": [],
    }

    def add_unique(key: str, values: list[str]) -> None:
        for value in values:
            value = value.strip().lower().replace("_", " ")
            if value and value not in constraints[key]:
                constraints[key].append(value)

    def contains_alias(alias: str) -> bool:
        alias = alias.lower().replace("_", " ").strip()
        if not alias:
            return False
        # Short Latin aliases must match complete tokens. Without boundaries,
        # "ds" also matches "3ds", and "wii" matches "wiiu".
        if re.fullmatch(r"[a-z0-9 +.\-']+", alias):
            pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            return re.search(pattern, text) is not None
        return alias in text

    for group, aliases in _DOMAIN_ALIASES.items():
        out_key = f"{group}_keywords"
        for canonical, variants in aliases.items():
            if any(contains_alias(v) for v in variants):
                add_unique(out_key, [canonical, canonical.replace("_", " "), *variants])

    # If the user asks for a game, prefer game software over accessories. Do not
    # force this when the query explicitly names accessory or hardware terms.
    accessory_terms = {"controller", "headset", "console", "storage", "accessory"}
    if constraints["product_type_keywords"]:
        has_accessory_intent = any(
            term in text for term in ("controller", "headset", "console", "memory", "microsd", "accessory", "case")
        )
        if not has_accessory_intent and any(x in text for x in ("game", "games", "video game", "ゲーム")):
            add_unique("product_type_keywords", ["video_game", "video game", "games"])
        elif has_accessory_intent:
            constraints["product_type_keywords"] = [
                v for v in constraints["product_type_keywords"]
                if not (v in {"video game", "video_game", "games", "game"} and any(t in text for t in accessory_terms))
            ]

    return constraints


def _fallback_condition_terms(query: str) -> dict[str, Any]:
    raw_terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]*|[\u3040-\u30ff\u3400-\u9fff]+", query.lower())
    terms = _keyword_list(raw_terms, max_items=12)
    joined = " ".join(terms)
    product_terms = list(terms)
    category_terms: list[str] = []
    attr_terms: list[str] = []

    if "switch" in joined:
        product_terms.extend(["nintendo switch", "nintendo_switch"])
    if "mario" in joined or "マリオ" in joined:
        product_terms.extend(["mario", "マリオ"])
    if "game" in joined or "ゲーム" in joined:
        category_terms.extend(["games", "video game"])
    if any(x in joined for x in ("family", "kid", "party", "家族", "子供")):
        attr_terms.extend(["family", "kids", "party", "multiplayer", "local_multiplayer"])
    if "baseball" in joined or "野球" in joined:
        attr_terms.extend(["baseball", "sports"])
    if any(x in joined for x in ("good review", "high rated", "人気", "高評価", "レビュー")):
        min_rating: float | None = 4.0
    else:
        min_rating = None

    return {
        "product_keywords": _keyword_list(product_terms, max_items=12),
        "category_keywords": _keyword_list(category_terms, max_items=8),
        "attribute_keywords": _keyword_list(attr_terms or terms, max_items=12),
        "min_rating": min_rating,
    }


def _format_applied_conditions(conditions: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for key, label in (
        ("platform_keywords", "platform"),
        ("franchise_keywords", "franchise"),
        ("product_type_keywords", "product_type"),
        ("product_keywords", "product"),
        ("category_keywords", "category"),
        ("attribute_keywords", "attribute"),
    ):
        values = conditions.get(key) or []
        if values:
            labels.append(f"{label}: {', '.join(str(v) for v in values[:3])}")
    if conditions.get("min_rating"):
        labels.append(f"min_rating: {conditions['min_rating']}")
    return labels


def _dialogue_condition_groups(conditions: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split extracted conditions into the fixed conversational search policy.

    Only product type, platform, and franchise are exclusion gates. Everything
    else remains a ranking signal so sparse open-vocabulary attributes do not
    collapse a useful candidate pool to one or zero products.
    """
    hard: list[str] = []
    soft: list[str] = []
    for key, label in (
        ("product_type_keywords", "product_type"),
        ("platform_keywords", "platform"),
        ("franchise_keywords", "franchise"),
    ):
        values = conditions.get(key) or []
        if values:
            hard.append(f"{label}: {', '.join(str(v) for v in values[:3])}")
    for key, label in (
        ("product_keywords", "product"),
        ("category_keywords", "category"),
        ("attribute_keywords", "attribute"),
    ):
        values = conditions.get(key) or []
        if values:
            soft.append(f"{label}: {', '.join(str(v) for v in values[:3])}")
    if conditions.get("min_rating"):
        soft.append(f"rating_preference: {conditions['min_rating']}")
    return hard, soft

def _build_fix_prompt(lang: str) -> str:
    target = "Japanese" if lang == "ja" else "English"
    return f"""\
You are a Cypher expert. Fix the broken query so it runs correctly in Neo4j 5.
Preserve the original intent and all required RETURN columns.

Graph Schema:
{_SCHEMA}

Required RETURN: product_id, title, title_ja, image_url, price, avg_rating, rating_count, score, explanation, matched_attrs
Always end with: ORDER BY score DESC LIMIT $limit

Write the top-level "explanation" AND the per-product `'...' AS explanation` string literal inside
the Cypher in {target} — do not silently revert either one to English while fixing the query.

Output JSON only: {{"cypher": "<fixed Cypher>", "explanation": "<one sentence>"}}
"""


def _format_attr_vocab(vocab: list[dict]) -> str:
    if not vocab:
        return ""
    lines = ["## Attribute Types Currently in the Graph (attr_type: example values)"]
    for v in vocab:
        examples = ", ".join(str(x) for x in v["examples"][:6])
        lines.append(f"  {v['attr_type']}: {examples}")
    return "\n".join(lines)


def _format_output_language(lang: str) -> str:
    target = "Japanese" if lang == "ja" else "English"
    return (
        "## Output Language (CRITICAL)\n"
        f"Write ALL user-facing text in {target}, one sentence each:\n"
        '- the top-level "explanation" field in the JSON output\n'
        "- the per-product `'...' AS explanation` string literal inside the Cypher's RETURN clause\n"
        f"The examples above show these in English for illustration only — the actual wording you "
        f"write must be in {target} regardless of what language the examples use."
    )


def _build_search_prompt(
    genre: str,
    user_ctx: dict | None,
    dynamic_few_shot: list[dict] | None = None,
    attr_vocab_text: str = "",
    lang: str = "en",
    has_uid: bool = False,
) -> str:
    """has_uid: True only when a user_id was given AND that user has actual rating/
    attribute history — i.e. $uid will actually be bound AND worth personalizing with."""
    parts = [
        f"You are a Cypher query generator for a Neo4j {genre} product knowledge graph.\n"
        "Given a user search query, generate ONE Cypher READ query that best answers it.\n"
        "Think freely — design the query that fits the request. "
        "You may traverse any relationships in the schema, combine multiple MATCH clauses, "
        "or invent a novel scoring expression. Do not limit yourself to a fixed set of patterns.",
        f"## Graph Schema\n{_SCHEMA}",
    ]
    if attr_vocab_text:
        parts.append(attr_vocab_text)
    parts.append(_FEW_SHOT_EXAMPLES)
    if dynamic_few_shot:
        parts.append(_format_dynamic_few_shot(dynamic_few_shot))
    if has_uid:
        parts.append(_format_user_ctx(user_ctx or {}))
        parts.append(
            "## Personalization\n"
            "Incorporate the user context above to personalize results.\n"
            "- Use $uid when referencing this user in Cypher\n"
            "- Exclude products the user already RATED or VIEWED when possible"
        )
    else:
        parts.append(
            "## No User Context (CRITICAL)\n"
            "$uid is NOT bound for this request — either this is an anonymous request, or "
            "the user has no rating/attribute history yet. Do NOT reference $uid anywhere in "
            "the query (ignore the $uid pattern in the examples above), and NEVER hardcode a "
            "literal user_id string as a substitute for $uid.\n"
            "You MAY still use aggregate patterns over OTHER users' RATED data not anchored to "
            "a specific user — e.g. products frequently rated highly by users who also rated a "
            "matching product highly. Just don't bind the query to one specific $uid."
        )
    parts.append(_format_output_language(lang))
    parts.append(_RULES)
    return "\n\n".join(parts)


_HOME_PATH_CATALOG = """\
## Allowed home-recommendation graph paths

Choose exactly ONE primary path and at most ONE secondary path. Do not invent
labels, relationship types, or properties outside the supplied graph schema.

P1 — rated-item attribute similarity
  User-[:RATED]->Seed Product-[:HAS_ATTRIBUTE]->Attribute<-[:HAS_ATTRIBUTE]-Candidate Product
  Use when the user's highly rated products expose useful catalog attributes.

P2 — peer collaborative filtering
  User-[:RATED]->Shared Product<-[:RATED]-Peer User-[:RATED]->Candidate Product
  Use when multiple peers rated both the shared product and candidate at least 4.

P3 — chronological peer transition
  User-[:RATED]->Seed Product<-[:RATED]-Peer User-[:RATED]->Candidate Product
  Require the peer's candidate rating timestamp to be later than its seed rating timestamp.

P4 — review-confirmed shared attribute
  User-[:RATED]->Seed Product-[:HAS_ATTRIBUTE]->Attribute
      <-[:MENTIONS {sentiment:'positive'}]-Review-[:ABOUT]->Candidate Product
  Use positive review evidence to confirm an attribute already grounded in a liked seed.

P5 — category or brand affinity
  User-[:RATED]->Seed Product-[:BELONGS_TO]->Category<-[:BELONGS_TO]-Candidate Product
  User-[:RATED]->Seed Product-[:MADE_BY]->Brand<-[:MADE_BY]-Candidate Product
  Use as a backfill path when attribute or peer evidence is sparse.
"""


_HOME_TEXT2CYPHER_FEW_SHOTS = r"""\
## Home Text2Cypher examples

Example 1 — P1 attribute similarity
Input: Recommend unseen products similar to this user's recent high ratings.
Output:
{"cypher":"MATCH (u:User {user_id: $uid})-[r:RATED]->(seed:Product) WHERE toFloat(r.rating) >= 4 WITH u, seed, r ORDER BY toInteger(r.timestamp) DESC LIMIT 12 MATCH (seed)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(p:Product) WHERE NOT a.attr_type IN $ignored_attr_types AND NOT (u)-[:RATED|VIEWED]->(p) WITH p, collect(DISTINCT seed.title)[0..6] AS seed_titles, collect(DISTINCT a) AS all_attrs WHERE any(a IN all_attrs WHERE a.attr_type = 'domain_product_type') WITH p, seed_titles, all_attrs, reduce(weight=0.0, a IN all_attrs | weight + CASE a.attr_type WHEN 'domain_franchise' THEN 5.0 WHEN 'domain_platform' THEN 3.0 WHEN 'game_mode' THEN 2.0 WHEN 'gameplay_style' THEN 2.0 WHEN 'genre' THEN 2.0 WHEN 'domain_product_type' THEN 0.5 ELSE 1.0 END) AS evidence_score, [a IN all_attrs WHERE a.attr_type IN ['domain_franchise','domain_platform','game_mode','gameplay_style','genre','domain_product_type']] + [a IN all_attrs WHERE NOT a.attr_type IN ['domain_franchise','domain_platform','game_mode','gameplay_style','genre','domain_product_type']] AS ordered_attrs WITH p, seed_titles, ordered_attrs, evidence_score, size(all_attrs) AS shared RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.description AS description, p.description_ja AS description_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, evidence_score + coalesce(toFloat(p.avg_rating), 3.5) * 0.4 AS score, '' AS explanation, [a IN ordered_attrs[0..8] | {attr_type:a.attr_type, value:a.value, value_ja:a.value_ja}] AS matched_attrs, {condition_matches:0, behavior_matches:shared, transition_peers:0, collaborative_peers:0, shared_rated_attributes:shared, shared_viewed_attributes:0, review_confirmations:0} AS reason_metrics, 'attribute_similarity' AS recommendation_strategy, 'User -> high-rated product -> shared attribute -> candidate product' AS graph_path, seed_titles, 'behavior_only' AS recommendation_source ORDER BY score DESC LIMIT $limit","explanation":"Uses meaningful shared graph attributes from the user's recent high-rated products while preserving the user's product type."}

Example 2 — P2 collaborative filtering
Input: Recommend products liked by users whose taste overlaps with this user.
Output:
{"cypher":"MATCH (u:User {user_id: $uid})-[ur:RATED]->(seed:Product)<-[pr:RATED]-(peer:User)-[cr:RATED]->(p:Product) WHERE toFloat(ur.rating) >= 4 AND toFloat(pr.rating) >= 4 AND toFloat(cr.rating) >= 4 AND peer <> u AND NOT (u)-[:RATED|VIEWED]->(p) WITH p, collect(DISTINCT seed.title)[0..3] AS seed_titles, count(DISTINCT peer) AS peer_count RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.description AS description, p.description_ja AS description_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, log(toFloat(peer_count) + 1) * 1.5 + coalesce(toFloat(p.avg_rating), 3.5) * 0.4 AS score, '' AS explanation, [] AS matched_attrs, {condition_matches:0, behavior_matches:peer_count, transition_peers:0, collaborative_peers:peer_count, shared_rated_attributes:0, shared_viewed_attributes:0, review_confirmations:0} AS reason_metrics, 'collaborative_filtering' AS recommendation_strategy, 'User -> shared high-rated product -> similar user -> candidate product' AS graph_path, seed_titles, 'behavior_only' AS recommendation_source ORDER BY score DESC LIMIT $limit","explanation":"Uses high ratings from users who share highly rated products with this user."}

Example 3 — P4 review-confirmed attributes
Input: Recommend products whose shared attributes are confirmed by positive reviews.
Output:
{"cypher":"MATCH (u:User {user_id: $uid})-[r:RATED]->(seed:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)<-[:MENTIONS {sentiment:'positive'}]-(rev:Review)-[:ABOUT]->(p:Product) WHERE toFloat(r.rating) >= 4 AND NOT a.attr_type IN $ignored_attr_types AND NOT (u)-[:RATED|VIEWED]->(p) WITH p, collect(DISTINCT seed.title)[0..3] AS seed_titles, collect(DISTINCT a)[0..8] AS attrs, count(DISTINCT rev) AS confirmations WITH p, seed_titles, attrs, confirmations, size(attrs) AS shared RETURN p.product_id AS product_id, p.title AS title, p.title_ja AS title_ja, p.description AS description, p.description_ja AS description_ja, p.image_url AS image_url, p.price AS price, p.avg_rating AS avg_rating, p.rating_count AS rating_count, toFloat(shared) + log(toFloat(confirmations) + 1) * 1.2 + coalesce(toFloat(p.avg_rating), 3.5) * 0.4 AS score, '' AS explanation, [a IN attrs | {attr_type:a.attr_type, value:a.value, value_ja:a.value_ja}] AS matched_attrs, {condition_matches:0, behavior_matches:shared, transition_peers:0, collaborative_peers:0, shared_rated_attributes:shared, shared_viewed_attributes:0, review_confirmations:confirmations} AS reason_metrics, 'review_confirmed_attribute' AS recommendation_strategy, 'User -> high-rated product -> shared attribute <- positive review -> candidate product' AS graph_path, seed_titles, 'behavior_only' AS recommendation_source ORDER BY score DESC LIMIT $limit","explanation":"Uses meaningful attributes shared with liked products and independently confirmed in positive reviews."}
"""


_HOME_TEXT2CYPHER_RULES = """\
## Mandatory generation rules
- Generate one READ-ONLY Neo4j 5 Cypher query. Never use CREATE, MERGE, DELETE,
  DETACH, SET, REMOVE, DROP, LOAD CSV, FOREACH, or write procedures.
- Reference the current user only as $uid. Never embed a literal user_id.
- Use only $uid, $limit, and $ignored_attr_types parameters. Do not invent other parameters.
- Use only P1-P5. Choose one primary path and no more than one secondary path.
- Prefer rating >= 4 as the liked-history threshold.
- Any HAS_ATTRIBUTE path must exclude a.attr_type values in $ignored_attr_types.
- When the supplied history contains domain_product_type, P1 candidates must
  share at least one domain_product_type value with a highly rated seed. This
  prevents game accessories from outranking games solely through platform overlap.
- Exclude candidates already connected from this user by RATED or VIEWED.
- Do not hard-filter candidates by guessed attribute values. Traverse from the
  user's actual history instead.
- Do not add a positive attr_type whitelist such as `a.attr_type IN [...]` to
  P1/P4. The shared path must keep domain_product_type observable; exclude only
  the supplied $ignored_attr_types.
- Avoid unbounded Cartesian products. Aggregate before combining secondary evidence.
- The final RETURN must contain these aliases exactly:
  product_id, title, title_ja, description, description_ja, image_url, price,
  avg_rating, rating_count, score, explanation, matched_attrs, reason_metrics,
  recommendation_strategy, graph_path, seed_titles, recommendation_source.
- matched_attrs must be a list of maps with attr_type, value, and value_ja.
- reason_metrics must contain condition_matches, behavior_matches,
  transition_peers, collaborative_peers, shared_rated_attributes,
  shared_viewed_attributes, and review_confirmations.
- recommendation_source must be the literal 'behavior_only'.
- End exactly with ORDER BY score DESC LIMIT $limit.
- Keep the per-row explanation as an empty string. A separate grounded step
  explains the final executed query and products after Neo4j succeeds.

Return JSON only:
{"cypher":"<valid Cypher>","explanation":"<short technical intent>"}
"""

_HOME_REQUIRED_RETURN_ALIASES = (
    "product_id",
    "title",
    "title_ja",
    "description",
    "description_ja",
    "image_url",
    "price",
    "avg_rating",
    "rating_count",
    "score",
    "explanation",
    "matched_attrs",
    "reason_metrics",
    "recommendation_strategy",
    "graph_path",
    "seed_titles",
    "recommendation_source",
)


def _build_home_prompt(
    genre: str,
    user_ctx: dict,
    attr_vocab_text: str = "",
    lang: str = "en",
    dynamic_few_shot: list[dict] | None = None,
) -> str:
    """呼び出し元(recommend_home())は履歴が無いユーザーにはLLMを呼ばず直接人気商品を
    返すため、このプロンプトは常に実履歴のあるuser_ctxが渡される前提で組み立てる。"""
    parts = [
        f"You generate constrained home-recommendation Cypher for a Neo4j {genre} knowledge graph.\n"
        "The user did not enter a search query. Select a graph path that is grounded in the "
        "provided rating/view history and returns unseen personalized candidates.",
        f"## Graph Schema\n{_SCHEMA}",
    ]
    if attr_vocab_text:
        parts.append(attr_vocab_text)
    parts.append(_HOME_PATH_CATALOG)
    parts.append(_HOME_TEXT2CYPHER_FEW_SHOTS)
    # Search-time dynamic few-shots may target an old graph schema or dialogue
    # conditions. They are intentionally excluded from the home generator.
    parts.append(_format_user_ctx(user_ctx))
    parts.append(
        "## Task\n"
        "Generate a personalized home query now. Prefer the path whose evidence "
        "is actually present in the supplied user context."
    )
    parts.append(_HOME_TEXT2CYPHER_RULES)
    return "\n\n".join(parts)


def _format_user_ctx(ctx: dict) -> str:
    lines = ["## User Context"]
    if ctx.get("rated"):
        lines.append("Rated products (high rating first):")
        for p in ctx["rated"]:
            product_id = p.get("product_id")
            suffix = f" (product_id={product_id})" if product_id else ""
            lines.append(f"  [{p['rating']:.1f}★] {p['title']}{suffix}")
            for attr in p.get("attributes", [])[:8]:
                if attr.get("attr_type") and attr.get("value"):
                    lines.append(f"    - {attr['attr_type']}: {attr['value']}")
    if ctx.get("viewed"):
        lines.append("Recently viewed:")
        for p in ctx["viewed"]:
            lines.append(f"  {p['title']}")
    if ctx.get("preferred_attrs"):
        lines.append("Inferred preferred attributes (from 4+ star ratings):")
        for a in ctx["preferred_attrs"]:
            lines.append(f"  {a['attr_type']}: {a['value']}  (×{a['freq']})")
    if ctx.get("recent_queries"):
        lines.append("Recent searches (newest first):")
        for q in ctx["recent_queries"]:
            lines.append(f'  "{q}"')
    return "\n".join(lines)


def _build_cypher_explanation_prompt(cypher: str, lang: str) -> tuple[str, str]:
    target = "Japanese" if _normalize_lang(lang) == "ja" else "English"
    system = f"""\
You explain the FINAL Cypher query that a recommendation system actually ran.
Write the UI text in {target}. Explain only behavior visible in the query; never
describe an intended path that the query did not execute.

Cover these points when present:
1. which history signals are used,
2. the graph path traversed,
3. candidate exclusions or thresholds,
4. the ranking signals.

Rules:
- Do not translate Cypher line by line.
- In the summary, avoid syntax words such as MATCH, WITH, collect, and OPTIONAL MATCH.
- Render RATED as rating history, VIEWED as viewing history, HAS_ATTRIBUTE as
  product attributes, and MENTIONS as review mentions.
- Never say the user purchased a product: the graph records ratings and views.
- Never expose user identifiers or parameter values.
- Do not add a franchise, title, filter, or ranking factor absent from the query.
- A list slice such as [0..6] is a display limit, not an observed count. Never
  describe its bound as the number of matched attributes or products.
- summary should be one clear sentence, roughly 60-150 Japanese characters when Japanese.

Return JSON only:
{{
  "summary": "...",
  "graph_path": "User -> ... -> Candidate Product",
  "history_used": ["..."],
  "filters": ["..."],
  "ranking": ["..."]
}}
"""
    return system, f"Explain this final executed Cypher:\n\n{cypher}"


def _build_home_reason_prompt(results: list[Recommendation], lang: str) -> tuple[str, str]:
    target = "Japanese" if _normalize_lang(lang) == "ja" else "English"
    evidence = []
    for rec in results:
        raw_attrs = [
            {"attr_type": attr.attr_type, "value": attr.value}
            for attr in rec.matched_attrs[:8]
        ]
        platform_values = {
            attr["value"] for attr in raw_attrs if attr["attr_type"] == "domain_platform"
        }
        # Multi-platform evidence is often technically true but produces a
        # misleading sentence for one concrete SKU. Prefer franchise/gameplay
        # evidence unless the candidate has one unambiguous normalized platform.
        if len(platform_values) != 1:
            raw_attrs = [
                attr for attr in raw_attrs if attr["attr_type"] != "domain_platform"
            ]
        evidence.append(
            {
                "product_id": rec.product_id,
                "candidate_title": rec.display_title or rec.title,
                "strategy": rec.recommendation_strategy,
                "graph_path": rec.graph_path,
                "seed_titles": rec.seed_titles[:3],
                "matched_attributes": raw_attrs[:6],
                "reason_metrics": rec.reason_metrics.model_dump(),
            }
        )
    system = f"""\
You turn executed knowledge-graph evidence into grounded product recommendation
reasons. Write every reason in {target}.

Rules:
- Use only the supplied evidence for that product.
- Mention a seed title or matched series/platform only when explicitly supplied.
- Prefer franchise, genre, game mode, or gameplay evidence. Mention platform
  only when one unambiguous normalized platform is supplied.
- Say rated/highly rated or viewed; never claim the user purchased or played it.
- Do not expose graph terms, Cypher, internal strategy names, metrics, or user IDs.
- Prefer one natural sentence per product. Avoid repeating an identical opening.
- If evidence is sparse, use a conservative statement instead of inventing detail.

Return JSON only:
{{"reasons":[{{"product_id":"...","explanation":"..."}}]}}
"""
    return system, json.dumps({"products": evidence}, ensure_ascii=False)


def _string_list(value: Any, max_items: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
        if len(values) >= max_items:
            break
    return values


def _sanitize_home_cypher(cypher: str) -> str:
    """Remove a common local-LLM overconstraint without changing the chosen path.

    Some models copy the graph vocabulary into a second positive ``attr_type``
    whitelist even though the prompt explicitly forbids it. Besides reducing
    recall, they sometimes emit that whitelist as a second WHERE clause. The
    home contract already supplies the safe negative list, so discard only this
    redundant positive restriction before validation.
    """
    cleaned = re.sub(
        r"\s+AND\s+\(seed\)-\[:HAS_ATTRIBUTE\]->\(a\)",
        "",
        cypher,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+AND\s+a\.attr_type\s+IN\s*\[[^\]]*\]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+WHERE\s+a\.attr_type\s+IN\s*\[[^\]]*\](?=\s+WITH\b)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _format_chat_user_profile(ctx: dict | None) -> str:
    """Compact private profile for choosing a useful dialogue follow-up."""
    if not ctx or not (ctx.get("rated") or ctx.get("viewed") or ctx.get("preferred_attrs")):
        return ""
    lines = [
        "## Optional User Profile (secondary signal, never overrides current dialogue)",
    ]
    if ctx.get("rated"):
        titles = ", ".join(str(p.get("title", "")) for p in ctx["rated"][:3])
        if titles:
            lines.append(f"Recent/high-rated examples: {titles}")
    if ctx.get("preferred_attrs"):
        attrs = ", ".join(
            f"{a.get('attr_type')}: {a.get('value')}"
            for a in ctx["preferred_attrs"][:5]
            if a.get("attr_type") and a.get("value")
        )
        if attrs:
            lines.append(f"Observed history signals: {attrs}")
    return "\n".join(lines)


def _format_dynamic_few_shot(examples: list[dict]) -> str:
    """Format past successful (query, cypher) pairs as user-specific few-shot examples."""
    lines = ["## This User's Past Successful Queries (led to clicks — prioritize similar patterns)"]
    for ex in examples:
        q = ex.get("query", "")
        c = ex.get("cypher", "")
        e = ex.get("explanation", "")
        lines.append(f'User: "{q}"')
        lines.append(json.dumps({"cypher": c, "explanation": e}))
        lines.append("")
    return "\n".join(lines)


# ── env / config helpers ───────────────────────────────────────────────────────

def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_PROVIDER_DEFAULTS: dict[str, tuple[str, str | None, str]] = {
    "gemini": (
        "GEMINI_API_KEY",
        None,
        "gemini-2.0-flash",
    ),
    "groq": (
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com",
        "deepseek-chat",
    ),
    "openai": (
        "OPENAI_API_KEY",
        None,
        "gpt-4o-mini",
    ),
    "ollama": (
        "OLLAMA_API_KEY",
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "llama3.2",
    ),
}


# ── Gemini native SDK wrapper ──────────────────────────────────────────────────

class _GeminiMessage:
    def __init__(self, text: str) -> None:
        self.content = text

class _GeminiChoice:
    def __init__(self, text: str) -> None:
        self.message = _GeminiMessage(text)

class _GeminiUsage:
    def __init__(self, meta: Any) -> None:
        self.input_tokens  = int(getattr(meta, "prompt_token_count",     0) or 0)
        self.output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
        self.total_tokens  = int(getattr(meta, "total_token_count",      0) or 0)

class _GeminiResponse:
    def __init__(self, resp: Any) -> None:
        self.choices = [_GeminiChoice(getattr(resp, "text", "") or "")]
        self.usage   = _GeminiUsage(getattr(resp, "usage_metadata", None))

class _GeminiCompletions:
    def __init__(self, client: Any, types_mod: Any) -> None:
        self._client = client
        self._types  = types_mod

    def create(
        self, model: str, messages: list,
        response_format: dict | None = None,
        temperature: float = 0,
        max_tokens: int | None = None,
        **_: Any,
    ) -> _GeminiResponse:
        types = self._types
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        system_instruction = system_parts[0] if system_parts else None
        contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = "model" if m["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
        mime = "application/json" if (response_format or {}).get("type") == "json_object" else None
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type=mime,
        )
        resp = self._client.models.generate_content(
            model=model, contents=contents, config=config
        )
        return _GeminiResponse(resp)

class _GeminiChat:
    def __init__(self, completions: _GeminiCompletions) -> None:
        self.completions = completions

class _GeminiCompat:
    def __init__(self, api_key: str) -> None:
        try:
            from google import genai as _genai
            from google.genai import types as _types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai not installed. Run: pip install google-genai"
            ) from exc
        self.chat = _GeminiChat(
            _GeminiCompletions(_genai.Client(api_key=api_key), _types)
        )


def _build_llm_client(provider: str, model: str | None, base_url: str | None):
    """Return (client, resolved_model). Client exposes OpenAI-compatible interface."""
    provider = provider.lower().strip()
    if provider not in _PROVIDER_DEFAULTS:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Choose from: {list(_PROVIDER_DEFAULTS)}"
        )
    env_key, default_base_url, default_model = _PROVIDER_DEFAULTS[provider]
    resolved_model = model or default_model

    api_key = os.environ.get(env_key)
    if not api_key:
        if provider == "ollama":
            api_key = "ollama"  # Ollama ignores the key; OpenAI SDK needs a non-empty string
        else:
            raise RuntimeError(
                f"API key not found. Set {env_key} in your .env file.\n"
                f"  cp .env.example .env  # then fill in {env_key}"
            )

    if provider == "gemini":
        return _GeminiCompat(api_key), resolved_model

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai not installed. Run: pip install openai") from exc

    resolved_base_url = base_url or default_base_url
    kwargs: dict[str, Any] = {"api_key": api_key}
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    return OpenAI(**kwargs), resolved_model


def _create_json_completion(client: Any, **kwargs: Any) -> Any:
    """Request JSON mode, retrying without it for LM Studio model runtimes.

    Some LM Studio backends accept only ``json_schema`` or plain text rather
    than OpenAI's ``json_object`` mode. Every caller already gives an explicit
    JSON-only prompt, so a plain-text retry remains parseable and keeps local
    development independent of the loaded model's response-format support.
    """
    if getattr(client, "_am_plain_json_only", False):
        return client.chat.completions.create(**kwargs)
    try:
        return client.chat.completions.create(
            **kwargs,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        message = str(exc).lower()
        if "response_format" not in message and "json_object" not in message:
            raise
        try:
            setattr(client, "_am_plain_json_only", True)
        except Exception:
            pass
        return client.chat.completions.create(**kwargs)


# ── JSON / Cypher parsing ──────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


# ── result mapping ─────────────────────────────────────────────────────────────

# matched_attrsに入るdomain_*属性のcanonical値（"nintendo_switch"等）は機械生成の
# 正規化値でvalue_jaを持たないため、表示用の変換表を持つ。プラットフォーム名は
# 日本のストアフロントでもローマ字表記が標準なので日英共通。ここに無い値は
# _prettify_attr_value()がスネークケースを整形する（"action_rpg"→"Action RPG"）。
_ATTR_VALUE_DISPLAY: dict[str, str] = {
    # platform（日英共通のローマ字表記）
    "nintendo_switch": "Nintendo Switch",
    "ps5": "PlayStation 5",
    "ps4": "PlayStation 4",
    "ps3": "PlayStation 3",
    "playstation_vita": "PlayStation Vita",
    "xbox_series_x": "Xbox Series X|S",
    "xbox_one": "Xbox One",
    "xbox_360": "Xbox 360",
    "wii_u": "Wii U",
    "wii": "Wii",
    "nintendo_3ds": "Nintendo 3DS",
    "nintendo_ds": "Nintendo DS",
    "pc": "PC",
}

_ATTR_VALUE_DISPLAY_JA: dict[str, str] = {
    # product_type
    "video_game": "ゲームソフト",
    "controller": "コントローラー",
    "headset": "ヘッドセット",
    "console": "ゲーム機本体",
    "storage": "ストレージ",
    "accessory": "アクセサリ",
    # franchise（_DOMAIN_ALIASESのcanonical値）
    "mario": "マリオ",
    "zelda": "ゼルダ",
    "pokemon": "ポケモン",
    "sonic": "ソニック",
    "minecraft": "マインクラフト",
    "final_fantasy": "ファイナルファンタジー",
    "spider_man": "スパイダーマン",
    "animal_crossing": "どうぶつの森",
    "kirby": "カービィ",
    "splatoon": "スプラトゥーン",
    "lego": "レゴ",
    "star_wars": "スターウォーズ",
    "assassins_creed": "アサシンクリード",
    # genre系のよく出るcanonical値
    "action": "アクション",
    "adventure": "アドベンチャー",
    "action_adventure": "アクションアドベンチャー",
    "rpg": "RPG",
    "action_rpg": "アクションRPG",
    "jrpg": "JRPG",
    "puzzle": "パズル",
    "shooter": "シューティング",
    "first_person_shooter": "FPS",
    "racing": "レース",
    "sports": "スポーツ",
    "simulation": "シミュレーション",
    "fighting": "格闘",
    "horror": "ホラー",
    "survival_horror": "サバイバルホラー",
    "strategy": "ストラテジー",
    "platformer": "プラットフォーマー",
    "party": "パーティ",
    "rhythm": "リズム",
    "stealth": "ステルス",
    "open_world": "オープンワールド",
    "sandbox": "サンドボックス",
}

_ACRONYMS = {"rpg", "fps", "3ds", "ds", "pc", "hd", "4k", "vr", "dlc", "2d", "3d"}


def _prettify_attr_value(value: str) -> str:
    """スネークケースのcanonical値を表示用に整形（"action_rpg"→"Action RPG"）。"""
    words = re.split(r"[\s_]+", value.strip())
    return " ".join(
        w.upper() if w.lower() in _ACRONYMS else w.capitalize() for w in words if w
    )


def _attr_display_value(raw_value: str, value_ja: Any, lang: str) -> str:
    """matched_attrsの1値を表示用文字列に変換する。優先順:
    グラフ由来のvalue_ja(ja時) > 日英共通表 > 日本語表(ja時) > スネークケース整形。
    キーは正規化（小文字・空白/ハイフン→アンダースコア）してから引くので、
    "Nintendo 3DS"と"nintendo_3ds"は同じ表示に揃い、重複排除にも掛かる。"""
    if lang == "ja" and value_ja:
        return str(value_ja)
    key = re.sub(r"[\s\-]+", "_", raw_value.strip().lower())
    if key in _ATTR_VALUE_DISPLAY:
        return _ATTR_VALUE_DISPLAY[key]
    if lang == "ja" and key in _ATTR_VALUE_DISPLAY_JA:
        return _ATTR_VALUE_DISPLAY_JA[key]
    if "_" in raw_value or raw_value.islower():
        return _prettify_attr_value(raw_value)
    return raw_value


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_to_recommendation(record: Any, lang: str = "en") -> Recommendation:
    matched_attrs: list[MatchedAttr] = []
    seen_attrs: set[tuple[str, str]] = set()
    for m in (record.get("matched_attrs") or []):
        if isinstance(m, dict) and m.get("attr_type") and m.get("value"):
            display_value = _attr_display_value(str(m["value"]), m.get("value_ja"), lang)
            key = (str(m["attr_type"]), display_value)
            if key in seen_attrs:
                continue
            seen_attrs.add(key)
            matched_attrs.append(
                MatchedAttr(attr_type=str(m["attr_type"]), value=display_value)
            )
    title_ja = record.get("title_ja") or None
    description = record.get("description")
    description_ja = record.get("description_ja")
    display_description = description_ja if (lang == "ja" and description_ja) else description
    raw_metrics = record.get("reason_metrics") or {}
    if not isinstance(raw_metrics, dict):
        raw_metrics = {}
    metric_names = (
        "condition_matches",
        "behavior_matches",
        "transition_peers",
        "collaborative_peers",
        "shared_rated_attributes",
        "shared_viewed_attributes",
        "review_confirmations",
    )
    reason_metrics = ReasonMetrics(
        **{name: _to_int(raw_metrics.get(name)) or 0 for name in metric_names}
    )
    return Recommendation(
        product_id=str(record.get("product_id", "")),
        title=str(record.get("title", "")),
        display_title=title_ja if (lang == "ja" and title_ja) else None,
        description=_strip_html(display_description) or None,
        image_url=record.get("image_url") or None,
        price=_to_float(record.get("price")),
        avg_rating=_to_float(record.get("avg_rating")),
        rating_count=_to_int(record.get("rating_count")),
        score=float(record.get("score") or 0.0),
        matched_attrs=matched_attrs,
        reason_metrics=reason_metrics,
        explanation=str(record.get("explanation", "")),
        recommendation_source=str(record.get("recommendation_source") or "popular"),
        recommendation_strategy=(
            str(record.get("recommendation_strategy"))
            if record.get("recommendation_strategy")
            else None
        ),
        graph_path=(str(record.get("graph_path")) if record.get("graph_path") else None),
        seed_titles=_string_list(record.get("seed_titles"), max_items=3),
    )


# ── conversational recommendation (CRS) — chat constants ────────────────────────

# 対話型推薦：聞き返しは最大この回数まで（LLMがaction判断に失敗し続けた場合の安全網）
MAX_QUESTIONS = 5
# 商品カテゴリを除き、短期的な希望をこの数だけ確認してから最終結果へ進む。
# ASK中にも暫定候補を返すため、質問を増やしても結果を待たせるだけにはならない。
MIN_CONFIRMED_PREFERENCES = 3


def _normalize_lang(lang: str | None) -> str:
    return "ja" if (lang or "").lower().startswith("ja") else "en"


def _build_chat_system_prompt(
    genre: str, attr_vocab_text: str, target_language: str, user_profile: str = ""
) -> str:
    """カテゴリ非依存のチャットプロンプト。

    どんな属性を聞くべきかはハードコードせず、グラフに実際にある attr_type 語彙
    (attr_vocab_text) から都度組み立てる。ask/search の判断・スロット追跡もすべて
    LLM自身のfilled_slots/actionに委ね、Python側はMAX_QUESTIONSの安全網のみ持つ。
    """
    vocab_section = attr_vocab_text or (
        "(no attribute data available yet in the graph — ask about general product preferences)"
    )
    return f"""You are a friendly shopping assistant for a catalog of {genre} products. \
The catalog itself is in English; the user may write in Japanese or English. \
Write everything you SHOW the user (question, options, preference_summary) in {target_language}.

{vocab_section}

{user_profile}

Through conversation, ask about 3-4 clarifying preferences that would help rank a
search in the catalog above. Infer the likely product category from the conversation and
prioritize the attribute types most relevant to that category from the list above (plus
price range or minimum rating if it seems relevant) — do not ask about an attribute type
that clearly doesn't apply to what the user is looking for.

Ask ONE question at a time, with 3-5 quick-reply options written in {target_language}.
ALWAYS include one "no preference / skip this" option as the last option.

DECISION RULE:
- filled_slots = number of DISTINCT preferences the user has explicitly confirmed so far
  (do not count the product category itself, and do not count a "no preference" answer).
- action = "ask" while filled_slots < {MIN_CONFIRMED_PREFERENCES} AND fewer than {MAX_QUESTIONS} questions have been
  asked so far AND the user hasn't said they have no preferences at all.
- action = "search" once filled_slots >= {MIN_CONFIRMED_PREFERENCES}, OR the user said they have no preference at
  all, OR {MAX_QUESTIONS} questions have already been asked.
- Use the full conversation history (including your own prior questions) to avoid asking
  about something already answered or already skipped.
- The current dialogue is the user's primary intent. A profile is only a secondary
  signal for choosing a useful follow-up question; never replace, contradict, or
  silently add a preference that the user did not state in this conversation.
- When an optional profile is available, prefer a follow-up dimension that can
  distinguish its observed platforms, franchises, or play styles. You may use
  them as answer options, but always retain a neutral/no-preference option and
  do not claim that the user has already chosen one.

Always include "preference_summary": ALL of the user's confirmed preferences accumulated
across the ENTIRE conversation so far (not just this latest turn) as short labels for
display, written in {target_language} only (do not mix languages). This MUST include
the product category/type itself if it's known (e.g. what kind of product the user
originally asked for), in addition to every other preference confirmed in any turn.

CONVERSATION HISTORY NOTE: previous assistant messages in the history contain only the
question text shown to the user. This does NOT mean you should respond in plain text —
you MUST ALWAYS respond with a valid JSON object.

Return ONLY this JSON object (no other text before or after):
{{
  "action": "ask" | "search",
  "question": "(if action=ask) the question text, otherwise null",
  "options": ["(if action=ask) quick-reply options"],
  "slot": "(a short snake_case name for what this question is asking about) | null",
  "filled_slots": <integer>,
  "preference_summary": []
}}"""


def _strip_html(text: str | None) -> str | None:
    if text is None:
        return None
    return _html_mod.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# ── Recommender ────────────────────────────────────────────────────────────────

class Recommender:
    def __init__(self, config_path: Path | None = None) -> None:
        cfg_path = config_path or (Path(__file__).parent.parent.parent / "config.yaml")
        cfg: dict = {}
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

        _load_env(cfg_path.parent / ".env")

        llm_cfg: dict = cfg.get("llm", {})
        t2c_cfg: dict = cfg.get("text2cypher", {})
        neo4j_cfg: dict = cfg.get("neo4j", {})

        # Environment overrides make the same branch usable with LM Studio or
        # a remote OpenAI-compatible endpoint without editing shared config.yaml.
        provider = str(os.environ.get("LLM_PROVIDER") or llm_cfg.get("provider", "gemini"))
        model = os.environ.get("LLM_MODEL") or llm_cfg.get("model") or None
        base_url = os.environ.get("LLM_BASE_URL") or llm_cfg.get("base_url") or None
        self._llm, self._model = _build_llm_client(provider, model, base_url)
        self._max_attempts: int = int(t2c_cfg.get("max_cypher_attempts", 3))
        self._chat_temperature = float(llm_cfg.get("chat_temperature", 0.35))
        self._structured_temperature = float(llm_cfg.get("structured_temperature", 0.0))

        neo4j_uri = neo4j_cfg.get("uri") or os.environ.get("NEO4J_URI", "")
        neo4j_user = (
            os.environ.get("NEO4J_USERNAME") or neo4j_cfg.get("username", "neo4j")
        )
        neo4j_password = (
            os.environ.get("NEO4J_PASSWORD") or neo4j_cfg.get("password", "")
        )
        self._neo4j_db = (
            os.environ.get("NEO4J_DATABASE") or neo4j_cfg.get("database", "neo4j")
        )

        if not neo4j_uri:
            raise RuntimeError("neo4j.uri is not set in config.yaml")
        if not neo4j_password:
            raise RuntimeError("NEO4J_PASSWORD is not set in .env")

        self._driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self._genre: str = str(cfg.get("genre", "products"))
        self._attr_vocab_text: str | None = None  # lazily populated, see _get_attr_vocab_text()
        self._home_cache: dict[str, dict[str, Any]] = {}  # see _get_or_generate_home()

    # ── public API ──────────────────────────────────────────────────────────────

    def graph_readiness(self) -> dict[str, Any]:
        """Check the graph capabilities required by the current v3 recommender.

        This is deliberately capability-based rather than trusting a local file
        name: a stale Neo4j volume must be observable before it reaches a demo.
        """
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                node_counts = {
                    str(r["label"] or "unlabeled"): int(r["count"])
                    for r in session.run(
                        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count"
                    )
                }
                domain_coverage = {kind: 0 for kind in _DOMAIN_ATTR_TYPES}
                for record in session.run(
                    "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "WHERE a.attr_type IN $domain_types "
                    "RETURN a.attr_type AS attr_type, count(DISTINCT p) AS products",
                    domain_types=_DOMAIN_ATTR_TYPES,
                ):
                    domain_coverage[str(record["attr_type"])] = int(record["products"])
                product_row = session.run(
                    "MATCH (p:Product) "
                    "RETURN count(p) AS total, "
                    "count(CASE WHEN coalesce(p.description_ja, '') <> '' THEN 1 END) AS ja_count"
                ).single()
                review_row = session.run(
                    "MATCH (r:Review) "
                    "RETURN count(r) AS total, "
                    "count(CASE WHEN coalesce(r.text_ja, '') <> '' THEN 1 END) AS ja_count"
                ).single()
        except Exception as exc:
            return {
                "status": "degraded",
                "graph_profile": "unavailable",
                "node_counts": {},
                "domain_coverage": {},
                "japanese_description_coverage": 0.0,
                "japanese_review_coverage": 0.0,
                "issues": [f"Neo4j connection or schema check failed: {exc.__class__.__name__}"],
            }

        product_total = int(product_row["total"] or 0) if product_row else 0
        product_ja = int(product_row["ja_count"] or 0) if product_row else 0
        review_total = int(review_row["total"] or 0) if review_row else 0
        review_ja = int(review_row["ja_count"] or 0) if review_row else 0
        description_coverage = product_ja / product_total if product_total else 0.0
        review_coverage = review_ja / review_total if review_total else 0.0
        issues: list[str] = []
        if product_total == 0:
            issues.append("No Product nodes found")
        if description_coverage < 0.9:
            issues.append("Japanese product description coverage is below 90%")
        missing_domains = [kind for kind, count in domain_coverage.items() if count == 0]
        if missing_domains:
            issues.append(f"Missing normalized domain attributes: {', '.join(missing_domains)}")
        ready = not issues
        return {
            "status": "ready" if ready else "degraded",
            "graph_profile": "video_games_v3_compatible" if ready else "unknown_or_legacy",
            "node_counts": node_counts,
            "domain_coverage": domain_coverage,
            "japanese_description_coverage": round(description_coverage, 4),
            "japanese_review_coverage": round(review_coverage, 4),
            "issues": issues,
        }

    def recommend(
        self, query: str, user_id: str | None = None, limit: int = 10, lang: str = "en"
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        fallback = False
        normalized_lang = _normalize_lang(lang)
        user_ctx = self._get_user_context(user_id) if user_id else None
        # $uidが使えるのはuser_idがあり、かつ実際にRATED/属性の履歴がある場合のみ
        # （履歴が無ければ$uidを束縛しても個人化の意味が無く、誤ってuidを使われるのを防ぐ）
        has_uid = bool(user_ctx and (user_ctx.get("rated") or user_ctx.get("viewed") or user_ctx.get("preferred_attrs")))

        cypher, explanation, results, diagnostics = self._run_metapath_recommendation(
            query, user_id if has_uid else None, limit, normalized_lang
        )
        if results:
            intent = SearchIntent(cypher=cypher, cypher_explanation=explanation, **diagnostics)
            if user_id:
                self.log_search(user_id, search_id, query, cypher, explanation, [r.product_id for r in results])
            return search_id, intent, results, fallback

        # A search request must never silently turn into a history-only or
        # popularity-only list.  Returning no results is preferable to claiming
        # that an unrelated game satisfies the user's stated dialogue needs.
        if query.strip():
            intent = SearchIntent(cypher=cypher, cypher_explanation=explanation, **diagnostics)
            if user_id:
                self.log_search(user_id, search_id, query, cypher, explanation, [])
            return search_id, intent, [], False

        # Empty queries are not semantic searches. Keep their behavior explicit
        # rather than routing them through the obsolete free-form Text2Cypher path.
        cypher, explanation = _FALLBACK_CYPHER, _popular_explanation(normalized_lang)
        results = self._run_popular(limit, normalized_lang)
        fallback = True
        intent = SearchIntent(
            cypher=cypher,
            cypher_explanation=explanation,
            condition_source="none",
            retrieval_status="fallback_popular",
            no_result_reason=None,
        )
        if user_id:
            self.log_search(user_id, search_id, query, cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    def recommend_dialogue(
        self, query: str, limit: int = 10, lang: str = "ja"
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        """Recommend from the current conversation without behavior history.

        Product type, platform, and franchise are the only exclusion gates.
        Open-vocabulary preferences such as genre, play style, mood, and
        difficulty contribute to ranking but never remove an otherwise valid
        domain candidate.
        """
        search_id = str(uuid.uuid4())
        normalized_lang = _normalize_lang(lang)
        cypher, explanation, results, diagnostics = self._run_metapath_recommendation(
            query,
            None,
            limit,
            normalized_lang,
            dialogue_soft_preferences=True,
        )
        intent = SearchIntent(cypher=cypher, cypher_explanation=explanation, **diagnostics)
        return search_id, intent, results, False

    _HOME_CACHE_TTL_SECONDS = 3600  # RATEDはこのデモでは実行時に変化しないので長めでよい

    def _explain_executed_cypher(
        self, cypher: str, lang: str, fallback: str = ""
    ) -> dict[str, Any]:
        """Explain the final validated Cypher, not the LLM's initial intention."""
        default_summary = fallback or (
            "評価・閲覧履歴から知識グラフをたどり、未確認の商品を順位付けしました。"
            if _normalize_lang(lang) == "ja"
            else "Traversed the knowledge graph from rating and viewing history to rank unseen products."
        )
        try:
            system, user = _build_cypher_explanation_prompt(cypher, lang)
            data = self._call_llm(system, user)
        except Exception as exc:
            print(f"[recommender] Cypher explanation failed: {exc}", file=sys.stderr)
            data = {}
        ja = _normalize_lang(lang) == "ja"
        history_used = _string_list(data.get("history_used"))
        if not history_used and re.search(r"\[:RATED", cypher, re.IGNORECASE):
            history_used.append("評価履歴" if ja else "Rating history")
        if re.search(r"\[:VIEWED", cypher, re.IGNORECASE) and not any(
            ("閲覧" in item if ja else "view" in item.lower()) for item in history_used
        ):
            history_used.append("閲覧履歴" if ja else "Viewing history")
        filters = _string_list(data.get("filters"))
        if not filters and re.search(r"NOT\s*\(u\)-\[:RATED\|VIEWED\]", cypher, re.IGNORECASE):
            filters.append(
                "評価・閲覧済みの商品を除外" if ja else "Excludes rated and viewed products"
            )
        return {
            "summary": str(data.get("summary") or default_summary).strip(),
            "graph_path": (str(data.get("graph_path")).strip() if data.get("graph_path") else None),
            "history_used": history_used,
            "filters": filters,
            "ranking": _string_list(data.get("ranking")),
        }

    @staticmethod
    def _fallback_product_reason(rec: Recommendation, lang: str) -> str:
        ja = _normalize_lang(lang) == "ja"
        seed = rec.seed_titles[0] if rec.seed_titles else None
        attr = rec.matched_attrs[0].value if rec.matched_attrs else None
        metrics = rec.reason_metrics
        if seed and attr:
            return (
                f"『{seed}』を高く評価しており、共通する「{attr}」属性を持つためおすすめです。"
                if ja
                else f"Recommended because it shares the {attr} attribute with {seed}, which you rated highly."
            )
        if metrics.transition_peers > 0:
            return (
                "同じ商品を好むユーザーが、その後に高く評価している候補です。"
                if ja else "Recommended because users with similar taste rated it highly afterwards."
            )
        if metrics.collaborative_peers > 0:
            return (
                "あなたと同じ商品を高く評価したユーザーにも支持されている候補です。"
                if ja else "Recommended because users who share your high ratings also rated it highly."
            )
        if metrics.review_confirmations > 0:
            return (
                "高評価した商品との共通点が、肯定的なレビューでも確認されている候補です。"
                if ja else "Recommended because a shared preference is also confirmed by positive reviews."
            )
        if seed:
            return (
                f"高く評価した『{seed}』から知識グラフをたどって見つけた候補です。"
                if ja else f"Found by traversing the knowledge graph from {seed}, which you rated highly."
            )
        return (
            "評価・閲覧履歴とのつながりを知識グラフ上で確認できた候補です。"
            if ja else "Recommended from a knowledge-graph connection to your rating or viewing history."
        )

    def _generate_home_product_reasons(
        self, results: list[Recommendation], lang: str
    ) -> list[Recommendation]:
        if not results:
            return results
        try:
            system, user = _build_home_reason_prompt(results, lang)
            data = self._call_llm(system, user)
            rows = data.get("reasons") if isinstance(data, dict) else None
        except Exception as exc:
            print(f"[recommender] product reason generation failed: {exc}", file=sys.stderr)
            rows = None
        generated: dict[str, str] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                product_id = str(row.get("product_id") or "").strip()
                explanation = str(row.get("explanation") or "").strip()
                if product_id and explanation:
                    generated[product_id] = explanation
        for rec in results:
            rec.explanation = generated.get(rec.product_id) or self._fallback_product_reason(rec, lang)
        return results

    def _get_or_generate_home(
        self, user_id: str, limit: int, lang: str
    ) -> tuple[str, str, list[Recommendation], dict[str, Any]] | None:
        """履歴のあるユーザー向けホーム推薦をキャッシュ優先で返す（生成に失敗したらNone）。

        recommend_home()（通常の表示・SearchLogに残る）とwarm_home_cache()（タブを
        バックグラウンドに回した時などの先読み・ログに残さない）の両方から使う共通ロジック。
        """
        cache_key = f"{user_id}:{lang}:{limit}"
        cached = self._home_cache.get(cache_key)
        if cached and (time.time() - cached["cached_at"]) < self._HOME_CACHE_TTL_SECONDS:
            return (
                cached["cypher"], cached["explanation"], cached["results"],
                cached.get("explanation_details", {}),
            )

        user_ctx = self._get_user_context(user_id)
        try:
            system_prompt = _build_home_prompt(
                self._genre, user_ctx, self._get_attr_vocab_text(), lang
            )
            cypher, intended_explanation, results = self._generate_cypher_and_execute(
                system_prompt=system_prompt,
                user_msg="Generate a personalized home recommendation query from this user's graph history.",
                limit=limit,
                exec_params={
                    "limit": limit,
                    "uid": user_id,
                    "ignored_attr_types": _IGNORED_BEHAVIOR_ATTR_TYPES,
                },
                has_uid=True,
                lang=lang,
                fix_prompt=system_prompt,
                required_aliases=_HOME_REQUIRED_RETURN_ALIASES,
                sanitize_home=True,
            )
        except Exception as exc:
            print(f"[recommender] home Text2Cypher generation failed: {exc}", file=sys.stderr)
            return None
        if not results:
            return None
        explanation_details = self._explain_executed_cypher(
            cypher, lang, intended_explanation
        )
        explanation = str(explanation_details["summary"])
        results = self._generate_home_product_reasons(results, lang)
        self._home_cache[cache_key] = {
            "cypher": cypher,
            "explanation": explanation,
            "explanation_details": explanation_details,
            "results": results,
            "cached_at": time.time(),
        }
        return cypher, explanation, results, explanation_details

    def warm_home_cache(self, user_id: str | None, limit: int = 10, lang: str = "en") -> None:
        """タブを閉じる/バックグラウンドに回した時などに呼ばれるfire-and-forget用途。

        次回開いた時にrecommend_home()が即座に返せるよう、ホーム推薦を先読みして
        キャッシュしておく。応答を待つ相手がいないのでSearchLogには残さない。
        キャッシュが既に新しければ_get_or_generate_home()内で即returnされ、LLMは呼ばれない。
        """
        if not user_id:
            return  # 非個人化モード（user_id無し）はキャッシュ対象がないため何もしない
        normalized_lang = _normalize_lang(lang)
        user_ctx = self._get_user_context(user_id)
        has_history = bool(user_ctx.get("rated") or user_ctx.get("viewed") or user_ctx.get("preferred_attrs"))
        if not has_history:
            return  # 履歴が無いユーザーは人気商品フォールバックの高速パスで十分、キャッシュ不要
        self._get_or_generate_home(user_id, limit, normalized_lang)

    def recommend_home(
        self, user_id: str | None, limit: int = 10, lang: str = "en"
    ) -> tuple[str, SearchIntent, list[Recommendation], bool]:
        search_id = str(uuid.uuid4())
        normalized_lang = _normalize_lang(lang)
        # RATEDは評価履歴、VIEWEDはオンライン行動履歴として元パス推薦に使える。
        # user_id自体が無い（非個人化モード）場合はhas_history=Falseとして扱う。
        user_ctx = self._get_user_context(user_id) if user_id else None
        has_history = bool(user_ctx and (user_ctx.get("rated") or user_ctx.get("viewed") or user_ctx.get("preferred_attrs")))
        generated = self._get_or_generate_home(user_id, limit, normalized_lang) if has_history and user_id else None
        if generated:
            cypher, explanation, results, explanation_details = generated
            fallback = False
            intent = SearchIntent(
                cypher=cypher,
                cypher_explanation=explanation,
                graph_path=explanation_details.get("graph_path"),
                history_used=explanation_details.get("history_used", []),
                filters=explanation_details.get("filters", []),
                ranking=explanation_details.get("ranking", []),
                condition_source="none",
                retrieval_status="matched",
            )
        else:
            # has_history=Falseなら想定内（個人化する材料が無いだけ）、
            # has_history=Trueなのに生成に失敗した場合だけ本当のフォールバック扱いにする。
            cypher, explanation = _FALLBACK_CYPHER, _popular_explanation(normalized_lang)
            results = self._run_popular(limit, normalized_lang)
            fallback = has_history
            intent = SearchIntent(
                cypher=cypher,
                cypher_explanation=explanation,
                condition_source="none",
                retrieval_status="fallback_popular",
                no_result_reason=("No personalized graph candidates were available" if has_history else None),
            )
        if user_id:
            self.log_search(user_id, search_id, "[home]", cypher, explanation, [r.product_id for r in results])
        return search_id, intent, results, fallback

    def chat(
        self, messages: list[dict[str, Any]], limit: int = 10, lang: str = "ja",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """対話型推薦の1ターン。

        どの属性について聞くか・いつ検索に切り替えるかはハードコードせず、LLM自身の
        action/filled_slotsに委ねる（カテゴリ非依存）。Python側はMAX_QUESTIONSの
        安全網と、LLM呼び出し自体が失敗した場合に検索へフォールバックする処理のみ持つ。
        ASK中・SEARCH後のどちらも、履歴を使わない対話専用の固定検索へ委譲する。
        商品種別・機種・シリーズだけを必須条件とし、その他の希望は順位付けに使う。
        """
        all_user_msgs = [m for m in messages if m.get("role") == "user"]
        asked = sum(1 for m in messages if m.get("role") == "assistant")
        normalized_lang = _normalize_lang(lang)
        target_language = "Japanese" if normalized_lang == "ja" else "English"

        user_ctx = self._get_user_context(user_id) if user_id else None
        system = _build_chat_system_prompt(
            self._genre,
            self._get_attr_vocab_text(),
            target_language,
            _format_chat_user_profile(user_ctx),
        )
        llm_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in messages:
            role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
            llm_messages.append({"role": role, "content": m.get("content", "")})

        try:
            response = _create_json_completion(
                self._llm,
                model=self._model, messages=llm_messages,
                temperature=self._chat_temperature,
            )
            data = _parse_llm_json(response.choices[0].message.content or "{}")
        except Exception as exc:
            print(f"[recommender] chat LLM call failed, forcing search: {exc}", file=sys.stderr)
            data = {}

        summary = data.get("preference_summary") or []
        try:
            filled_slots = int(data.get("filled_slots", 0))
        except (TypeError, ValueError):
            filled_slots = 0

        # LLM呼び出し自体が失敗した場合(dataが空)は、聞き返しを続けられないので検索へ倒す
        should_search = (
            not data
            or data.get("action") == "search"
            or filled_slots >= MIN_CONFIRMED_PREFERENCES
            or asked >= MAX_QUESTIONS
        )

        # ASK中も会話条件だけから暫定候補を取得する。質問が進むたびに同じ
        # 固定検索を再実行し、必須条件で候補を保ちながら希望条件で順位を更新する。
        query_text = " ".join(m.get("content", "") for m in all_user_msgs)
        search_id, intent, products, fallback = self.recommend_dialogue(
            query_text, limit, normalized_lang
        )

        # ── 結果を返す：ASKは暫定、SEARCHは最終 ─────────────────────────────
        if should_search:
            return {
                "action": "search",
                "question": None,
                "options": [],
                "preference_summary": summary,
                "intent": intent,
                "recommendations": products,
                "search_id": search_id,
                "fallback": fallback,
                "provisional": False,
            }

        fallback_question = "他にご希望はありますか？" if normalized_lang == "ja" else "Any other preferences?"
        return {
            "action": "ask",
            "question": data.get("question") or fallback_question,
            "options": data.get("options") or [],
            "preference_summary": summary,
            "intent": intent,
            "recommendations": products,
            "search_id": None,
            "fallback": fallback,
            "provisional": True,
        }

    def _get_attr_vocab_text(self) -> str:
        """Build a prompt vocabulary while always retaining normalized domain types.

        The v3 domain attributes are intentionally shared nodes (for example only
        13 platform values), so a naive top-frequency query would hide them from
        the LLM despite their high product coverage.
        """
        if self._attr_vocab_text is not None:
            return self._attr_vocab_text
        vocab: list[dict[str, Any]] = []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                domain_res = session.run(
                    "MATCH (a:Attribute) WHERE a.attr_type IN $domain_types "
                    "RETURN a.attr_type AS attr_type, "
                    "       collect(DISTINCT a.value)[0..20] AS examples, "
                    "       count(*) AS freq "
                    "ORDER BY attr_type",
                    domain_types=_DOMAIN_ATTR_TYPES,
                )
                general_res = session.run(
                    "MATCH (a:Attribute) "
                    "WHERE NOT a.attr_type IN $domain_types "
                    "RETURN a.attr_type AS attr_type, "
                    "       collect(DISTINCT a.value)[0..6] AS examples, "
                    "       count(*) AS freq "
                    "ORDER BY freq DESC LIMIT 20",
                    domain_types=_DOMAIN_ATTR_TYPES,
                )
                vocab = [
                    {"attr_type": r["attr_type"], "examples": r["examples"]}
                    for r in domain_res
                ]
                vocab.extend(
                    {"attr_type": r["attr_type"], "examples": r["examples"]}
                    for r in general_res
                )
        except Exception as exc:
            print(f"[recommender] attr vocabulary lookup failed: {exc}", file=sys.stderr)
        self._attr_vocab_text = _format_attr_vocab(vocab)
        return self._attr_vocab_text

    def sample_users(self, limit: int = 10) -> list[dict[str, Any]]:
        """パーソナライズのデモ用に、評価履歴を持つ実ユーザーを何件か返す。"""
        with self._driver.session(database=self._neo4j_db) as session:
            res = session.run(
                "MATCH (u:User)-[r:RATED]->(:Product) "
                "WITH u, count(r) AS rated_count "
                "WHERE rated_count >= 3 "
                "RETURN u.user_id AS user_id, rated_count "
                "ORDER BY rated_count DESC LIMIT $limit",
                limit=limit,
            )
            return [{"user_id": r["user_id"], "rated_count": r["rated_count"]} for r in res]

    def get_reviews(self, product_id: str, limit: int = 5, lang: str = "en") -> list[dict[str, Any]]:
        """商品IDに紐づくレビューをhelpful_vote降順で返す。(Review)-[:ABOUT]->(Product) エッジを使用。
        lang="ja"の場合、backfill_display_fields.py --reviews-jaで付与済みのtitle_ja/text_jaが
        あればそちらを本文として返す（無ければ通常のtext/titleにフォールバック）。"""
        cypher = """
MATCH (p:Product {product_id: $product_id})<-[:ABOUT]-(r:Review)
WHERE r.text IS NOT NULL AND size(coalesce(r.text, '')) > 10
RETURN r.title AS title,
       r.title_ja AS title_ja,
       r.text AS text,
       r.text_ja AS text_ja,
       toFloat(r.rating) AS rating,
       toInteger(r.helpful_vote) AS helpful_vote,
       r.verified AS verified_purchase
ORDER BY r.helpful_vote DESC, r.rating DESC, r.review_id ASC
LIMIT $limit
"""
        use_ja = _normalize_lang(lang) == "ja"
        with self._driver.session(database=self._neo4j_db) as session:
            result = session.run(cypher, product_id=product_id, limit=limit)
            rows = []
            for record in result:
                r = dict(record)
                text_ja = r.pop("text_ja", None)
                title_ja = r.pop("title_ja", None)
                translated = bool(use_ja and text_ja)
                r["text"] = text_ja if translated else _strip_html(r.get("text"))
                r["title"] = title_ja if (use_ja and title_ja) else _strip_html(r.get("title"))
                r["translated"] = translated
                r["display_language"] = "ja" if translated else "en"
                rows.append(r)
            return rows

    def get_description(self, product_id: str, lang: str = "en") -> dict[str, Any] | None:
        """Return the same language-selected description used by recommendation cards."""
        with self._driver.session(database=self._neo4j_db) as session:
            record = session.run(
                "MATCH (p:Product {product_id: $product_id}) "
                "RETURN p.description AS description, p.description_ja AS description_ja",
                product_id=product_id,
            ).single()
        if record is None:
            return None
        raw_description = record.get("description")
        raw_description_ja = record.get("description_ja")
        use_ja = _normalize_lang(lang) == "ja"
        return {
            "description": _strip_html(raw_description_ja if use_ja and raw_description_ja else raw_description) or None,
            "translated": bool(use_ja and raw_description_ja),
        }

    _MAX_VIEWED: int = 20
    _MAX_SEARCHES: int = 30

    def log_search(
        self,
        user_id: str,
        search_id: str,
        query: str,
        cypher: str,
        explanation: str,
        result_product_ids: list[str],
    ) -> None:
        """Record a search as a SearchLog node linked to the user."""
        ts = int(time.time() * 1000)
        write_cypher = (
            "MERGE (u:User {user_id: $uid}) "
            "CREATE (sl:SearchLog {"
            "  log_id: $log_id, query: $q, cypher: $cypher,"
            "  explanation: $explanation, result_product_ids: $pids,"
            "  result_count: $rc, timestamp: $ts"
            "}) "
            "CREATE (u)-[:SEARCHED]->(sl)"
        )
        # Keep only the latest _MAX_SEARCHES SearchLog nodes per user.
        trim_cypher = (
            "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
            "WITH sl ORDER BY sl.timestamp DESC "
            "WITH collect(sl) AS all_sl "
            "FOREACH (old IN all_sl[$keep..] | DETACH DELETE old)"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                session.run(
                    write_cypher,
                    uid=user_id, log_id=search_id, q=query,
                    cypher=cypher, explanation=explanation,
                    pids=result_product_ids, rc=len(result_product_ids), ts=ts,
                )
                session.run(trim_cypher, uid=user_id, keep=self._MAX_SEARCHES)
        except Exception as exc:
            print(f"[recommender] log_search failed: {exc}", file=sys.stderr)

    def log_view(self, user_id: str, product_id: str, search_id: str | None = None) -> None:
        """Record that a user viewed a product (VIEWED edge in Neo4j)."""
        ts = int(time.time() * 1000)
        write_cypher = (
            "MERGE (u:User {user_id: $uid}) "
            "WITH u "
            "MATCH (p:Product {product_id: $pid}) "
            "CREATE (u)-[:VIEWED {timestamp: $ts, search_id: $sid}]->(p)"
        )
        # Keep only the latest _MAX_VIEWED edges per user; delete the rest.
        trim_cypher = (
            "MATCH (u:User {user_id: $uid})-[v:VIEWED]->() "
            "WITH v ORDER BY v.timestamp DESC "
            "WITH collect(v) AS vs "
            "FOREACH (old IN vs[$keep..] | DELETE old)"
        )
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                session.run(write_cypher, uid=user_id, pid=product_id, ts=ts, sid=search_id)
                session.run(trim_cypher, uid=user_id, keep=self._MAX_VIEWED)
        except Exception as exc:
            print(f"[recommender] log_view failed: {exc}", file=sys.stderr)

    def clear_behavior_history(self, user_id: str) -> dict[str, int]:
        """RATED（データセット由来の評価履歴）はそのままに、このユーザーの永続的な行動データ
        ——VIEWED行動ログとSearchLog検索履歴——だけを削除する。デモで行動ログをまっさらな
        状態に戻したい時に使う（推薦の根拠であるRATED自体は変更しない）。"""
        with self._driver.session(database=self._neo4j_db) as session:
            viewed_result = session.run(
                "MATCH (:User {user_id: $uid})-[r:VIEWED]->() "
                "WITH collect(r) AS rs "
                "FOREACH (x IN rs | DELETE x) "
                "RETURN size(rs) AS n",
                uid=user_id,
            ).single()
            search_result = session.run(
                "MATCH (:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                "WITH collect(sl) AS sls "
                "FOREACH (x IN sls | DETACH DELETE x) "
                "RETURN size(sls) AS n",
                uid=user_id,
            ).single()
        # 古い履歴を前提に生成されたホーム推薦キャッシュも破棄する
        for key in [k for k in self._home_cache if k.startswith(f"{user_id}:")]:
            del self._home_cache[key]
        return {
            "viewed_deleted": viewed_result["n"] if viewed_result else 0,
            "searches_deleted": search_result["n"] if search_result else 0,
        }

    def close(self) -> None:
        self._driver.close()

    # ── user context from Neo4j ─────────────────────────────────────────────────

    def _get_user_context(self, user_id: str) -> dict:
        rated: list[dict] = []
        viewed: list[dict] = []
        preferred_attrs: list[dict] = []
        recent_queries: list[str] = []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product) "
                    "WHERE toFloat(r.rating) >= 4 "
                    "WITH p, r ORDER BY toFloat(r.rating) DESC, toInteger(r.timestamp) DESC LIMIT 6 "
                    "OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "RETURN p.product_id AS product_id, p.title AS title, r.rating AS rating, "
                    "collect(DISTINCT {attr_type:a.attr_type, value:a.value, value_ja:a.value_ja})[0..8] "
                    "AS attributes",
                    uid=user_id,
                )
                rated = [
                    {
                        "product_id": r["product_id"],
                        "title": r["title"],
                        "rating": r["rating"],
                        "attributes": [dict(a) for a in (r["attributes"] or []) if a.get("attr_type")],
                    }
                    for r in res
                ]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[v:VIEWED]->(p:Product) "
                    "RETURN p.title AS title "
                    "ORDER BY v.timestamp DESC LIMIT 4",
                    uid=user_id,
                )
                viewed = [{"title": r["title"]} for r in res]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product)"
                    "-[:HAS_ATTRIBUTE]->(a:Attribute) "
                    "WHERE r.rating >= 4 "
                    "AND a.attr_type IN $profile_attr_types "
                    "RETURN a.attr_type AS attr_type, a.value AS value, "
                    "count(*) AS freq "
                    "ORDER BY freq DESC LIMIT 8",
                    uid=user_id,
                    profile_attr_types=_CHAT_PROFILE_ATTR_TYPES,
                )
                preferred_attrs = [
                    {"attr_type": r["attr_type"], "value": r["value"], "freq": r["freq"]}
                    for r in res
                ]

                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                    "WHERE sl.query <> '[home]' "
                    "RETURN sl.query AS query "
                    "ORDER BY sl.timestamp DESC LIMIT 5",
                    uid=user_id,
                )
                recent_queries = [r["query"] for r in res]
        except Exception as exc:
            print(f"[recommender] _get_user_context failed: {exc}", file=sys.stderr)
        return {
            "rated": rated,
            "viewed": viewed,
            "preferred_attrs": preferred_attrs,
            "recent_queries": recent_queries,
        }

    def _get_dynamic_few_shot(self, user_id: str) -> list[dict]:
        """Return past (query, cypher) pairs that led to at least one click."""
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                res = session.run(
                    "MATCH (u:User {user_id: $uid})-[:SEARCHED]->(sl:SearchLog) "
                    "WHERE sl.cypher IS NOT NULL AND sl.query <> '[home]' "
                    "WITH u, sl "
                    "OPTIONAL MATCH (u)-[v:VIEWED]->(:Product) "
                    "WHERE v.search_id = sl.log_id "
                    "WITH sl, count(v) AS clicks "
                    "WHERE clicks > 0 "
                    "RETURN sl.query AS query, sl.cypher AS cypher, sl.explanation AS explanation "
                    "ORDER BY clicks DESC, sl.timestamp DESC LIMIT 3",
                    uid=user_id,
                )
                return [
                    {"query": r["query"], "cypher": r["cypher"], "explanation": r["explanation"]}
                    for r in res
                ]
        except Exception as exc:
            print(f"[recommender] _get_dynamic_few_shot failed: {exc}", file=sys.stderr)
            return []

    # ── LLM call ────────────────────────────────────────────────────────────────

    def _call_llm(
        self, system: str, user: str, max_tokens: int = 1500
    ) -> dict[str, Any]:
        resp = _create_json_completion(
            self._llm,
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._structured_temperature,
            max_tokens=max_tokens,
        )
        return _parse_llm_json(resp.choices[0].message.content or "{}")

    # ── structured conditions + fixed meta-path retrieval ─────────────────────

    def _extract_conditions(self, query: str, lang: str) -> dict[str, Any]:
        fallback = _fallback_condition_terms(query)
        if not query.strip():
            return {
                "product_keywords": [],
                "category_keywords": [],
                "attribute_keywords": [],
                "platform_keywords": [],
                "franchise_keywords": [],
                "product_type_keywords": [],
                "min_rating": None,
                "condition_source": "none",
            }
        condition_source = "llm"
        try:
            data = self._call_llm(
                _build_condition_prompt(self._genre, self._get_attr_vocab_text(), lang),
                query,
            )
        except Exception as exc:
            print(f"[recommender] condition extraction failed, using fallback terms: {exc}", file=sys.stderr)
            data = {}
            condition_source = "heuristic_fallback"
        if not data:
            condition_source = "heuristic_fallback"

        product_keywords = _keyword_list(data.get("product_keywords")) or fallback["product_keywords"]
        category_keywords = _keyword_list(data.get("category_keywords")) or fallback["category_keywords"]
        llm_attribute_keywords = _keyword_list(data.get("attribute_keywords"))
        attribute_keywords = llm_attribute_keywords or fallback["attribute_keywords"]
        platform_keywords = _keyword_list(data.get("platform_keywords"))
        franchise_keywords = _keyword_list(data.get("franchise_keywords"))
        product_type_keywords = _keyword_list(data.get("product_type_keywords"))
        try:
            min_rating = data.get("min_rating", fallback.get("min_rating"))
            min_rating = float(min_rating) if min_rating not in (None, "") else fallback.get("min_rating")
        except (TypeError, ValueError):
            min_rating = fallback.get("min_rating")

        # Keep the common Video_Games demo terms robust even when the LLM returns
        # Japanese-only labels or omits obvious catalog keywords.
        expanded_query = " ".join([query, *product_keywords, *attribute_keywords])
        expanded = _fallback_condition_terms(expanded_query)
        explicit_domain = _domain_constraints_from_terms([query])
        inferred_domain = _domain_constraints_from_terms(
            [expanded_query, *platform_keywords, *franchise_keywords, *product_type_keywords]
        )
        product_keywords = _keyword_list([*product_keywords, *expanded["product_keywords"]], max_items=12)
        category_keywords = _keyword_list([*category_keywords, *expanded["category_keywords"]], max_items=8)
        # When the LLM has already identified genuine preference attributes,
        # do not append every raw conversation token as another attribute. That
        # would make platform/franchise wording dominate the soft ranking.
        expanded_attribute_keywords = (
            [] if llm_attribute_keywords else expanded["attribute_keywords"]
        )
        attribute_keywords = _keyword_list(
            [*attribute_keywords, *expanded_attribute_keywords], max_items=12
        )
        # Explicitly named domain values in the conversation are deterministic
        # and override LLM guesses. This prevents a model from interpreting
        # "Nintendo 3DS" as both 3DS and the broader Nintendo DS platform.
        platform_keywords = _keyword_list(
            explicit_domain["platform_keywords"]
            or [*platform_keywords, *inferred_domain["platform_keywords"]],
            max_items=12,
        )
        franchise_keywords = _keyword_list(
            explicit_domain["franchise_keywords"]
            or [*franchise_keywords, *inferred_domain["franchise_keywords"]],
            max_items=12,
        )
        product_type_keywords = _keyword_list(
            explicit_domain["product_type_keywords"]
            or [*product_type_keywords, *inferred_domain["product_type_keywords"]],
            max_items=12,
        )
        if min_rating is None:
            min_rating = expanded.get("min_rating")

        return {
            "product_keywords": product_keywords,
            "category_keywords": category_keywords,
            "attribute_keywords": attribute_keywords,
            "platform_keywords": platform_keywords,
            "franchise_keywords": franchise_keywords,
            "product_type_keywords": product_type_keywords,
            "min_rating": min_rating,
            "condition_source": condition_source,
        }

    def _run_metapath_recommendation(
        self,
        query: str,
        user_id: str | None,
        limit: int,
        lang: str,
        dialogue_soft_preferences: bool = False,
    ) -> tuple[str, str, list[Recommendation], dict[str, Any]]:
        normalized_lang = _normalize_lang(lang)
        conditions = self._extract_conditions(query, normalized_lang)
        has_query = bool(
            conditions["product_keywords"]
            or conditions["category_keywords"]
            or conditions["attribute_keywords"]
            or conditions["platform_keywords"]
            or conditions["franchise_keywords"]
            or conditions["product_type_keywords"]
        )
        if not user_id and not has_query:
            return "", "", [], {
                "applied_conditions": [],
                "condition_source": "none",
                "retrieval_status": "no_match",
                "no_result_reason": "No dialogue conditions or user history were supplied",
            }

        explanations = _metapath_explanations(normalized_lang)
        hard_conditions, soft_conditions = _dialogue_condition_groups(conditions)
        params: dict[str, Any] = {
            "limit": limit,
            "candidate_product_ids": [],
            "product_keywords": conditions["product_keywords"],
            "category_keywords": conditions["category_keywords"],
            "attribute_keywords": conditions["attribute_keywords"],
            "platform_keywords": conditions["platform_keywords"],
            "franchise_keywords": conditions["franchise_keywords"],
            "product_type_keywords": conditions["product_type_keywords"],
            # The regular /recommend path keeps open-vocabulary query terms as
            # exclusion gates. Conversational recommendation deliberately makes
            # them ranking-only signals; only the three domain fields below gate.
            "required_condition_keywords": (
                []
                if dialogue_soft_preferences
                else _required_condition_keywords(conditions)
            ),
            "platform_attr_types": _PLATFORM_ATTR_TYPES,
            "franchise_attr_types": _FRANCHISE_ATTR_TYPES,
            "product_type_attr_types": _PRODUCT_TYPE_ATTR_TYPES,
            "platform_required": bool(conditions["platform_keywords"]),
            "franchise_required": bool(conditions["franchise_keywords"]),
            "product_type_required": bool(conditions["product_type_keywords"]),
            "ignored_behavior_attr_types": _IGNORED_BEHAVIOR_ATTR_TYPES,
            # Rating is also a preference in dialogue mode. The existing score
            # already rewards rating, while a threshold would incorrectly turn
            # it into a fourth hard constraint.
            "min_rating": (
                0.0
                if dialogue_soft_preferences
                else float(conditions.get("min_rating") or 0.0)
            ),
            "has_query": has_query,
            "dialogue_soft_preferences": dialogue_soft_preferences,
            "transition_explanation": explanations["transition"],
            "peer_explanation": explanations["peer"],
            "rated_explanation": explanations["rated"],
            "viewed_explanation": explanations["viewed"],
            "condition_explanation": explanations["condition"],
        }
        if user_id:
            params["uid"] = user_id
            cypher = _METAPATH_USER_CYPHER
        else:
            cypher = _METAPATH_CONDITION_CYPHER

        candidate_count = 0
        if user_id and has_query:
            # Stage 1: keep only the strongest dialogue/domain matches. Stage 2
            # below applies the costly user-behavior meta-path to this pool.
            candidate_params = dict(params)
            candidate_params.pop("uid", None)
            candidate_params["limit"] = max(50, limit)
            candidate_rows = self._execute_and_map(
                _METAPATH_CONDITION_CYPHER, candidate_params, normalized_lang
            )
            candidate_ids = [row.product_id for row in candidate_rows]
            candidate_count = len(candidate_ids)
            if candidate_ids:
                params["candidate_product_ids"] = candidate_ids

        results = self._execute_and_map(cypher, params, normalized_lang)
        relaxed = False
        used_condition_only = False

        # 緩和ステップ0: 自由語彙の対話条件（ジャンル等）を先に外す。
        # ジャンルは属性としてグラフにほぼ存在しない（本物のジャンル値は~150商品）ため、
        # "adventure"のような語を全結果の必須条件にすると「マリオ×Switch×アドベンチャー」
        # ですら0件になる。名指しされたフランチャイズ/機種のゲートは守ったまま、
        # ジャンル語はスコアリング（query_attrs等の加点）としてだけ効かせる。
        # フランチャイズを先に捨てる旧順序だと「マリオを捨ててadventureを守る」逆転が起き、
        # マインクラフト1件だけが返る事故になっていた。
        # ただしフランチャイズ・機種のどちらのゲートも無いクエリ（例: 「ホラーゲーム」）で
        # ジャンルまで外すと無関係な汎用ゲームが返るだけなので、その場合は緩和せず
        # Zhang設計どおり正直に0件を返す。
        can_relax_open_vocab = bool(
            params["franchise_required"] or params["platform_required"]
        )
        if (
            not dialogue_soft_preferences
            and not results
            and has_query
            and can_relax_open_vocab
            and params["required_condition_keywords"]
        ):
            relaxed_params = dict(params)
            relaxed_params["required_condition_keywords"] = []
            if user_id and params.get("candidate_product_ids") == [] and candidate_count == 0:
                relaxed_candidate_params = dict(relaxed_params)
                relaxed_candidate_params.pop("uid", None)
                relaxed_candidate_params["candidate_product_ids"] = []
                relaxed_candidate_params["limit"] = max(50, limit)
                relaxed_candidates = self._execute_and_map(
                    _METAPATH_CONDITION_CYPHER, relaxed_candidate_params, normalized_lang
                )
                relaxed_ids = [row.product_id for row in relaxed_candidates]
                if relaxed_ids:
                    relaxed_params["candidate_product_ids"] = relaxed_ids
                    candidate_count = len(relaxed_ids)
            results = self._execute_and_map(cypher, relaxed_params, normalized_lang)
            if results:
                params = relaxed_params
                relaxed = True

        if (
            not dialogue_soft_preferences
            and not results
            and has_query
            and params["franchise_required"]
        ):
            relaxed_params = dict(params)
            relaxed_params["franchise_required"] = False
            # ステップ0で外した自由語彙条件が復活しないようここでも空にする
            relaxed_params["required_condition_keywords"] = []
            # Keep platform/product-type constraints, but allow a nearby game
            # when a very specific franchise query has no catalog coverage.
            if user_id:
                relaxed_candidate_params = dict(relaxed_params)
                relaxed_candidate_params.pop("uid", None)
                relaxed_candidate_params["candidate_product_ids"] = []
                relaxed_candidate_params["limit"] = max(50, limit)
                relaxed_candidates = self._execute_and_map(
                    _METAPATH_CONDITION_CYPHER, relaxed_candidate_params, normalized_lang
                )
                relaxed_ids = [row.product_id for row in relaxed_candidates]
                if relaxed_ids:
                    relaxed_params["candidate_product_ids"] = relaxed_ids
                    candidate_count = len(relaxed_ids)
            results = self._execute_and_map(cypher, relaxed_params, normalized_lang)
            if results:
                params = relaxed_params
                relaxed = True

        if not results and has_query and user_id:
            # For explicit searches, relevance to the query is more important
            # than forcing a behavior path. If the personalized meta-path is too
            # narrow, fall back to the condition-only query before legacy
            # Text2Cypher.
            condition_params = dict(params)
            condition_params.pop("uid", None)
            cypher = _METAPATH_CONDITION_CYPHER
            results = self._execute_and_map(cypher, condition_params, normalized_lang)
            used_condition_only = bool(results)
        if dialogue_soft_preferences:
            top_explanation = explanations["dialogue_soft"]
        else:
            top_explanation = explanations["top"] if user_id else explanations["condition_top"]
        if results:
            status = "matched_after_relaxation" if (relaxed or used_condition_only) else "matched"
            no_result_reason = None
        elif conditions["condition_source"] == "heuristic_fallback":
            status = "no_match"
            no_result_reason = "Condition extraction used heuristic fallback and found no catalog match"
        else:
            status = "no_match"
            no_result_reason = "No catalog item satisfied all dialogue constraints"
        return cypher, top_explanation, results, {
            "applied_conditions": _format_applied_conditions(conditions),
            "hard_conditions": hard_conditions if dialogue_soft_preferences else [],
            "soft_conditions": soft_conditions if dialogue_soft_preferences else [],
            "condition_source": conditions["condition_source"],
            "retrieval_status": status,
            "no_result_reason": no_result_reason,
            "candidate_count": candidate_count or len(results),
        }

    # ── Cypher generation with retry-on-error / retry-on-empty ──────────────────

    def _generate_cypher_and_execute(
        self,
        system_prompt: str,
        user_msg: str,
        limit: int,
        exec_params: dict[str, Any],
        has_uid: bool = False,
        lang: str = "en",
        fix_prompt: str | None = None,
        required_aliases: tuple[str, ...] | None = None,
        sanitize_home: bool = False,
    ) -> tuple[str, str, list[Recommendation]]:
        """Cypherの生成・検証・実行を1つのリトライループの中で行う。

        構文/検証エラーだけでなく、「構文的には正しいが実行結果が0件」だった場合も
        リトライ対象にする（以前は0件を検証エラーと区別できず、即座に諦めて全面
        フォールバックしていた）。全試行を使い切っても結果が得られなければ
        ("", "", []) を返し、呼び出し元が全面フォールバックするための合図とする。
        """
        data = self._call_llm(system_prompt, user_msg, max_tokens=3500)
        cypher: str = data.get("cypher", "").strip()
        if sanitize_home:
            cypher = _sanitize_home_cypher(cypher)
        explanation: str = data.get("explanation", "")
        active_fix_prompt = fix_prompt or _build_fix_prompt(lang)

        for attempt in range(self._max_attempts):
            is_last = attempt >= self._max_attempts - 1
            try:
                self._validate_cypher(
                    cypher, limit, has_uid, exec_params, required_aliases=required_aliases
                )
            except Exception as exc:
                if is_last:
                    break
                cypher, explanation = self._request_cypher_fix(
                    active_fix_prompt, user_msg, cypher, f"Neo4j error:\n{exc}"
                )
                if sanitize_home:
                    cypher = _sanitize_home_cypher(cypher)
                continue

            results = self._execute_and_map(cypher, exec_params, lang)
            if results:
                return cypher, explanation, results
            if is_last:
                break
            cypher, explanation = self._request_cypher_fix(
                active_fix_prompt, user_msg, cypher,
                "This query ran without error but matched 0 products. Keep the P1-P5 "
                "contract, but choose another allowed path or remove an unnecessary "
                "hard filter. Do not invent graph schema or repeat the same query.",
            )
            if sanitize_home:
                cypher = _sanitize_home_cypher(cypher)

        return "", "", []

    def _request_cypher_fix(self, fix_prompt: str, user_msg: str, cypher: str, feedback: str) -> tuple[str, str]:
        fix_user = f"Original request: {user_msg}\n\nCurrent Cypher:\n{cypher}\n\n{feedback}"
        try:
            fix_data = self._call_llm(fix_prompt, fix_user, max_tokens=3500)
            return fix_data.get("cypher", cypher).strip(), fix_data.get("explanation", "")
        except Exception:
            return cypher, ""

    _UID_REF = re.compile(r"\$uid\b")
    _HARDCODED_USER_ID = re.compile(r"user_id\s*[:=]\s*['\"]")
    _PARAM_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
    _WRITE_CLAUSE = re.compile(
        r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b",
        re.IGNORECASE,
    )

    def _validate_cypher(
        self,
        cypher: str,
        limit: int,
        has_uid: bool = False,
        params: dict[str, Any] | None = None,
        required_aliases: tuple[str, ...] | None = None,
    ) -> None:
        if not cypher:
            raise ValueError("Empty Cypher query")
        if len(cypher) > 30000:
            raise ValueError("Cypher query is unexpectedly long")
        if self._WRITE_CLAUSE.search(cypher):
            raise ValueError("Only read-only Cypher is allowed")
        if re.search(r"\bCALL\s+(dbms|db|apoc)\.", cypher, re.IGNORECASE):
            raise ValueError("Database procedures are not allowed in generated Cypher")
        if self._HARDCODED_USER_ID.search(cypher):
            raise ValueError(
                "Query hardcodes a literal user_id string instead of using the $uid "
                "parameter. NEVER hardcode a user_id — always reference $uid (only when "
                "a user is actually bound for this request)."
            )
        if not has_uid and self._UID_REF.search(cypher):
            raise ValueError(
                "Query references $uid but no user_id was provided for this request — "
                "$uid will not be bound. Rewrite the query without $uid or any pattern "
                "that requires a specific User node."
            )
        available_params = {"limit", *(params or {}).keys()}
        unknown_params = set(self._PARAM_REF.findall(cypher)) - available_params
        if unknown_params:
            raise ValueError(f"Cypher references unsupported parameters: {sorted(unknown_params)}")
        if required_aliases:
            missing_aliases = [
                alias
                for alias in required_aliases
                if not re.search(rf"\bAS\s+{re.escape(alias)}\b", cypher, re.IGNORECASE)
            ]
            if missing_aliases:
                raise ValueError(f"Cypher is missing required RETURN aliases: {missing_aliases}")
        if not re.search(
            r"ORDER\s+BY\s+score\s+DESC\s+LIMIT\s+\$limit\s*;?\s*$",
            cypher,
            re.IGNORECASE,
        ):
            raise ValueError("Cypher must end with ORDER BY score DESC LIMIT $limit")
        explain_params = {"limit": limit, **(params or {})}
        with self._driver.session(database=self._neo4j_db) as session:
            session.run(f"EXPLAIN {cypher}", **explain_params).consume()

    # ── query execution ──────────────────────────────────────────────────────────

    def _execute_and_map(self, cypher: str, params: dict, lang: str = "en") -> list[Recommendation]:
        if not cypher:
            return []
        try:
            with self._driver.session(database=self._neo4j_db) as session:
                result = session.run(cypher, **params)
                return [_record_to_recommendation(dict(record), lang) for record in result]
        except Exception as exc:
            print(f"[recommender] Cypher execution failed: {exc}", file=sys.stderr)
            return []

    def _run_popular(self, limit: int, lang: str) -> list[Recommendation]:
        """LLMを介さず、人気・高評価商品を直接クエリする（フォールバック用途と、
        パーソナライズ不要な初期表示の高速パス用途で共有する）。"""
        return self._execute_and_map(
            _FALLBACK_CYPHER, {"limit": limit, "explanation": _popular_explanation(lang)}, lang
        )
