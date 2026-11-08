"""game_popularity reads the player's own games once and returns the players of every other game as
one columnar row; games_by_appids reads played_count from the degree store too."""

import pytest

from tests.fake_graph import FakeGraphReader
from tests.recording_reader import PopularityReader, RecordingGraphReader
from utils.graph_reader import GAME_POPULARITY

CASES = [("u1", ["30"]), ("u1", []), ("p8", ["150"]), ("solo", []), ("nobody", [])]


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_players_equal_the_formula(player_id, hidden):
    played_appids = [game["appid"] for game in FakeGraphReader().played_games(player_id, hidden)]
    reader = PopularityReader(FakeGraphReader())
    expected = FakeGraphReader().game_popularity(player_id, played_appids)
    assert reader.game_popularity(player_id, played_appids) == expected
    assert reader.statements == [GAME_POPULARITY]


def test_only_own_games_outside_played_appids_leave_the_player_out():
    # u1 played Alpha, Bravo, Charlie and Golf, and only Alpha is visible.
    graph = FakeGraphReader()
    players = {row["appid"]: row["players"] for row in PopularityReader(graph).game_popularity("u1", ["10"])}
    assert "10" not in players
    for appid in ("20", "30", "70"):
        assert players[appid] == len(graph.players_by_game[appid]) - 1
    assert players["40"] == len(graph.players_by_game["40"])


def test_games_by_appids_reads_played_count_from_the_degree_store():
    reader = RecordingGraphReader()
    assert reader.games_by_appids(["10"]) == []
    [statement] = reader.statements
    assert "COUNT { (g)<-[:PLAYED]-() } AS played_count" in statement
    assert "(:Player) | 1]) AS played_count" not in statement
