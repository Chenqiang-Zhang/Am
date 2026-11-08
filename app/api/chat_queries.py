"""Read-only graph templates of the conversational recommender (retriever G).

T1 (this file so far): hard conditions. A game passes only if it has every included attribute, none of the
excluded ones, a known price within the budget, and is not an anchor, a shown game or a rejected game.
T2 (anchor similarity) and T3 (same developer, co-play) will rank inside the same T1 filter.

Tags count only through HAS_TAG edges with no rank (genres and features) or a rank within TAG_MAX_RANK (user
tags), the same edges GAME_COLUMNS shows, the simulator's target attributes use and the questioner asks about.
Dialogue queries live here, not in utils/graph_reader.py, which only gains appended functions.
"""

import math
from dataclasses import dataclass

from utils.graph_reader import TAG_MAX_RANK, GraphReader

from .chat_state import Candidate, ChatEvidence, ChatState, attribute_kind, attribute_value, rank_candidates, tag_key

# A user tag at rank 1 counts 1.0 and one at rank TAG_MAX_RANK counts 0.1; genres and features have no rank.
UNRANKED_TAG_STRENGTH = 0.5
# Popularity only orders games that match the conditions equally well: log(1 + players) scaled to [0, 1)
# and weighted below one rank step of one tag.
POPULARITY_WEIGHT = 0.05


@dataclass(frozen=True)
class Vocabulary:
    """The IDs the graph has, to validate a state and to expand a tag group to its Tag.tag_ids."""

    tag_ids_by_key: dict[str, list[str]]
    studio_keys: set[str]
    appids: set[str]

    @property
    def attribute_keys(self) -> set[str]:
        return set(self.tag_ids_by_key) | self.studio_keys


def load_vocabulary(reader: GraphReader) -> Vocabulary:
    tag_ids_by_key: dict[str, list[str]] = {}
    for row in reader.query("MATCH (t:Tag) RETURN t.tag_id AS tag_id ORDER BY tag_id"):
        tag_ids_by_key.setdefault(tag_key(row["tag_id"]), []).append(row["tag_id"])
    studios = reader.query("MATCH (s:Studio) WHERE s.studio_id <> '' RETURN s.studio_id AS studio_id")
    games = reader.query("MATCH (g:Game) RETURN g.appid AS appid")
    return Vocabulary(
        tag_ids_by_key=tag_ids_by_key,
        studio_keys={"studio:" + row["studio_id"] for row in studios},
        appids={row["appid"] for row in games},
    )


def tag_strength(rank: int | None) -> float:
    if rank is None:
        return UNRANKED_TAG_STRENGTH
    return 1.0 - 0.9 * (min(max(rank, 1), TAG_MAX_RANK) - 1) / (TAG_MAX_RANK - 1)


T1_QUERY = """
MATCH (g:Game)
WHERE NOT g.appid IN $unavailable
  AND ($max_price IS NULL OR (g.price IS NOT NULL AND g.price <= $max_price))
  AND all(ids IN $include_tags WHERE EXISTS {
        MATCH (g)-[ht:HAS_TAG]->(t:Tag) WHERE t.tag_id IN ids AND (ht.rank IS NULL OR ht.rank <= $max_rank) })
  AND none(ids IN $exclude_tags WHERE EXISTS {
        MATCH (g)-[ht:HAS_TAG]->(t:Tag) WHERE t.tag_id IN ids AND (ht.rank IS NULL OR ht.rank <= $max_rank) })
  AND all(s IN $include_studios WHERE EXISTS { MATCH (g)-[:DEVELOPED_BY]->(:Studio {studio_id: s}) })
  AND none(s IN $exclude_studios WHERE EXISTS { MATCH (g)-[:DEVELOPED_BY]->(:Studio {studio_id: s}) })
WITH g, COUNT { (g)<-[:PLAYED]-(:Player) } AS players
RETURN g.appid AS appid, players,
       [(g)-[ht:HAS_TAG]->(t:Tag)
        WHERE t.tag_id IN $wanted_tag_ids AND (ht.rank IS NULL OR ht.rank <= $max_rank)
        | {tag_id: t.tag_id, rank: ht.rank}] AS matched
"""


def t1_parameters(state: ChatState, vocabulary: Vocabulary) -> dict:
    def tag_groups(keys: list[str]) -> list[list[str]]:
        return [vocabulary.tag_ids_by_key[key] for key in keys if attribute_kind(key) == "tag"]

    def studios(keys: list[str]) -> list[str]:
        return [attribute_value(key) for key in keys if attribute_kind(key) == "studio"]

    include_tags = tag_groups(state.include)
    return {
        "unavailable": state.unavailable_appids(),
        "max_price": state.max_price,
        "include_tags": include_tags,
        "exclude_tags": tag_groups(state.exclude),
        "include_studios": studios(state.include),
        "exclude_studios": studios(state.exclude),
        "wanted_tag_ids": [tag_id for ids in include_tags for tag_id in ids],
        "max_rank": TAG_MAX_RANK,
    }


def score_t1_rows(rows: list[dict], max_players: int) -> list[Candidate]:
    """Each included tag adds its strongest edge on the game; popularity breaks near-ties."""
    popularity_scale = math.log1p(max(max_players, 1))
    candidates = []
    for row in rows:
        best: dict[str, ChatEvidence] = {}
        for match in row["matched"]:
            key = tag_key(match["tag_id"])
            strength = tag_strength(match["rank"])
            if key not in best or strength > best[key].share:
                best[key] = ChatEvidence(path="tag", bridge_id=match["tag_id"], share=strength)
        evidence = sorted(best.values(), key=lambda item: (-item.share, item.bridge_id or ""))
        popularity = POPULARITY_WEIGHT * math.log1p(row["players"]) / popularity_scale
        candidates.append(
            Candidate(appid=row["appid"], score=sum(item.share for item in evidence) + popularity, evidence=evidence)
        )
    return rank_candidates(candidates)


class GraphRetriever:
    """Retriever G. Validates the state against the graph, then runs T1."""

    def __init__(self, reader: GraphReader, vocabulary: Vocabulary | None = None) -> None:
        self.reader = reader
        self.vocabulary = vocabulary or load_vocabulary(reader)
        self.max_players = reader.query(
            "MATCH (g:Game) RETURN coalesce(max(COUNT { (g)<-[:PLAYED]-(:Player) }), 0) AS n"
        )[0]["n"]

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]:
        state = state.restricted_to(self.vocabulary.attribute_keys, self.vocabulary.appids)
        rows = self.reader.query(T1_QUERY, **t1_parameters(state, self.vocabulary))
        return score_t1_rows(rows, self.max_players)[:k]
