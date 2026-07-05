# Amazon Reviews'23 Knowledge Graph Recommender

An experimental product recommendation system built on the Amazon Reviews'23 `All_Beauty` dataset. The system transforms review data and product metadata into a Neo4j knowledge graph, then exposes a REST API that accepts natural-language queries and returns ranked product recommendations with graph-traced explanations.

## Architecture

```
Raw Data (Amazon Reviews'23)
    ↓ build_kg_csv.py
Neo4j Knowledge Graph
    ↓ extract_product_attributes_llm.py → attributes_to_kg_csv.py → import_attributes_to_neo4j.py
    + Attribute nodes (LLM-extracted)
    ↓
REST API (FastAPI)
    ├── LLM intent extraction  (natural language → structured attribute filters)
    ├── Controlled query planning (SearchIntent → allow-listed backend actions)
    ├── Conversational recommendation (multi-turn preference collection)
    ├── Multi-path graph recall (attributes + feature text + title/category/store)
    ├── Hybrid ranking          (match coverage + rating quality + popularity + price availability)
    └── Reason feedback logging (recommendation explanation feedback)
```

## Graph Schema

**Nodes**

| Label | Key Properties |
|---|---|
| `User` | `user_id` |
| `Product` | `product_id`, `title`, `main_category`, `price`, `average_rating`, `rating_number` |
| `Review` | `review_id`, `title`, `text`, `rating`, `timestamp`, `helpful_vote`, `verified_purchase` |
| `Category` | `category_id`, `name` |
| `Store` | `store_id`, `name` (used as brand proxy) |
| `Feature` | `feature_id`, `text`, `normalized_text` |
| `Attribute` | `attribute_id`, `name`, `value`, `attribute_type` (LLM-extracted) |

**Relationships**

| Relationship | Direction | Properties |
|---|---|---|
| `WROTE` | User → Review | — |
| `REVIEWS` | Review → Product | — |
| `RATED` | User → Product | `rating`, `timestamp`, `verified_purchase` |
| `BELONGS_TO` | Product → Category | — |
| `SOLD_BY` | Product → Store | — |
| `HAS_FEATURE` | Product → Feature | — |
| `HAS_ATTRIBUTE` | Product → Attribute | `confidence`, `evidence`, `model` |

## Repository Structure

```text
.
├── README.md
├── KG_README.md
├── Graph_rule.md          # Graph construction rules (Japanese)
├── requirements.txt
├── data/                  # Raw data — local only, not committed
├── kg_output/             # Generated CSVs — local only, not committed
├── docs/                  # Local documentation — not committed
├── neo4j/                 # Cypher import scripts
├── scripts/               # Data pipeline scripts
│   ├── build_kg_csv.py                    # Build base graph CSVs
│   ├── extract_product_attributes_llm.py  # LLM attribute extraction
│   ├── extract_attributes_from_details.py # Zero-cost attribute extraction from metadata details
│   ├── filter_catalog_before_llm.py       # Pre-LLM product cleaning and review filtering
│   ├── enrich_product_images.py           # Add Product.image_url from metadata
│   ├── attributes_to_kg_csv.py            # Convert attributes JSONL → CSV
│   ├── check_api_contract.py              # Frontend/backend response contract smoke test
│   ├── evaluate_recommenders_offline.py   # Offline history-based recommender evaluation
│   ├── import_kg_to_neo4j.py              # Import base graph via Bolt
│   ├── import_attributes_to_neo4j.py      # Import attributes via Bolt
│   └── split_csv_for_aura_github.py       # Split CSVs for GitHub upload
├── api/                   # Recommendation REST API
│   ├── main.py            # FastAPI app
│   ├── recommender.py     # LLM intent extraction + Neo4j graph query
│   └── models.py          # Pydantic request/response models
├── web/                   # React + TypeScript conversational UI
└── data.ipynb             # Data exploration notebook
```

## Quick Start

### 1. Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set Up Environment

```bash
cp .env.example .env
# Edit .env — set NEO4J_URI/USERNAME/PASSWORD and OPENAI_API_KEY or DEEPSEEK_API_KEY
```

### 3. Start Neo4j

**Option A — Docker (recommended for local dev):**

```bash
docker run -d --name neo4j-am \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  neo4j:latest
```

Then set in `.env`:
```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
NEO4J_DATABASE=neo4j
```

**Option B — Neo4j Aura (cloud):** set `NEO4J_URI=neo4j+s://...` in `.env`.

### 4. Build and Import the Knowledge Graph

Place the Amazon Reviews'23 data files locally:
```text
data/All_Beauty.jsonl.gz
data/meta_All_Beauty.jsonl.gz
```

Clean products before graph expansion or LLM extraction:
```bash
python3 scripts/filter_catalog_before_llm.py
```

Build CSVs and import:
```bash
python3 scripts/build_kg_csv.py
python3 scripts/import_kg_to_neo4j.py
```

Extract product attributes with LLM and import:
```bash
python3 scripts/extract_product_attributes_llm.py \
  --provider deepseek --limit 1000 --resume --compact-input --batch-size 5

python3 scripts/attributes_to_kg_csv.py \
  --input-path kg_output/attributes/product_attributes_llm.jsonl

python3 scripts/import_attributes_to_neo4j.py
```

### 5. Start the Recommendation API

```bash
uvicorn api.main:app --reload
```

Open `http://localhost:8000/docs` for the interactive Swagger UI.

### 6. Start the Frontend UI

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api/*` to `http://localhost:8000`.

## API Usage

### `POST /recommend`

Accepts a natural-language query and returns ranked product recommendations with graph-backed evidence and a score breakdown.

**Request:**
```json
{
  "query": "I have dry and sensitive skin, looking for a gentle face moisturizer with hyaluronic acid, preferably fragrance-free",
  "limit": 5,
  "lang": "en"
}
```

**Response (abbreviated):**
```json
{
  "query": "...",
  "intent": {
    "attribute_filters": [
      {"attribute_type": "skin_type", "value": "dry", "weight": 1.0},
      {"attribute_type": "ingredient", "value": "hyaluronic acid", "weight": 1.0}
    ],
    "keywords": ["gentle", "fragrance-free"]
  },
  "query_plan": {
    "source": "controlled_query_plan",
    "history_policy": "positive_behavior_attributes",
    "actions": [
      {"name": "attribute_recall", "enabled": true, "reason": "Use structured Attribute nodes.", "cypher_template": "ATTRIBUTE_SEARCH_CYPHER"},
      {"name": "feature_text_recall", "enabled": true, "reason": "Use feature text recall.", "cypher_template": "FEATURE_SEARCH_CYPHER"},
      {"name": "filter_available", "enabled": true, "reason": "Exclude unavailable products.", "cypher_template": "embedded_in_recall_where_clause"}
    ],
    "safety_notes": ["LLM output is parsed only as SearchIntent; raw Cypher from the LLM is never executed."]
  },
  "recommendations": [
    {
      "product_id": "B0...",
      "title": "...",
      "display_title": "...",
      "display_language": "en",
      "availability_status": "available",
      "data_quality_score": 0.82,
      "score": 2.85,
      "matched_attributes": [
        {"attribute_type": "skin_type", "value": "dry", "confidence": 0.9, "evidence": "Skin Type: Dry"},
        {"attribute_type": "ingredient", "value": "hyaluronic acid", "confidence": 1.0, "evidence": "Hyaluronic Acid"}
      ],
      "matched_terms": ["dry", "fragrance-free", "gentle", "hyaluronic acid"],
      "matched_feature_evidence": ["Fragrance free moisturizer for dry sensitive skin"],
      "score_breakdown": {
        "attribute_match": 0.95,
        "feature_text_match": 0.75,
        "field_match": 0.25,
        "rating_quality": 0.82,
        "popularity": 0.64,
        "query_coverage": 0.86,
        "price_availability": 1.0
      },
      "reason_quantification": {
        "attribute_match": 0.95,
        "feature_text_match": 0.75,
        "field_match": 0.25,
        "rating_quality": 0.82,
        "popularity": 0.64,
        "query_coverage": 0.86,
        "price_availability": 1.0
      },
      "explanation": "Matched - skin_type: dry | ingredient: hyaluronic acid | text terms: dry, fragrance-free, gentle, hyaluronic acid | rating: 4.5 from 328 ratings",
      "display_explanation": "Matched - skin_type: dry | ingredient: hyaluronic acid | text terms: dry, fragrance-free, gentle, hyaluronic acid | rating: 4.5 from 328 ratings"
    }
  ]
}
```

### `POST /chat`

Runs one turn of conversational recommendation. The backend uses the conversation history to decide whether to ask one more preference question or search immediately.

```json
{
  "messages": [
    {"role": "user", "content": "乾燥肌向けの保湿クリームが欲しい"}
  ],
  "limit": 8,
  "lang": "ja"
}
```

The response has either:

- `action: "ask"` with one question and quick-reply options
- `action: "search"` with `preference_summary`, `intent`, and recommendations

### `POST /recommendations/{product_id}/feedback`

Stores user feedback on whether the recommendation reason was useful.

```json
{
  "query": "dry sensitive skin moisturizer",
  "lang": "en",
  "helpful": true,
  "reason_rating": 5,
  "selected_reasons": ["reason_helpful"],
  "comment": "The matched terms were clear."
}
```

Feedback is appended to `logs/recommendation_feedback.jsonl`, which is ignored by Git.

### `POST /behavior/events`

Stores lightweight user behavior events in Neo4j for personalization. The frontend generates an anonymous `user_id` in `localStorage` and sends events such as `impression`, `product_click`, `review_open`, `amazon_click`, `feedback_yes`, `feedback_no`, and `filter_change`.

```json
{
  "user_id": "anon_abc123",
  "event_type": "amazon_click",
  "product_id": "B07N5B8WWR",
  "query": "moisturizer for dry sensitive skin",
  "rank": 1,
  "source": "chat"
}
```

The recommender uses positive behavior history to boost products with similar graph attributes and lightly down-ranks products the same user has already strongly interacted with.

The recommender uses three recall paths, then re-ranks the merged candidates:

- `Product -[:HAS_ATTRIBUTE]-> Attribute` for precise structured matches
- `Product -[:HAS_FEATURE]-> Feature` for broader text matches from product metadata
- `Product` title/category and `Product -[:SOLD_BY]-> Store` for simple field matches

The final score combines attribute match, feature-text match, field match, query coverage, Bayesian-smoothed rating quality, popularity, and price availability. The `matched_attributes` array represents the most precise graph path that justifies a recommendation:
`User Query → [LLM intent] → Attribute ←[HAS_ATTRIBUTE]← Product`

The recommender also performs data cleaning and deduplication:

- strips HTML tags and entities from evidence/reviews
- normalizes whitespace and empty text
- deduplicates candidates by product ID and cleaned title
- exposes multilingual display fields (`display_title`, `display_explanation`, `price_display`) for the frontend
- marks products without a dataset price as `currently_unavailable`; the frontend can filter available, unavailable, or all results
- defaults recommendation recall to `sellable_status = "available"` and `data_quality_score >= 0.6`

The API also returns a `query_plan` field. This is the controlled replacement for raw LLM-generated Cypher: the LLM or heuristic parser extracts `SearchIntent`, while the backend maps that intent to allow-listed actions and fixed Cypher templates.

### API contract check

After backend or frontend changes, run:

```bash
conda run -n py312 python scripts/check_api_contract.py --base-url http://127.0.0.1:8000
```

This checks that `/recommend`, `/recommend/home`, and `/chat` still expose the fields expected by the frontend, including recommendation reason fields and `query_plan`.

### Product quality audit

Run the audit after importing or expanding product data:

```bash
conda run -n py312 python scripts/audit_product_quality.py
```

The script scans all `Product` nodes, writes `sellable_status`, `data_quality_score`, and `quality_flags` back to Neo4j, and generates JSON/Markdown reports under `reports/product_quality/`. Use `--dry-run` to generate reports without writing to Neo4j.

### Offline evaluation

Run an offline comparison using users with review history:

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

The script first checks graph data readiness, then compares `popularity_baseline`, `kg_no_history_home`, `title_keyword_profile`, `bm25_history_profile`, `kg_attribute_history`, and `hybrid_rrf`. By default it evaluates against recommendable held-out products only, so the ground truth matches the backend's availability and quality filters. It writes the summary, intermediate files, and English-language charts to `reports/evaluation/offline_comparison/`. Metrics include strict exact-ASIN ranking metrics plus `title_overlap` as a softer similar-product discovery proxy.

### Review mention extraction

Run review mention extraction when you want review-derived attribute signals:

```bash
conda run -n py312 python scripts/extract_review_mentions_llm.py --limit 500
```

The script reads existing `Review` nodes, extracts attribute mentions and sentiment with an LLM, then writes `Review -[:MENTIONS]-> Attribute` relationships. When these relationships exist, recommendation ranking adds positive review mention boosts and negative review mention penalties for query-relevant attributes.

## Data Scale

| Entity | Count |
|---|---|
| Products | 112,590 |
| Users | 168,659 |
| Reviews | 200,000 |
| Stores | 30,361 |
| Features | 89,666 |
| Categories | 2 |
| Attributes (LLM) | ~1,000 products covered so far |

## Next Steps

- Expand LLM attribute extraction coverage (currently ~1,000 of 112,590 products)
- Add collaborative filtering path: find similar users via shared rating history
- Add multi-step graph exploration endpoint (`GET /product/{id}/related`)
- Add natural language explanation generation from matched graph paths
