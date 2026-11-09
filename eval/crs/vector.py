"""Retriever V: vector search over one English document per game (E1_PLAN.md).

The document states facts G works with: the tag groups T1 counts (no rank, or a rank within TAG_MAX_RANK)
that are genres or include a user tag, the developers and the price. Left out are titles, which G does not
search, and feature-only groups (Steam services such as achievements or cloud saves), which no question asks
about and which, on dev, pulled the vectors towards whatever games listed the fewest of them. A state's included
tags become "A game tagged X, Y." and its developers "Developer: Z.", embedded once; each excluded tag or
developer is embedded by its name alone and subtracted (Rocchio, weight EXCLUDE_WEIGHT). Anchors add the mean of
their document vectors (weight ANCHOR_WEIGHT) whatever the relation: V has no developer path and no co-play, so
"same developer as X" and "what X's players play" both become "like X". Price conditions are a
hard filter as in G, and a game of unknown price passes no price bound. The score is the cosine similarity,
which V-entropy uses as the questioner's weights. The wording was chosen on dev turn-1 lists (E1_PLAN.md).

Embeddings come from an Ollama server (EMBED_BASE_URL, model EMBED_MODEL). Document vectors are cached under
data/eval/vectors/, keyed by the model's digest and a hash of all documents.
"""

import hashlib
import json
import os
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.api.chat_state import Candidate, ChatState, attribute_kind, tag_key
from utils.graph_reader import TAG_MAX_RANK, GraphReader

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EXCLUDE_WEIGHT = 0.5
ANCHOR_WEIGHT = 0.375
EMBED_BATCH = 64
CACHE_ROOT = Path("data/eval/vectors")

Embed = Callable[[list[str]], np.ndarray]

VECTOR_DOC_QUERY = """
MATCH (g:Game)
RETURN g.appid AS appid, g.price AS price,
       [(g)-[ht:HAS_TAG]->(t:Tag) WHERE ht.rank IS NULL OR ht.rank <= $max_rank |
            {tag_id: t.tag_id, name: t.name, rank: ht.rank}] AS tags,
       [(g)-[:DEVELOPED_BY]->(s:Studio) WHERE s.studio_id <> '' | {studio_id: s.studio_id, name: s.name}] AS studios
"""

KIND_ORDER = ("genre", "feature", "usertag")
USER_TAG_GROUPS_QUERY = "MATCH (t:Tag) WHERE t.tag_id STARTS WITH 'usertag:' RETURN t.tag_id AS tag_id"


@dataclass(frozen=True)
class VectorCorpus:
    appids: tuple[str, ...]
    documents: tuple[str, ...]
    prices: tuple[float | None, ...]
    names: dict[str, str]  # attribute key -> the name written in documents and queries


def _price_text(price: float | None) -> str:
    if price is None:
        return "unknown"
    return "free" if price == 0 else f"${price:.2f}"


def _sentence(label: str, names: list[str]) -> str:
    return f"{label}: {', '.join(names)}." if names else ""


def write_document(tags: list[dict], studios: list[dict], price: float | None, user_tag_groups: set[str]) -> str:
    """Genres, then user tags by rank, then features that are also a user tag group; each group once."""
    genres = sorted({tag["name"] for tag in tags if tag["tag_id"].startswith("genre:")})
    seen = {tag_key(tag["tag_id"]) for tag in tags if tag["tag_id"].startswith("genre:")}
    listed: list[str] = []
    user_tags = sorted((t for t in tags if t["tag_id"].startswith("usertag:")), key=lambda t: (t["rank"] or 0, t["name"]))
    features = sorted((t for t in tags if t["tag_id"].startswith("feature:")), key=lambda t: t["name"])
    for tag in user_tags + features:
        key = tag_key(tag["tag_id"])
        if key not in seen and key in user_tag_groups:
            seen.add(key)
            listed.append(tag["name"])
    parts = [
        _sentence("Genres", genres),
        _sentence("Tags", listed),
        _sentence("Developer", sorted(studio["name"] for studio in studios)),
        f"Price: {_price_text(price)}.",
    ]
    return " ".join(part for part in parts if part)


def load_corpus(reader: GraphReader) -> VectorCorpus:
    rows = sorted(reader.query(VECTOR_DOC_QUERY, max_rank=TAG_MAX_RANK), key=lambda row: row["appid"])
    user_tag_groups = {tag_key(row["tag_id"]) for row in reader.query(USER_TAG_GROUPS_QUERY)}
    names: dict[str, tuple[int, str]] = {}  # a user tag's name over a feature's over a genre's
    for row in rows:
        for tag in row["tags"]:
            rank = -KIND_ORDER.index(tag["tag_id"].split(":", 1)[0])
            key = tag_key(tag["tag_id"])
            if key not in names or rank < names[key][0]:
                names[key] = (rank, tag["name"])
        for studio in row["studios"]:
            names.setdefault("studio:" + studio["studio_id"], (0, studio["name"]))
    return VectorCorpus(
        appids=tuple(row["appid"] for row in rows),
        documents=tuple(write_document(row["tags"], row["studios"], row["price"], user_tag_groups) for row in rows),
        prices=tuple(row["price"] for row in rows),
        names={key: name for key, (_, name) in names.items()},
    )


def query_text(keys: list[str], names: dict[str, str]) -> str:
    """Included tags and developers as one short sentence; price keys are left to the filter."""
    tags = [names[key] for key in keys if attribute_kind(key) == "tag" and key in names]
    studios = [names[key] for key in keys if attribute_kind(key) == "studio" and key in names]
    text = f"A game tagged {', '.join(tags)}." if tags else "A game."
    return text + (f" Developer: {', '.join(studios)}." if studios else "")


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class VectorRetriever:
    """Retriever V."""

    def __init__(self, corpus: VectorCorpus, document_vectors: np.ndarray, embed: Embed,
                 exclude_weight: float = EXCLUDE_WEIGHT, anchor_weight: float = ANCHOR_WEIGHT) -> None:
        self.corpus = corpus
        self.vectors = _normalize(np.asarray(document_vectors, dtype=np.float32))
        self.embed = embed
        self.exclude_weight = exclude_weight
        self.anchor_weight = anchor_weight
        self.index = {appid: i for i, appid in enumerate(corpus.appids)}
        self.prices = np.array([np.nan if p is None else p for p in corpus.prices], dtype=np.float64)
        self._cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def _text_vector(self, text: str) -> np.ndarray:
        with self._lock:
            cached = self._cache.get(text)
        if cached is None:
            cached = _normalize(np.asarray(self.embed([text]), dtype=np.float32))[0]
            with self._lock:
                self._cache[text] = cached
        return cached

    def query_vector(self, state: ChatState) -> np.ndarray:
        query = self._text_vector(query_text(state.include, self.corpus.names))
        anchors = [self.index[appid] for appid in state.anchors if appid in self.index]
        if anchors:
            query = query + self.anchor_weight * _normalize(self.vectors[anchors].mean(axis=0))
        excluded = [key for key in state.exclude if attribute_kind(key) in ("tag", "studio")
                    and key in self.corpus.names]
        if excluded:
            negative = np.mean([self._text_vector(self.corpus.names[key]) for key in excluded], axis=0)
            query = query - self.exclude_weight * negative
        return _normalize(query)

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]:
        allowed = np.ones(len(self.corpus.appids), dtype=bool)
        known = ~np.isnan(self.prices)
        if state.max_price is not None:
            allowed &= known & (np.nan_to_num(self.prices, nan=np.inf) <= state.max_price)
        if state.min_price is not None:
            allowed &= known & (np.nan_to_num(self.prices, nan=-np.inf) > state.min_price)
        for appid in state.unavailable_appids():
            if appid in self.index:
                allowed[self.index[appid]] = False
        positions = np.flatnonzero(allowed)
        if len(positions) == 0 or k <= 0:
            return []
        scores = self.vectors[positions] @ self.query_vector(state)
        order = np.argsort(-scores, kind="stable")[:k]  # positions follow appid order, so ties go by appid
        return [Candidate(appid=self.corpus.appids[positions[i]], score=float(scores[i])) for i in order]


# ---- the embedding server ----

def ollama_embed(texts: list[str], base_url: str = EMBED_BASE_URL, model: str = EMBED_MODEL) -> np.ndarray:
    body = json.dumps({"model": model, "input": texts}).encode()
    request = urllib.request.Request(base_url + "/api/embed", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return np.asarray(json.loads(response.read())["embeddings"], dtype=np.float32)


def model_identity(base_url: str = EMBED_BASE_URL, model: str = EMBED_MODEL) -> dict:
    with urllib.request.urlopen(base_url + "/api/tags", timeout=30) as response:
        models = json.loads(response.read())["models"]
    wanted = model if ":" in model else model + ":latest"
    for item in models:
        if item["name"] == wanted:
            return {"name": item["name"], "digest": item["digest"],
                    "quantization": item.get("details", {}).get("quantization_level")}
    raise SystemExit(f"embedding model {wanted} is not on {base_url}; run `ollama pull {model}`")


def document_vectors(corpus: VectorCorpus, embed: Embed, digest: str) -> tuple[np.ndarray, dict]:
    """All document vectors, from the cache when the model and every document are unchanged."""
    documents_sha = hashlib.sha256("\n".join(f"{a}\t{d}" for a, d in zip(corpus.appids, corpus.documents))
                                   .encode()).hexdigest()
    path = CACHE_ROOT / f"{EMBED_MODEL.replace(':', '_')}-{digest[:12]}-{documents_sha[:12]}.npy"
    info = {"documents": len(corpus.appids), "documents_sha256": documents_sha, "cache": str(path)}
    if path.exists():
        return np.load(path), {**info, "cached": True}
    vectors = np.concatenate([embed(list(corpus.documents[i:i + EMBED_BATCH]))
                              for i in range(0, len(corpus.documents), EMBED_BATCH)])
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, vectors)
    return vectors, {**info, "cached": False}
