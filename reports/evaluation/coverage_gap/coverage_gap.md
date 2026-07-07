# Coverage Gap Analysis

Created at: `2026-07-07T17:17:53.150818+00:00`
Evaluation source: `reports/evaluation/offline_comparison_200_users`

## Summary

- Unique holdout products: `386`
- Weighted holdout events: `400`
- Candidate catalog size: `17,280`
- Unique holdouts in candidate catalog: `69`
- Unique holdouts outside candidate catalog: `317`
- Priority repair candidates: `316`
- Attribute-extraction priority meta rows: `6`

## Pool Split

| Pool | Unique Products | Weighted Events |
|---|---:|---:|
| `discoverable_pool` | 316 | 329 |
| `available_pool` | 69 | 70 |
| `excluded_pool` | 1 | 1 |

## Top Exclusion Reasons

| Reason | Unique Products | Weighted Events |
|---|---:|---:|
| `missing_price` | 316 | 329 |
| `missing_features` | 272 | 282 |
| `low_quality_score` | 269 | 279 |
| `status_low_quality` | 269 | 279 |
| `in_candidate_catalog` | 69 | 70 |
| `status_currently_unavailable` | 45 | 48 |
| `duplicate_suspect` | 11 | 12 |
| `missing_attributes` | 6 | 6 |
| `status_duplicate_suspect` | 3 | 3 |
| `missing_or_short_title` | 1 | 1 |

## Recommended Next Actions

- `keep_unavailable_or_source_price`: 316 priority candidates
- `reaudit_quality_after_enrichment`: 268 priority candidates
- `deduplicate_or_keep_best_variant`: 10 priority candidates
- `extract_attributes`: 6 priority candidates

## Top Priority Products

| Product | Count | Score | Pool | Reasons | Title |
|---|---:|---:|---|---|---|
| `B00OS9YWJY` | 2 | 14.87 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Pure Hyaluronic Acid Serum with Vitamin C for Face - Organic Anti Aging & Anti-W |
| `B087G54FLM` | 2 | 14.60 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Poly Nail Gel Kit, 7 Colors Nail Extension Gel with UV Lamp, Poly Extension Deco |
| `B01AYTGWA8` | 2 | 14.50 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Fungus Nail Treatment,Sky-shop Fungus Nail Repair Oil, Fungal Nail Eliminator fo |
| `B09473GGM4` | 2 | 14.11 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Tirtyl Hand Soap Sheet Variety Pack - 240 Pack with Portable Storage Tin - Campi |
| `B08CPF22H2` | 2 | 13.83 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Gold Collagen Under Eye Patches Mask for dark circles and puffiness by Levitural |
| `B09L563Q84` | 2 | 13.70 | `discoverable_pool` | duplicate_suspect, low_quality_score, missing_features, missing_price, status_low_quality | Large Silk Scrunchies For Hair - Tara Sartoria Artisan Handmade |
| `B07QN8B5VG` | 2 | 13.53 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | N3 No Name Necessary Pore-Minimizing Mattifying Oil and Shine Control Anti-aging |
| `B08LCB741R` | 2 | 13.52 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Microfiber Hair Towel Two Layers Turban Wrap YANJIE Super Absorbent Shower Head  |
| `B07FP11ZN6` | 2 | 13.48 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Fifth & Skin (MEDIUM) Better'n Ur Skin - Prep n Set Blur Powder - Natural Face P |
| `B01HIIT4TE` | 2 | 13.45 | `discoverable_pool` | missing_price, status_currently_unavailable | Anti Aging Eye Wrinkle Cream for Women & Men with Hyaluronic Acid Matrixyl 3000  |
| `B07H53GCV2` | 2 | 13.03 | `discoverable_pool` | missing_price, status_currently_unavailable | 10 Pcs Brown and 10 Pcs Black Wig Grip Headband for women |
| `B089KBMST6` | 2 | 13.00 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Mini Rubber Bands Soft Elastic Bands for Kids Hair Non-slip Rubber Hair Bands So |
| `B08H4SYXR4` | 1 | 12.93 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | Caudalie Vinosource SOS Intense Hydration Set: Hydrating Trio for Sensitive Skin |
| `B07T3Z58HL` | 2 | 12.70 | `discoverable_pool` | missing_price, status_currently_unavailable | Hand Crafted Ceramic Aromatherapy Essential Oil Diffuser,100ml Fragrant Room Spr |
| `B01IVICJXS` | 1 | 12.42 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | Makeup Palette ,Start Makers 6 Colors Contour Highlighting Kit Oval Toothbrush a |
| `B00MFWKN9E` | 1 | 11.96 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | Anti Aging Moisturizer with Peptides-Advanced Luxury Concentrated Argireline Cre |
| `B01EST7TBQ` | 1 | 11.77 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | Hair Growth Serum - Help Anti-Hair Loss & Promotes Hair Long - Fast Effective in |
| `B01FFM6Q1Y` | 1 | 11.59 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | Pentop New Design Wearable Silicone Nail Polish Holder Rubber Nail Polish Bottle |
| `B01G6IJ66S` | 1 | 11.28 | `discoverable_pool` | low_quality_score, missing_attributes, missing_features, missing_price, status_low_quality | TIAMALL Makeup Brush Cleaner Brush Cleaning Mat Silicone Cleaning Pad Cosmetic B |
| `B0848VJ18X` | 1 | 11.17 | `discoverable_pool` | low_quality_score, missing_features, missing_price, status_low_quality | Native Deodorant - Natural Deodorant For Women and Men - 3 Pack - Aluminum Free, |

## Output Files

- JSON report: `reports/evaluation/coverage_gap/coverage_gap.json`
- Product rows: `reports/evaluation/coverage_gap/coverage_gap_products.jsonl`
- Priority candidates: `reports/evaluation/coverage_gap/priority_candidates.jsonl`
- Priority meta subset: `reports/evaluation/coverage_gap/priority_candidates_meta.jsonl`
- Attribute priority meta subset: `reports/evaluation/coverage_gap/attribute_priority_candidates_meta.jsonl`
- Chart: `reports/evaluation/coverage_gap/coverage_gap_by_reason.png`
