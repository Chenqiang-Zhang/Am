# Amazon Reviews'23 Knowledge Graph Recommender

This is an experimental product recommendation project built on the Amazon Reviews'23 `All_Beauty` category. The current goal is to transform review data and product metadata into a Neo4j-ready knowledge graph. Later stages will use LLMs for attribute extraction, user-intent parsing, and recommendation explanation generation.

## Current Graph

The first version of the graph contains these node types:

- `User`
- `Product`
- `Review`
- `Category`
- `Store`
- `Feature`

Main relationship types:

- `(User)-[:WROTE]->(Review)`
- `(Review)-[:REVIEWS]->(Product)`
- `(User)-[:RATED]->(Product)`
- `(Product)-[:BELONGS_TO]->(Category)`
- `(Product)-[:SOLD_BY]->(Store)`
- `(Product)-[:HAS_FEATURE]->(Feature)`

For the detailed graph schema, Neo4j import instructions, and example recommendation queries, see [KG_README.md](KG_README.md).

## Recommended Repository Structure

```text
.
├── README.md
├── KG_README.md
├── requirements.txt
├── data/                  # Local raw data, not committed to Git
├── kg_output/             # Generated CSV exports, not committed to Git
├── neo4j/                 # Cypher import scripts
├── scripts/               # Data processing and CSV build scripts
└── data.ipynb             # Data analysis notebook
```

This repository is intended to version-control code, documentation, graph schema notes, Cypher scripts, and experiment records. Raw datasets and generated CSV exports are large, so they should stay local or be stored with Git LFS, object storage, or a separate data-release repository.

## Quick Start

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Place the Amazon Reviews'23 files locally:

```text
data/All_Beauty.jsonl.gz
data/meta_All_Beauty.jsonl.gz
```

Generate Neo4j CSV files:

```bash
python3 scripts/build_kg_csv.py
```

Extract product attributes with the OpenAI API:

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY or DEEPSEEK_API_KEY.
python3 scripts/extract_product_attributes_llm.py \
  --provider deepseek \
  --limit 20 \
  --resume \
  --compact-input \
  --skip-sparse \
  --batch-size 5
```

The extractor reads credentials from the environment or a local `.env` file, then writes JSONL output to `kg_output/attributes/product_attributes_llm.jsonl` by default. Start with a small `--limit`, inspect the result quality, then scale up with `--resume`.

Convert extracted attributes into Neo4j CSV files:

```bash
python3 scripts/attributes_to_kg_csv.py
```

Then import the generated `nodes_attributes.csv` and `rel_product_attribute.csv` with `neo4j/import_openai_attributes.cypher`.
For Aura, import them directly over Bolt:

```bash
python3 scripts/import_attributes_to_neo4j.py --input-dir kg_output/all_beauty_aura_small
```

Import the base graph directly into Neo4j Aura over Bolt:

```bash
# Put NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD in .env first.
python3 scripts/import_kg_to_neo4j.py
```

For Aura instances with a 200k node limit, build and import a smaller graph first:

```bash
python3 scripts/build_kg_csv.py \
  --max-reviews 30000 \
  --max-meta 20000 \
  --max-features-per-product 10 \
  --output-dir kg_output/all_beauty_aura_small

python3 scripts/import_kg_to_neo4j.py --input-dir kg_output/all_beauty_aura_small
```

If you use Neo4j Aura and want Aura to read CSV files through GitHub raw URLs, generate smaller CSV chunks:

```bash
python3 scripts/split_csv_for_aura_github.py \
  --base-url https://raw.githubusercontent.com/USER/REPO/main
```

Then upload the CSV files from `kg_output/all_beauty_github/all_beauty/` to a publicly accessible location and run `neo4j/all_beauty_import_github_chunks.cypher`.

## Next Steps

- Use LLMs to extract more normalized product attributes from titles, descriptions, and reviews, such as effects, skin type, scent, texture, and usage scenario.
- Use LLMs to parse natural-language user needs into graph query constraints.
- Retrieve evidence paths from Neo4j, then use an LLM to generate recommendation explanations grounded in those paths.
