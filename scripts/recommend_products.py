from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
    "all",
    "beauty",
    "product",
    "products",
}


@dataclass
class Product:
    product_id: str
    title: str = ""
    main_category: str = ""
    price: float | None = None
    average_rating: float | None = None
    rating_number: int | None = None
    feature_ids: set[str] = field(default_factory=set)
    attribute_ids: set[str] = field(default_factory=set)


@dataclass
class Interaction:
    product_id: str
    weight: float
    source: str
    rating: float | None = None


@dataclass
class Recommendation:
    product: Product
    score: float
    personalization_score: float
    query_score: float
    quality_score: float
    evidence_features: list[str]
    evidence_attributes: list[str]
    evidence_terms: list[str]
    matched_terms: list[str]


class Catalog:
    def __init__(self) -> None:
        self.products: dict[str, Product] = {}
        self.features: dict[str, str] = {}
        self.attributes: dict[str, str] = {}
        self.ratings_by_user: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self.feature_products: dict[str, set[str]] = defaultdict(set)
        self.attribute_products: dict[str, set[str]] = defaultdict(set)
        self.product_tokens: dict[str, Counter[str]] = {}

    @classmethod
    def load(cls, input_dir: Path) -> "Catalog":
        catalog = cls()
        catalog._load_products(input_dir / "nodes_products.csv")
        catalog._load_features(input_dir / "nodes_features.csv")
        catalog._load_product_features(input_dir / "rel_product_feature.csv")
        catalog._load_attributes(input_dir / "nodes_attributes.csv")
        catalog._load_product_attributes(input_dir / "rel_product_attribute.csv")
        catalog._load_ratings(input_dir / "rel_rated.csv")
        catalog._build_product_tokens()
        return catalog

    def _load_products(self, path: Path) -> None:
        for row in read_csv(path):
            product_id = clean_text(row.get("product_id"))
            if not product_id:
                continue
            self.products[product_id] = Product(
                product_id=product_id,
                title=clean_text(row.get("title")),
                main_category=clean_text(row.get("main_category")),
                price=parse_float(row.get("price")),
                average_rating=parse_float(row.get("average_rating")),
                rating_number=parse_int(row.get("rating_number")),
            )

    def _load_features(self, path: Path) -> None:
        if not path.exists():
            return
        for row in read_csv(path):
            feature_id = clean_text(row.get("feature_id"))
            text = clean_text(row.get("normalized_text")) or clean_text(row.get("text"))
            if feature_id and text:
                self.features[feature_id] = text

    def _load_product_features(self, path: Path) -> None:
        if not path.exists():
            return
        for row in read_csv(path):
            product_id = clean_text(row.get("product_id"))
            feature_id = clean_text(row.get("feature_id"))
            product = self.products.get(product_id)
            if product and feature_id:
                product.feature_ids.add(feature_id)
                self.feature_products[feature_id].add(product_id)

    def _load_attributes(self, path: Path) -> None:
        if not path.exists():
            return
        for row in read_csv(path):
            attribute_id = clean_text(row.get("attribute_id"))
            name = clean_text(row.get("name"))
            value = clean_text(row.get("value"))
            attr_type = clean_text(row.get("attribute_type"))
            label = ": ".join(part for part in (attr_type, name, value) if part)
            if attribute_id and label:
                self.attributes[attribute_id] = label

    def _load_product_attributes(self, path: Path) -> None:
        if not path.exists():
            return
        for row in read_csv(path):
            product_id = clean_text(row.get("product_id"))
            attribute_id = clean_text(row.get("attribute_id"))
            product = self.products.get(product_id)
            if product and attribute_id:
                product.attribute_ids.add(attribute_id)
                self.attribute_products[attribute_id].add(product_id)

    def _load_ratings(self, path: Path) -> None:
        if not path.exists():
            return
        for row in read_csv(path):
            user_id = clean_text(row.get("user_id"))
            product_id = clean_text(row.get("product_id"))
            rating = parse_float(row.get("rating"))
            if user_id and product_id and rating is not None:
                self.ratings_by_user[user_id].append((product_id, rating))

    def _build_product_tokens(self) -> None:
        for product in self.products.values():
            tokens = Counter(tokenize(product.title))
            tokens.update(tokenize(product.main_category))
            for feature_id in product.feature_ids:
                tokens.update(tokenize(self.features.get(feature_id, "")))
            for attribute_id in product.attribute_ids:
                tokens.update(tokenize(self.attributes.get(attribute_id, "")))
            self.product_tokens[product.product_id] = tokens

    def interactions_for_user(
        self,
        user_id: str | None,
        min_positive_rating: float,
        purchased_product_ids: list[str],
        viewed_product_ids: list[str],
    ) -> list[Interaction]:
        interactions: list[Interaction] = []
        if user_id:
            for product_id, rating in self.ratings_by_user.get(user_id, []):
                if rating >= min_positive_rating:
                    interactions.append(Interaction(product_id, rating_weight(rating), "rating", rating))
        for product_id in purchased_product_ids:
            if product_id in self.products:
                interactions.append(Interaction(product_id, 1.25, "purchase"))
        for product_id in viewed_product_ids:
            if product_id in self.products:
                interactions.append(Interaction(product_id, 0.45, "view"))
        return interactions


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required CSV file: {path}")
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split()).strip()


def parse_float(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    parsed = parse_float(value)
    return None if parsed is None else int(parsed)


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.lower()) if token not in STOPWORDS and len(token) > 1]


def rating_weight(rating: float) -> float:
    return max(0.1, (rating - 2.5) / 2.5)


def quality_score(product: Product) -> float:
    rating = product.average_rating or 0.0
    count = product.rating_number or 0
    rating_component = max(0.0, min(rating / 5.0, 1.0))
    confidence = min(math.log1p(count) / math.log1p(30_000), 1.0)
    return 0.65 * rating_component + 0.35 * confidence


def query_score(product_tokens: Counter[str], query_tokens: list[str]) -> tuple[float, list[str]]:
    if not query_tokens:
        return 0.0, []
    query_counts = Counter(query_tokens)
    matched: list[str] = []
    score = 0.0
    for token, q_count in query_counts.items():
        count = product_tokens.get(token, 0)
        if count:
            matched.append(token)
            score += min(count, 3) * q_count
    normalized = score / max(len(query_counts), 1)
    return min(normalized / 3.0, 1.0), matched


def candidate_product_ids(catalog: Catalog, interactions: list[Interaction], query_tokens: list[str]) -> set[str]:
    candidates: set[str] = set()
    history_tokens: set[str] = set()
    for interaction in interactions:
        product = catalog.products.get(interaction.product_id)
        if not product:
            continue
        history_tokens.update(catalog.product_tokens.get(product.product_id, Counter()))
        for feature_id in product.feature_ids:
            candidates.update(catalog.feature_products.get(feature_id, set()))
        for attribute_id in product.attribute_ids:
            candidates.update(catalog.attribute_products.get(attribute_id, set()))

    text_seed_tokens = set(query_tokens).union(history_tokens)
    if text_seed_tokens:
        for product_id, tokens in catalog.product_tokens.items():
            if text_seed_tokens.intersection(tokens):
                candidates.add(product_id)

    if not candidates:
        candidates.update(catalog.products)
    return candidates


def personalization_score(
    catalog: Catalog,
    candidate: Product,
    interactions: list[Interaction],
    max_evidence: int,
) -> tuple[float, list[str], list[str], list[str]]:
    if not interactions:
        return 0.0, [], [], []

    feature_hits: Counter[str] = Counter()
    attribute_hits: Counter[str] = Counter()
    term_hits: Counter[str] = Counter()
    raw_score = 0.0
    candidate_tokens = catalog.product_tokens.get(candidate.product_id, Counter())

    for interaction in interactions:
        source = catalog.products.get(interaction.product_id)
        if not source:
            continue
        shared_features = candidate.feature_ids.intersection(source.feature_ids)
        shared_attributes = candidate.attribute_ids.intersection(source.attribute_ids)
        if shared_features:
            raw_score += interaction.weight * len(shared_features)
            for feature_id in shared_features:
                feature_hits[feature_id] += 1
        if shared_attributes:
            raw_score += interaction.weight * 1.4 * len(shared_attributes)
            for attribute_id in shared_attributes:
                attribute_hits[attribute_id] += 1

        source_tokens = catalog.product_tokens.get(source.product_id, Counter())
        shared_terms = set(candidate_tokens).intersection(source_tokens)
        if shared_terms:
            useful_terms = sorted(shared_terms, key=lambda term: candidate_tokens[term] + source_tokens[term], reverse=True)[:8]
            raw_score += interaction.weight * 0.22 * len(useful_terms)
            for term in useful_terms:
                term_hits[term] += 1

    norm = math.log1p(raw_score) / math.log1p(25)
    evidence_features = [
        catalog.features[feature_id]
        for feature_id, _count in feature_hits.most_common(max_evidence)
        if feature_id in catalog.features
    ]
    evidence_attributes = [
        catalog.attributes[attribute_id]
        for attribute_id, _count in attribute_hits.most_common(max_evidence)
        if attribute_id in catalog.attributes
    ]
    evidence_terms = [term for term, _count in term_hits.most_common(max_evidence)]
    return min(norm, 1.0), evidence_features, evidence_attributes, evidence_terms


def recommend(
    catalog: Catalog,
    query: str,
    interactions: list[Interaction],
    top_k: int,
    exclude_history: bool,
    weights: tuple[float, float, float],
) -> list[Recommendation]:
    query_tokens = tokenize(query)
    history_ids = {interaction.product_id for interaction in interactions}
    candidates = candidate_product_ids(catalog, interactions, query_tokens)

    results: list[Recommendation] = []
    for product_id in candidates:
        if exclude_history and product_id in history_ids:
            continue
        product = catalog.products.get(product_id)
        if not product:
            continue
        p_score, evidence_features, evidence_attributes, evidence_terms = personalization_score(catalog, product, interactions, 5)
        q_score, matched_terms = query_score(catalog.product_tokens.get(product_id, Counter()), query_tokens)
        qual_score = quality_score(product)
        total_score = weights[0] * p_score + weights[1] * q_score + weights[2] * qual_score
        if total_score <= 0:
            continue
        results.append(
            Recommendation(
                product=product,
                score=total_score,
                personalization_score=p_score,
                query_score=q_score,
                quality_score=qual_score,
                evidence_features=evidence_features,
                evidence_attributes=evidence_attributes,
                evidence_terms=evidence_terms,
                matched_terms=matched_terms,
            )
        )

    results.sort(
        key=lambda item: (
            item.score,
            item.query_score,
            item.personalization_score,
            item.quality_score,
            item.product.rating_number or 0,
        ),
        reverse=True,
    )
    return results[:top_k]


def result_to_dict(result: Recommendation) -> dict[str, Any]:
    product = result.product
    return {
        "product_id": product.product_id,
        "title": product.title,
        "score": round(result.score, 4),
        "score_parts": {
            "personalization": round(result.personalization_score, 4),
            "query": round(result.query_score, 4),
            "quality": round(result.quality_score, 4),
        },
        "average_rating": product.average_rating,
        "rating_number": product.rating_number,
        "price": product.price,
        "matched_terms": result.matched_terms,
        "evidence_terms": result.evidence_terms,
        "evidence_features": result.evidence_features[:5],
        "evidence_attributes": result.evidence_attributes[:5],
    }


def print_text_results(results: list[Recommendation]) -> None:
    for idx, result in enumerate(results, 1):
        product = result.product
        print(f"{idx}. {product.title or product.product_id}")
        print(f"   product_id={product.product_id} score={result.score:.4f}")
        print(
            "   parts="
            f"personalization:{result.personalization_score:.3f} "
            f"query:{result.query_score:.3f} "
            f"quality:{result.quality_score:.3f}"
        )
        if product.average_rating is not None:
            print(f"   rating={product.average_rating} count={product.rating_number or 0} price={product.price}")
        if result.matched_terms:
            print(f"   matched_terms={', '.join(result.matched_terms[:8])}")
        if result.evidence_terms:
            print(f"   personalization_terms={', '.join(result.evidence_terms[:8])}")
        if result.evidence_attributes:
            print(f"   evidence_attributes={'; '.join(result.evidence_attributes[:3])}")
        if result.evidence_features:
            print(f"   evidence_features={'; '.join(result.evidence_features[:3])}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank product recommendations from KG CSV files.")
    parser.add_argument("--input-dir", type=Path, default=Path("kg_output/all_beauty_aura_small"))
    parser.add_argument("--user-id", default=None, help="Use this user's positive rating history as personalization signal.")
    parser.add_argument("--query", default="", help="Natural-language product need or search query.")
    parser.add_argument("--purchased-product-id", action="append", default=[], help="Extra purchased product id. Can be repeated.")
    parser.add_argument("--viewed-product-id", action="append", default=[], help="Extra viewed product id. Can be repeated.")
    parser.add_argument("--min-positive-rating", type=float, default=4.0)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--personalization-weight", type=float, default=0.55)
    parser.add_argument("--query-weight", type=float, default=0.35)
    parser.add_argument("--quality-weight", type=float, default=0.10)
    parser.add_argument("--include-history", action="store_true", help="Allow already-interacted products in results.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog = Catalog.load(args.input_dir)
    interactions = catalog.interactions_for_user(
        args.user_id,
        args.min_positive_rating,
        args.purchased_product_id,
        args.viewed_product_id,
    )
    results = recommend(
        catalog=catalog,
        query=args.query,
        interactions=interactions,
        top_k=args.top_k,
        exclude_history=not args.include_history,
        weights=(args.personalization_weight, args.query_weight, args.quality_weight),
    )

    if args.json:
        payload = {
            "input_dir": str(args.input_dir),
            "user_id": args.user_id,
            "query": args.query,
            "history_count": len(interactions),
            "results": [result_to_dict(result) for result in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"history_count={len(interactions)} results={len(results)}")
        print_text_results(results)


if __name__ == "__main__":
    main()
