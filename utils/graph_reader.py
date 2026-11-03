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


class SeedGame(TypedDict):
    appid: str
    weight: float


class CoPlaySeed(TypedDict):
    appid: str
    name: str
    weight: float
    co_players: int
    seed_players: int
    term: float


class CoPlayCandidate(TypedDict):
    appid: str
    score: float
    game_players: int
    seed_count: int
    seeds: list[CoPlaySeed]


class TagConceptMatch(TypedDict):
    concept: str
    name: str
    name_ja: str | None
    weight: float
    seed_appid: str
    seed_name: str
    term: float


class TagConceptCandidate(TypedDict):
    appid: str
    score: float
    concept_count: int
    match_count: int
    matches: list[TagConceptMatch]


class StudioLink(TypedDict):
    studio_id: str
    studio: str
    seed_appid: str
    seed_name: str
    seed_role: str
    game_role: str
    titles: int
    weight: float
    term: float


class StudioCandidate(TypedDict):
    appid: str
    score: float
    seed_count: int
    links: list[StudioLink]


class GamePopularity(TypedDict):
    appid: str
    players: int


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
            "ORDER BY played_count DESC, review_count DESC, appid ASC LIMIT $limit",
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
            "ORDER BY player_id LIMIT $limit",
            limit=limit,
        )

    def played_games(self, player_id: str, hide_appids: list[str]) -> list[PlayedGame]:
        return self.query(
            "MATCH (:Player {player_id: $player_id})-[pl:PLAYED]->(g:Game) "
            "WHERE NOT g.appid IN $hide "
            "RETURN g.appid AS appid, g.name AS name, pl.hours AS hours, "
            "coalesce(pl.hours_2w, 0) AS hours_2w ORDER BY hours DESC, appid ASC",
            player_id=player_id,
            hide=hide_appids,
        )

    def tag_preferences(self, played: list[PlayedGame], limit: int) -> list[TagPreference]:
        return self.query(
            "MATCH (:Game) WITH count(*) AS total "
            "UNWIND $played AS pl "
            "MATCH (g:Game {appid: pl.appid})-[ht:HAS_TAG]->(t:Tag) "
            "WHERE ht.rank IS NULL OR ht.rank <= $max_rank "
            "WITH total, t, g, pl ORDER BY pl.hours DESC, g.appid ASC "
            "WITH total, t, sum(log(1 + pl.hours)) AS pref, head(collect(g.name)) AS via "
            "WITH t, via, pref * log(toFloat(total) / size([(t)<-[:HAS_TAG]-() | 1])) AS weight "
            "WHERE weight > 0 "
            "RETURN t.tag_id AS tag_id, t.name AS name, t.name_ja AS name_ja, weight, via "
            "ORDER BY weight DESC, tag_id ASC LIMIT $limit",
            played=played,
            max_rank=TAG_MAX_RANK,
            limit=limit,
        )

    def games_sharing_tags(self, prefs: list[TagPreference], played_appids: list[str], limit: int) -> list[GameMatchedByTags]:
        rows = self.query(
            "UNWIND $prefs AS pf "
            "MATCH (t:Tag {tag_id: pf.tag_id})<-[ht:HAS_TAG]-(g:Game) "
            "WHERE (ht.rank IS NULL OR ht.rank <= $max_rank) AND NOT g.appid IN $played_appids "
            "WITH g, t, pf ORDER BY pf.weight DESC, t.tag_id ASC "
            "WITH g, sum(pf.weight) AS score, "
            "collect({tag: t.name, tag_ja: t.name_ja, via: pf.via, weight: pf.weight}) AS hits "
            "ORDER BY score DESC, g.appid ASC LIMIT $limit "
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
            "WITH peer, seed, pl ORDER BY pl.hours DESC, seed.appid ASC "
            "WITH peer, count(seed) AS shared, "
            "head(collect({name: seed.name, appid: seed.appid, hours: pl.hours})) AS via "
            "WHERE shared >= $min_shared "
            "MATCH (peer)-[:PLAYED]->(g:Game) WHERE NOT g.appid IN $played_appids "
            "WITH g, peer, via ORDER BY via.hours DESC, via.appid ASC "
            "WITH g, count(peer) AS peer_count, head(collect(via.name)) AS via "
            "ORDER BY peer_count DESC, g.appid ASC LIMIT $limit "
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
            "WITH s, seed, pl ORDER BY pl.hours DESC, seed.appid ASC "
            "WITH s, sum(log(1 + pl.hours)) AS weight, head(collect(seed.name)) AS via "
            "MATCH (s)<-[:DEVELOPED_BY|PUBLISHED_BY]-(g:Game) WHERE NOT g.appid IN $played_appids "
            "WITH g, s, weight, via ORDER BY weight DESC, s.studio_id ASC "
            "WITH g, max(weight) AS score, head(collect(s.name)) AS studio, head(collect(via)) AS via "
            "ORDER BY score DESC, g.appid ASC LIMIT $limit "
            f"RETURN {GAME_COLUMNS}, score, studio, via",
            played=played,
            played_appids=played_appids,
            limit=limit,
        )
        return [with_recommend_rate(row) for row in rows]

    # Static recommendation paths (M3). Each query scores every game the player has not visibly played
    # and returns the path instances behind the score, strongest first. PLAYED edges only start at
    # Player nodes, so COUNT { (g)<-[:PLAYED]-() } reads the degree store; the player's own edge is
    # subtracted so that a hidden game looks exactly as if its edge had been deleted.

    def co_play_scores(
        self, player_id: str, seeds: list[SeedGame], played_appids: list[str], evidence_limit: int
    ) -> list[CoPlayCandidate]:
        """score(g) = Σ_s w_s · co(s, g) / sqrt(deg(s) · deg(g)), all counts without the player."""
        return self.query(
            "MATCH (me:Player {player_id: $player_id}) "
            "UNWIND $seeds AS seed_row "
            "MATCH (seed:Game {appid: seed_row.appid})<-[:PLAYED]-(peer:Player)-[:PLAYED]->(g:Game) "
            "WHERE peer <> me "
            "WITH me, seed, seed_row.weight AS seed_weight, g, count(peer) AS co_players "
            "WHERE NOT g.appid IN $played_appids "
            "WITH g, seed, seed_weight, co_players, "
            "COUNT { (seed)<-[:PLAYED]-() } - CASE WHEN EXISTS { (me)-[:PLAYED]->(seed) } THEN 1 ELSE 0 END "
            "AS seed_players, "
            "COUNT { (g)<-[:PLAYED]-() } - CASE WHEN EXISTS { (me)-[:PLAYED]->(g) } THEN 1 ELSE 0 END "
            "AS game_players "
            "WITH g, game_players, seed, seed_weight, co_players, seed_players, "
            "seed_weight * co_players / sqrt(toFloat(seed_players) * game_players) AS term "
            "ORDER BY term DESC, seed.appid ASC "
            "WITH g, game_players, sum(term) AS score, count(seed) AS seed_count, "
            "collect({appid: seed.appid, name: seed.name, weight: seed_weight, co_players: co_players, "
            "seed_players: seed_players, term: term})[..$evidence_limit] AS seeds "
            "RETURN g.appid AS appid, score, game_players, seed_count, seeds "
            "ORDER BY score DESC, appid ASC",
            player_id=player_id,
            seeds=seeds,
            played_appids=played_appids,
            evidence_limit=evidence_limit,
        )

    def tag_concept_scores(
        self, seeds: list[SeedGame], played_appids: list[str], concept_limit: int, evidence_limit: int
    ) -> list[TagConceptCandidate]:
        """A genre and a user tag with the same name are one concept (the part of tag_id after the
        colon); Feature tags are left out. IDF and matching both use edges with rank <= TAG_MAX_RANK
        or no rank. weight(c) = Σ_{seeds with c} w_s · ln(N / df(c)), kept for the top concepts;
        score(g) = Σ_{c of g} weight(c) / sqrt(number of concepts of g)."""
        return self.query(
            "MATCH (game:Game) WITH count(game) AS total "
            "UNWIND $seeds AS seed_row "
            "MATCH (seed:Game {appid: seed_row.appid})-[seed_tag:HAS_TAG]->(t:Tag) "
            "WHERE NOT t:Feature AND (seed_tag.rank IS NULL OR seed_tag.rank <= $max_rank) "
            "WITH DISTINCT total, seed, seed_row.weight AS seed_weight, split(t.tag_id, ':')[1] AS concept "
            "ORDER BY seed_weight DESC, seed.appid ASC "
            "WITH total, concept, sum(seed_weight) AS preference, head(collect(seed)) AS via "
            "MATCH (tag:Tag) WHERE NOT tag:Feature AND split(tag.tag_id, ':')[1] = concept "
            "WITH total, concept, preference, via, tag ORDER BY tag.tag_id ASC "
            "WITH total, concept, preference, via, collect(tag) AS tags "
            "UNWIND tags AS tag "
            "MATCH (tag)<-[owner_tag:HAS_TAG]-(owner:Game) "
            "WHERE owner_tag.rank IS NULL OR owner_tag.rank <= $max_rank "
            "WITH total, concept, preference, via, tags, count(DISTINCT owner) AS document_frequency "
            "WITH concept, via, tags, preference * log(toFloat(total) / document_frequency) AS weight "
            "WHERE weight > 0 "
            "WITH concept, via, tags, weight ORDER BY weight DESC, concept ASC LIMIT $concept_limit "
            "UNWIND tags AS tag "
            "MATCH (tag)<-[game_tag:HAS_TAG]-(g:Game) "
            "WHERE (game_tag.rank IS NULL OR game_tag.rank <= $max_rank) AND NOT g.appid IN $played_appids "
            "WITH DISTINCT g, concept, weight, via, head(tags) AS label "
            "WITH g, collect({concept: concept, weight: weight, via: via, label: label}) AS matched "
            "WITH g, matched, size(reduce(seen = [], key IN "
            "[(g)-[own:HAS_TAG]->(own_tag:Tag) WHERE NOT own_tag:Feature "
            "AND (own.rank IS NULL OR own.rank <= $max_rank) | split(own_tag.tag_id, ':')[1]] "
            "| CASE WHEN key IN seen THEN seen ELSE seen + key END)) AS concept_count "
            "UNWIND matched AS match "
            "WITH g, concept_count, match, match.weight / sqrt(toFloat(concept_count)) AS term "
            "ORDER BY term DESC, match.concept ASC "
            "WITH g, concept_count, sum(term) AS score, count(match) AS match_count, "
            "collect({concept: match.concept, name: match.label.name, name_ja: match.label.name_ja, "
            "weight: match.weight, seed_appid: match.via.appid, seed_name: match.via.name, term: term})"
            "[..$evidence_limit] AS matches "
            "RETURN g.appid AS appid, score, concept_count, match_count, matches "
            "ORDER BY score DESC, appid ASC",
            seeds=seeds,
            played_appids=played_appids,
            max_rank=TAG_MAX_RANK,
            concept_limit=concept_limit,
            evidence_limit=evidence_limit,
        )

    def studio_scores(
        self,
        seeds: list[SeedGame],
        played_appids: list[str],
        developer_weight: float,
        publisher_weight: float,
        evidence_limit: int,
    ) -> list[StudioCandidate]:
        """A game's role for a studio is developer if it has DEVELOPED_BY to it, else publisher.
        Each seed counts once per candidate, through its strongest shared studio:
        w_s · (developer_weight if both are developer else publisher_weight) / ln(2 + titles)."""
        return self.query(
            "UNWIND $seeds AS seed_row "
            "MATCH (seed:Game {appid: seed_row.appid})-[seed_link:DEVELOPED_BY|PUBLISHED_BY]->(studio:Studio) "
            "WITH seed, seed_row.weight AS seed_weight, studio, collect(type(seed_link)) AS seed_relations "
            "MATCH (studio)<-[:DEVELOPED_BY|PUBLISHED_BY]-(title:Game) "
            "WITH seed, seed_weight, studio, seed_relations, count(DISTINCT title) AS titles "
            "MATCH (studio)<-[game_link:DEVELOPED_BY|PUBLISHED_BY]-(g:Game) WHERE NOT g.appid IN $played_appids "
            "WITH seed, seed_weight, studio, seed_relations, titles, g, collect(type(game_link)) AS game_relations "
            "WITH g, seed, seed_weight, studio, titles, "
            "CASE WHEN 'DEVELOPED_BY' IN seed_relations THEN 'developer' ELSE 'publisher' END AS seed_role, "
            "CASE WHEN 'DEVELOPED_BY' IN game_relations THEN 'developer' ELSE 'publisher' END AS game_role "
            "WITH g, seed, seed_weight, studio, titles, seed_role, game_role, "
            "seed_weight * CASE WHEN seed_role = 'developer' AND game_role = 'developer' "
            "THEN $developer_weight ELSE $publisher_weight END / log(2 + titles) AS term "
            "ORDER BY term DESC, studio.studio_id ASC "
            "WITH g, seed, seed_weight, head(collect({studio_id: studio.studio_id, studio: studio.name, "
            "seed_role: seed_role, game_role: game_role, titles: titles, term: term})) AS link "
            "WITH g, seed, seed_weight, link ORDER BY link.term DESC, seed.appid ASC "
            "WITH g, sum(link.term) AS score, count(seed) AS seed_count, "
            "collect(link {.*, seed_appid: seed.appid, seed_name: seed.name, weight: seed_weight})"
            "[..$evidence_limit] AS links "
            "RETURN g.appid AS appid, score, seed_count, links ORDER BY score DESC, appid ASC",
            seeds=seeds,
            played_appids=played_appids,
            developer_weight=developer_weight,
            publisher_weight=publisher_weight,
            evidence_limit=evidence_limit,
        )

    def game_popularity(self, player_id: str, played_appids: list[str]) -> list[GamePopularity]:
        """Players of every game the player has not visibly played, not counting the player."""
        return self.query(
            "MATCH (g:Game) WHERE NOT g.appid IN $played_appids "
            "RETURN g.appid AS appid, COUNT { (g)<-[:PLAYED]-() } "
            "- CASE WHEN EXISTS { (:Player {player_id: $player_id})-[:PLAYED]->(g) } THEN 1 ELSE 0 END AS players "
            "ORDER BY players DESC, appid ASC",
            player_id=player_id,
            played_appids=played_appids,
        )

    def games_by_appids(self, appids: list[str]) -> list[GameRow]:
        rows = self.query(
            f"UNWIND $appids AS wanted MATCH (g:Game {{appid: wanted}}) RETURN {GAME_COLUMNS}",
            appids=appids,
        )
        position = {appid: index for index, appid in enumerate(appids)}
        return sorted((with_recommend_rate(row) for row in rows), key=lambda row: position[row["appid"]])

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
