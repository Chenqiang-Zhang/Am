# Video Games Backend Setup

The current backend targets the Amazon Reviews'23 `Video_Games` subset. Runtime code is under
`app/api/`, and the backend expects the schema documented in `Graph_rule.md`.

## Processed Data

A teammate-provided Neo4j dump is available locally at:

```text
/Users/chenqiang/Desktop/data/neo4j.dump
```

The dump was created with Neo4j `5.26` and contains the processed Video Games graph, including
Japanese display fields for product titles, reviews, and attribute values.

Verified local graph size:

| Label | Count |
|---|---:|
| `Attribute` | 132,751 |
| `Review` | 57,354 |
| `User` | 2,918 |
| `Product` | 1,728 |
| `Brand` | 127 |
| `Category` | 96 |

| Relationship | Count |
|---|---:|
| `MENTIONS` | 261,353 |
| `WROTE` | 57,354 |
| `ABOUT` | 57,354 |
| `RATED` | 57,354 |
| `HAS_ATTRIBUTE` | 14,081 |
| `MADE_BY` | 1,728 |
| `BELONGS_TO` | 1,728 |
| `SUBCATEGORY_OF` | 160 |

## Restore the Dump

Use a separate container from older All Beauty graphs to avoid data and port conflicts.

```bash
mkdir -p ~/mm_video_games_kg
cp /Users/chenqiang/Desktop/data/neo4j.dump ~/mm_video_games_kg/

docker volume create neo4j-video-games-data

docker run --rm \
  -v neo4j-video-games-data:/data \
  -v ~/mm_video_games_kg:/backups \
  neo4j:5.26-community \
  neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true

docker run -d --name neo4j-video-games \
  -p 7476:7474 -p 7691:7687 \
  -v neo4j-video-games-data:/data \
  -e NEO4J_AUTH=neo4j/password123 \
  neo4j:5.26-community
```

Verify:

```bash
docker exec neo4j-video-games cypher-shell -u neo4j -p password123 \
  "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY c DESC"
```

## Backend Configuration

Point local `.env` to the restored graph:

```env
NEO4J_URI=bolt://localhost:7691
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
NEO4J_DATABASE=neo4j
```

`config.yaml` currently uses:

```yaml
genre: "Video_Games"
llm:
  provider: "deepseek"
```

Make sure the corresponding API key is present in `.env`.

## Run and Check the Backend

```bash
uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Basic checks:

```bash
curl -s http://127.0.0.1:8000/health
curl -s 'http://127.0.0.1:8000/users/sample?limit=5'
curl -s -X POST http://127.0.0.1:8000/recommend/home \
  -H 'Content-Type: application/json' \
  -d '{"limit":5,"lang":"ja"}'
curl -s 'http://127.0.0.1:8000/products/B0C3KYVDWT/reviews?limit=2&lang=ja'
```

Text2Cypher search check:

```bash
curl -s -X POST http://127.0.0.1:8000/recommend \
  -H 'Content-Type: application/json' \
  -d '{"query":"I want a Nintendo Switch game or accessory with good reviews","limit":5,"lang":"en"}'
```

Expected behavior: the API returns Video Games products, Japanese display fields are available
when `lang="ja"`, and Text2Cypher requests return `fallback: false` when the LLM provider is
configured correctly.

## Run the Frontend Together

The React/Vite frontend lives in `app/web/` and proxies `/api/*` to
`http://localhost:8000`, so the backend must use port `8000` for local
end-to-end testing.

```bash
cd app/web
npm ci
npm run dev -- --host 127.0.0.1 --port 5173
```

Open `http://localhost:5173` and verify the proxy with:

```bash
curl -s http://127.0.0.1:5173/api/health
curl -s -X POST http://127.0.0.1:5173/api/recommend/home \
  -H 'Content-Type: application/json' \
  -d '{"limit":5,"lang":"ja"}'
```
