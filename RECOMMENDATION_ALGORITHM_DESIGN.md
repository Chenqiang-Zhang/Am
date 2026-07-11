# Recommendation Algorithm Design Slides

## Slide 1: Feedback on the Meta-Path Proposal

Ochi's meta-path recommendation proposal is a good direction because it makes the knowledge graph responsible for retrieval and explanation, instead of letting the LLM directly control the database query.

The recommended architecture is:

```text
Dialogue / search input
  -> LLM extracts structured conditions
  -> Neo4j recalls candidates through graph meta-paths
  -> transition-first ranking combines behavior, attributes, categories, rating, popularity
  -> strongest graph path becomes the recommendation reason
```

## Slide 2: Current Graph Support

The latest Video_Games graph is sufficient for a first meta-path implementation.

| Graph Element | Current Count | Use in Recommendation |
|---|---:|---|
| Product | 1,728 | candidate catalog |
| User | 2,918 | personalization anchor |
| Review | 57,354 | review evidence |
| RATED | 57,354 | offline user behavior signal |
| VIEWED | 26 | online behavior signal, still sparse |
| HAS_ATTRIBUTE | 14,081 | product-attribute similarity |
| BELONGS_TO | 1,728 | category filtering |
| MENTIONS | 261,353 | future review-confirmed attribute evidence |

Conclusion: RATED and HAS_ATTRIBUTE are strong enough for the first version. VIEWED should be treated as an online incremental signal, not the main evaluation signal yet.

## Slide 3: Revised Role of the LLM

Old framing:

```text
LLM generates Cypher directly
```

Revised framing:

```text
LLM structures the user's intent:
- product keywords
- category keywords
- attribute keywords
- optional rating constraint

The graph database executes fixed retrieval/ranking logic.
```

This is safer and easier to evaluate because the algorithm is no longer a different generated query for every request.

## Slide 4: Minimal Meta-Path Version

Implemented paths:

```text
Path A:
User
  -> RATED / VIEWED
  -> seed Product
  -> HAS_ATTRIBUTE
  -> candidate Product

Path B:
User
  -> RATED seed Product
  <- RATED peer User
  -> RATED candidate Product

Path C:
User
  -> recent high-rated seed Product
  <- high-rated peer User
  -> next high-rated candidate Product
```

Dialogue conditions are used as filters and ranking signals:

```text
candidate Product
  -> title/title_ja keyword match
  -> BELONGS_TO Category match
  -> HAS_ATTRIBUTE Attribute match
```

Ranking combines:

- sequential transition support from similar users
- recent item-item collaborative similarity
- shared attributes from highly rated products
- shared attributes from recently viewed products
- peer users with overlapping taste
- direct match to dialogue conditions
- average rating
- rating count

## Slide 5: Recommendation Reason

The explanation is derived from the strongest path:

- Often chosen next after games similar to the user's recent likes
- Shares attributes with products this user rated highly
- Shares attributes with products this user recently viewed
- Highly rated by users with overlapping taste
- Matches the structured dialogue constraints

This makes the reason inspectable in the developer view because the returned `intent.cypher` is a fixed meta-path query, and `matched_attrs` shows the attributes used in the path.

## Slide 6: Current Scope and Future Expansion

Current minimum version:

- implemented: User -> Product -> Attribute -> Product
- implemented: User -> Product <- Peer User -> Product
- implemented: User -> recent Product <- Peer User -> next Product
- implemented: dialogue condition filtering by product/category/attribute terms
- implemented: home recommendation uses the same meta-path for users with history
- retained: legacy Text2Cypher as a fallback only

Next versions:

- add Category meta-path: User -> Product -> Category -> Product
- add review-confirmed path: Product -> Review -> MENTIONS -> Attribute -> Product
- replace hand-tuned weights with learned weights from larger offline and online feedback
- increase VIEWED data through frontend interaction logging

## Slide 7: Demo Scenario

Use Mario/Nintendo Switch examples for demo recording because the current graph has enough matching products.

Recommended prompt:

```text
family friendly Mario Nintendo Switch game
```

Avoid using narrow queries such as "baseball game" for the main demo, because the current 1,728-product subset may not cover them reliably.
