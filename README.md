# Amazon Reviews'23 Knowledge Graph Recommender

An experimental product recommendation system built on the Amazon Reviews'23 dataset. The genre/category is configurable via `config.yaml` (currently `Video_Games`) — the schema, pipeline scripts, and Cypher queries are all genre-agnostic. The system transforms review data and product metadata into a Neo4j knowledge graph, then exposes a REST API where an LLM writes and runs Cypher queries directly against the graph — so every recommendation reason is a real, inspectable graph path, not a black box.

## Architecture

```
Raw Data (Amazon Reviews'23)
    ↓ select_kcore.py (decide the k-core user/product selection — data scale)
    ↓ build_base_graph.py
Base graph CSVs (Product/User/Review/Category/Brand)
    ↓ extract_product_attributes.py (rule-based `details` extraction + LLM text extraction, merged)
    ↓ extract_review_mentions.py (LLM, from review text)
    ↓ canonicalize_attributes.py (optional — LLM-driven attr_type/value synonym canonicalization)
    ↓ build_attribute_graph.py
Attribute node/edge CSVs (genre-agnostic attr_type)
    ↓ import_kg_to_neo4j.py  (imports everything above in one pass)
Neo4j Knowledge Graph
    ↓ enrich_products.py --images --titles-ja (optional — Product.image_url / Product.title_ja)
    ↓
REST API (FastAPI)
    ├── Text2Cypher search      (LLM writes and runs a Cypher query per request; graph schema and the
    │                            attr_type vocabulary are read from the live graph, so the prompt adapts
    │                            to whatever genre/catalog is actually loaded — no hardcoded categories;
    │                            falls back to a popularity query whenever generation/execution fails
    │                            OR legitimately returns zero rows)
    ├── Personalization         (user rating/attribute history + past successful queries as few-shot;
    │                            $uid is only ever bound when a user_id has real RATED/attribute history)
    ├── Home recommendations    (behavior-based, no query text; skips the LLM entirely for users with
    │                            no history, and caches each personalized user's generated query in
    │                            memory so a background "warm" call — fired when the tab is hidden/
    │                            closed — can make the next page open instant)
    ├── Trending (no-LLM path)  (GET /recommend/trending — popularity query only, used for anonymous
    │                            visitors so the first paint has no LLM latency)
    ├── Conversational chat     (LLM decides what to ask and when to search, based on the same live
    │                            attr_type vocabulary — genre-agnostic; Python only enforces a hard cap
    │                            on the number of questions and a fallback if the LLM call itself fails)
    └── Review lookup and view logging (all written to Neo4j)
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
├── kg_build/              # KG-build pipeline — data creation (run: python kg_build/<name>.py)
│   ├── select_kcore.py            # 1. Decide the k-core user/product selection (data scale)
│   ├── build_base_graph.py        # 2. Build base graph CSVs (Product/User/Review/Category/Brand)
│   ├── extract_product_attributes.py  # 3a. Attribute extraction: zero-cost rule-based (metadata `details`, genre-agnostic) + LLM (title/features/description), merged
│   ├── extract_review_mentions.py     # 3b. LLM attribute-mention extraction from review text
│   ├── canonicalize_attributes.py     # 4. (optional) LLM-driven attr_type/value synonym canonicalization
│   ├── build_attribute_graph.py       # 5. Merge extraction output above → Attribute node/edge CSVs
│   ├── import_kg_to_neo4j.py          # 6. Import everything via Bolt (local Neo4j or Aura)
│   ├── enrich_products.py             # 7. (optional) Add Product.image_url / Product.title_ja to existing nodes
│   ├── wipe_neo4j.py                  # (utility) Delete all data from the configured Neo4j instance
│   └── utils/                         # Shared helper modules used only within kg_build/ — not run directly
│       ├── llm_client.py              #   LLM client builder (gemini/groq/deepseek/openai/ollama)
│       ├── llm_json.py                #   Chat/responses JSON-call + batch-with-fallback helpers
│       ├── neo4j_io.py                #   .env loading + Neo4j connection resolution
│       ├── csv_io.py                  #   JSONL/CSV read-write helpers
│       └── text_utils.py              #   Text-cleaning / attr_type-normalization / ID-hashing helpers
├── eval/                  # Evaluation — reads the live graph + app/api/, independent of kg_build/
│   └── eval_offline.py            # Offline leave-one-out evaluation (HR@K/NDCG@K vs. an Item-KNN baseline)
├── app/                   # The running application (backend + frontend)
│   ├── api/                       # Recommendation REST API
│   │   ├── main.py                # FastAPI app / routes
│   │   ├── recommender.py         # Text2Cypher generation, chat, personalization, reviews
│   │   └── models.py              # Pydantic request/response models
│   └── web/                       # React + TypeScript conversational UI
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

`config.yaml` controls the LLM provider/model, data paths, and Text2Cypher retry settings — see the comments in that file. Product/user selection is controlled separately by the k-core size (`--k` on `select_kcore.py`, see step 4 below), not by a config value.

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

Place the Amazon Reviews'23 data files locally (paths from `config.yaml`'s `data` section; currently the `Video_Games` category):
```text
data/Video_Games.jsonl.gz
data/meta_Video_Games.jsonl.gz
```

Download them directly from the dataset's raw file host (~1 GB combined for `Video_Games`; swap the category name in both URLs to use a different genre):
```bash
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Video_Games.jsonl.gz
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Video_Games.jsonl.gz
```

Run the pipeline in order (all under `kg_build/`; matches the docstring in `import_kg_to_neo4j.py`):

```bash
# 1. Decide which users/products to include: a bipartite k-core where every
#    user and every product has at least k interactions (k=3 by default).
#    Writes selected_user_ids.txt / selected_product_ids.txt to
#    <output_dir>/kcore_selection.
python3 kg_build/select_kcore.py --k 3

# 2. Base graph (Product/User/Review/Category/Brand), scoped to the k-core
#    selection from step 1.
python3 kg_build/build_base_graph.py

# 3. Attribute extraction (product metadata + review mentions).
#    --product-ids-file scopes extraction to the products actually selected
#    into the base graph in step 2.
python3 kg_build/extract_product_attributes.py --resume --product-ids-file kg_output/video_games_kcore3/nodes_products.csv
python3 kg_build/extract_review_mentions.py --resume

# 4. (optional) Canonicalize attr_type/value synonyms created by the two
#    independent extraction passes above (e.g. "item_form" vs "texture")
python3 kg_build/canonicalize_attributes.py

# 5. Merge extraction output into Attribute node/edge CSVs
#    (applies the canonicalization map from step 4, and drops any attributes
#    for products outside nodes_products.csv, if present)
python3 kg_build/build_attribute_graph.py

# 6. Import everything (base graph + attributes, if CSVs exist) via Bolt
python3 kg_build/import_kg_to_neo4j.py

# 7. (optional) Add Product.image_url from metadata (enables product thumbnails in the UI)
#    and/or translate Product.title to Japanese (Product.title_ja, used when a request's
#    lang="ja"). Safe to re-run after scaling up — only untranslated products are picked up.
python3 kg_build/enrich_products.py --images --titles-ja
```

k を大きくするほど密（1ユーザーあたりの相互作用数が多い）だがユーザー・商品数は少ないグラフになる。再選定する場合は再度ステップ1から実行する（選定が変わるため、ステップ2以降も再実行が必要）。

`--provider`/`--model` on step 3 default to `config.yaml`'s `llm.provider`/`llm.model`; pass them explicitly to override.

`extract_product_attributes.py` always runs a zero-cost, genre-agnostic rule-based
pass over the metadata `details` field first (deterministic, no LLM call, no hand-maintained
key mapping), then runs the LLM over title/features/description. Each product's rule-derived
attributes are passed to the LLM as `known_attributes` so it extracts only genuinely new
information instead of re-deriving the same facts. To skip the LLM entirely and extract
attributes for every product at zero cost:
```bash
python3 kg_build/extract_product_attributes.py --rule-only --limit -1
```
This is useful when running the costed LLM pass only on a small `--limit` for budget
reasons, while still covering the full catalog with rule-based attributes.

### 5. Start the Recommendation API

```bash
uvicorn app.api.main:app --reload
```

Or, to honor `config.yaml`'s `api.host`/`api.port` instead of uvicorn's defaults:
```bash
python -m app.api.main
```

Open `http://localhost:8000/docs` (or your configured host/port) for the interactive Swagger UI.

### 6. Start the Frontend UI

```bash
cd app/web
npm install
npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api/*` to `http://localhost:8000`.

### 7. Try It Out

1. On open, the chat page immediately shows a first batch of recommendations before you type
   anything — this is not a chat turn (no "assistant is typing" bubble), just a lightweight background
   fetch. Anonymous visitors get `GET /recommend/trending` (no LLM call, near-instant). If a test user
   with rating history is selected, `POST /recommend/home` is used instead, and gets faster on repeat
   visits — see the caching note below.
2. In the top of the chat UI, pick a **test user** from the dropdown (`TestUserSelect`) — either
   "匿名" (anonymous, no personalization), the built-in "オリジナルテストユーザー" demo ID, or one of
   the real `user_id`s fetched live from `GET /users/sample` (these are real users with ≥3 ratings
   in the current graph, so their picks will actually show personalized results/home recommendations).
3. Type a query in natural language (Japanese or English), e.g. "小学生の子供と一緒に遊べる協力プレイのSwitchゲームが欲しい"
   or "a co-op couch game for the PS5 that's fun for kids and adults together".
4. The assistant will either ask a clarifying question (answer it, or pick "こだわらない" / "no
   preference" to skip) or go straight to search once it has enough signal.
5. Recommendations show the LLM's one-sentence `explanation` — written in the UI's current language
   (toggle "日本語"/"EN" at the top) — and, in dev mode, the matched attributes and the raw generated
   Cypher (`intent.cypher`) so every recommendation reason is inspectable, not a black box. If Text2Cypher
   generation/execution fails, or the query legitimately matches nothing, the list falls back to popular
   highly-rated products instead of showing an empty screen (`fallback: true` in the response).
6. Opening "レビューを見る" or clicking "Amazon.comで見る" logs a `VIEWED` edge via `/behavior/view`,
   linked to the originating `search_id`. `_get_dynamic_few_shot()` reads these back (joined against
   `SearchLog`) to prioritize past queries that led to a click when building this user's next prompt.
7. Once a test user with history has loaded home recommendations, switching tabs away or closing the
   tab fires a beacon to `POST /recommend/home/warm`, which regenerates and caches that user's
   personalized query server-side in the background. The next time the page is opened (within the
   1-hour cache TTL), `/recommend/home` returns instantly from that cache instead of waiting on the LLM.

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

`lang` ("ja" | "en", default "en") controls the language of both the top-level `intent.cypher_explanation` and each recommendation's `explanation` — the LLM is instructed to write both in the requested language (the few-shot examples in the prompt are English for illustration only), and — if `enrich_products.py --titles-ja` has been run — `lang="ja"` also populates `display_title` with the cached Japanese translation (`null` otherwise; the frontend falls back to `title`).

If Cypher generation/execution fails after retries, **or the generated query runs successfully but returns zero rows**, the response falls back to a popularity-based query (`fallback: true`) instead of showing an empty result.

### `GET /recommend/trending`

No request body — query params `limit` (default 10) and `lang` (default "en"). Returns popular, highly-rated products directly from Neo4j with no LLM call at all, so it has effectively no latency beyond the database round-trip. Used for the initial recommendations shown to anonymous visitors before they've typed anything, and reused internally as the fallback query everywhere else.

### `POST /recommend/home`

Behavior-based recommendations with no query text (`user_id` required, `lang` optional as above). For a user with no `RATED`/attribute history, this skips the LLM entirely and is equivalent to `/recommend/trending`. For a user with history, the LLM generates a personalized Cypher query on first call and the result is cached server-side (per `user_id`+`lang`+`limit`, 1-hour TTL) — later calls return instantly from that cache. See `/recommend/home/warm` below for how the cache gets pre-populated before the user even asks.

### `POST /recommend/home/warm`

Fire-and-forget: takes the same body as `/recommend/home` and always returns `204` immediately. Triggers the same cache-populating generation as `/recommend/home` in the background, but doesn't wait for it or write a `SearchLog` entry (so switching tabs repeatedly doesn't spam search history). The web UI calls this via `navigator.sendBeacon` on `visibilitychange`/`pagehide`, so a user's personalized home recommendations are typically already cached by the time they reopen the app.

### `POST /behavior/view`

Logs that a user viewed a product (`user_id`, `product_id`, optional `search_id`) as a `VIEWED` edge, used as a personalization signal. The web UI calls this when a test user clicks "Amazon.comで見る" on a recommendation card.

### `POST /chat`

Runs one turn of conversational recommendation. Each turn, the LLM is given the attribute types actually present in the graph (queried once from Neo4j and cached) plus the genre from `config.yaml`, and decides itself — via `action`/`filled_slots` in its structured response — whether to ask another clarifying question or move to search; this makes the question flow adapt to whatever catalog/genre is loaded, with no hardcoded categories or question templates. Python only enforces a hard cap (`MAX_QUESTIONS = 5`) and falls back to searching immediately if the LLM call itself fails. Once search is triggered, it delegates to the same Text2Cypher path as `/recommend`.

```json
{
  "messages": [
    {"role": "user", "content": "小学生の子供と一緒に遊べる協力プレイのSwitchゲームが欲しい"}
  ],
  "limit": 8,
  "lang": "ja",
  "user_id": null
}
```

The response has either:
- `action: "ask"` with one question and quick-reply options (`search_id: null`)
- `action: "search"` with `preference_summary`, `intent` (cypher/explanation), recommendations, and
  `search_id` (so a later `/behavior/view` call can be linked back to this search)

### `GET /users/sample`

Returns a handful of real `user_id`s with rating history, for demoing personalization without an auth system.

### `GET /products/{product_id}/reviews`

Returns top reviews for a product, ordered by helpful votes.

## Data Scale

Data scale is controlled by the k-core size (`--k` on `select_kcore.py`, default 3): the largest bipartite subgraph where every user and every product has at least k interactions. This guarantees every user/product in the graph has enough history for personalization and offline evaluation to be meaningful (unlike naive top-N-products sampling, which leaves most users with only a single rating). `<output_dir>/kcore_selection/kcore_summary.json` records the resulting user/item/edge counts for a given k; after running `build_base_graph.py`, the actual imported counts are written to `kg_output/<output_dir>/build_summary.json`.

## Next Steps

- Expand LLM attribute extraction coverage to the full product set
- Add a collaborative-filtering few-shot path using shared rating history across users
- Add a multi-step graph exploration endpoint (`GET /product/{id}/related`)
- Explicit feedback on recommendation reasons (a `GAVE_FEEDBACK` edge / thumbs up-down UI) was tried
  and removed — `_get_dynamic_few_shot()`'s implicit click signal (`VIEWED` joined against `SearchLog`)
  covers the same "was this search useful" need without the extra UI/endpoint surface
- The home-recommendation cache (`Recommender._home_cache`) lives in process memory — it resets on
  restart and wouldn't be shared if the API were ever scaled to multiple instances; move it to Neo4j
  or a shared store (e.g. Redis) if that becomes an issue
- Free-tier LLM providers (Groq in particular) have a low daily token quota that a class demo can burn
  through quickly — `config.yaml`'s `llm.provider` can be switched to `gemini` as a higher-quota
  fallback if `/recommend`/`/chat` start returning `fallback: true` unexpectedly (check the API
  process's stderr for a `429`/`rate_limit_exceeded` error to confirm)
