# Amazon Reviews'23 Knowledge Graph Recommender

An experimental product recommendation system built on the Amazon Reviews'23 `All_Beauty` dataset. The system transforms review data and product metadata into a Neo4j knowledge graph, then exposes a REST API where an LLM writes and runs Cypher queries directly against the graph — so every recommendation reason is a real, inspectable graph path, not a black box.

## Architecture

```
Raw Data (Amazon Reviews'23)
    ↓ build_kg_csvs.py
Base graph CSVs (Product/User/Review/Category/Brand)
    ↓ extract_product_attributes.py (rule-based `details` extraction + LLM text extraction, merged)
    ↓ extract_review_mentions.py (LLM, from review text)
    ↓ normalize_attributes.py (optional — LLM-driven attr_type/value synonym canonicalization)
    ↓ build_attribute_csvs.py
Attribute node/edge CSVs (genre-agnostic attr_type)
    ↓ import_kg_to_neo4j.py  (imports everything above in one pass)
Neo4j Knowledge Graph
    ↓ enrich_product_images.py (optional — Product.image_url)
    ↓ translate_titles.py (optional — Product.title_ja)
    ↓
REST API (FastAPI)
    ├── Text2Cypher search      (LLM writes and runs a Cypher query per request; graph schema and the
    │                            attr_type vocabulary are read from the live graph, so the prompt adapts
    │                            to whatever genre/catalog is actually loaded — no hardcoded categories)
    ├── Personalization         (user rating/attribute history + past successful queries as few-shot;
    │                            $uid is only ever bound when a user_id has real RATED/attribute history)
    ├── Home recommendations    (behavior-based, no query text)
    ├── Conversational chat     (LLM decides what to ask and when to search, based on the same live
    │                            attr_type vocabulary — genre-agnostic; Python only enforces a hard cap
    │                            on the number of questions and a fallback if the LLM call itself fails)
    └── Review lookup & reason feedback logging
```

## Graph Schema

The authoritative schema definition is [`Graph_rule.md`](Graph_rule.md) (kept in sync with the repository-root copy at [`../Graph_rule.md`](../Graph_rule.md)). Summary:

**Nodes**

| Label | Key Properties |
|---|---|
| `User` | `user_id` |
| `Product` | `product_id`, `title`, `title_ja`, `price`, `avg_rating`, `rating_count`, `description`, `image_url` |
| `Review` | `review_id`, `title`, `text`, `rating`, `timestamp`, `helpful_vote`, `verified` |
| `Category` | `category_id`, `name`, `level` |
| `Brand` | `brand_id`, `name` |
| `Attribute` | `attribute_id`, `attr_type`, `value` (LLM-extracted, genre-agnostic) |
| `SearchLog` | `log_id`, `query`, `cypher`, `explanation`, `result_product_ids`, `result_count`, `timestamp` (personalization few-shot) |

**Relationships**

| Relationship | Direction | Properties |
|---|---|---|
| `WROTE` | User → Review | — |
| `ABOUT` | Review → Product | — |
| `RATED` | User → Product | `rating`, `timestamp` |
| `VIEWED` | User → Product | `timestamp`, `search_id` |
| `SEARCHED` | User → SearchLog | — |
| `BELONGS_TO` | Product → Category | — |
| `SUBCATEGORY_OF` | Category → Category | — |
| `MADE_BY` | Product → Brand | — |
| `HAS_ATTRIBUTE` | Product → Attribute | `confidence`, `evidence`, `source`, `model` |
| `MENTIONS` | Review → Attribute | `sentiment`, `confidence` |

`Store`/`Feature` nodes and the `HAS_FEATURE`/`REVIEWS` relationships from earlier iterations have been retired — feature text is folded into `Product.description`, and `Store` was merged into `Brand`.

## Repository Structure

```text
.
├── README.md
├── Graph_rule.md          # Full copy of the schema doc, manually kept in sync with ../Graph_rule.md
├── config.yaml            # Genre/data-scale/LLM-provider/API config
├── requirements.txt
├── data/                  # Raw data — local only, not committed
├── kg_output/             # Generated CSVs — local only, not committed
├── docs/                  # Local documentation — not committed
├── scripts/               # Data pipeline scripts
│   ├── build_kg_csvs.py               # Build base graph CSVs (Product/User/Review/Category/Brand)
│   ├── llm_client.py                  # Shared LLM client builder (gemini/groq/deepseek/openai/ollama)
│   ├── extract_product_attributes.py  # Attribute extraction: zero-cost rule-based (metadata `details`, genre-agnostic) + LLM (title/features/description), merged
│   ├── extract_review_mentions.py     # LLM attribute-mention extraction from review text
│   ├── normalize_attributes.py        # (optional) LLM-driven attr_type/value synonym canonicalization
│   ├── build_attribute_csvs.py        # Merge extraction output above → Attribute node/edge CSVs
│   ├── import_kg_to_neo4j.py          # Import everything via Bolt (local Neo4j or Aura)
│   ├── enrich_product_images.py       # (optional) Add Product.image_url from metadata
│   └── translate_titles.py            # (optional) Add Product.title_ja via LLM translation
├── api/                   # Recommendation REST API
│   ├── main.py            # FastAPI app / routes
│   ├── recommender.py     # Text2Cypher generation, chat, personalization, reviews, feedback
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
```

Edit `.env` with:
- `NEO4J_URI` / `NEO4J_PASSWORD` (Neo4j Aura connection info)
- An API key for whichever LLM provider you choose in `config.yaml` (`llm.provider`): `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, or `OPENAI_API_KEY`. Gemini and Groq both have a free tier.

`config.yaml` controls the LLM provider/model, data scale (`scale.max_meta`/`scale.max_reviews`), and Text2Cypher retry settings — see the comments in that file.

### 3. Start Neo4j

**Option A — Docker (local dev):**

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

**Option B — Neo4j Aura (cloud):** set `NEO4J_URI=neo4j+s://...` in `.env`. Free-tier Aura instances auto-pause after inactivity — resume them from the [Aura console](https://console.neo4j.io) before running the API or pipeline scripts.

### 4. Build and Import the Knowledge Graph

Place the Amazon Reviews'23 data files locally:
```text
data/All_Beauty.jsonl.gz
data/meta_All_Beauty.jsonl.gz
```

Run the pipeline in order (matches the docstring in `import_kg_to_neo4j.py`):

```bash
# 1. Base graph (Product/User/Review/Category/Brand)
python3 scripts/build_kg_csvs.py

# 2. Attribute extraction (product metadata + review mentions).
#    --product-ids-file scopes extraction to the products actually selected
#    into the base graph in step 1 (scale.max_meta) — omit it only if you
#    intend to pre-warm the zero-cost rule-based cache for the full catalog
#    ahead of a future scale.max_meta increase.
python3 scripts/extract_product_attributes.py --resume --product-ids-file kg_output/all_beauty/nodes_products.csv
python3 scripts/extract_review_mentions.py --resume

# 3. (optional) Canonicalize attr_type/value synonyms created by the two
#    independent extraction passes above (e.g. "item_form" vs "texture")
python3 scripts/normalize_attributes.py

# 4. Merge extraction output into Attribute node/edge CSVs
#    (applies the canonicalization map from step 3, and drops any attributes
#    for products outside nodes_products.csv, if present)
python3 scripts/build_attribute_csvs.py

# 5. Import everything (base graph + attributes, if CSVs exist) via Bolt
python3 scripts/import_kg_to_neo4j.py

# 6. (optional) Add Product.image_url from metadata — enables product thumbnails in the UI
python3 scripts/enrich_product_images.py

# 7. (optional) Translate Product.title to Japanese (Product.title_ja) — enables
#    localized titles when a request's lang="ja". Safe to re-run after scaling up
#    (only untranslated products are picked up).
python3 scripts/translate_titles.py
```

`--provider`/`--model` on step 2 default to `config.yaml`'s `llm.provider`/`llm.model`; pass them explicitly to override.

`extract_product_attributes.py` always runs a zero-cost, genre-agnostic rule-based
pass over the metadata `details` field first (deterministic, no LLM call, no hand-maintained
key mapping), then runs the LLM over title/features/description. Each product's rule-derived
attributes are passed to the LLM as `known_attributes` so it extracts only genuinely new
information instead of re-deriving the same facts. To skip the LLM entirely and extract
attributes for every product at zero cost:
```bash
python3 scripts/extract_product_attributes.py --rule-only --limit -1
```
This is useful when running the costed LLM pass only on a small `--limit` for budget
reasons, while still covering the full catalog with rule-based attributes.

### 5. Start the Recommendation API

```bash
uvicorn api.main:app --reload
```

Or, to honor `config.yaml`'s `api.host`/`api.port` instead of uvicorn's defaults:
```bash
python -m api.main
```

Open `http://localhost:8000/docs` (or your configured host/port) for the interactive Swagger UI.

### 6. Start the Frontend UI

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api/*` to `http://localhost:8000`.

### 7. Try It Out

1. In the top of the chat UI, pick a **test user** from the dropdown (`TestUserSelect`) — either
   "匿名" (anonymous, no personalization), the built-in "オリジナルテストユーザー" demo ID, or one of
   the real `user_id`s fetched live from `GET /users/sample` (these are real users with ≥3 ratings
   in the current graph, so their picks will actually show personalized results/home recommendations).
2. Type a query in natural language (Japanese or English), e.g. "乾燥肌向けの保湿クリームが欲しい"
   or "a gentle fragrance-free moisturizer for dry skin".
3. The assistant will either ask a clarifying question (answer it, or pick "こだわらない" / "no
   preference" to skip) or go straight to search once it has enough signal.
4. Recommendations show the LLM's one-sentence `explanation`, and (in dev mode) the matched
   attributes and the raw generated Cypher (`intent.cypher`) so every recommendation reason is
   inspectable, not a black box.
5. Click 👍/👎 under a recommendation to send `/recommendations/{id}/feedback` (logged to
   `logs/recommendation_feedback.jsonl` for offline review — it does not yet feed back into ranking).

If step 4 in the pipeline (Neo4j import) hasn't been run, or Neo4j is unreachable, `/health` will
still return `ok` but `/recommend`/`/chat` calls will fail — check the API process's stderr output
first.

## API Usage

### `POST /recommend`

Accepts a natural-language query. The LLM generates one Cypher query against the graph and returns its results. Personalization only kicks in when `user_id` refers to a user with actual `RATED`/attribute history — a `user_id` with no history is treated the same as an anonymous request (no `$uid` is bound, so the LLM cannot reference it; a validator rejects any generated Cypher that references `$uid` when it isn't bound, or that hardcodes a literal `user_id` string instead of using `$uid`).

**Request:**
```json
{
  "query": "I have dry and sensitive skin, looking for a gentle face moisturizer with hyaluronic acid, preferably fragrance-free",
  "user_id": null,
  "limit": 10,
  "lang": "en"
}
```

**Response (abbreviated):**
```json
{
  "query": "...",
  "mode": "search",
  "intent": {
    "cypher": "MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute) WHERE ... RETURN p.product_id AS product_id, ... ORDER BY score DESC LIMIT $limit",
    "cypher_explanation": "Finds fragrance-free moisturizers matched to dry/sensitive skin with hyaluronic acid"
  },
  "recommendations": [
    {
      "product_id": "B0...",
      "title": "...",
      "display_title": null,
      "image_url": "https://m.media-amazon.com/images/...",
      "price": 18.5,
      "avg_rating": 4.5,
      "rating_count": 328,
      "score": 2.85,
      "matched_attrs": [
        {"attr_type": "skin_type", "value": "dry"},
        {"attr_type": "ingredient", "value": "hyaluronic acid"}
      ],
      "explanation": "Matches dry/sensitive skin and contains hyaluronic acid; fragrance-free"
    }
  ],
  "search_id": "uuid",
  "fallback": false
}
```

`lang` ("ja" | "en", default "en") controls the language of the generated `explanation` text, and — if `translate_titles.py` has been run — populates `display_title` with the cached Japanese translation when `lang="ja"` (`null` otherwise; the frontend falls back to `title`).

If Cypher generation/execution fails after retries, `fallback: true` and the response falls back to a popularity-based query.

### `POST /recommend/home`

Behavior-based recommendations with no query text (`user_id` required, `lang` optional as above). Falls back to popular products for users with no history.

### `POST /behavior/view`

Logs that a user viewed a product (`user_id`, `product_id`, optional `search_id`), used as a personalization signal.

### `POST /chat`

Runs one turn of conversational recommendation. Each turn, the LLM is given the attribute types actually present in the graph (queried once from Neo4j and cached) plus the genre from `config.yaml`, and decides itself — via `action`/`filled_slots` in its structured response — whether to ask another clarifying question or move to search; this makes the question flow adapt to whatever catalog/genre is loaded, with no hardcoded categories or question templates. Python only enforces a hard cap (`MAX_QUESTIONS = 5`) and falls back to searching immediately if the LLM call itself fails. Once search is triggered, it delegates to the same Text2Cypher path as `/recommend`.

```json
{
  "messages": [
    {"role": "user", "content": "乾燥肌向けの保湿クリームが欲しい"}
  ],
  "limit": 8,
  "lang": "ja",
  "user_id": null
}
```

The response has either:
- `action: "ask"` with one question and quick-reply options
- `action: "search"` with `preference_summary`, `intent` (cypher/explanation), and recommendations

### `GET /users/sample`

Returns a handful of real `user_id`s with rating history, for demoing personalization without an auth system.

### `GET /products/{product_id}/reviews`

Returns top reviews for a product, ordered by helpful votes.

### `POST /recommendations/{product_id}/feedback`

Stores user feedback on whether a recommendation reason was useful, appended to `logs/recommendation_feedback.jsonl` (git-ignored).

```json
{
  "query": "dry sensitive skin moisturizer",
  "lang": "en",
  "helpful": true,
  "reason_rating": 5,
  "selected_reasons": ["reason_helpful"],
  "comment": "The explanation was clear."
}
```

## Data Scale

Data scale is controlled by `config.yaml`'s `scale` section (`max_meta` = number of products, `max_reviews` = per-product review cap). After running `build_kg_csvs.py`, the actual counts for the current config are written to `kg_output/<output_dir>/build_summary.json`.

## Next Steps

- Expand LLM attribute extraction coverage to the full product set
- Add a collaborative-filtering few-shot path using shared rating history across users
- Add a multi-step graph exploration endpoint (`GET /product/{id}/related`)
- Wire `/recommend/home` and `/behavior/view` into the web frontend (currently backend-only)
