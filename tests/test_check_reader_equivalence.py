"""eval/check_reader_equivalence.py's main() on in-memory stand-ins for both readers: what it prints and
its exit code. The comparison itself runs on Neo4j in tests/test_cypher_integration.py."""

import re
import sys

import pytest
from neo4j.exceptions import TransientError

import eval.check_reader_equivalence as check_reader_equivalence
from app.api.static_recommend import DEFAULT_SEED_SCHEME, SeedScheme
from tests.fake_graph import FakeGraphReader

NOTHING = "; nothing compared for any player"


class StandIn(FakeGraphReader):
    """The fake as either reader. Its calls with a player in `failing` fail transiently, as Cypher with that
    $player_id may on a busy database. For a player in `different` game_popularity drops its last row;
    with `different_edge` tag_concept_scores does so without played_appids, which only an edge case asks."""

    def __init__(self, failing=(), different=(), different_edge=False):
        super().__init__()
        self.failing, self.different, self.different_edge = set(failing), set(different), different_edge

    def query(self, cypher, **params):
        if cypher in check_reader_equivalence.PRECONDITIONS.values():
            return [{"n": 0}]
        if cypher.startswith("MATCH (p:Player) RETURN p.player_id"):
            return [{"player_id": player, "games": len(appids)} for player, appids in self.games_by_player.items()]
        raise AssertionError(cypher)

    def check(self, player_id):
        if player_id in self.failing:
            raise TransientError("forced")

    def played_games(self, player_id, hide_appids):
        self.check(player_id)
        return super().played_games(player_id, hide_appids)

    def co_play_scores(self, player_id, *args):
        self.check(player_id)
        return super().co_play_scores(player_id, *args)

    def game_popularity(self, player_id, played_appids):
        self.check(player_id)
        rows = super().game_popularity(player_id, played_appids)
        return rows[:-1] if player_id in self.different else rows

    def tag_concept_scores(self, seeds, played_appids, concept_limit, evidence_limit, concepts=None):
        rows = super().tag_concept_scores(seeds, played_appids, concept_limit, evidence_limit, concepts)
        return rows[:-1] if self.different_edge and not played_appids else rows


def run(monkeypatch, capsys, *arguments, new=None, reference=None, limit=None):
    """main() with these arguments and stand-ins: (exit code, printed lines)."""
    monkeypatch.setattr(check_reader_equivalence, "GraphReader", lambda graph_name: new or StandIn())
    monkeypatch.setattr(check_reader_equivalence, "ReferenceReader", lambda graph_name: reference or StandIn())
    if limit is not None:
        monkeypatch.setattr(check_reader_equivalence, "REFERENCE_SEED_LIMIT", limit)
    monkeypatch.setattr(sys, "argv", ["check_reader_equivalence", *arguments])
    with pytest.raises(SystemExit) as exit_info:
        check_reader_equivalence.main()
    return exit_info.value.code, capsys.readouterr().out.splitlines()


def summary(lines):
    return next(line for line in lines if line.startswith("compared "))


def verdicts(lines, player_id):
    """How the player's line ends: same, DIFFERENT: <names>, <reader> failed: <code> or that it was only timed."""
    line = next(line for line in lines if line.split()[1:2] == [player_id])
    return re.search(r"(same|DIFFERENT: .*|(?:new|reference) failed: .*|\(no reference: timed only\))$", line)[1]


def test_equal_readers_pass_and_print_the_tag_path_as_one_median(monkeypatch, capsys):
    code, lines = run(monkeypatch, capsys, "--players", "u1", "p8")
    assert code == 0 and summary(lines).startswith("compared 2 players")
    assert (verdicts(lines, "u1"), verdicts(lines, "p8")) == ("same", "same")
    header = lines.index("median seconds over 2 players: reference -> new (not old-vs-new speed-ups)")
    medians = [line[2:22].rstrip() for line in lines[header + 1 :]]
    assert medians == list(check_reader_equivalence.MEDIANS) and "tag path" in medians and "tag_concepts" not in medians


@pytest.mark.parametrize(
    "reference,code,compared",
    [
        (StandIn(different={"p8"}), 1, 2),
        (StandIn(different={"p8"}, failing={"u1"}), 1, 1),  # a failure does not hide the mismatch
        (StandIn(different_edge=True), 1, 2),  # only an edge case differs
        (StandIn(failing={"u1"}), 0, 1),
        (StandIn(failing={"u1", "p8", "nobody"}), 2, 0),
    ],
    ids=["mismatch", "mismatch_and_a_failure", "edge_case_mismatch", "one_failure", "every_reference_fails"],
)
def test_a_mismatch_exits_1_and_a_run_without_a_comparison_2(monkeypatch, capsys, reference, code, compared):
    exit_code, lines = run(monkeypatch, capsys, "--players", "u1", "p8", reference=reference)
    assert exit_code == code and summary(lines).startswith(f"compared {compared} players")
    assert summary(lines).endswith(NOTHING) == (compared == 0)
    for player_id in ("u1", "p8"):
        if player_id in reference.failing:
            assert verdicts(lines, player_id).startswith("reference failed: ")
        elif player_id in reference.different:
            assert re.match(r"DIFFERENT: (.*, )?game_popularity(, |$)", verdicts(lines, player_id))
        elif reference.different_edge:
            assert verdicts(lines, player_id) == "DIFFERENT: no played_appids: tag_concept_scores"
        else:
            assert verdicts(lines, player_id) == "same"


def test_a_mismatch_of_the_unknown_player_exits_1(monkeypatch, capsys):
    code, lines = run(monkeypatch, capsys, "--players", "u1", "p8", reference=StandIn(different={"nobody"}))
    unknown = next(line for line in lines if line.startswith("unknown player: "))
    assert code == 1 and unknown.startswith("unknown player: DIFFERENT: ") and unknown.endswith("game_popularity")
    assert "1 with a mismatch" in summary(lines)


def test_the_tag_path_median_adds_both_tag_calls():
    seconds = {"co_play_scores": 0.5, "tag_concepts": 1.0, "tag_concept_scores": 2.0, "collect_path_scores": 4.0}
    assert check_reader_equivalence.median_terms(seconds) == {
        "co_play_scores": 0.5,
        "tag path": 3.0,
        "collect_path_scores": 4.0,
    }


def test_a_failure_of_the_new_reader_is_reported_and_counted(monkeypatch, capsys):
    code, lines = run(monkeypatch, capsys, "--players", "p8", "u1", new=StandIn(failing={"u1"}))
    assert code == 0 and verdicts(lines, "u1").startswith("new failed: ") and verdicts(lines, "p8") == "same"
    assert "0 where the reference failed, 1 where the new reader failed" in summary(lines)
    code, lines = run(monkeypatch, capsys, "--players", "u1", new=StandIn(failing={"u1"}))
    assert code == 2 and summary(lines).startswith("compared 0 players") and summary(lines).endswith(NOTHING)
    assert any(line.startswith("unknown player: new failed: ") for line in lines)  # its seeds are u1's games


class FailingInAnEdgeCase(StandIn):
    """The new reader failing transiently only in one of u1's edge cases: co-play without played_appids."""

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        if player_id == "u1" and not played_appids:
            raise TransientError("forced")
        return super().co_play_scores(player_id, seeds, played_appids, evidence_limit)


def test_a_failure_of_the_new_reader_in_an_edge_case_is_the_new_readers(monkeypatch, capsys):
    code, lines = run(monkeypatch, capsys, "--players", "u1", "p8", new=FailingInAnEdgeCase())
    assert code == 0 and verdicts(lines, "u1").startswith("new failed: ") and verdicts(lines, "p8") == "same"
    assert "0 where the reference failed, 1 where the new reader failed" in summary(lines)


def test_a_failing_precondition_query_compares_nothing(monkeypatch, capsys):
    class Busy(StandIn):
        def query(self, cypher, **params):
            raise TransientError("forced")

    code, lines = run(monkeypatch, capsys, "--players", "u1", new=Busy())
    assert code == 2 and lines[0].startswith("new failed: ") and lines[1].endswith(" players=0")
    assert summary(lines).endswith("1 where the new reader failed, 0 skipped for size (timed only)" + NOTHING)


def test_a_player_above_the_reference_limit_is_only_timed(monkeypatch, capsys):
    """p8 has 11 visible games: with --all-seeds and a limit of 5 the reference does not run for it."""
    code, lines = run(monkeypatch, capsys, "--players", "p8", "--all-seeds", limit=5)
    assert code == 0 and verdicts(lines, "p8") == "(no reference: timed only)"
    assert summary(lines) == (
        "compared 0 players (0 path query rows) and 5 edge cases: 0 with a mismatch, 0 where the reference "
        "failed, 0 where the new reader failed, 1 skipped for size (timed only)" + NOTHING
    )
    reference = StandIn(failing={"u1"})
    code, lines = run(monkeypatch, capsys, "--players", "u1", "p8", "--all-seeds", limit=5, reference=reference)
    assert code == 2 and "1 where the reference failed" in summary(lines) and "1 skipped for size" in summary(lines)


@pytest.mark.parametrize(
    "flags,scheme",
    [
        ([], SeedScheme()),
        (["--uniform"], SeedScheme(weighting="uniform")),
        (["--all-seeds"], SeedScheme(peer_limit=None)),
        (["--uniform", "--all-seeds"], DEFAULT_SEED_SCHEME),  # the shipped scheme
    ],
    ids=["log_hours_10", "uniform_10", "log_hours_all", "uniform_all"],
)
def test_the_flags_choose_the_seed_scheme(monkeypatch, capsys, flags, scheme):
    code, lines = run(monkeypatch, capsys, "--players", "u1", *flags)
    assert code == 0 and lines[2].startswith("graph=ucsd ") and f" scheme={scheme} players=1" in lines[2]
    assert verdicts(lines, "u1") == "same" and summary(lines).startswith("compared 1 players")
