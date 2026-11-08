"""Whether a game meets a dialogue state's conditions, decided without the retrievers' queries.

The graph is dumped with one plain query and every condition is re-decided in Python, so a bug in a retriever's
Cypher shows up as a mismatch instead of being copied into the expected answer. The rules are the ones in
E1_PLAN.md: a tag counts through a HAS_TAG edge with no rank (genres, features) or a rank within TAG_MAX_RANK
(user tags), studios through DEVELOPED_BY, and a price condition never lets a game of unknown price through.
"""

from dataclasses import dataclass

from app.api.chat_state import ChatState, attribute_kind, tag_key
from utils.graph_reader import TAG_MAX_RANK, GraphReader

GAME_DUMP_QUERY = """
MATCH (g:Game)
RETURN g.appid AS appid, g.price AS price,
       [(g)-[ht:HAS_TAG]->(t:Tag) | {tag_id: t.tag_id, rank: ht.rank}] AS tags,
       [(g)-[:DEVELOPED_BY]->(s:Studio) | s.studio_id] AS developers
"""


@dataclass(frozen=True)
class GameFacts:
    price: float | None
    keys: frozenset[str]  # counted tag groups and developers, as attribute keys
    tag_edges: tuple[tuple[str, int | None], ...]  # every HAS_TAG edge as (tag_id, rank)
    developers: tuple[str, ...]


def counted_keys(tags: list[dict], developers: list[str]) -> frozenset[str]:
    keys = {tag_key(tag["tag_id"]) for tag in tags if tag["rank"] is None or tag["rank"] <= TAG_MAX_RANK}
    keys |= {"studio:" + studio for studio in developers if studio}
    return frozenset(keys)


def dump_games(reader: GraphReader) -> dict[str, GameFacts]:
    games = {}
    for row in reader.query(GAME_DUMP_QUERY):
        games[row["appid"]] = GameFacts(
            price=row["price"],
            keys=counted_keys(row["tags"], row["developers"]),
            tag_edges=tuple(sorted((tag["tag_id"], tag["rank"]) for tag in row["tags"])),
            developers=tuple(sorted(row["developers"])),
        )
    return games


@dataclass(frozen=True)
class ConstraintCheck:
    include: bool
    exclude: bool
    budget: bool

    @property
    def all(self) -> bool:
        return self.include and self.exclude and self.budget


def check(game: GameFacts, state: ChatState) -> ConstraintCheck:
    """The state's include, exclude and price conditions, each on its own. Shown games are not a condition here."""
    wanted = [key for key in state.include if attribute_kind(key) in ("tag", "studio")]
    unwanted = [key for key in state.exclude if attribute_kind(key) in ("tag", "studio")]
    budget = True
    if state.max_price is not None:
        budget = game.price is not None and game.price <= state.max_price
    if state.min_price is not None:
        budget = budget and game.price is not None and game.price > state.min_price
    return ConstraintCheck(
        include=all(key in game.keys for key in wanted),
        exclude=not any(key in game.keys for key in unwanted),
        budget=budget,
    )


def satisfies(appid: str, game: GameFacts, state: ChatState) -> bool:
    """What T1 must return for `state`: every condition met and not a game the state rules out."""
    return appid not in state.unavailable_appids() and check(game, state).all
