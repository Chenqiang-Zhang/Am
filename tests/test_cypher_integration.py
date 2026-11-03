"""Runs the Cypher in utils/graph_reader.py against a real, throwaway Neo4j loaded with the fixture
graph (through kg.neo4j_loader, so the schema is the real one) and checks it against
tests/fake_graph.py.

Skipped unless STATIC_REC_TEST_NEO4J_URI and STATIC_REC_TEST_NEO4J_PASSWORD point at a Neo4j 5
database that is empty or was filled by this test before. Everything in it is deleted. Example:

    docker run -d --rm --name static-rec-test-neo4j -p 127.0.0.1:17687:7687 \\
        -e NEO4J_AUTH=neo4j/<password> neo4j:5.26-community
    STATIC_REC_TEST_NEO4J_URI=bolt://127.0.0.1:17687 STATIC_REC_TEST_NEO4J_PASSWORD=<password> \\
        python -m pytest tests/test_cypher_integration.py
"""

import os

import pytest
from neo4j import GraphDatabase

import kg.neo4j_loader
from tests.fake_graph import PLAYER_IDS, FakeGraphReader, write_loader_input
from utils import settings
from utils.graph_reader import GraphReader

GRAPH_NAME = "static_rec_test"
MARKER_LABEL = "StaticRecTestFixture"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("STATIC_REC_TEST_NEO4J_URI") and os.environ.get("STATIC_REC_TEST_NEO4J_PASSWORD")),
    reason="needs a throwaway Neo4j (STATIC_REC_TEST_NEO4J_URI / STATIC_REC_TEST_NEO4J_PASSWORD)",
)

# (player_id, hidden appids) pairs to compare on.
CASES = [("u1", []), ("u1", ["30"]), ("u1", ["10"]), ("p2", ["60"]), ("p6", []), ("solo", []), ("p7", [])]


@pytest.fixture(scope="module")
def reader(tmp_path_factory):
    connection = settings.neo4j_connection(GRAPH_NAME)
    driver = GraphDatabase.driver(connection["uri"], auth=(connection["user"], connection["password"]))
    with driver, driver.session(database=connection["database"]) as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        markers = session.run(f"MATCH (n:{MARKER_LABEL}) RETURN count(n) AS n").single()["n"]
        if nodes and not markers:
            pytest.fail(f"{connection['uri']} holds data this test did not create; refusing to delete it")
        session.run("MATCH (n) DETACH DELETE n").consume()
        session.run(f"CREATE (:{MARKER_LABEL})").consume()

        directory = tmp_path_factory.mktemp("graph")
        write_loader_input(directory)
        original = kg.neo4j_loader.graph_data_dir
        kg.neo4j_loader.graph_data_dir = lambda graph_name: directory
        try:
            kg.neo4j_loader.load_graph(GRAPH_NAME)
        finally:
            kg.neo4j_loader.graph_data_dir = original

    graph_reader = GraphReader(GRAPH_NAME)
    yield graph_reader
    graph_reader.close()


def normalize(value):
    """Round floats and sort the per-game tag list, whose order the database does not fix."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        normalized = {key: normalize(inner) for key, inner in value.items()}
        if isinstance(normalized.get("tags"), list):
            normalized["tags"] = sorted(normalized["tags"], key=repr)
        return normalized
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def assert_same(actual, expected):
    assert normalize(actual) == normalize(expected)


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_shared_and_v0_queries_match_the_reference(reader, player_id, hidden):
    fake = FakeGraphReader()
    played = reader.played_games(player_id, hidden)
    assert_same(played, fake.played_games(player_id, hidden))
    played_appids = [game["appid"] for game in played]

    preferences = reader.tag_preferences(played, 30)
    assert_same(preferences, fake.tag_preferences(played, 30))
    assert_same(reader.games_sharing_tags(preferences, played_appids, 100), fake.games_sharing_tags(preferences, played_appids, 100))
    assert_same(
        reader.games_played_by_peers(player_id, played[:10], played_appids, 100),
        fake.games_played_by_peers(player_id, played[:10], played_appids, 100),
    )
    assert_same(
        reader.games_from_played_studios(played, played_appids, 100),
        fake.games_from_played_studios(played, played_appids, 100),
    )


def test_popular_and_chat_queries_run(reader):
    fake = FakeGraphReader()
    assert_same(reader.popular_games(12), fake.popular_games(12))
    assert_same(reader.top_reviews("10", 5), fake.top_reviews("10", 5))
    assert_same(reader.sample_players(10), fake.sample_players(10))
    assert_same(reader.player_reviews("u1", ["30"], 3), fake.player_reviews("u1", ["30"], 3))
    assert_same(reader.player_aspects("u1", [], 8), fake.player_aspects("u1", [], 8))
    assert sorted(reader.top_tags(80)) == sorted(fake.top_tags(80))
    assert reader.aspect_names() == fake.aspect_names()
    search = reader.search_games(["Action", "Puzzle"], ["gameplay"], ["fun"], 30.0, 10)
    expected = fake.search_games(["Action", "Puzzle"], ["gameplay"], ["fun"], 30.0, 10)
    assert sorted((row["appid"], round(row["score"], 9)) for row in search) == sorted(
        (row["appid"], round(row["score"], 9)) for row in expected
    )
    assert sorted(reader.all_play_records()) == sorted(fake.all_play_records())
    assert reader.tag_ids_by_game() == fake.tag_ids_by_game()
    assert {row["player_id"] for row in reader.sample_players(50)} <= set(PLAYER_IDS)
