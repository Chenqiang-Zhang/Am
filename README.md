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
    ├── Multi-path graph recall (attributes + feature text + title/category/store)
    └── Hybrid ranking          (match coverage + rating quality + popularity)
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
│   ├── attributes_to_kg_csv.py            # Convert attributes JSONL → CSV
│   ├── import_kg_to_neo4j.py              # Import base graph via Bolt
│   ├── import_attributes_to_neo4j.py      # Import attributes via Bolt
│   └── split_csv_for_aura_github.py       # Split CSVs for GitHub upload
├── api/                   # Recommendation REST API
│   ├── main.py            # FastAPI app
│   ├── recommender.py     # LLM intent extraction + Neo4j graph query
│   └── models.py          # Pydantic request/response models
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

## API Usage

### `POST /recommend`

Accepts a natural-language query and returns ranked product recommendations with graph-backed evidence and a score breakdown.

**Request:**
```json
{
  "query": "I have dry and sensitive skin, looking for a gentle face moisturizer with hyaluronic acid, preferably fragrance-free",
  "limit": 5
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
  "recommendations": [
    {
      "product_id": "B0...",
      "title": "...",
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
        "query_coverage": 0.86
      },
      "explanation": "Matched - skin_type: dry | ingredient: hyaluronic acid | text terms: dry, fragrance-free, gentle, hyaluronic acid | rating: 4.5 from 328 ratings"
    }
  ]
}
```

The recommender uses three recall paths, then re-ranks the merged candidates:

- `Product -[:HAS_ATTRIBUTE]-> Attribute` for precise structured matches
- `Product -[:HAS_FEATURE]-> Feature` for broader text matches from product metadata
- `Product` title/category and `Product -[:SOLD_BY]-> Store` for simple field matches

The final score combines attribute match, feature-text match, field match, query coverage, Bayesian-smoothed rating quality, and popularity. The `matched_attributes` array represents the most precise graph path that justifies a recommendation:
`User Query → [LLM intent] → Attribute ←[HAS_ATTRIBUTE]← Product`

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
