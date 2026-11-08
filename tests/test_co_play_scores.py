"""co_play_scores counts co(s, g) for batches of seeds in bit fields packed into integers and adds the
terms up in Python. Against a stand-in for Neo4j it must equal the formula, written pair by pair in
FakeGraphReader.co_play_scores, for every batch size and word size."""

import math
import random

import pytest

import utils.graph_reader as graph_reader
from tests.fake_graph import FakeGraphReader
from tests.recording_reader import CoPlayReader
from utils.graph_reader import pack_fields

CASES = [("u1", ["30"]), ("u1", []), ("p2", ["60"]), ("p6", []), ("p8", []), ("p8", ["150"]), ("solo", [])]


def arguments(player_id, hidden, weight=math.log1p, evidence_limit=3):
    """All visible games as seeds, as with SeedScheme(peer_limit=None)."""
    played = FakeGraphReader().played_games(player_id, hidden)
    seeds = [{"appid": game["appid"], "weight": weight(game["hours"])} for game in played]
    return player_id, seeds, [game["appid"] for game in played], evidence_limit


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_scores_equal_the_formula(player_id, hidden):
    args = arguments(player_id, hidden)
    reader = CoPlayReader(FakeGraphReader())
    assert reader.co_play_scores(*args) == FakeGraphReader().co_play_scores(*args)
    assert len(reader.statements) == 2  # the context and one batch of counts


@pytest.mark.parametrize("player_id,hidden", [("p6", []), ("p8", ["150"])])
def test_uniform_weights_and_other_evidence_limits_equal_the_formula(player_id, hidden):
    # With w_s = 1 terms tie often (for p6, Golf(70) and Kilo(110) on Alpha): ties go by seed appid.
    for evidence_limit in (0, 1, 3, 20):
        args = arguments(player_id, hidden, weight=lambda hours: 1.0, evidence_limit=evidence_limit)
        assert CoPlayReader(FakeGraphReader()).co_play_scores(*args) == FakeGraphReader().co_play_scores(*args)


@pytest.mark.parametrize("seed_batch", [1, 2, 5, 11])
def test_every_batch_size_gives_the_same_scores(monkeypatch, seed_batch):
    args = arguments("p8", [])  # 12 seeds
    expected = CoPlayReader(FakeGraphReader()).co_play_scores(*args)
    monkeypatch.setattr(graph_reader, "SEED_BATCH", seed_batch)
    reader = CoPlayReader(FakeGraphReader())
    assert reader.co_play_scores(*args) == expected
    assert len(reader.statements) == 1 + math.ceil(12 / seed_batch)


@pytest.mark.parametrize("player_id,hidden", CASES)
def test_narrow_words_give_the_same_scores(monkeypatch, player_id, hidden):
    # Degrees of at most 7 make fields of at most 3 bits: 3-bit words hold one to three fields each.
    assert max(len(players) for players in FakeGraphReader().players_by_game.values()) <= 7
    args = arguments(player_id, hidden)
    expected = CoPlayReader(FakeGraphReader()).co_play_scores(*args)
    monkeypatch.setattr(graph_reader, "WORD_BITS", 3)
    reader = CoPlayReader(FakeGraphReader())
    assert reader.co_play_scores(*args) == expected
    if len(args[1]) > 1:
        assert "word1" in reader.statements[-1]


def test_pack_fields_puts_the_widest_field_first_into_the_first_word_with_room():
    assert pack_fields([3, 63, 5, 60]) == ([(1, 60), (0, 0), (2, 0), (1, 0)], 3)
    assert pack_fields([1]) == ([(0, 0)], 1)


def test_packed_fields_stay_inside_their_word_and_never_overlap():
    generator = random.Random(0)
    for _ in range(200):
        widths = [generator.randint(1, 17) for _ in range(generator.randint(1, 80))]
        places, words = pack_fields(widths)
        taken = [0] * words
        for (word, shift), width in zip(places, widths):
            field = ((1 << width) - 1) << shift
            assert field < 1 << graph_reader.WORD_BITS and not taken[word] & field
            taken[word] |= field
        assert all(taken)


def test_a_seed_outside_played_appids_is_not_paired_with_itself():
    # Bravo(20) is a seed but not visible, so it is also a candidate; only Alpha(10) links to it.
    args = ("u1", [{"appid": "10", "weight": 1.0}, {"appid": "20", "weight": 2.0}], ["10"], 3)
    rows = {row["appid"]: row for row in CoPlayReader(FakeGraphReader()).co_play_scores(*args)}
    assert [(seed["appid"], seed["co_players"]) for seed in rows["20"]["seeds"]] == [("10", 1)]
    assert rows["20"]["seed_count"] == 1
    assert list(rows.values()) == FakeGraphReader().co_play_scores(*args)


def test_seeds_the_player_did_not_play_keep_all_their_players():
    # u1 played none of these seeds, so deg(s) is their whole degree; Alpha(10) is u1's and loses u1.
    graph = FakeGraphReader()
    others = sorted(appid for appid, players in graph.players_by_game.items() if players and "u1" not in players)
    seeds = [{"appid": appid, "weight": 1.0} for appid in others] + [{"appid": "10", "weight": 2.0}]
    args = ("u1", seeds, ["10"], len(seeds))
    rows = CoPlayReader(FakeGraphReader()).co_play_scores(*args)
    assert rows == FakeGraphReader().co_play_scores(*args)
    seed_players = {seed["appid"]: seed["seed_players"] for row in rows for seed in row["seeds"]}
    assert seed_players == {appid: len(graph.players_by_game[appid]) - (appid == "10") for appid in others + ["10"]}


def test_unknown_players_unknown_seeds_and_no_seeds():
    _, seeds, played_appids, _ = arguments("u1", [])
    reader = CoPlayReader(FakeGraphReader())
    assert reader.co_play_scores("nobody", seeds, played_appids, 3) == []
    assert len(reader.statements) == 1
    with_unknown = seeds + [{"appid": "999", "weight": 1.0}]
    assert reader.co_play_scores("u1", with_unknown, played_appids, 3) == reader.co_play_scores(
        "u1", seeds, played_appids, 3
    )
    reader.statements.clear()
    assert reader.co_play_scores("u1", [], played_appids, 3) == []
    assert reader.statements == []
