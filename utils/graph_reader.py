from typing import Any, TypedDict

from neo4j import READ_ACCESS, GraphDatabase

from utils import settings

TAG_MAX_RANK = 10
MIN_SHARED_GAMES_WITH_PEER = 2


class TagLabel(TypedDict):
    name: str
    name_ja: str | None
    rank: int | None


class GameRow(TypedDict):
    appid: str
    name: str
    name_ja: str | None
    description: str | None
    description_ja: str | None
    price: float | None
    currency: str | None
    tags: list[TagLabel]
    review_count: int
    recommend_rate: float | None
    played_count: int


class TagMatch(TypedDict):
    tag: str
    tag_ja: str | None
    via: str
    weight: float


class GameMatchedByTags(GameRow):
    score: float
    hits: list[TagMatch]


class GameMatchedByPeers(GameRow):
    peer_count: int
    via: str


class GameMatchedByStudio(GameRow):
    score: float
    studio: str
    via: str


class GameMatchedBySearch(GameRow):
    score: float
    matched_tags: list[str]
    matched_aspects: list[str]
    keyword_hits: int


class PlayedGame(TypedDict):
    appid: str
    name: str
    hours: float
    hours_2w: float


class TagPreference(TypedDict):
    tag_id: str
    name: str
    name_ja: str | None
    weight: float
    via: str


class ReviewRow(TypedDict):
    text: str
    recommended: bool | None
    votes_up: int | None
    created_at: str | None
    hours: float | None


class PlayerReview(TypedDict):
    name: str
    recommended: bool | None
    text: str


class AspectCount(TypedDict):
    aspect: str
    count: int


class SamplePlayer(TypedDict):
    player_id: str
    played_count: int


GAME_COLUMNS = f"""
g.appid AS appid, g.name AS name, g.name_ja AS name_ja,
g.description AS description, g.description_ja AS description_ja,
g.price AS price, g.currency AS currency,
[(g)-[ht:HAS_TAG]->(t:Tag) WHERE ht.rank IS NULL OR ht.rank <= {TAG_MAX_RANK}
 | {{name: t.name, name_ja: t.name_ja, rank: ht.rank}}] AS tags,
size([(g)<-[:ABOUT]-(r:Review) | r]) AS review_count,
size([(g)<-[:ABOUT]-(r:Review) WHERE r.recommended | r]) AS positive_count,
size([(g)<-[:PLAYED]-(:Player) | 1]) AS played_count
"""


def with_recommend_rate(row: dict[str, Any]) -> dict[str, Any]:
    positive = row.pop("positive_count")
    row["recommend_rate"] = positive / row["review_count"] if row["review_count"] else None
    return row


class GraphReader:
    def __init__(self, graph_name: str = settings.GRAPH) -> None:
        connection = settings.neo4j_connection(graph_name)
        self.database_name = connection["database"]
        self.neo4j_driver = GraphDatabase.driver(
            connection["uri"], auth=(connection["user"], connection["password"])
        )

    def close(self) -> None:
        self.neo4j_driver.close()

    def query(self, cypher: str, **params: Any) -> list[Any]:
        with self.neo4j_driver.session(database=self.database_name, default_access_mode=READ_ACCESS) as session:
            return [dict(record) for record in session.run(cypher, **params)]

    def popular_games(self, limit: int) -> list[GameRow]:
        rows = self.query(
            f"MATCH (g:Game) RETURN {GAME_COLUMNS} "
            "ORDER BY played_count DESC, review_count DESC LIMIT $limit",
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    def top_reviews(self, appid: str, limit: int) -> list[ReviewRow]:
        return self.query(
            "MATCH (g:Game {appid: $appid})<-[:ABOUT]-(r:Review) "
            "WHERE r.text IS NOT NULL AND size(r.text) > 10 "
            "OPTIONAL MATCH (r)<-[:WROTE]-(:Player)-[pl:PLAYED]->(g) "
            "RETURN r.text AS text, r.recommended AS recommended, r.votes_up AS votes_up, "
            "toString(r.created_at) AS created_at, pl.hours AS hours "
            "ORDER BY r.votes_up DESC, r.review_id ASC LIMIT $limit",
            appid=appid,
            limit=limit,
        )

    def sample_players(self, limit: int) -> list[SamplePlayer]:
        return self.query(
            "MATCH (p:Player)-[pl:PLAYED]->(:Game) "
            "WITH p, count(pl) AS played_count WHERE played_count >= 3 "
            "RETURN p.player_id AS player_id, played_count "
            "ORDER BY played_count DESC LIMIT $limit",
            limit=limit,
        )

    def played_games(self, player_id: str, hide_appids: list[str]) -> list[PlayedGame]:
        return self.query(
            "MATCH (:Player {player_id: $player_id})-[pl:PLAYED]->(g:Game) "
            "WHERE NOT g.appid IN $hide "
            "RETURN g.appid AS appid, g.name AS name, pl.hours AS hours, "
            "coalesce(pl.hours_2w, 0) AS hours_2w ORDER BY hours DESC",
            player_id=player_id,
            hide=hide_appids,
        )

    def tag_preferences(self, played: list[PlayedGame], limit: int) -> list[TagPreference]:
        return self.query(
            "MATCH (:Game) WITH count(*) AS total "
            "UNWIND $played AS pl "
            "MATCH (g:Game {appid: pl.appid})-[ht:HAS_TAG]->(t:Tag) "
            "WHERE ht.rank IS NULL OR ht.rank <= $max_rank "
            "WITH total, t, g, pl ORDER BY pl.hours DESC "
            "WITH total, t, sum(log(1 + pl.hours)) AS pref, head(collect(g.name)) AS via "
            "WITH t, via, pref * log(toFloat(total) / size([(t)<-[:HAS_TAG]-() | 1])) AS weight "
            "WHERE weight > 0 "
            "RETURN t.tag_id AS tag_id, t.name AS name, t.name_ja AS name_ja, weight, via "
            "ORDER BY weight DESC LIMIT $limit",
            played=played,
            max_rank=TAG_MAX_RANK,
            limit=limit,
        )

    def games_sharing_tags(self, prefs: list[TagPreference], played_appids: list[str], limit: int) -> list[GameMatchedByTags]:
        rows = self.query(
            "UNWIND $prefs AS pf "
            "MATCH (t:Tag {tag_id: pf.tag_id})<-[ht:HAS_TAG]-(g:Game) "
            "WHERE (ht.rank IS NULL OR ht.rank <= $max_rank) AND NOT g.appid IN $played_appids "
            "WITH g, sum(pf.weight) AS score, "
            "collect({tag: t.name, tag_ja: t.name_ja, via: pf.via, weight: pf.weight}) AS hits "
            "ORDER BY score DESC LIMIT $limit "
            f"RETURN {GAME_COLUMNS}, score, hits",
            prefs=prefs,
            played_appids=played_appids,
            max_rank=TAG_MAX_RANK,
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    def games_played_by_peers(
        self, player_id: str, seeds: list[PlayedGame], played_appids: list[str], limit: int
    ) -> list[GameMatchedByPeers]:
        rows = self.query(
            "UNWIND $seeds AS pl "
            "MATCH (seed:Game {appid: pl.appid})<-[:PLAYED]-(peer:Player) "
            "WHERE peer.player_id <> $player_id "
            "WITH peer, count(seed) AS shared, head(collect(seed.name)) AS via "
            "WHERE shared >= $min_shared "
            "MATCH (peer)-[:PLAYED]->(g:Game) WHERE NOT g.appid IN $played_appids "
            "WITH g, count(peer) AS peer_count, head(collect(via)) AS via "
            "ORDER BY peer_count DESC LIMIT $limit "
            f"RETURN {GAME_COLUMNS}, peer_count, via",
            player_id=player_id,
            seeds=seeds,
            played_appids=played_appids,
            min_shared=MIN_SHARED_GAMES_WITH_PEER,
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    def games_from_played_studios(self, played: list[PlayedGame], played_appids: list[str], limit: int) -> list[GameMatchedByStudio]:
        rows = self.query(
            "UNWIND $played AS pl "
            "MATCH (seed:Game {appid: pl.appid})-[:DEVELOPED_BY|PUBLISHED_BY]->(s:Studio) "
            "WITH s, seed, pl ORDER BY pl.hours DESC "
            "WITH s, sum(log(1 + pl.hours)) AS weight, head(collect(seed.name)) AS via "
            "MATCH (s)<-[:DEVELOPED_BY|PUBLISHED_BY]-(g:Game) WHERE NOT g.appid IN $played_appids "
            "WITH g, max(weight) AS score, head(collect(s.name)) AS studio, head(collect(via)) AS via "
            "ORDER BY score DESC LIMIT $limit "
            f"RETURN {GAME_COLUMNS}, score, studio, via",
            played=played,
            played_appids=played_appids,
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    def player_reviews(self, player_id: str, hide_appids: list[str], limit: int) -> list[PlayerReview]:
        return self.query(
            "MATCH (:Player {player_id: $player_id})-[:WROTE]->(r:Review)-[:ABOUT]->(g:Game) "
            "WHERE NOT g.appid IN $hide "
            "RETURN g.name AS name, r.recommended AS recommended, r.text AS text "
            "ORDER BY r.votes_up DESC LIMIT $limit",
            player_id=player_id,
            hide=hide_appids,
            limit=limit,
        )

    def player_aspects(self, player_id: str, hide_appids: list[str], limit: int) -> list[AspectCount]:
        return self.query(
            "MATCH (:Player {player_id: $player_id})-[:WROTE]->(r:Review)-[:ABOUT]->(g:Game) "
            "WHERE NOT g.appid IN $hide "
            "MATCH (r)-[:MENTIONS]->(a:Aspect) "
            "RETURN a.name AS aspect, count(*) AS count ORDER BY count DESC LIMIT $limit",
            player_id=player_id,
            hide=hide_appids,
            limit=limit,
        )

    def top_tags(self, limit: int) -> list[str]:
        rows = self.query(
            "MATCH (t:Tag)<-[ht:HAS_TAG]-(:Game) WHERE ht.rank IS NULL OR ht.rank <= $max_rank "
            "RETURN t.name AS name, count(*) AS n ORDER BY n DESC LIMIT $limit",
            max_rank=TAG_MAX_RANK,
            limit=limit,
        )
        return [row["name"] for row in rows]

    def aspect_names(self) -> list[str]:
        rows = self.query("MATCH (a:Aspect) RETURN a.aspect_id AS aspect ORDER BY aspect")
        return [row["aspect"] for row in rows]

    def search_games(
        self,
        tags: list[str],
        aspects: list[str],
        keywords: list[str],
        max_price: float | None,
        limit: int,
    ) -> list[GameMatchedBySearch]:
        rows = self.query(
            "MATCH (g:Game) "
            "WHERE $max_price IS NULL OR g.price IS NULL OR g.price <= $max_price "
            "WITH g, "
            "[(g)-[ht:HAS_TAG]->(t:Tag) WHERE t.name IN $tags AND (ht.rank IS NULL OR ht.rank <= $max_rank) "
            " | t.name] AS matched_tags, "
            "[(g)<-[:ABOUT]-(:Review)-[:MENTIONS]->(a:Aspect) WHERE a.aspect_id IN $aspects | a.name] "
            " AS matched_aspects, "
            "size([(g)<-[:ABOUT]-(r:Review) "
            " WHERE any(k IN $keywords WHERE toLower(r.text) CONTAINS toLower(k)) | r]) AS keyword_hits "
            "WITH g, matched_tags, matched_aspects, keyword_hits, "
            "size(matched_tags) * 3.0 + log(1 + size(matched_aspects)) + log(1 + keyword_hits) AS score "
            "WHERE score > 0 "
            "ORDER BY score DESC LIMIT $limit "
            f"RETURN {GAME_COLUMNS}, score, matched_tags, matched_aspects, keyword_hits",
            tags=tags,
            aspects=aspects,
            keywords=keywords,
            max_price=max_price,
            max_rank=TAG_MAX_RANK,
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    def all_play_records(self) -> list[tuple[str, str, float]]:
        rows = self.query(
            "MATCH (p:Player)-[pl:PLAYED]->(g:Game) "
            "RETURN p.player_id AS player_id, g.appid AS appid, pl.hours AS hours"
        )
        return [(row["player_id"], row["appid"], row["hours"]) for row in rows]

    def tag_ids_by_game(self) -> dict[str, set[str]]:
        rows = self.query(
            "MATCH (g:Game) RETURN g.appid AS appid, "
            "[(g)-[ht:HAS_TAG]->(t:Tag) WHERE ht.rank IS NULL OR ht.rank <= $max_rank | t.tag_id] AS tags",
            max_rank=TAG_MAX_RANK,
        )
        return {row["appid"]: set(row["tags"]) for row in rows}
