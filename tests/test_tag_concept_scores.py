"""tag_concept_scores reads the concept table, then the concepts each candidate matched, and adds the
terms up in Python. Against a stand-in for Neo4j it must equal the formula, written concept by concept
in FakeGraphReader.tag_concept_scores. The stand-in answers TAG_CONCEPTS with a concept table of its own,
which must equal FakeGraphReader.concept_table; that one is pinned where the appid order decides."""

import math

import pytest

from app.api.static_recommend import SeedScheme, collect_path_scores
from tests.fake_graph import FakeGraphReader
from tests.recording_reader import RecordingGraphReader, TagConceptReader
from utils.graph_reader import TAG_CANDIDATES, TAG_CONCEPTS

CASES = [("u1", ["30"]), ("u1", []), ("p2", ["60"]), ("p6", []), ("p8", []), ("p8", ["150"]), ("solo", [])]
SEEDS = [{"appid": "40", "weight": 1.0}]


def arguments(player_id, hidden, weight=math.log1p, concept_limit=30, evidence_limit=3):
    """All visible games as seeds, as collect_path_scores() passes them."""
    played = FakeGraphReader().played_games(player_id, hidden)
    seeds = [{"appid": game["appid"], "weight": weight(game["hours"])} for game in played]
    return seeds, [game["appid"] for game in played], concept_limit, evidence_limit


class CannedReader(RecordingGraphReader):
    """Answers each statement with the next of the given answers and keeps the parameters."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)
        self.params = []

    def query(self, cypher, **params):
        self.statements.append(cypher)
        self.params.append(params)
        return self.answers.pop(0)


def concept_row(concept, weight):
    return {
        "concept": concept,
        "weight": weight,
        "seed_appid": "10",
        "seed_name": "Alpha",
        "name": concept.upper(),
        "name_ja": None,
        "tag_ids": [f"genre:{concept}", f"usertag:{concept}"],
    }


def test_the_concept_table_with_equal_seed_weights_orders_seeds_by_appid_as_a_string():
    # p8's games as seeds with w_s = 1, as under the default scheme: Mike(130) comes before India(90).
    seeds = [{"appid": appid, "weight": 1.0} for appid in sorted(FakeGraphReader().games_by_player["p8"], key=int)]
    table = FakeGraphReader().concept_table(seeds, 30)
    assert [
        (row["concept"], [holder["appid"] for holder in row["holders"]], row["seed_appid"], row["document_frequency"])
        for row in table
    ] == [
        ("strategy", ["80", "90"], "80", 2),
        ("indie", ["130", "90"], "130", 4),
        ("puzzle", ["130", "90"], "130", 4),
        ("multiplayer", ["90"], "90", 2),
        ("relaxing", ["90"], "90", 2),
        ("simulation", ["90"], "90", 2),
        ("open_world", ["90"], "90", 3),
        ("rpg", ["90"], "90", 3),
        ("shooter", ["90"], "90", 3),
        ("action", ["90"], "90", 6),
    ]
    for row in table:
        assert row["games"] == 22
        assert row["weight"] == len(row["holders"]) * math.log(22 / row["document_frequency"])
        assert row["seed_name"] == row["holders"][0]["name"]


@pytest.mark.parametrize("player_id,hidden", CASES)
@pytest.mark.parametrize("weight", [math.log1p, lambda hours: 1.0], ids=["log_hours", "uniform"])
def test_the_concept_table_equals_the_formula(player_id, hidden, weight):
    seeds = [{"appid": "999", "weight": 3.0}] + arguments(player_id, hidden, weight)[0]
    for concept_limit in (1, 3, 30):
        reader = TagConceptReader(FakeGraphReader())
        assert reader.tag_concepts(seeds, concept_limit) == FakeGraphReader().tag_concepts(seeds, concept_limit)
        assert reader.statements == [TAG_CONCEPTS]


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_scores_equal_the_formula(player_id, hidden):
    args = arguments(player_id, hidden)
    reader = TagConceptReader(FakeGraphReader())
    assert reader.tag_concept_scores(*args) == FakeGraphReader().tag_concept_scores(*args)
    assert reader.statements == [TAG_CONCEPTS, TAG_CANDIDATES]


@pytest.mark.parametrize("player_id,hidden", [("p6", []), ("p8", ["150"])])
def test_uniform_weights_and_other_limits_equal_the_formula(player_id, hidden):
    # With w_s = 1 terms tie often: ties go by concept, equal scores by appid.
    for concept_limit in (1, 3, 30):
        for evidence_limit in (0, 1, 3, 20):
            args = arguments(player_id, hidden, lambda hours: 1.0, concept_limit, evidence_limit)
            expected = FakeGraphReader().tag_concept_scores(*args)
            assert TagConceptReader(FakeGraphReader()).tag_concept_scores(*args) == expected


def test_seeds_outside_played_appids_and_unknown_seeds_equal_the_formula():
    # All of u1's games are seeds but only Alpha is visible, so the other three are candidates too.
    seeds = [{"appid": "999", "weight": 3.0}] + arguments("u1", [])[0]
    for played_appids in (["10"], []):
        args = (seeds, played_appids, 30, 3)
        rows = TagConceptReader(FakeGraphReader()).tag_concept_scores(*args)
        assert rows == FakeGraphReader().tag_concept_scores(*args)
        assert {"20", "30", "70"} <= {row["appid"] for row in rows}


def test_without_concepts_the_candidates_are_not_read():
    reader = TagConceptReader(FakeGraphReader())
    assert reader.tag_concept_scores([], ["10"], 30, 3) == []
    assert reader.tag_concept_scores([{"appid": "140", "weight": 1.0}], [], 30, 3) == []  # November has no tags
    assert reader.tag_concept_scores(SEEDS, [], 30, 3, []) == []
    assert reader.statements == [TAG_CONCEPTS, TAG_CONCEPTS]


def test_a_concept_table_passed_in_is_not_read_again():
    reader = CannedReader([{"appids": ["10"], "matched": [[0]], "concept_counts": [1]}])
    [row] = reader.tag_concept_scores(SEEDS, [], 30, 3, [concept_row("a", 2.0)])
    assert reader.statements == [TAG_CANDIDATES]
    assert (row["appid"], row["score"], row["matched"]) == ("10", 2.0, ["a"])


class TagPathReader(TagConceptReader):
    """Records and answers the tag statements as TagConceptReader; the other path queries come from the fake."""

    def played_games(self, player_id, hide_appids):
        return self.graph.played_games(player_id, hide_appids)

    def co_play_scores(self, *args):
        return self.graph.co_play_scores(*args)

    def studio_scores(self, *args):
        return self.graph.studio_scores(*args)

    def game_popularity(self, *args):
        return self.graph.game_popularity(*args)


@pytest.mark.parametrize(
    "scheme", [SeedScheme(), SeedScheme(weighting="uniform", peer_limit=None)], ids=["log_hours", "default"]
)
@pytest.mark.parametrize(
    "player_id,hidden,statements",
    [("u1", ["30"], [TAG_CONCEPTS, TAG_CANDIDATES]), ("p8", [], [TAG_CONCEPTS, TAG_CANDIDATES]), ("p7", ["40"], [])],
)
def test_collect_path_scores_reads_the_concept_table_once(scheme, player_id, hidden, statements):
    """collect_path_scores passes the table it read for the tag reason on to tag_concept_scores, so the tag
    path runs TAG_CONCEPTS and TAG_CANDIDATES once each (none without seeds: p7 hides its only game)."""
    reader = TagPathReader(FakeGraphReader())
    path_scores = collect_path_scores(reader, player_id, frozenset(hidden), scheme)
    assert reader.statements == statements
    assert path_scores == collect_path_scores(FakeGraphReader(), player_id, frozenset(hidden), scheme)


def test_a_candidate_is_built_from_the_concept_rows_it_matched():
    concepts = [concept_row("b", 2.0), concept_row("a", 2.0), concept_row("c", 1.0)]
    columns = {"appids": ["20", "10", "30"], "matched": [[2, 0], [1, 0, 2], [1]], "concept_counts": [4, 4, 1]}
    reader = CannedReader(concepts, [columns])
    rows = reader.tag_concept_scores(SEEDS, ["40"], 30, 2)
    assert reader.params[1]["tag_ids"] == [concept["tag_ids"] for concept in concepts]
    # term = weight / sqrt(concept_count); the evidence is the first 2 by (-term, concept)
    assert [(row["appid"], row["score"], row["concept_count"], row["match_count"]) for row in rows] == [
        ("10", 2.5, 4, 3),
        ("30", 2.0, 1, 1),
        ("20", 1.5, 4, 2),
    ]
    assert [(match["concept"], match["term"]) for match in rows[0]["matches"]] == [("a", 1.0), ("b", 1.0)]
    assert [row["matched"] for row in rows] == [["a", "b", "c"], ["a"], ["b", "c"]]  # all, by (-term, concept)
    seed = {"seed_appid": "10", "seed_name": "Alpha"}
    assert rows[2]["matches"] == [
        {"concept": "b", "name": "B", "name_ja": None, "weight": 2.0, **seed, "term": 1.0},
        {"concept": "c", "name": "C", "name_ja": None, "weight": 1.0, **seed, "term": 0.5},
    ]


def test_terms_are_added_one_by_one_from_the_largest():
    """As Cypher's sum() after ORDER BY term DESC: 1e16 + 1 rounds back to 1e16, twice. Added from the
    smallest, or with Python's sum() (which compensates rounding since 3.12), the total is 1e16 + 2."""
    concepts = [concept_row("x", 1.0), concept_row("y", 1e16), concept_row("z", 1.0)]
    reader = CannedReader(concepts, [{"appids": ["10"], "matched": [[0, 1, 2]], "concept_counts": [1]}])
    [row] = reader.tag_concept_scores(SEEDS, [], 30, 3)
    assert row["score"] == 1e16
