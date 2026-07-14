"""
Propose the SEED for a growable attr_type/value vocabulary, from two INDEPENDENT sources
that are only merged at the end:

  1. metadata branch (no free-text sample needed): scan product metadata's `details` dict
     keys/values deterministically (no LLM), then ask the LLM ONCE to turn those raw fields
     into attr_type/value entries directly, together with the raw-key -> attr_type mapping.
  2. sample branch (no metadata needed): sample product/review free text and ask the LLM
     ONCE to propose additional attr_type/value entries for facts not captured by any
     structured field.

The two branches do not depend on each other's output and can be run in either order (or in
parallel). They are combined by a 2-step merge:
  merge step 1 (exact, no LLM): union values for attr_types that used the identical name in
    both branches.
  merge step 2 (LLM cleanup): a further LLM pass over that draft that merges near-duplicate
    attr_types the two branches phrased differently, drops attr_types that turn out not to be
    meaningful product attributes, and tidies up names/descriptions/values — without inventing
    brand-new values that have no basis in the draft.

This runs once, after build_base_graph.py and before extract_product_attributes.py /
extract_review_mentions.py. Its output is a DRAFT for a human to review and edit before real
extraction starts.

attr_vocab.yaml is not a fixed/closed list — it's the starting point for a GROWABLE
database (see utils/attr_vocab.py's GrowableVocab). During real extraction, the LLM
is told to reuse an existing (attr_type, value) pair whenever one fits, and may add a
new one only when truly necessary; additions are written back into this same file as
extraction proceeds, so there's no separate post-hoc canonicalization step — the seed
proposed here just needs to be a reasonable starting point, not exhaustive.

Reads:
  meta_path            (config.yaml data.meta_path — product metadata, gzipped JSONL)
  nodes_products.csv    (optional, from build_base_graph.py — restricts sampling to
                          products actually selected into the current k-core graph)
  nodes_reviews.csv     (from build_base_graph.py)

Writes (under <output_dir>/attributes/):
  attr_vocab.yaml       {"attr_types": [{"name", "description", "values": [...]}, ...]}
                         Every attr_type gets a closed "values" list (size controlled by
                         --min-values-per-type/--max-values-per-type) — there is no "open,
                         free-text" attr_type produced by this script. Both attr_type
                         membership and value membership grow during real extraction via
                         GrowableVocab, starting from this seed.
  detail_key_map.yaml   {"Genre": "game_genre", "Item model number": null, ...}
                         (static — does not grow at runtime; produced entirely by the
                         metadata branch, independent of the sample branch)

Usage:
    python3 kg_build/propose_attr_vocab.py --sample-products 150 --sample-reviews 300 \
        --min-values-per-type 3 --max-values-per-type 12
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any

import yaml

from extract_product_attributes import IGNORED_DETAIL_KEYS
from utils.csv_io import read_jsonl_gz
from utils.llm_client import build_client, provider_from_config
from utils.llm_json import chat_json_call
from utils.text_utils import as_list, clean_text, normalize_attr_type, normalize_value

FORBIDDEN_NAMES = {"brand", "feature", "other", "misc", "features", "attribute", "attributes"}


def _normalize_entries(raw: list[Any], min_values: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Shared name/value normalization + dedup + min-values filter, used by both branches.
    Returns (entries, dropped_names)."""
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    dropped: list[str] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        name = normalize_attr_type(str(e.get("name", "")))
        if not name or name in FORBIDDEN_NAMES or name in seen:
            continue
        seen.add(name)
        raw_values = e.get("values") or []
        seen_values: set[str] = set()
        values: list[str] = []
        for v in raw_values:
            nv = normalize_value(str(v))
            if nv and nv not in seen_values:
                seen_values.add(nv)
                values.append(nv)
        if len(values) < min_values:
            dropped.append(name)
            continue
        entries.append({"name": name, "description": str(e.get("description", "")).strip(), "values": values})
    return entries, dropped


# ── metadata branch (independent of the sample branch) ─────────────────────────

def collect_detail_keys(
    meta_path: Path, allowed_ids: set[str] | None, sample_values_per_key: int = 5,
) -> list[dict[str, Any]]:
    """Deterministic scan of product metadata's `details` dict — no LLM call. Returns
    [{"key": raw_key, "count": n_products, "sample_values": [...]}, ...] sorted by count
    desc, excluding IGNORED_DETAIL_KEYS."""
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for row in read_jsonl_gz(meta_path):
        pid = clean_text(row.get("parent_asin"))
        if not pid:
            continue
        if allowed_ids is not None and pid not in allowed_ids:
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        for key, raw_val in details.items():
            if key in IGNORED_DETAIL_KEYS:
                continue
            counts[key] = counts.get(key, 0) + 1
            vals = samples.setdefault(key, [])
            sval = clean_text(str(raw_val))[:60]
            if sval and sval not in vals and len(vals) < sample_values_per_key:
                vals.append(sval)
    return [
        {"key": k, "count": c, "sample_values": samples.get(k, [])}
        for k, c in sorted(counts.items(), key=lambda x: -x[1])
    ]


METADATA_VOCAB_SYSTEM_PROMPT = """\
You are designing a CLOSED attribute taxonomy from a catalog's structured product metadata
fields, for a knowledge graph of {genre}.

Below is every raw metadata field name actually present in this catalog's `details` dict,
each with how many products have it and a few real example raw values.

For EACH raw field name, decide:
- it maps onto an attr_type — pick or invent a snake_case attr_type name (merge multiple raw
  field names into the SAME attr_type when they clearly represent the same concept, e.g.
  "Platform" and "Compatible Devices" might both become "platform"), with a short description
  and a closed "values" list grounded in the real example values you were given (normalized to
  snake_case), or
- null, if the field is not a meaningful product attribute (e.g. an internal ID, a
  shipping/package detail, a manufacturer code) or its values are too free-form/unique
  per-product to reduce to a small closed set

Return valid JSON only:
{{"attr_types": [{{"name": "snake_case_name", "description": "one short phrase", "values": ["snake_case_value", ...]}}, ...],
 "key_map": {{"raw_field_name": "snake_case_name_or_null", ...}}}}

Rules:
- snake_case for attr_type names and values
- Do NOT include: brand (it is a separate graph node), feature, other, misc (too generic)
- Every attr_type's "values" list must contain between {min_values} and {max_values} entries.
  Never propose an attr_type with an empty or near-empty values list — if a field can't
  support that many genuinely distinct values, map it to null instead.
- Every raw field name given to you must appear as a key in "key_map"
- Every non-null value in "key_map" must be a name present in "attr_types"
"""


def propose_vocab_from_metadata(
    client: Any, model: str, genre: str, detail_fields: list[dict[str, Any]],
    min_values: int, max_values: int, max_output_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, str | None], list[str]]:
    """Independent of the sample branch — uses only detail_fields. Returns
    (entries, key_map, dropped_names)."""
    if not detail_fields:
        return [], {}, []
    system = METADATA_VOCAB_SYSTEM_PROMPT.format(genre=genre, min_values=min_values, max_values=max_values)
    user = {"task": "Derive a closed attr_type taxonomy directly from these metadata fields.",
            "metadata_fields": detail_fields}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    parsed, _usage = chat_json_call(client, model, messages, max_output_tokens=max_output_tokens, retries=3)

    raw_entries = parsed.get("attr_types", []) if isinstance(parsed, dict) else []
    entries, dropped = _normalize_entries(raw_entries, min_values)
    vocab_names = {e["name"] for e in entries}

    raw_map = parsed.get("key_map", {}) if isinstance(parsed, dict) else {}
    valid_keys = {f["key"] for f in detail_fields}
    key_map: dict[str, str | None] = {}
    for k, v in raw_map.items():
        if k not in valid_keys:
            continue
        v_norm = normalize_attr_type(str(v)) if v else None
        key_map[k] = v_norm if v_norm in vocab_names else None
    # any raw key the LLM silently dropped from its response still needs an entry
    for f in detail_fields:
        key_map.setdefault(f["key"], None)
    return entries, key_map, dropped


# ── sample branch (independent of the metadata branch) ──────────────────────────

VOCAB_SYSTEM_PROMPT = """\
You are designing a compact, CLOSED attribute taxonomy for a product knowledge graph of {genre},
based on free-text product and review content. (Structured metadata fields are handled by a
separate process and will be merged in later — focus only on what the text itself reveals.)

Below you will be given a sample of real product records (title/features/description) and a
sample of real customer reviews from this catalog. Based on what you actually see in the
sample, propose a closed list of attr_type category names that would meaningfully classify
facts about these products — covering both what's stated in product descriptions AND what
reviewers mention. EVERY attr_type must also come with its own closed list of allowed values —
there is no "open, free-text" option. If a concept cannot naturally be reduced to a closed set
of recurring options, do not propose it as an attr_type at all.

Return valid JSON only:
{{"attr_types": [{{"name": "snake_case_name", "description": "one short phrase", "values": ["snake_case_value", ...]}}, ...]}}

Rules:
- snake_case for both names and values
- Each category must be broad enough to apply across MANY different products in this catalog
  (not a one-off fact about a single sampled item)
- Categories must be clearly distinct from each other in meaning
- Do NOT include: brand (it is a separate graph node), feature, other, misc (too generic)
- Return between {min_count} and {max_count} attr_types — prefer fewer, clearly-distinct
  categories over many overlapping ones
- Every attr_type's "values" list must contain between {min_values} and {max_values} entries.
  Never return an empty "values" list for any attr_type.
- Values must be genuinely distinct options, not near-duplicates of each other, and must
  realistically cover most products in this catalog for that attr_type
"""


def sample_products(meta_path: Path, allowed_ids: set[str] | None, n: int, seed: int) -> list[dict]:
    pool: list[dict] = []
    for row in read_jsonl_gz(meta_path):
        pid = clean_text(row.get("parent_asin"))
        if not pid:
            continue
        if allowed_ids is not None and pid not in allowed_ids:
            continue
        pool.append(row)
    rng = random.Random(seed)
    chosen = rng.sample(pool, min(n, len(pool))) if pool else []
    out = []
    for row in chosen:
        out.append({
            "title": clean_text(row.get("title"))[:300],
            "features": [clean_text(x)[:200] for x in as_list(row.get("features"))[:5] if clean_text(x)],
            "description": [clean_text(x)[:200] for x in as_list(row.get("description"))[:3] if clean_text(x)],
        })
    return out


def sample_reviews(reviews_csv: Path, n: int, seed: int, min_text_len: int = 60) -> list[str]:
    pool: list[str] = []
    if not reviews_csv.exists():
        return pool
    with reviews_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            text = clean_text(row.get("text", ""))
            if len(text) >= min_text_len:
                pool.append(text[:400])
    rng = random.Random(seed)
    return rng.sample(pool, min(n, len(pool))) if pool else []


def propose_vocab_from_samples(
    client: Any, model: str, genre: str, products: list[dict], reviews: list[str],
    min_count: int, max_count: int, min_values: int, max_values: int, max_output_tokens: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Independent of the metadata branch — uses only free-text samples. Returns
    (entries, dropped_names)."""
    system = VOCAB_SYSTEM_PROMPT.format(
        genre=genre, min_count=min_count, max_count=max_count, min_values=min_values, max_values=max_values,
    )
    user = {
        "task": "Propose a closed attr_type taxonomy from this free-text sample.",
        "sample_products": products,
        "sample_reviews": reviews,
    }
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    parsed, _usage = chat_json_call(client, model, messages, max_output_tokens=max_output_tokens, retries=3)
    raw_entries = parsed.get("attr_types", []) if isinstance(parsed, dict) else []
    return _normalize_entries(raw_entries, min_values)


# ── merge step 1 (exact, no LLM): union values where both branches used the same name ──

def merge_vocab_entries(
    metadata_entries: list[dict[str, Any]], sample_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Combine the two independently-proposed entry lists. Same attr_type name from both
    branches -> union of values (dedup, order preserved). Different names are kept as separate
    entries here — near-duplicates that the two branches happened to phrase differently are
    caught by the LLM cleanup pass below, not here. Returns (merged_entries,
    names_that_appeared_in_both)."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    merged_in_both: list[str] = []
    for group in (metadata_entries, sample_entries):
        for e in group:
            name = e["name"]
            if name not in merged:
                merged[name] = {"name": name, "description": e["description"], "values": list(e["values"])}
                order.append(name)
                continue
            existing = merged[name]
            before = len(existing["values"])
            existing["values"] = list(dict.fromkeys(existing["values"] + e["values"]))
            if not existing["description"]:
                existing["description"] = e["description"]
            if len(existing["values"]) != before or name not in merged_in_both:
                merged_in_both.append(name)
    return [merged[n] for n in order], sorted(set(merged_in_both))


# ── merge step 2 (LLM cleanup): dedupe near-duplicates, drop junk, fix wording ──────────

CLEANUP_SYSTEM_PROMPT = """\
You are cleaning up a DRAFT closed attribute taxonomy for a product knowledge graph of {genre}.
The draft was assembled from two independent sources (structured product metadata and
free-text product/review samples), so it may contain: attr_types that are not meaningful
product attributes, unclear or redundant names/descriptions, near-duplicate attr_types that
are really the same concept under different names or phrasing, and near-duplicate or
awkwardly-phrased values within an attr_type.

Below is the full draft list of attr_types, each with its description and current values.

Produce a CLEANED UP, FINAL version:
- Merge attr_types that clearly represent the same concept into one (pick the clearest
  snake_case name; keep the union of their values, deduped)
- Drop attr_types that are not meaningful, genre-relevant product attributes (too generic,
  an internal/administrative artifact, not something a user would search or filter by)
- Rewrite any name/description that is unclear, redundant, or mis-scoped
- Within an attr_type, drop near-duplicate/redundant values (e.g. "long lasting" vs
  "long_lasting") and fix awkward phrasing — do NOT invent brand-new values with no basis in
  what's already there; you are cleaning up existing values, not generating new ones
- Every attr_type must end up with between {min_values} and {max_values} distinct, clearly
  different values

Return valid JSON only:
{{"attr_types": [{{"name": "snake_case_name", "description": "one short phrase",
  "values": ["snake_case_value", ...], "merged_from": ["draft_name1", "draft_name2", ...]}}, ...]}}

"merged_from" must list every draft attr_type name (exactly as given to you) that was folded
into this final entry — include the entry's own original name if it was kept unchanged or just
renamed. Every draft name you decide to drop should simply not appear in any "merged_from" list.
"""


def cleanup_vocab_with_llm(
    client: Any, model: str, genre: str, entries: list[dict[str, Any]],
    min_values: int, max_values: int, max_output_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """LLM pass over the exact-merged draft: merges near-duplicate attr_types (different
    names/phrasing for the same concept), drops non-meaningful ones, and tidies up
    names/descriptions/values. Returns (final_entries, rename_map) where rename_map maps every
    draft name that survived (possibly renamed/merged) to its final name — a draft name absent
    from rename_map was dropped."""
    if not entries:
        return [], {}
    draft_names = {e["name"] for e in entries}
    system = CLEANUP_SYSTEM_PROMPT.format(genre=genre, min_values=min_values, max_values=max_values)
    user = {"task": "Clean up and merge this draft attr_type taxonomy.", "draft_attr_types": entries}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]
    parsed, _usage = chat_json_call(client, model, messages, max_output_tokens=max_output_tokens, retries=3)
    raw = parsed.get("attr_types", []) if isinstance(parsed, dict) else []

    final_entries: list[dict[str, Any]] = []
    rename_map: dict[str, str] = {}
    seen_names: set[str] = set()
    for e in raw:
        if not isinstance(e, dict):
            continue
        name = normalize_attr_type(str(e.get("name", "")))
        if not name or name in FORBIDDEN_NAMES or name in seen_names:
            continue
        seen_values: set[str] = set()
        values: list[str] = []
        for v in e.get("values") or []:
            nv = normalize_value(str(v))
            if nv and nv not in seen_values:
                seen_values.add(nv)
                values.append(nv)
        if len(values) < min_values:
            continue  # dropped: its merged_from names simply stay out of rename_map
        seen_names.add(name)
        final_entries.append({"name": name, "description": str(e.get("description", "")).strip(), "values": values})
        for src in e.get("merged_from") or [e.get("name", "")]:
            src_norm = normalize_attr_type(str(src))
            if src_norm in draft_names:
                rename_map[src_norm] = name
    return final_entries, rename_map


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Propose a closed attr_type vocabulary from metadata + a content sample.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--meta-path", type=Path)
    parser.add_argument("--reviews-csv", type=Path)
    parser.add_argument("--product-ids-file", type=Path, default=None,
                         help="CSV with a 'product_id' column (e.g. nodes_products.csv). "
                              "Restricts sampling to these products. Defaults to "
                              "<output_dir>/nodes_products.csv if present.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--provider", choices=["gemini", "groq", "deepseek", "openai", "ollama"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--sample-products", type=int, default=150)
    parser.add_argument("--sample-reviews", type=int, default=300)
    parser.add_argument("--min-count", type=int, default=30,
                         help="Minimum number of attr_types to propose from the free-text sample branch.")
    parser.add_argument("--max-count", type=int, default=60,
                         help="Maximum number of attr_types to propose from the free-text sample branch.")
    parser.add_argument("--min-values-per-type", type=int, default=3,
                         help="Minimum number of allowed values per attr_type, in EITHER branch. Entries "
                              "with fewer values (after dedup) than this are dropped, not kept as an "
                              "open/empty-values attr_type.")
    parser.add_argument("--max-values-per-type", type=int, default=12,
                         help="Maximum number of allowed values per attr_type (a target given to the LLM).")
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.min_values_per_type < 1:
        parser.error("--min-values-per-type must be >= 1 (empty-values attr_types are not supported)")
    if args.max_values_per_type < args.min_values_per_type:
        parser.error("--max-values-per-type must be >= --min-values-per-type")
    return args


def main() -> None:
    args = parse_args()

    cfg: dict = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    data_cfg = cfg.get("data", {})
    llm_cfg = cfg.get("llm", {})
    config_dir = args.config.resolve().parent
    out_dir = config_dir / data_cfg.get("output_dir", "kg_output/video_games")

    meta_path = args.meta_path or (config_dir / data_cfg.get("meta_path", "data/meta_Video_Games.jsonl.gz"))
    reviews_csv = args.reviews_csv or (out_dir / "nodes_reviews.csv")
    attrs_dir = args.output_dir or (out_dir / "attributes")
    attrs_dir.mkdir(parents=True, exist_ok=True)

    product_ids_file = args.product_ids_file or (out_dir / "nodes_products.csv")
    allowed_ids: set[str] | None = None
    if product_ids_file.exists():
        with product_ids_file.open(encoding="utf-8") as f:
            allowed_ids = {row["product_id"] for row in csv.DictReader(f)}
        print(f"Restricting to {len(allowed_ids):,} products from {product_ids_file.name}")

    cfg_provider, cfg_model, cfg_base_url = provider_from_config(llm_cfg)
    provider = args.provider or cfg_provider
    model_arg = args.model or cfg_model
    client, model = build_client(provider, model_arg, cfg_base_url)
    genre = cfg.get("genre", "products")

    # ── branch 1: metadata (independent) ────────────────────────────────────────
    print("Collecting distinct 'details' keys from product metadata (no LLM call)...")
    detail_fields = collect_detail_keys(meta_path, allowed_ids)
    print(f"  {len(detail_fields)} distinct keys (excluding {len(IGNORED_DETAIL_KEYS)} always-ignored keys)")

    print(f"[metadata branch] Proposing attr_types from metadata fields via {provider}/{model}...")
    metadata_entries, key_map, dropped_meta = propose_vocab_from_metadata(
        client, model, genre, detail_fields, args.min_values_per_type, args.max_values_per_type, args.max_output_tokens,
    )
    print(f"  proposed {len(metadata_entries)} attr_types from metadata "
          f"({sum(1 for v in key_map.values() if v)} of {len(key_map)} raw keys mapped)")
    if dropped_meta:
        print(f"  dropped {len(dropped_meta)} metadata attr_type(s) with too few values: {', '.join(dropped_meta)}")

    # ── branch 2: free-text sample (independent) ────────────────────────────────
    print(f"Sampling {args.sample_products} products and {args.sample_reviews} reviews (seed={args.seed})...")
    products = sample_products(meta_path, allowed_ids, args.sample_products, args.seed)
    reviews = sample_reviews(reviews_csv, args.sample_reviews, args.seed)
    print(f"  {len(products)} products, {len(reviews)} reviews sampled")

    print(f"[sample branch] Proposing attr_types from free-text samples via {provider}/{model}...")
    sample_entries, dropped_sample = propose_vocab_from_samples(
        client, model, genre, products, reviews,
        args.min_count, args.max_count, args.min_values_per_type, args.max_values_per_type,
        args.max_output_tokens,
    )
    print(f"  proposed {len(sample_entries)} attr_types from samples")
    if dropped_sample:
        print(f"  dropped {len(dropped_sample)} sample attr_type(s) with too few values: {', '.join(dropped_sample)}")

    # ── merge step 1 (exact, no LLM): union values on identical names ───────────
    draft_entries, merged_names = merge_vocab_entries(metadata_entries, sample_entries)
    print(f"Exact-merged into {len(draft_entries)} draft attr_types"
          + (f" ({len(merged_names)} appeared in both branches and were combined: {', '.join(merged_names)})"
             if merged_names else " (no exact-name overlap between the two branches)"))

    # ── merge step 2 (LLM cleanup): dedupe near-duplicates, drop junk, fix wording ──
    print(f"[cleanup] Deduping/cleaning up the draft taxonomy via {provider}/{model}...")
    vocab_entries, rename_map = cleanup_vocab_with_llm(
        client, model, genre, draft_entries, args.min_values_per_type, args.max_values_per_type, args.max_output_tokens,
    )
    dropped_in_cleanup = sorted({e["name"] for e in draft_entries} - set(rename_map))
    print(f"  cleaned up to {len(vocab_entries)} final attr_types (from {len(draft_entries)} draft)")
    if dropped_in_cleanup:
        print(f"  dropped {len(dropped_in_cleanup)} draft attr_type(s) as junk/non-meaningful: "
              f"{', '.join(dropped_in_cleanup)}")

    # detail_key_map.yaml must point at FINAL names — rewrite through rename_map (a draft
    # name the cleanup dropped entirely has no rename_map entry, so its keys become null)
    key_map = {k: (rename_map.get(v) if v else None) for k, v in key_map.items()}

    vocab_path = attrs_dir / "attr_vocab.yaml"
    vocab_path.write_text(
        yaml.safe_dump({"attr_types": vocab_entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"  wrote {vocab_path}")

    key_map_path = attrs_dir / "detail_key_map.yaml"
    key_map_path.write_text(
        yaml.safe_dump(key_map, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"  wrote {key_map_path} ({sum(1 for v in key_map.values() if v)} mapped, "
          f"{sum(1 for v in key_map.values() if not v)} dropped)")

    print(f"\nReview and edit {vocab_path.name} / {key_map_path.name} by hand before running "
          f"extract_product_attributes.py / extract_review_mentions.py.")


if __name__ == "__main__":
    main()
