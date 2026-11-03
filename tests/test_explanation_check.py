"""M4: eval/explanation_check.py, against an in-memory copy of the graph facts."""

import json
import sys

import pytest

import eval.evaluate
import eval.explanation_check as explanation_check
from app.api.models import Evidence
from app.api.static_recommend import recommend_games
from eval.explanation_check import check_evidence, check_recommendations, sample_players
from eval.rankers import build_graph_ranker, build_graph_v0_ranker
from tests.fake_graph import FakeGraphReader, tag_ja
from utils.graph_reader import TAG_MAX_RANK


class FakeFacts:
    """The facts eval.explanation_check.CypherFacts reads, computed from the fixture."""

    def __init__(self, reader: FakeGraphReader) -> None:
        self.reader = reader

    def played(self, player_id):
        return {appid: self.reader.games[appid]["name"] for appid in self.reader.games_by_player.get(player_id, ())}

    def co_players(self, player_id, first_appid, second_appid):
        return len(self.reader.others(player_id, first_appid) & self.reader.others(player_id, second_appid))

    def players(self, player_id, appid):
        return len(self.reader.others(player_id, appid))

    def merged_tag_names(self, appid, tag_key):
        names = set()
        for current_tag_id, kind, name, rank in self.reader.tag_edges(appid):
            if current_tag_id.endswith(":" + tag_key) and kind != "feature" and (rank is None or rank <= TAG_MAX_RANK):
                names |= {name, tag_ja(name)} - {None}
        return names

    def has_tag_named(self, appid, name):
        return any(
            tag_name == name and (rank is None or rank <= TAG_MAX_RANK)
            for _, _, tag_name, rank in self.reader.tag_edges(appid)
        )

    def studio_links(self, appid, wanted_studio_id):
        links = [(name, role) for current, name, role in self.reader.studio_edges(appid) if current == wanted_studio_id]
        relations = {"DEVELOPED_BY" if role == "developer" else "PUBLISHED_BY" for _, role in links}
        return (links[0][0] if links else None), relations

    def studio_relations_named(self, appid, name):
        return {
            "DEVELOPED_BY" if role == "developer" else "PUBLISHED_BY"
            for _, studio_name, role in self.reader.studio_edges(appid)
            if studio_name == name
        }

    def studio_titles(self, wanted_studio_id):
        return sum(
            1
            for appid in self.reader.games
            if any(current == wanted_studio_id for current, _, _ in self.reader.studio_edges(appid))
        )


READER = FakeGraphReader()
FACTS = FakeFacts(READER)
PLAYERS = ["u1", "p1", "p2", "p3", "p4", "p5", "p6", "solo"]
HIDDEN = {"u1": "30", "p2": "60", "p6": "90", "p4": "80"}


def assertions_for(item, appid="50", player_id="u1", hidden=frozenset({"30"})):
    visible = {key: value for key, value in FACTS.played(player_id).items() if key not in hidden}
    return {name: holds for name, holds, _ in check_evidence(FACTS, player_id, visible, appid, item)}


def test_current_recommender_evidence_is_fully_valid():
    reports = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["current"])
    report = reports["current"]
    assert report.failures == []
    assert report.validity == 1.0
    assert set(report.assertions) == {
        "seed_played",
        "co_players",
        "seed_has_tag",
        "game_has_tag",
        "seed_studio",
        "game_studio",
        "studio_titles",
        "players",
    }
    assert 0 < report.explained < report.recommendations  # small catalogue: some lists end with popular games


def test_v0_text_fails_where_the_proposal_says():
    report = check_recommendations(READER, FACTS, PLAYERS, HIDDEN, 10, ["v0"])["v0"]
    assert report.validity is not None and report.validity < 1.0
    assert {failure["assertion"] for failure in report.failures} == {"co_players", "seed_has_tag"}
    # The peer count is not the number of players of the named game: p1 and p2 played Alpha and Echo.
    echo = next(failure for failure in report.failures if (failure["player_id"], failure["appid"]) == ("u1", "50"))
    assert (echo["evidence"]["count"], echo["graph"]) == (1, 2)
    # The second and third tags need not belong to the named game: Alpha has no Multiplayer tag.
    india = next(failure for failure in report.failures if (failure["player_id"], failure["appid"]) == ("p1", "90"))
    assert india["explanation"] == "Because you played Alpha. Shared tags: Shooter, Action, Multiplayer"
    assert india["evidence"]["bridge_name"] == "Multiplayer"


def recommended_evidence(path):
    for item in recommend_games(READER, "u1", 13, "en", frozenset({"30"})):
        for evidence in item.evidence:
            if evidence.path == path:
                return item.appid, evidence
    raise AssertionError(path)


@pytest.mark.parametrize(
    "path,change,broken",
    [
        ("peer", {"count": 99}, "co_players"),
        ("peer", {"seed_appid": "30", "seed_name": "Charlie"}, "seed_played"),  # the hidden game
        ("peer", {"seed_name": "Alpha 2"}, "seed_played"),
        ("tag", {"bridge_id": "strategy", "bridge_name": "Strategy"}, "seed_has_tag"),
        ("studio", {"seed_role": "publisher"}, "seed_studio"),
        ("studio", {"candidate_role": "publisher"}, "game_studio"),
        ("studio", {"bridge_name": "Valve Corp"}, "seed_studio"),
        ("studio", {"count": 7}, "studio_titles"),
        ("popular", {"count": 4}, "players"),
    ],
)
def test_tampered_evidence_is_caught(path, change, broken):
    appid, evidence = recommended_evidence(path)
    assert all(assertions_for(evidence, appid).values())
    assert not assertions_for(evidence.model_copy(update=change), appid)[broken]


def test_feature_tags_and_far_ranks_do_not_count_as_owned():
    # Charlie(30) carries user tag Action only at rank 11; Lima(120) only a Feature tag.
    action = Evidence(path="tag", seed_appid="10", seed_name="Alpha", bridge_id="action", bridge_name="Action")
    assert not assertions_for(action, appid="30", hidden=frozenset())["game_has_tag"]
    feature = Evidence(path="tag", seed_appid="10", seed_name="Alpha", bridge_id="single_player", bridge_name="Single-player")
    assert not assertions_for(feature, appid="120", hidden=frozenset())["game_has_tag"]


def test_sampling_matches_run_zero_of_the_evaluation():
    play_records = READER.all_play_records()
    players, hidden = sample_players(play_records, 4, 0)
    assert len(players) == 4 and set(hidden) == set(players)
    assert all(hidden[player_id] in READER.games_by_player[player_id] for player_id in players)
    assert sample_players(play_records, 4, 0) == (players, hidden)


def test_command_line_prints_and_writes_a_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(explanation_check, "GraphReader", lambda graph_name: FakeGraphReader())
    monkeypatch.setattr(explanation_check, "CypherFacts", FakeFacts)
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["explanation_check", "--users", "5", "--output", str(output)])
    explanation_check.main()
    printed = capsys.readouterr().out
    assert "current:" in printed and "v0:" in printed
    report = json.loads(output.read_text())
    assert report["versions"]["current"]["evidence_validity"] == 1.0
    assert set(report["versions"]) == {"current", "v0"}


def test_evaluate_runs_all_four_rankers(monkeypatch, capsys):
    monkeypatch.setattr(eval.evaluate, "GraphReader", lambda graph_name: FakeGraphReader())
    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate", "--users", "4", "--runs", "2", "--k", "3", "--rankers", "popularity", "item_knn", "graph_v0", "graph"],
    )
    eval.evaluate.main()
    printed = capsys.readouterr().out
    for name in ("popularity", "item_knn", "graph_v0", "graph"):
        assert f"\n{name}\n" in printed


def test_graph_rankers_take_weights_and_return_appids():
    peer_only = build_graph_ranker(READER, weights={"peer": 1.0})("u1", 3, {"30"})
    assert peer_only == [item.appid for item in recommend_games(READER, "u1", 3, "en", frozenset({"30"}), weights={"peer": 1.0})]
    assert len(build_graph_v0_ranker(READER)("u1", 3, {"30"})) == 3
