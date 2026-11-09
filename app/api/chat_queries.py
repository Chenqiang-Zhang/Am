"""Read-only graph templates of the conversational recommender (retriever G).

T1: hard conditions. A game passes only if it has every included attribute, none of the excluded ones, a known
price within max_price and above min_price, and is not an anchor, a shown game or a rejected game.
T3: multi-hop relations to the anchors, inside the same T1 filter.
  - same_developer: anchor -DEVELOPED_BY-> studio <-DEVELOPED_BY- game. Only games sharing a developer pass.
  - co_played: players who played an anchor for at least CO_PLAY_MIN_HOURS, and the other games they played,
    ranked by lift with a pseudo-count so that neither popular games nor tiny ones dominate.
T2: anchor similarity (relation "similar"), inside the same T1 filter. Each tag group and developer a game
shares with an anchor adds its IDF over all games, so a rare shared tag or a shared developer counts more
than a common one.

Tags count only through HAS_TAG edges with no rank (genres and features) or a rank within TAG_MAX_RANK (user
tags), the same edges GAME_COLUMNS shows, the simulator's target attributes use and the questioner asks about.
Popularity and co-play leave out one player's PLAYED edges when the retriever is built for an evaluated player
(`excluding_player`), so a hidden play never counts toward its own target.
Dialogue queries live here, not in utils/graph_reader.py, which only gains appended functions.
"""

import copy
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

from utils.graph_reader import TAG_MAX_RANK, GraphReader

from .chat_state import Candidate, ChatEvidence, ChatState, attribute_kind, attribute_value, rank_candidates, tag_key

# A user tag at rank 1 counts 1.0 and one at rank TAG_MAX_RANK counts 0.1; genres and features have no rank.
UNRANKED_TAG_STRENGTH = 0.5
# Popularity only orders games that match the conditions equally well: log(1 + players) scaled to [0, 1)
# and weighted below one rank step of one tag.
POPULARITY_WEIGHT = 0.05
# T3 same_developer: each developer shared with an anchor counts as much as one rank-1 tag.
STUDIO_STRENGTH = 1.0
# T3 co_played: an anchor's fans are players with at least this many hours on it.
CO_PLAY_MIN_HOURS = 2.0
# Lift = (co / fans) / ((players + CO_PLAY_PRIOR) / all players). The pseudo-count keeps games played by a few
# fans only from topping the list. Chosen by reading the lists for Portal 2, Counter-Strike: GO and Terraria
# (no evaluation targets): plain lift put games with 5 players first, co-play count put Garry's Mod first.
CO_PLAY_PRIOR = 50
# Co-play counts depend only on the anchors and the left-out player, not on the rest of the state, so a
# retriever keeps the last few and later turns of a dialogue skip the aggregation.
CO_PLAY_CACHE_SIZE = 16


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


T1_MATCH = """
MATCH (g:Game)
WHERE NOT g.appid IN $unavailable
  AND ($max_price IS NULL OR (g.price IS NOT NULL AND g.price <= $max_price))
  AND ($min_price IS NULL OR (g.price IS NOT NULL AND g.price > $min_price))
  AND all(ids IN $include_tags WHERE EXISTS {
        MATCH (g)-[ht:HAS_TAG]->(t:Tag) WHERE t.tag_id IN ids AND (ht.rank IS NULL OR ht.rank <= $max_rank) })
  AND none(ids IN $exclude_tags WHERE EXISTS {
        MATCH (g)-[ht:HAS_TAG]->(t:Tag) WHERE t.tag_id IN ids AND (ht.rank IS NULL OR ht.rank <= $max_rank) })
  AND all(s IN $include_studios WHERE EXISTS { MATCH (g)-[:DEVELOPED_BY]->(:Studio {studio_id: s}) })
  AND none(s IN $exclude_studios WHERE EXISTS { MATCH (g)-[:DEVELOPED_BY]->(:Studio {studio_id: s}) })
"""
T1_RETURN = """
WITH g, COUNT { (g)<-[:PLAYED]-(p:Player) WHERE $exclude_player IS NULL OR p.player_id <> $exclude_player } AS players
RETURN g.appid AS appid, players,
       [(g)-[ht:HAS_TAG]->(t:Tag)
        WHERE t.tag_id IN $wanted_tag_ids AND (ht.rank IS NULL OR ht.rank <= $max_rank)
        | {tag_id: t.tag_id, rank: ht.rank}] AS matched
"""
T1_QUERY = T1_MATCH + T1_RETURN

# T3 same_developer: T1 plus a developer shared with an anchor. Empty studio_ids are not a developer.
T3_STUDIO_QUERY = T1_MATCH + """
  AND EXISTS { MATCH (g)-[:DEVELOPED_BY]->(s:Studio)<-[:DEVELOPED_BY]-(a:Game)
               WHERE a.appid IN $anchors AND s.studio_id <> '' }
""" + T1_RETURN.rstrip() + """,
       [(g)-[:DEVELOPED_BY]->(s:Studio)<-[:DEVELOPED_BY]-(a:Game)
        WHERE a.appid IN $anchors AND s.studio_id <> '' | {studio_id: s.studio_id, anchor: a.appid}] AS studios
"""

# T3 co_played: for each anchor, its fans and how many of them played each other game. Run next to T1_QUERY,
# which supplies the filter and each game's players; the evaluated player is never a fan.
CO_PLAY_QUERY = """
MATCH (a:Game)<-[e:PLAYED]-(p:Player)
WHERE a.appid IN $anchors AND e.hours >= $min_hours
  AND ($exclude_player IS NULL OR p.player_id <> $exclude_player)
WITH a, collect(p) AS fans
UNWIND fans AS p
MATCH (p)-[:PLAYED]->(c:Game)
WHERE NOT c.appid IN $anchors
RETURN a.appid AS anchor, size(fans) AS fans, c.appid AS appid, count(p) AS co
"""
PLAYERS_QUERY = """
MATCH (p:Player)
WHERE EXISTS { (p)-[:PLAYED]->(:Game) } AND ($exclude_player IS NULL OR p.player_id <> $exclude_player)
RETURN count(p) AS n
"""


def t1_parameters(state: ChatState, vocabulary: Vocabulary, exclude_player: str | None = None) -> dict:
    def tag_groups(keys: list[str]) -> list[list[str]]:
        return [vocabulary.tag_ids_by_key[key] for key in keys if attribute_kind(key) == "tag"]

    def studios(keys: list[str]) -> list[str]:
        return [attribute_value(key) for key in keys if attribute_kind(key) == "studio"]

    include_tags = tag_groups(state.include)
    return {
        "unavailable": state.unavailable_appids(),
        "max_price": state.max_price,
        "min_price": state.min_price,
        "exclude_player": exclude_player,
        "include_tags": include_tags,
        "exclude_tags": tag_groups(state.exclude),
        "include_studios": studios(state.include),
        "exclude_studios": studios(state.exclude),
        "wanted_tag_ids": [tag_id for ids in include_tags for tag_id in ids],
        "max_rank": TAG_MAX_RANK,
    }


def t1_score(row: dict, popularity_scale: float) -> tuple[float, list[ChatEvidence]]:
    """Each included tag adds its strongest edge on the game; popularity breaks near-ties."""
    best: dict[str, ChatEvidence] = {}
    for match in row["matched"]:
        key = tag_key(match["tag_id"])
        strength = tag_strength(match["rank"])
        if key not in best or strength > best[key].share:
            best[key] = ChatEvidence(path="tag", bridge_id=match["tag_id"], share=strength)
    evidence = sorted(best.values(), key=lambda item: (-item.share, item.bridge_id or ""))
    popularity = POPULARITY_WEIGHT * math.log1p(row["players"]) / popularity_scale
    return sum(item.share for item in evidence) + popularity, evidence


def popularity_scale(max_players: int) -> float:
    return math.log1p(max(max_players, 1))


def score_t1_rows(rows: list[dict], max_players: int) -> list[Candidate]:
    scale = popularity_scale(max_players)
    candidates = []
    for row in rows:
        score, evidence = t1_score(row, scale)
        candidates.append(Candidate(appid=row["appid"], score=score, evidence=evidence))
    return rank_candidates(candidates)


def score_studio_rows(rows: list[dict], max_players: int) -> list[Candidate]:
    """T1's score plus STUDIO_STRENGTH for each developer the game shares with an anchor."""
    scale = popularity_scale(max_players)
    candidates = []
    for row in rows:
        score, evidence = t1_score(row, scale)
        anchors_by_studio: dict[str, str] = {}
        for link in sorted(row["studios"], key=lambda link: (link["studio_id"], link["anchor"])):
            anchors_by_studio.setdefault(link["studio_id"], link["anchor"])
        studios = [
            ChatEvidence(path="studio", bridge_id=studio_id, anchor_appid=anchor, share=STUDIO_STRENGTH)
            for studio_id, anchor in anchors_by_studio.items()
        ]
        candidates.append(Candidate(
            appid=row["appid"], score=score + STUDIO_STRENGTH * len(studios), evidence=studios + evidence,
        ))
    return rank_candidates(candidates)


def co_play_lift(co: int, fans: int, players: int, all_players: int) -> float:
    return (co / fans) / ((players + CO_PLAY_PRIOR) / max(all_players, 1))


def score_co_play_rows(rows: list[dict], co_rows: list[dict], max_players: int, all_players: int) -> list[Candidate]:
    """Games that pass T1 and that an anchor's fans played: T1's score plus the log of the best anchor's lift.

    A lift of e (about 2.7) adds as much as one rank-1 tag. Games no fan played do not pass.
    """
    scale = popularity_scale(max_players)
    t1_rows = {row["appid"]: row for row in rows}
    links: dict[str, list[ChatEvidence]] = {}
    lifts: dict[str, float] = {}
    for co_row in co_rows:
        row = t1_rows.get(co_row["appid"])
        if row is None or co_row["co"] <= 0:
            continue
        lift = co_play_lift(co_row["co"], co_row["fans"], row["players"], all_players)
        links.setdefault(row["appid"], []).append(ChatEvidence(
            path="co_play", anchor_appid=co_row["anchor"], count=co_row["co"], share=co_row["co"] / co_row["fans"],
        ))
        lifts[row["appid"]] = max(lift, lifts.get(row["appid"], 0.0))
    candidates = []
    for appid, evidence in links.items():
        score, tags = t1_score(t1_rows[appid], scale)
        evidence.sort(key=lambda item: (-(item.count or 0), item.anchor_appid or ""))
        candidates.append(Candidate(appid=appid, score=score + math.log(lifts[appid]), evidence=evidence + tags))
    return rank_candidates(candidates)


@dataclass(frozen=True)
class GameAttributes:
    """One game's attributes as the questioner sees them: tag groups through counting edges, developers, price."""

    tag_keys: frozenset[str]
    studio_keys: frozenset[str]
    price: float | None
    players: int


GAME_ATTRIBUTES_QUERY = """
MATCH (g:Game)
RETURN g.appid AS appid, g.price AS price, COUNT { (g)<-[:PLAYED]-(:Player) } AS players,
       [(g)-[ht:HAS_TAG]->(t:Tag) WHERE ht.rank IS NULL OR ht.rank <= $max_rank | t.tag_id] AS tag_ids,
       [(g)-[:DEVELOPED_BY]->(s:Studio) WHERE s.studio_id <> '' | s.studio_id] AS studio_ids
"""


def load_game_attributes(reader: GraphReader) -> dict[str, GameAttributes]:
    return {
        row["appid"]: GameAttributes(
            tag_keys=frozenset(tag_key(tag_id) for tag_id in row["tag_ids"]),
            studio_keys=frozenset("studio:" + studio_id for studio_id in row["studio_ids"]),
            price=row["price"],
            players=row["players"],
        )
        for row in reader.query(GAME_ATTRIBUTES_QUERY, max_rank=TAG_MAX_RANK)
    }


@dataclass(frozen=True)
class AnchorProfile:
    """What T2 compares: each game's counted tag groups and developers, and their IDF over all games."""

    keys_by_appid: dict[str, frozenset[str]]
    idf: dict[str, float]


def anchor_profile(attributes: dict[str, GameAttributes]) -> AnchorProfile:
    keys_by_appid = {appid: game.tag_keys | game.studio_keys for appid, game in attributes.items()}
    document_frequency: dict[str, int] = {}
    for keys in keys_by_appid.values():
        for key in keys:
            document_frequency[key] = document_frequency.get(key, 0) + 1
    games = max(len(keys_by_appid), 1)
    return AnchorProfile(keys_by_appid, {key: math.log(games / df) for key, df in document_frequency.items()})


def score_similar_rows(rows: list[dict], profile: AnchorProfile, anchors: list[str],
                       max_players: int) -> list[Candidate]:
    """T1's score plus, for each anchor, the IDF of every tag group and developer the game shares with it."""
    scale = popularity_scale(max_players)
    anchor_keys = [(anchor, profile.keys_by_appid.get(anchor, frozenset())) for anchor in anchors]
    candidates = []
    for row in rows:
        score, evidence = t1_score(row, scale)
        keys = profile.keys_by_appid.get(row["appid"], frozenset())
        shared = []
        for anchor, wanted in anchor_keys:
            for key in sorted(keys & wanted):
                path = "studio" if attribute_kind(key) == "studio" else "anchor_tag"
                shared.append(ChatEvidence(path=path, bridge_id=attribute_value(key) if path == "studio" else key,
                                           anchor_appid=anchor, share=profile.idf[key]))
        shared.sort(key=lambda item: (-item.share, item.bridge_id or "", item.anchor_appid or ""))
        candidates.append(Candidate(appid=row["appid"], score=score + sum(item.share for item in shared),
                                    evidence=shared + evidence))
    return rank_candidates(candidates)


class GraphRetriever:
    """Retriever G. Validates the state against the graph, then runs T3 for a same_developer or co_played
    anchor, T2 for a similar one and T1 without an anchor."""

    def __init__(self, reader: GraphReader, vocabulary: Vocabulary | None = None) -> None:
        self.reader = reader
        self.exclude_player: str | None = None
        self.vocabulary = vocabulary or load_vocabulary(reader)
        self.max_players = reader.query(
            "MATCH (g:Game) RETURN coalesce(max(COUNT { (g)<-[:PLAYED]-(:Player) }), 0) AS n"
        )[0]["n"]
        self._new_co_play_cache()
        # one box shared by every copy from excluding_player, so the profile is loaded once
        self._profile_box: dict[str, AnchorProfile] = {}
        self._profile_lock = threading.Lock()

    def profile(self) -> AnchorProfile:
        """T2's tag groups, developers and IDF, loaded on first use."""
        with self._profile_lock:
            if "profile" not in self._profile_box:
                self._profile_box["profile"] = anchor_profile(load_game_attributes(self.reader))
            return self._profile_box["profile"]

    def _new_co_play_cache(self) -> None:
        self._co_play_cache: OrderedDict[tuple[str, ...], tuple[list[dict], int]] = OrderedDict()
        self._co_play_lock = threading.Lock()

    def excluding_player(self, player_id: str) -> "GraphRetriever":
        """The same retriever with `player_id`'s plays left out of popularity and co-play (evaluation only)."""
        retriever = copy.copy(self)
        retriever.exclude_player = player_id
        retriever._new_co_play_cache()
        return retriever

    def co_play(self, anchors: list[str]) -> tuple[list[dict], int]:
        """CO_PLAY_QUERY's rows for these anchors and the number of players, both without exclude_player."""
        key = tuple(sorted(anchors))
        with self._co_play_lock:
            if key in self._co_play_cache:
                self._co_play_cache.move_to_end(key)
                return self._co_play_cache[key]
        co_rows = self.reader.query(
            CO_PLAY_QUERY, anchors=list(key), min_hours=CO_PLAY_MIN_HOURS, exclude_player=self.exclude_player,
        )
        all_players = self.reader.query(PLAYERS_QUERY, exclude_player=self.exclude_player)[0]["n"]
        with self._co_play_lock:
            self._co_play_cache[key] = (co_rows, all_players)
            while len(self._co_play_cache) > CO_PLAY_CACHE_SIZE:
                self._co_play_cache.popitem(last=False)
        return co_rows, all_players

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]:
        state = state.restricted_to(self.vocabulary.attribute_keys, self.vocabulary.appids)
        params = t1_parameters(state, self.vocabulary, self.exclude_player)
        if state.anchors and state.relation == "same_developer":
            rows = self.reader.query(T3_STUDIO_QUERY, **params, anchors=state.anchors)
            return score_studio_rows(rows, self.max_players)[:k]
        if state.anchors and state.relation == "co_played":
            rows = self.reader.query(T1_QUERY, **params)
            co_rows, all_players = self.co_play(state.anchors)
            return score_co_play_rows(rows, co_rows, self.max_players, all_players)[:k]
        rows = self.reader.query(T1_QUERY, **params)
        if state.anchors:  # relation "similar"
            return score_similar_rows(rows, self.profile(), state.anchors, self.max_players)[:k]
        return score_t1_rows(rows, self.max_players)[:k]
