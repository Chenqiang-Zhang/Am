from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any, Iterable


TEXT_WS_RE = re.compile(r"\s+")
SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|oz|ounce|ounces|ml|g|gram|grams|lb|lbs|count|ct|pcs|pack|packs|piece|pieces)\b",
    re.I,
)
PUNCT_RE = re.compile(r"[^a-z0-9]+")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return TEXT_WS_RE.sub(" ", str(value).replace("\x00", " ")).strip()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]], gzip_output: bool = True) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if gzip_output else open
    count = 0
    with opener(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def canonical_title(title: str) -> str:
    title = title.lower()
    title = SIZE_RE.sub(" ", title)
    title = re.sub(r"\b(?:new|bonus|refill|sample|travel size|value pack)\b", " ", title)
    title = PUNCT_RE.sub(" ", title)
    return TEXT_WS_RE.sub(" ", title).strip()


def price_value(row: dict[str, Any]) -> float | None:
    raw = row.get("price")
    if raw in (None, ""):
        return None
    try:
        price = float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return price if price > 0 else None


def amazon_url(row: dict[str, Any], allow_generated_url: bool) -> str:
    for key in ("amazon_url", "url", "product_url", "link"):
        value = clean_text(row.get(key))
        if value.startswith("http"):
            return value
    product_id = clean_text(row.get("parent_asin"))
    if allow_generated_url and product_id:
        return f"https://www.amazon.com/dp/{product_id}"
    return ""


def information_chars(row: dict[str, Any]) -> int:
    features = [clean_text(x) for x in as_list(row.get("features")) if clean_text(x)]
    descriptions = [clean_text(x) for x in as_list(row.get("description")) if clean_text(x)]
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    detail_values = [clean_text(value) for value in details.values() if clean_text(value)]
    return sum(len(x) for x in features[:8] + descriptions[:4] + detail_values[:12])


def reject_reasons(
    row: dict[str, Any],
    min_title_chars: int,
    min_info_chars: int,
    allow_generated_url: bool,
) -> list[str]:
    reasons: list[str] = []
    if not clean_text(row.get("parent_asin")):
        reasons.append("missing_product_id")
    if len(clean_text(row.get("title"))) < min_title_chars:
        reasons.append("missing_or_short_title")
    if price_value(row) is None:
        reasons.append("missing_or_invalid_price")
    if not amazon_url(row, allow_generated_url):
        reasons.append("missing_amazon_link")
    if information_chars(row) < min_info_chars:
        reasons.append("low_information_content")
    return reasons


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean product metadata before LLM attribute extraction and graph expansion."
    )
    parser.add_argument("--meta-path", type=Path, default=Path("data/meta_All_Beauty.jsonl.gz"))
    parser.add_argument("--review-path", type=Path, default=Path("data/All_Beauty.jsonl.gz"))
    parser.add_argument("--output-meta-path", type=Path, default=Path("kg_output/cleaned/meta_All_Beauty.clean.jsonl.gz"))
    parser.add_argument("--output-review-path", type=Path, default=Path("kg_output/cleaned/All_Beauty.clean.jsonl.gz"))
    parser.add_argument("--allowed-ids-path", type=Path, default=Path("kg_output/cleaned/clean_product_ids.txt"))
    parser.add_argument("--report-path", type=Path, default=Path("reports/dataset_quality/pre_llm_catalog_filter_report.json"))
    parser.add_argument("--min-title-chars", type=int, default=12)
    parser.add_argument("--min-info-chars", type=int, default=120)
    parser.add_argument("--max-products", type=int, default=-1, help="Use -1 for all eligible products.")
    parser.add_argument(
        "--require-source-amazon-url",
        action="store_true",
        help="Reject products unless the source metadata already has an http URL. By default a /dp/{ASIN} URL is generated.",
    )
    parser.add_argument("--skip-review-filter", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    allow_generated_url = not args.require_source_amazon_url
    counts = {
        "input_products": 0,
        "eligible_products_before_limit": 0,
        "selected_products": 0,
        "duplicate_title_rejected": 0,
        "input_reviews": 0,
        "selected_reviews": 0,
    }
    reject_counts: dict[str, int] = {}
    best_by_title: dict[str, tuple[int, dict[str, Any]]] = {}

    for row in iter_jsonl(args.meta_path):
        counts["input_products"] += 1
        reasons = reject_reasons(row, args.min_title_chars, args.min_info_chars, allow_generated_url)
        if reasons:
            for reason in reasons:
                reject_counts[reason] = reject_counts.get(reason, 0) + 1
            continue
        key = canonical_title(clean_text(row.get("title")))
        if not key:
            reject_counts["empty_canonical_title"] = reject_counts.get("empty_canonical_title", 0) + 1
            continue
        enriched = dict(row)
        enriched["price"] = price_value(row)
        enriched["amazon_url"] = amazon_url(row, allow_generated_url)
        score = information_chars(enriched) + int(float(enriched.get("rating_number") or 0) ** 0.5)
        previous = best_by_title.get(key)
        if previous is None or score > previous[0]:
            if previous is not None:
                counts["duplicate_title_rejected"] += 1
            best_by_title[key] = (score, enriched)
        else:
            counts["duplicate_title_rejected"] += 1

    products = [row for _, row in sorted(best_by_title.values(), key=lambda pair: pair[0], reverse=True)]
    counts["eligible_products_before_limit"] = len(products)
    if args.max_products >= 0:
        products = products[: args.max_products]
    counts["selected_products"] = write_jsonl(args.output_meta_path, products, gzip_output=True)

    allowed_ids = {clean_text(row.get("parent_asin")) for row in products if clean_text(row.get("parent_asin"))}
    args.allowed_ids_path.parent.mkdir(parents=True, exist_ok=True)
    args.allowed_ids_path.write_text("\n".join(sorted(allowed_ids)) + "\n", encoding="utf-8")

    if not args.skip_review_filter:
        def selected_reviews() -> Iterable[dict[str, Any]]:
            for review in iter_jsonl(args.review_path):
                counts["input_reviews"] += 1
                if clean_text(review.get("parent_asin")) in allowed_ids:
                    counts["selected_reviews"] += 1
                    yield review

        write_jsonl(args.output_review_path, selected_reviews(), gzip_output=True)

    report = {
        **counts,
        "reject_reasons": dict(sorted(reject_counts.items())),
        "output_meta_path": str(args.output_meta_path),
        "output_review_path": None if args.skip_review_filter else str(args.output_review_path),
        "allowed_ids_path": str(args.allowed_ids_path),
        "allow_generated_amazon_url": allow_generated_url,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
