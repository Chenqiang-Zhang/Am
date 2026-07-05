from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from pathlib import Path
from typing import Any, Iterable


SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|oz|ounce|ounces|ml|g|gram|grams|lb|lbs|count|ct|pcs|pack|packs|piece|pieces)\b",
    re.I,
)
QUANTITY_RE = re.compile(r"\b(?:pack of|set of|bundle|value pack|2-pack|3-pack|4-pack)\s*\d*\b", re.I)
PUNCT_RE = re.compile(r"[^a-z0-9]+")

USEFUL_DETAIL_KEYS = {
    "Brand",
    "Skin Type",
    "Hair Type",
    "Scent",
    "Item Form",
    "Product Benefits",
    "Special Feature",
    "Material Feature",
    "Active Ingredients",
    "Finish Type",
    "Color",
    "Size",
    "Material",
    "Specific Uses For Product",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split()).strip()


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def canonical_title(title: str) -> str:
    title = title.lower()
    title = SIZE_RE.sub(" ", title)
    title = QUANTITY_RE.sub(" ", title)
    title = re.sub(r"\b(?:new|bonus|refill|travel size|sample size)\b", " ", title)
    title = PUNCT_RE.sub(" ", title)
    return " ".join(title.split())


def load_existing_product_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".csv":
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    product_id = clean_text(row.get("product_id"))
                    if product_id:
                        ids.add(product_id)
        else:
            for row in iter_jsonl(path):
                product_id = clean_text(row.get("product_id"))
                if product_id:
                    ids.add(product_id)
    return ids


def quality(row: dict[str, Any], min_title_chars: int, min_info_chars: int, min_features: int, min_details: int) -> tuple[bool, list[str], int]:
    reasons: list[str] = []
    title = clean_text(row.get("title"))
    if not clean_text(row.get("parent_asin")):
        reasons.append("missing_product_id")
    if len(title) < min_title_chars:
        reasons.append("missing_or_short_title")

    features = [clean_text(x) for x in as_list(row.get("features")) if clean_text(x)]
    descriptions = [clean_text(x) for x in as_list(row.get("description")) if clean_text(x)]
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    useful_details = [k for k in USEFUL_DETAIL_KEYS if clean_text(details.get(k))]
    store = clean_text(row.get("store"))

    info_chars = sum(len(x) for x in features[:8]) + sum(len(x) for x in descriptions[:4])
    info_chars += sum(len(clean_text(details.get(k))) for k in useful_details)
    info_chars += len(store)

    if info_chars < min_info_chars:
        reasons.append("low_information_content")
    if len(features) < min_features and not descriptions:
        reasons.append("missing_feature_or_description")
    if len(useful_details) < min_details and not features and not descriptions:
        reasons.append("missing_useful_details")

    score = info_chars
    score += min(len(features), 8) * 80
    score += min(len(useful_details), 8) * 70
    score += 150 if store else 0
    try:
        score += int(float(row.get("rating_number") or 0) ** 0.5) * 10
    except (TypeError, ValueError):
        pass
    if row.get("price") not in (None, ""):
        score += 120

    return not reasons, reasons, score


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select clean, non-duplicate products for LLM attribute extraction.")
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--output-path", type=Path, default=Path("kg_output/attributes/candidates_for_llm.jsonl"))
    parser.add_argument("--report-path", type=Path, default=Path("kg_output/attributes/candidate_quality_report.json"))
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--min-title-chars", type=int, default=12)
    parser.add_argument("--min-info-chars", type=int, default=160)
    parser.add_argument("--min-features", type=int, default=1)
    parser.add_argument("--min-details", type=int, default=1)
    parser.add_argument(
        "--existing-products",
        type=Path,
        nargs="*",
        default=[
            Path("kg_output/all_beauty/rel_product_attribute.csv"),
            Path("kg_output/attributes/product_attributes_llm.jsonl"),
            Path("kg_output/attributes/product_attributes_deepseek_1000.jsonl"),
            Path("kg_output/attributes/product_attributes_llm_deepseek_1000.jsonl"),
            Path("kg_output/attributes/product_attributes_openai.jsonl"),
        ],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    existing_ids = load_existing_product_ids(args.existing_products)
    best_by_title: dict[str, tuple[int, dict[str, Any]]] = {}
    counts: dict[str, int] = {
        "input_rows": 0,
        "skipped_existing_attribute_product": 0,
        "skipped_duplicate_title": 0,
        "eligible_before_limit": 0,
    }
    reason_counts: dict[str, int] = {}

    for row in iter_jsonl(args.meta_path):
        counts["input_rows"] += 1
        product_id = clean_text(row.get("parent_asin"))
        if product_id in existing_ids:
            counts["skipped_existing_attribute_product"] += 1
            continue
        ok, reasons, score = quality(
            row,
            args.min_title_chars,
            args.min_info_chars,
            args.min_features,
            args.min_details,
        )
        if not ok:
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            continue
        key = canonical_title(clean_text(row.get("title")))
        if not key:
            reason_counts["empty_canonical_title"] = reason_counts.get("empty_canonical_title", 0) + 1
            continue
        previous = best_by_title.get(key)
        if previous is None or score > previous[0]:
            if previous is not None:
                counts["skipped_duplicate_title"] += 1
            best_by_title[key] = (score, row)
        else:
            counts["skipped_duplicate_title"] += 1

    candidates = [item for _, item in sorted(best_by_title.values(), key=lambda pair: pair[0], reverse=True)]
    counts["eligible_before_limit"] = len(candidates)
    if args.limit >= 0:
        candidates = candidates[: args.limit]

    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    with args.output_path.open("w", encoding="utf-8") as f:
        for row in candidates:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    report = {
        **counts,
        "selected": len(candidates),
        "existing_product_ids": len(existing_ids),
        "quality_reject_reasons": dict(sorted(reason_counts.items())),
        "output_path": str(args.output_path),
    }
    args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
