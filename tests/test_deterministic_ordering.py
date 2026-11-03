"""M2: the same user always gets the same Top-K and the same reasons."""

import pytest

from app.api.static_recommend import recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
from tests.fake_graph import FakeGraphReader
from tests.recording_reader import UNIQUE_SORT_KEYS, RecordingGraphReader, last_sort_key, order_by_clauses

RECOMMENDERS = {"current": recommend_games, "v0": recommend_games_v0}


def summarize(recommendations):
    return [
        (
            item.appid,
            round(item.score, 12),
            item.explanation,
            [(reason.kind, reason.label) for reason in item.reasons],
            [evidence.model_dump() for evidence in item.evidence or []],
        )
        for item in recommendations
    ]


@pytest.mark.parametrize("version", sorted(RECOMMENDERS))
@pytest.mark.parametrize("lang", ["en", "ja"])
def test_three_runs_give_identical_top_k_and_reasons(version, lang):
    reader = FakeGraphReader()
    runs = [summarize(RECOMMENDERS[version](reader, "u1", 10, lang, frozenset({"30"}))) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0], "the fixture user should get recommendations"


@pytest.mark.parametrize("version", sorted(RECOMMENDERS))
@pytest.mark.parametrize("player_id,hidden", [("u1", {"30"}), ("p2", set()), ("solo", set())])
def test_result_does_not_depend_on_database_row_order(version, player_id, hidden):
    recommend = RECOMMENDERS[version]
    expected = summarize(recommend(FakeGraphReader(), player_id, 10, "en", frozenset(hidden)))
    for shuffle_seed in range(5):
        reader = FakeGraphReader(shuffle_seed=shuffle_seed)
        assert summarize(recommend(reader, player_id, 10, "en", frozenset(hidden))) == expected


class TiedReader(FakeGraphReader):
    """v0 queries where every candidate gets the same score, in descending appid order."""

    def games_sharing_tags(self, prefs, played_appids, limit):
        rows = []
        for appid in ["90", "80", "60", "110", "100"]:
            hit = {"tag": "Action", "tag_ja": None, "via": "Alpha", "weight": 1.0}
            rows.append(dict(self.game_row(appid), score=1.0, hits=[hit]))
        return rows

    def games_played_by_peers(self, player_id, seeds, played_appids, limit):
        return []

    def games_from_played_studios(self, played, played_appids, limit):
        return []


def test_v0_equal_scores_are_ranked_by_appid():
    recommendations = recommend_games_v0(TiedReader(), "u1", 10, "en")
    assert [item.appid for item in recommendations] == ["100", "110", "60", "80", "90"]


def test_v0_tag_reason_does_not_depend_on_hit_order():
    class TiedHitsReader(TiedReader):
        def games_sharing_tags(self, prefs, played_appids, limit):
            hits = [
                {"tag": name, "tag_ja": None, "via": "Alpha", "weight": 1.0}
                for name in ["Shooter", "Action", "Puzzle", "Indie"]
            ]
            return [dict(self.game_row("90"), score=4.0, hits=hits)]

    first = recommend_games_v0(TiedHitsReader(), "u1", 10, "en")[0]
    reader = TiedHitsReader()
    original = reader.games_sharing_tags
    reader.games_sharing_tags = lambda *args: [dict(row, hits=row["hits"][::-1]) for row in original(*args)]
    second = recommend_games_v0(reader, "u1", 10, "en")[0]
    assert first.explanation == second.explanation == "Because you played Alpha. Shared tags: Action, Indie, Puzzle"


SEEDS = [{"appid": "10", "weight": 1.0}]
RANKING_QUERIES = {
    "popular_games": lambda reader: reader.popular_games(10),
    "played_games": lambda reader: reader.played_games("u1", []),
    "tag_preferences": lambda reader: reader.tag_preferences([{"appid": "10", "hours": 1.0}], 10),
    "games_sharing_tags": lambda reader: reader.games_sharing_tags([], [], 10),
    "games_played_by_peers": lambda reader: reader.games_played_by_peers("u1", [], [], 10),
    "games_from_played_studios": lambda reader: reader.games_from_played_studios([], [], 10),
    "co_play_scores": lambda reader: reader.co_play_scores("u1", SEEDS, ["10"], 3),
    "tag_concept_scores": lambda reader: reader.tag_concept_scores(SEEDS, ["10"], 30, 3),
    "studio_scores": lambda reader: reader.studio_scores(SEEDS, ["10"], 1.0, 0.5, 3),
    "game_popularity": lambda reader: reader.game_popularity("u1", ["10"]),
}


@pytest.mark.parametrize("name", sorted(RANKING_QUERIES))
def test_every_order_by_in_ranking_queries_ends_with_a_unique_key(name):
    reader = RecordingGraphReader()
    RANKING_QUERIES[name](reader)
    clauses = [clause for statement in reader.statements for clause in order_by_clauses(statement)]
    assert clauses, f"{name} should sort its rows"
    for clause in clauses:
        assert any(key in last_sort_key(clause) for key in UNIQUE_SORT_KEYS), (name, clause)


@pytest.mark.parametrize(
    "name",
    [
        "tag_preferences",
        "games_played_by_peers",
        "games_from_played_studios",
        "co_play_scores",
        "tag_concept_scores",
        "studio_scores",
    ],
)
def test_head_or_slice_of_collect_is_preceded_by_an_explicit_order(name):
    reader = RecordingGraphReader()
    RANKING_QUERIES[name](reader)
    clauses = reader.statements[0].split(" WITH ")
    picking = [index for index, clause in enumerate(clauses) if "head(collect(" in clause or ")[..$" in clause]
    assert picking
    for index in picking:
        assert "ORDER BY" in clauses[index - 1], (name, clauses[index - 1], clauses[index])
