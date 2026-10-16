import json
from typing import Any

from neo4j import GraphDatabase, Session

from utils import settings

from .files import graph_data_dir, read_jsonl

BATCH_SIZE = 1000
TAG_KIND_LABELS = {"genre": "Genre", "feature": "Feature", "usertag": "UserTag"}
STUDIO_ROLE_RELATIONS = {"developer": "DEVELOPED_BY", "publisher": "PUBLISHED_BY"}

SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT game_appid IF NOT EXISTS FOR (n:Game) REQUIRE n.appid IS UNIQUE",
    "CREATE CONSTRAINT tag_id IF NOT EXISTS FOR (n:Tag) REQUIRE n.tag_id IS UNIQUE",
    "CREATE CONSTRAINT studio_id IF NOT EXISTS FOR (n:Studio) REQUIRE n.studio_id IS UNIQUE",
    "CREATE CONSTRAINT review_id IF NOT EXISTS FOR (n:Review) REQUIRE n.review_id IS UNIQUE",
    "CREATE CONSTRAINT player_id IF NOT EXISTS FOR (n:Player) REQUIRE n.player_id IS UNIQUE",
    "CREATE CONSTRAINT aspect_id IF NOT EXISTS FOR (n:Aspect) REQUIRE n.aspect_id IS UNIQUE",
    "CREATE INDEX game_name IF NOT EXISTS FOR (n:Game) ON (n.name)",
    "CREATE INDEX game_release_date IF NOT EXISTS FOR (n:Game) ON (n.release_date)",
    "CREATE INDEX tag_name IF NOT EXISTS FOR (n:Tag) ON (n.name)",
    "CREATE FULLTEXT INDEX review_text IF NOT EXISTS FOR (n:Review) ON EACH [n.text]",
    "CREATE FULLTEXT INDEX game_name_text IF NOT EXISTS FOR (n:Game) ON EACH [n.name]",
]


def run_in_batches(session: Session, cypher: str, records: list[dict[str, Any]]) -> None:
    for start in range(0, len(records), BATCH_SIZE):
        session.run(cypher, rows=records[start : start + BATCH_SIZE]).consume()


def load_graph(graph_name: str) -> None:
    directory = graph_data_dir(graph_name)
    connection = settings.neo4j_connection(graph_name)
    games = list(read_jsonl(directory / "games.jsonl"))
    reviews = list(read_jsonl(directory / "reviews.jsonl"))
    play_records = list(read_jsonl(directory / "played.jsonl"))
    mentions = list(read_jsonl(directory / "mentions.jsonl"))
    aspects_path = directory / "aspects.json"
    aspects = json.loads(aspects_path.read_text()) if aspects_path.exists() else []

    game_tags = [dict(tag, appid=game["appid"]) for game in games for tag in game["tags"]]
    game_studios = [dict(studio, appid=game["appid"]) for game in games for studio in game["studios"]]
    player_ids = sorted({review["player_id"] for review in reviews} | {record["player_id"] for record in play_records})

    driver = GraphDatabase.driver(connection["uri"], auth=(connection["user"], connection["password"]))
    with driver, driver.session(database=connection["database"]) as session:
        for statement in SCHEMA_STATEMENTS:
            session.run(statement).consume()

        run_in_batches(
            session,
            "UNWIND $rows AS row MERGE (g:Game {appid: row.appid}) "
            "SET g.name = row.name, g.name_ja = row.name_ja, g.description = row.description, "
            "g.description_ja = row.description_ja, g.release_date = row.release_date, "
            "g.price = row.price, g.currency = row.currency",
            games,
        )
        for kind, label in TAG_KIND_LABELS.items():
            run_in_batches(
                session,
                "UNWIND $rows AS row MERGE (t:Tag {tag_id: row.tag_id}) "
                f"SET t.name = row.name, t.name_ja = row.name_ja, t:{label} "
                "WITH t, row MATCH (g:Game {appid: row.appid}) "
                "MERGE (g)-[has_tag:HAS_TAG]->(t) SET has_tag.rank = row.rank",
                [tag for tag in game_tags if tag["kind"] == kind],
            )
        for role, relation in STUDIO_ROLE_RELATIONS.items():
            run_in_batches(
                session,
                "UNWIND $rows AS row MERGE (s:Studio {studio_id: row.studio_id}) SET s.name = row.name "
                f"WITH s, row MATCH (g:Game {{appid: row.appid}}) MERGE (g)-[:{relation}]->(s)",
                [studio for studio in game_studios if studio["role"] == role],
            )
        run_in_batches(
            session,
            "UNWIND $rows AS row MERGE (:Player {player_id: row.player_id})",
            [{"player_id": player_id} for player_id in player_ids],
        )
        run_in_batches(
            session,
            "UNWIND $rows AS row MERGE (review:Review {review_id: row.review_id}) "
            "SET review.text = row.text, review.recommended = row.recommended, "
            "review.votes_up = row.votes_up, review.created_at = row.created_at "
            "WITH review, row MATCH (g:Game {appid: row.appid}) MERGE (review)-[:ABOUT]->(g) "
            "WITH review, row MATCH (p:Player {player_id: row.player_id}) MERGE (p)-[:WROTE]->(review)",
            reviews,
        )
        run_in_batches(
            session,
            "UNWIND $rows AS row MATCH (p:Player {player_id: row.player_id}), (g:Game {appid: row.appid}) "
            "MERGE (p)-[played:PLAYED]->(g) SET played.hours = row.hours, played.hours_2w = row.hours_2w",
            play_records,
        )
        run_in_batches(
            session,
            "UNWIND $rows AS row MERGE (a:Aspect {aspect_id: row.aspect}) SET a.name = row.aspect",
            [{"aspect": aspect} for aspect in aspects],
        )
        run_in_batches(
            session,
            "UNWIND $rows AS row MATCH (review:Review {review_id: row.review_id}) "
            "UNWIND row.aspects AS aspect_name MERGE (a:Aspect {aspect_id: aspect_name}) "
            "ON CREATE SET a.name = aspect_name "
            "MERGE (review)-[:MENTIONS]->(a)",
            mentions,
        )
    print(
        f"{graph_name}: loaded {len(games)} games, {len(player_ids)} players, {len(reviews)} reviews, "
        f"{len(play_records)} played, {len(mentions)} mentions"
    )
