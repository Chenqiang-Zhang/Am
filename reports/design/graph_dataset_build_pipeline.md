# Graph Dataset Build Pipeline

## Goal

Confirm a cleaner graph dataset construction flow before expanding LLM attribute extraction. The key change is to filter invalid products before spending LLM/GPU/API budget.

## Proposed Pipeline

```text
Raw Amazon Reviews'23 metadata and reviews
-> pre-LLM catalog filtering
-> clean metadata/review files
-> base KG CSV build
-> Neo4j import
-> low-cost metadata detail extraction
-> LLM attribute extraction on clean candidates
-> attribute import
-> product quality audit
-> dataset report
```

## Pre-LLM Filtering Rules

`scripts/filter_catalog_before_llm.py` applies these checks:

- remove products without `parent_asin`
- remove products with missing or very short titles
- remove products with missing or invalid price
- remove products with missing Amazon link
  - by default, `/dp/{ASIN}` links are generated when the source lacks a URL
  - use `--require-source-amazon-url` to require a source URL field
- remove products with too little metadata text
- deduplicate near-identical product titles and keep the higher-information row
- optionally filter reviews so only reviews for selected products remain

## Suggested Commands

```bash
conda run -n py312 python scripts/filter_catalog_before_llm.py

conda run -n py312 python scripts/build_kg_csv.py \
  --meta-path kg_output/cleaned/meta_All_Beauty.clean.jsonl.gz \
  --review-path kg_output/cleaned/All_Beauty.clean.jsonl.gz

conda run -n py312 python scripts/select_attribute_extraction_candidates.py \
  --meta-path kg_output/cleaned/meta_All_Beauty.clean.jsonl.gz

conda run -n py312 python scripts/extract_product_attributes_llm.py \
  --provider deepseek \
  --meta-path kg_output/attributes/candidates_for_llm.jsonl \
  --resume \
  --compact-input \
  --batch-size 5

conda run -n py312 python scripts/audit_product_quality.py
```

## Report Outputs

- `reports/dataset_quality/pre_llm_catalog_filter_report.json`
- `reports/product_quality/*.json`
- `reports/product_quality/*.md`

## Research Positioning

This pipeline supports the claim that the system is not simply "LLM over noisy product data." It first enforces product sellability and data completeness, then applies LLM extraction only to products that are worth representing in the graph.
