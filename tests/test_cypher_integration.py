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

import math
import os
import sys

import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import TransientError

import eval.check_reader_equivalence as check_reader_equivalence
import kg.neo4j_loader
import utils.graph_reader as graph_reader
from app.api.static_recommend import SeedScheme, collect_path_scores, recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
from eval.check_reader_equivalence import ReferenceReader
from eval.explanation_check import CypherFacts, check_evidence, check_recommendations
from tests.fake_graph import GAMES, PLAYER_IDS, FakeGraphReader, write_loader_input
from tests.test_explanation_check import FakeFacts
from utils import settings
from utils.graph_reader import GraphReader

GRAPH_NAME = "static_rec_test"
MARKER_LABEL = "StaticRecTestFixture"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("STATIC_REC_TEST_NEO4J_URI") and os.environ.get("STATIC_REC_TEST_NEO4J_PASSWORD")),
    reason="needs a throwaway Neo4j (STATIC_REC_TEST_NEO4J_URI / STATIC_REC_TEST_NEO4J_PASSWORD)",
)

# (player_id, hidden appids) pairs to compare on. p8 has more games than the 10 co-play seeds.
CASES = [
    ("u1", []), ("u1", ["30"]), ("u1", ["10"]), ("p2", ["60"]), ("p6", []), ("solo", []), ("p7", []), ("p7", ["40"]),
    ("p8", []), ("p8", ["150"]),
]


def write(cypher, **params):
    connection = settings.neo4j_connection(GRAPH_NAME)
    driver = GraphDatabase.driver(connection["uri"], auth=(connection["user"], connection["password"]))
    with driver, driver.session(database=connection["database"]) as session:
        session.run(cypher, **params).consume()


@pytest.fixture(scope="module")
def reader(tmp_path_factory):
    connection = settings.neo4j_connection(GRAPH_NAME)
    driver = GraphDatabase.driver(connection["uri"], auth=(connection["user"], connection["password"]))
    with driver, driver.session(database=connection["database"]) as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        markers = session.run(f"MATCH (n:{MARKER_LABEL}) RETURN count(n) AS n").single()["n"]
        if nodes and not markers:
            pytest.fail(f"{connection['uri']} holds data this test did not create; refusing to delete it")
        # Mark the database first and unmark it never: an interrupted run can always be cleaned up next time.
        session.run(f"MERGE (:{MARKER_LABEL})").consume()
        session.run("MATCH ()-[r]->() CALL { WITH r DELETE r } IN TRANSACTIONS OF 10000 ROWS").consume()
        session.run(
            f"MATCH (n) WHERE NOT n:{MARKER_LABEL} CALL {{ WITH n DELETE n }} IN TRANSACTIONS OF 10000 ROWS"
        ).consume()

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


@pytest.fixture(scope="module")
def reference(reader):
    """The statements the path queries replaced, on the same database."""
    reference_reader = ReferenceReader(GRAPH_NAME)
    yield reference_reader
    reference_reader.close()


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
    # Chat-only queries have no tie-break: compare equally ranked rows as sets.
    assert_same(
        sorted(reader.player_aspects("u1", [], 8), key=repr), sorted(fake.player_aspects("u1", [], 8), key=repr)
    )
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


SCHEMES = [
    SeedScheme(),
    SeedScheme(peer_limit=None),
    SeedScheme(weighting="uniform", peer_limit=None),
    SeedScheme(peer_limit=1),
]
SCHEME_IDS = ["default", "all", "uniform-all", "one-peer-seed"]


@pytest.mark.parametrize("scheme", SCHEMES, ids=SCHEME_IDS)
@pytest.mark.parametrize("player_id,hidden", CASES)
def test_path_queries_match_the_formulas(reader, player_id, hidden, scheme):
    hidden_appids = frozenset(hidden)
    assert_same(
        collect_path_scores(reader, player_id, hidden_appids, scheme),
        collect_path_scores(FakeGraphReader(), player_id, hidden_appids, scheme),
    )


def test_played_edges_run_from_a_player_to_a_game_without_parallels(reader):
    """co_play_scores relies on both: it matches PLAYED without labels, and its count fields cannot
    carry only if no player has two edges to one game."""
    assert reader.query("MATCH (a)-[r:PLAYED]->(b) WHERE NOT (a:Player AND b:Game) RETURN count(r) AS n") == [{"n": 0}]
    assert reader.query(
        "MATCH (p:Player)-[r:PLAYED]->(g:Game) WITH p, g, count(r) AS edges WHERE edges > 1 RETURN count(*) AS n"
    ) == [{"n": 0}]


@pytest.mark.parametrize("seed_batch,word_bits", [(1, 63), (2, 3)])
@pytest.mark.parametrize("player_id,hidden", CASES)
def test_co_play_counts_do_not_depend_on_batch_or_word_size(
    reader, monkeypatch, player_id, hidden, seed_batch, word_bits
):
    played = reader.played_games(player_id, hidden)
    seeds = [{"appid": game["appid"], "weight": math.log1p(game["hours"])} for game in played]
    args = (player_id, seeds, [game["appid"] for game in played], 3)
    expected = reader.co_play_scores(*args)
    monkeypatch.setattr(graph_reader, "SEED_BATCH", seed_batch)
    monkeypatch.setattr(graph_reader, "WORD_BITS", word_bits)
    assert reader.co_play_scores(*args) == expected


def test_co_play_seeds_the_player_did_not_play_equal_the_statement_they_replaced(reader, reference):
    graph = FakeGraphReader()
    others = sorted(appid for appid, players in graph.players_by_game.items() if players and "u1" not in players)
    seeds = [{"appid": appid, "weight": 1.0} for appid in others] + [{"appid": "10", "weight": 2.0}]
    args = ("u1", seeds, ["10"], len(seeds))
    rows = reader.co_play_scores(*args)
    assert rows == reference.co_play_scores(*args)
    assert_same(rows, graph.co_play_scores(*args))


@pytest.mark.parametrize("scheme", SCHEMES, ids=SCHEME_IDS)
@pytest.mark.parametrize("player_id,hidden", CASES + [("nobody", [])])
def test_path_queries_equal_the_statements_they_replaced(reader, reference, player_id, hidden, scheme):
    """Exactly, as eval/check_reader_equivalence.py checks on the real graph."""
    hidden_appids = frozenset(hidden)
    assert collect_path_scores(reader, player_id, hidden_appids, scheme) == collect_path_scores(
        reference, player_id, hidden_appids, scheme
    )
    for lang in ("en", "ja"):
        actual = recommend_games(reader, player_id, 20, lang, hidden_appids, scheme=scheme)
        expected = recommend_games(reference, player_id, 20, lang, hidden_appids, scheme=scheme)
        assert [item.model_dump() for item in actual] == [item.model_dump() for item in expected]


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_tag_and_popularity_rows_equal_the_statements_they_replaced(reader, reference, player_id, hidden):
    """Row for row, also with seeds outside played_appids, no played_appids, an unknown seed and an
    unknown player. The holders and document frequency of the concept table against their own statement."""
    played = reader.played_games(player_id, [])
    seeds = [{"appid": "999", "weight": 1.0}] + [
        {"appid": game["appid"], "weight": math.log1p(game["hours"])} for game in played
    ]
    for concept_limit in (30, 2):
        assert reader.tag_concepts(seeds, concept_limit) == reference.tag_concepts(seeds, concept_limit)
    visible = [game["appid"] for game in played if game["appid"] not in hidden]
    for played_appids in (visible, visible[::2], []):
        for concept_limit, evidence_limit in ((30, 3), (2, 1), (30, 20)):
            args = (seeds, played_appids, concept_limit, evidence_limit)
            assert reader.tag_concept_scores(*args) == reference.tag_concept_scores(*args)
        for popularity_of in (player_id, "nobody"):
            assert reader.game_popularity(popularity_of, played_appids) == reference.game_popularity(
                popularity_of, played_appids
            )


def test_games_by_appids_equals_the_statement_it_replaced(reader, reference):
    appids = [game["appid"] for game in reversed(GAMES)] + ["999"]

    def rows(graph_reader):  # the database does not fix the order of a game's tags
        return [dict(row, tags=sorted(row["tags"], key=repr)) for row in graph_reader.games_by_appids(appids)]

    assert rows(reader) == rows(reference)
    assert [row["appid"] for row in rows(reader)] == appids[:-1]


@pytest.mark.parametrize("failing,compared,code", [({"u1"}, 1, 0), ({"u1", "p8", "nobody"}, 0, 2)], ids=["one", "all"])
def test_the_equivalence_check_goes_on_after_a_reference_fails(reader, monkeypatch, capsys, failing, compared, code):
    """A player whose reference statements fail transiently is not compared; exit code 2 if no player is."""
    query = ReferenceReader.query

    def flaky(self, cypher, **params):
        if params.get("player_id") in failing:
            raise TransientError("forced")
        return query(self, cypher, **params)

    monkeypatch.setattr(ReferenceReader, "query", flaky)
    monkeypatch.setattr(sys, "argv", ["check_reader_equivalence", "--graph", GRAPH_NAME, "--players", "u1", "p8"])
    with pytest.raises(SystemExit) as exit_info:
        check_reader_equivalence.main()
    assert exit_info.value.code == code
    assert f"compared {compared} players" in capsys.readouterr().out


@pytest.mark.parametrize("recommend", [recommend_games, recommend_games_v0], ids=["current", "v0"])
@pytest.mark.parametrize("player_id,hidden", CASES + [("nobody", []), (None, [])])
def test_recommendations_match_the_reference(reader, recommend, player_id, hidden):
    for lang in ("en", "ja"):
        actual = recommend(reader, player_id, 20, lang, frozenset(hidden))
        expected = recommend(FakeGraphReader(), player_id, 20, lang, frozenset(hidden))
        assert_same([item.model_dump() for item in actual], [item.model_dump() for item in expected])


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_explanation_check_cypher_agrees_with_the_reference(reader, player_id, hidden):
    cypher_facts, fake_facts = CypherFacts(reader), FakeFacts(FakeGraphReader())
    assert cypher_facts.played(player_id) == fake_facts.played(player_id)
    visible = {appid: name for appid, name in cypher_facts.played(player_id).items() if appid not in hidden}
    for recommend in (recommend_games, recommend_games_v0):
        for item in recommend(reader, player_id, 20, "en", frozenset(hidden)):
            for evidence in item.evidence or []:
                actual = check_evidence(cypher_facts, player_id, visible, item.appid, evidence)
                expected = check_evidence(fake_facts, player_id, visible, item.appid, evidence)
                assert normalize(actual) == normalize(expected), (item.appid, evidence)


def test_explanation_check_end_to_end(reader):
    players = ["u1", "p1", "p2", "p3", "p4", "p5", "p6", "solo"]
    hidden = {"u1": "30", "p2": "60", "p6": "90", "p4": "80"}
    reports = check_recommendations(reader, CypherFacts(reader), players, hidden, 10, ["current", "v0"])
    assert reports["current"].validity == 1.0
    assert reports["v0"].validity < 1.0
    expected = check_recommendations(
        FakeGraphReader(), FakeFacts(FakeGraphReader()), players, hidden, 10, ["current", "v0"]
    )
    for version in ("current", "v0"):
        assert normalize(reports[version].summary()) == normalize(expected[version].summary())


@pytest.mark.parametrize("player_id,appid,hours", [("u1", "30", 5.0), ("p7", "40", 4.0)])
def test_deleting_the_hidden_edge_changes_nothing(reader, player_id, appid, hours):
    """Hiding a game in the query equals deleting the player's PLAYED edge."""
    hidden = frozenset({appid})
    before = collect_path_scores(reader, player_id, hidden)
    before_items = [item.model_dump() for item in recommend_games(reader, player_id, 20, "en", hidden)]
    edge = {"player_id": player_id, "appid": appid}
    write("MATCH (:Player {player_id: $player_id})-[edge:PLAYED]->(:Game {appid: $appid}) DELETE edge", **edge)
    try:
        assert collect_path_scores(reader, player_id, hidden) == before
        assert [item.model_dump() for item in recommend_games(reader, player_id, 20, "en", hidden)] == before_items
    finally:
        write(
            "MATCH (p:Player {player_id: $player_id}), (g:Game {appid: $appid}) "
            "MERGE (p)-[edge:PLAYED]->(g) SET edge.hours = $hours, edge.hours_2w = 0.0",
            hours=hours,
            **edge,
        )
