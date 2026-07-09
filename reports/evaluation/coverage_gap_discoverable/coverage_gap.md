# Coverage Gap Analysis

Created at: `2026-07-07T17:41:06.756232+00:00`
Evaluation source: `reports/evaluation/offline_comparison_discoverable`

## Summary

- Unique holdout products: `385`
- Weighted holdout events: `400`
- Candidate catalog size: `20,000`
- Unique holdouts in candidate catalog: `31`
- Unique holdouts outside candidate catalog: `354`
- Priority repair candidates: `289`
- Attribute-extraction priority meta rows: `5`

## Pool Split

| Pool | Unique Products | Weighted Events |
|---|---:|---:|
| `discoverable_pool` | 289 | 303 |
| `available_pool` | 96 | 97 |

## Top Exclusion Reasons

| Reason | Unique Products | Weighted Events |
|---|---:|---:|
| `missing_price` | 289 | 303 |
| `status_currently_unavailable` | 289 | 303 |
| `missing_features` | 267 | 278 |
| `not_in_candidate_catalog` | 49 | 50 |
| `in_candidate_catalog` | 31 | 31 |
| `low_quality_score` | 16 | 16 |
| `missing_attributes` | 5 | 5 |

## Recommended Next Actions

- `keep_unavailable_or_source_price`: 289 priority candidates
- `reaudit_quality_after_enrichment`: 16 priority candidates
- `extract_attributes`: 5 priority candidates

## Top Priority Products

| Product | Count | Score | Pool | Reasons | Title |
|---|---:|---:|---|---|---|
| `B00OS9YWJY` | 2 | 13.87 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Pure Hyaluronic Acid Serum with Vitamin C for Face - Organic Anti Aging & Anti-W |
| `B087G54FLM` | 2 | 13.60 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Poly Nail Gel Kit, 7 Colors Nail Extension Gel with UV Lamp, Poly Extension Deco |
| `B01AYTGWA8` | 2 | 13.50 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Fungus Nail Treatment,Sky-shop Fungus Nail Repair Oil, Fungal Nail Eliminator fo |
| `B01HIIT4TE` | 2 | 13.45 | `discoverable_pool` | missing_price, status_currently_unavailable | Anti Aging Eye Wrinkle Cream for Women & Men with Hyaluronic Acid Matrixyl 3000  |
| `B09473GGM4` | 2 | 13.11 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Tirtyl Hand Soap Sheet Variety Pack - 240 Pack with Portable Storage Tin - Campi |
| `B07H53GCV2` | 2 | 13.03 | `discoverable_pool` | missing_price, status_currently_unavailable | 10 Pcs Brown and 10 Pcs Black Wig Grip Headband for women |
| `B07T3Z58HL` | 2 | 12.70 | `discoverable_pool` | missing_price, status_currently_unavailable | Hand Crafted Ceramic Aromatherapy Essential Oil Diffuser,100ml Fragrant Room Spr |
| `B0851QJPZY` | 2 | 12.65 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Gentlehomme Face Moisturizer For Men - Mens Facial Lotion with Neroli Essential  |
| `B07QN8B5VG` | 2 | 12.53 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | N3 No Name Necessary Pore-Minimizing Mattifying Oil and Shine Control Anti-aging |
| `B08LCB741R` | 2 | 12.52 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Microfiber Hair Towel Two Layers Turban Wrap YANJIE Super Absorbent Shower Head  |
| `B08W8LKLHB` | 2 | 12.49 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Maestee 9 Pcs Gel Nail Polish Kit, 6 Colors (8ml) with Base and Top Coat and Mat |
| `B07FP11ZN6` | 2 | 12.48 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Fifth & Skin (MEDIUM) Better'n Ur Skin - Prep n Set Blur Powder - Natural Face P |
| `B01IVICJXS` | 1 | 12.42 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_currently_unavailable | Makeup Palette ,Start Makers 6 Colors Contour Highlighting Kit Oval Toothbrush a |
| `B0B4JPGX8P` | 2 | 12.27 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Elli K Essential Sincerity From AZ Time Reverse Double Ampoule – Made in USA - D |
| `B089KBMST6` | 2 | 12.00 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Mini Rubber Bands Soft Elastic Bands for Kids Hair Non-slip Rubber Hair Bands So |
| `B00MFWKN9E` | 1 | 11.96 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_currently_unavailable | Anti Aging Moisturizer with Peptides-Advanced Luxury Concentrated Argireline Cre |
| `B01EST7TBQ` | 1 | 11.77 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_currently_unavailable | Hair Growth Serum - Help Anti-Hair Loss & Promotes Hair Long - Fast Effective in |
| `B01FFM6Q1Y` | 1 | 11.59 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_currently_unavailable | Pentop New Design Wearable Silicone Nail Polish Holder Rubber Nail Polish Bottle |
| `B01G6IJ66S` | 1 | 11.28 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_currently_unavailable | TIAMALL Makeup Brush Cleaner Brush Cleaning Mat Silicone Cleaning Pad Cosmetic B |
| `B0811YBYVR` | 1 | 10.08 | `discoverable_pool` | missing_features, missing_price, status_currently_unavailable | Nail Files and Buffers, Teenitor 16PCS Professional Nail Manicure Tool for Acryl |

## Output Files

- JSON report: `reports/evaluation/coverage_gap_discoverable/coverage_gap.json`
- Product rows: `reports/evaluation/coverage_gap_discoverable/coverage_gap_products.jsonl`
- Priority candidates: `reports/evaluation/coverage_gap_discoverable/priority_candidates.jsonl`
- Priority meta subset: `reports/evaluation/coverage_gap_discoverable/priority_candidates_meta.jsonl`
- Attribute priority meta subset: `reports/evaluation/coverage_gap_discoverable/attribute_priority_candidates_meta.jsonl`
- Chart: `reports/evaluation/coverage_gap_discoverable/coverage_gap_by_reason.png`
