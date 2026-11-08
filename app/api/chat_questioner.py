"""Which attribute to ask about next (questioners G and G-static).

Both questioners ask from the same whitelist and stop by the same rule; only the choice differs:

- EntropyQuestioner (G): the attribute whose yes/no answer splits the candidate pool most evenly, by binary
  entropy weighted by the retriever's scores (the rule-based maximum-entropy policy of SCPR).
- FixedOrderQuestioner (G-static): genres, then features, then price bands, then frequent tags, then studios,
  skipping attributes every candidate or no candidate has.

Whitelist: the QUESTION_TAG_COUNT most frequent tag groups that include a user tag (counting the same HAS_TAG
edges as T1, so a yes or no maps straight to a T1 condition), developers with at least MIN_STUDIO_GAMES games,
and PRICE_BANDS. The pool a questioner sees is the retriever's top POOL_SIZE games after the shown list, weighted
by their scores. Stop: the pool has at most STOP_POOL_SIZE games, the state is at MAX_TURNS, or (G only) no
attribute reaches MIN_ENTROPY.
"""

import math
from dataclasses import dataclass

from utils.graph_reader import GraphReader

from .chat_queries import GameAttributes, load_game_attributes
from .chat_state import Candidate, ChatState, attribute_kind, attribute_value

QUESTION_TAG_COUNT = 100
MIN_STUDIO_GAMES = 5
PRICE_BANDS = (20.0, 10.0, 5.0, 0.0)
POOL_SIZE = 1000
STOP_POOL_SIZE = 10
MAX_TURNS = 5
MIN_ENTROPY = 0.3


def price_key(band: float) -> str:
    return f"price:{band:g}"


@dataclass(frozen=True)
class QuestionCatalog:
    """The whitelist in the fixed order of G-static, and every game's attributes to test it against."""

    keys: tuple[str, ...]
    games: dict[str, GameAttributes]

    def has(self, appid: str, key: str) -> bool:
        game = self.games.get(appid)
        if game is None:
            return False
        kind = attribute_kind(key)
        if kind == "tag":
            return key in game.tag_keys
        if kind == "studio":
            return key in game.studio_keys
        return game.price is not None and game.price <= float(attribute_value(key))


def build_catalog(games: dict[str, GameAttributes], tag_kinds: dict[str, set[str]]) -> QuestionCatalog:
    """`tag_kinds` maps a tag group key to the kinds of its tags ("genre", "feature", "usertag")."""
    tag_counts: dict[str, int] = {}
    studio_counts: dict[str, int] = {}
    for game in games.values():
        for key in game.tag_keys:
            tag_counts[key] = tag_counts.get(key, 0) + 1
        for key in game.studio_keys:
            studio_counts[key] = studio_counts.get(key, 0) + 1

    def by_frequency(counts: dict[str, int]) -> list[str]:
        return sorted(counts, key=lambda key: (-counts[key], key))

    tags = [key for key in by_frequency(tag_counts) if "usertag" in tag_kinds.get(key, set())][:QUESTION_TAG_COUNT]
    genres = [key for key in tags if "genre" in tag_kinds[key]]
    features = [key for key in tags if "feature" in tag_kinds[key] and key not in genres]
    others = [key for key in tags if key not in genres and key not in features]
    studios = [key for key in by_frequency(studio_counts) if studio_counts[key] >= MIN_STUDIO_GAMES]
    prices = [price_key(band) for band in PRICE_BANDS]
    return QuestionCatalog(keys=tuple(genres + features + prices + others + studios), games=games)


def load_catalog(reader: GraphReader) -> QuestionCatalog:
    tag_kinds: dict[str, set[str]] = {}
    for row in reader.query("MATCH (t:Tag) RETURN t.tag_id AS tag_id"):
        kind, name = row["tag_id"].split(":", 1)
        tag_kinds.setdefault("tag:" + name, set()).add(kind)
    return build_catalog(load_game_attributes(reader), tag_kinds)


def binary_entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)


def _askable(state: ChatState, catalog: QuestionCatalog) -> list[str]:
    done = set(state.include) | set(state.exclude) | set(state.asked)
    keys = [key for key in catalog.keys if key not in done]
    def narrows(key: str) -> bool:
        # a band at or above the budget, or at or below the floor, cannot narrow the pool any further
        if attribute_kind(key) != "price":
            return True
        band = float(attribute_value(key))
        below_budget = state.max_price is None or band < state.max_price
        above_floor = state.min_price is None or band > state.min_price
        return below_budget and above_floor

    return [key for key in keys if narrows(key)]


def _should_stop(state: ChatState, candidates: list[Candidate]) -> bool:
    return len(candidates) <= STOP_POOL_SIZE or state.turn >= MAX_TURNS


def _weights(candidates: list[Candidate]) -> list[float]:
    scores = [max(candidate.score, 0.0) for candidate in candidates]
    total = sum(scores)
    if total <= 0:
        return [1.0 / len(candidates)] * len(candidates)
    return [score / total for score in scores]


def entropy_table(state: ChatState, candidates: list[Candidate], catalog: QuestionCatalog) -> dict[str, float]:
    """Weighted binary entropy of every askable attribute over the pool, highest first, ties by key."""
    if not candidates:
        return {}
    weights = _weights(candidates)
    table = {}
    for key in _askable(state, catalog):
        p = sum(weight for candidate, weight in zip(candidates, weights) if catalog.has(candidate.appid, key))
        table[key] = binary_entropy(p)
    return dict(sorted(table.items(), key=lambda item: (-item[1], item[0])))


class EntropyQuestioner:
    """Questioner G."""

    def __init__(self, catalog: QuestionCatalog) -> None:
        self.catalog = catalog

    def choose(self, state: ChatState, candidates: list[Candidate]) -> str | None:
        if _should_stop(state, candidates):
            return None
        table = entropy_table(state, candidates, self.catalog)
        best = next(iter(table.items()), None)
        if best is None or best[1] < MIN_ENTROPY:
            return None
        return best[0]


class FixedOrderQuestioner:
    """Questioner G-static: the first askable attribute in the catalog order that splits the pool at all."""

    def __init__(self, catalog: QuestionCatalog) -> None:
        self.catalog = catalog

    def choose(self, state: ChatState, candidates: list[Candidate]) -> str | None:
        if _should_stop(state, candidates):
            return None
        for key in _askable(state, self.catalog):
            holders = sum(1 for candidate in candidates if self.catalog.has(candidate.appid, key))
            if 0 < holders < len(candidates):
                return key
        return None
