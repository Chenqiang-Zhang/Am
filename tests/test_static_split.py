"""F4: eval/static_split.py on synthetic play records."""

import hashlib
import json
import random
import sys
from collections import Counter

import pytest

from eval import static_split
from eval.static_split import (
    STRATA,
    assign_players,
    build_split,
    cases,
    dumps,
    load_split,
    players_by_stratum,
    proportional_allocation,
    stratum,
)

SALT = "static-rec-eval-v1"
SMALL = {
    "validation_size": 150,
    "test_sample_sizes": {"1": None, "2-3": None, "4-10": 90, "11+": 60},
    "subsets": {
        "v0": ("per_stratum", 40),
        "explanations": ("per_stratum", 6),
        "beyond_accuracy": ("proportional", 100),
    },
}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def synthetic_records(players=1500, seed=7):
    """Players with 1 to 40 games, so that every stratum and the ineligible players occur."""
    rng = random.Random(seed)
    records = []
    for index in range(players):
        player_id = sha(f"player {index}")[:32]
        games = rng.choice([1, 2, 2, 3, 4, 5, 6, 9, 11, 12, 15, 40])
        for appid in rng.sample(range(10, 900), games):
            records.append((player_id, str(appid), round(rng.uniform(0, 300), 1)))
    return records


RECORDS = synthetic_records()
GAMES = {}
for _player_id, _appid, _ in RECORDS:
    GAMES.setdefault(_player_id, set()).add(_appid)
ELIGIBLE = sorted(
    (player_id for player_id, appids in GAMES.items() if len(appids) >= 2),
    key=lambda player_id: sha(f"{SALT}:user:{player_id}"),
)
SPLIT = build_split(RECORDS, **SMALL)
PARTS = assign_players(GAMES, SMALL["validation_size"], SMALL["test_sample_sizes"], SMALL["subsets"])


def expected_stratum(player_id):
    visible = len(GAMES[player_id]) - 1
    return "1" if visible == 1 else "2-3" if visible <= 3 else "4-10" if visible <= 10 else "11+"


def ids(records_by_stratum):
    return [record["player_id"] for name in STRATA for record in records_by_stratum[name]]


def test_strata_follow_the_visible_history_length():
    visible_games = (1, 2, 3, 4, 10, 11, 3054)
    assert [stratum(visible) for visible in visible_games] == ["1", "2-3", "2-3", "4-10", "4-10", "11+", "11+"]
    with pytest.raises(ValueError):
        stratum(0)


def test_the_synthetic_population_covers_every_stratum_and_has_ineligible_players():
    assert set(Counter(expected_stratum(player_id) for player_id in ELIGIBLE)) == set(STRATA)
    assert len(ELIGIBLE) < len(GAMES)


def test_the_split_does_not_depend_on_the_order_of_the_records():
    shuffled = list(RECORDS) + RECORDS[:50]  # repeated records too
    random.Random(1).shuffle(shuffled)
    assert dumps(build_split(shuffled, **SMALL)) == dumps(SPLIT)
    assert dumps(build_split(list(reversed(RECORDS)), **SMALL)) == dumps(SPLIT)


def test_validation_holds_the_smallest_user_keys_and_test_the_rest():
    validation = ids(SPLIT["validation"])
    pool = [player_id for name in STRATA for player_id in PARTS["test_pool"][name]]
    assert set(validation) == set(ELIGIBLE[:150])
    assert set(pool) == set(ELIGIBLE[150:])
    assert not set(validation) & set(pool)
    assert not set(validation) & set(ids(SPLIT["test_sample"]))
    for name in STRATA:
        listed = [record["player_id"] for record in SPLIT["validation"][name]]
        assert listed == [player_id for player_id in ELIGIBLE[:150] if expected_stratum(player_id) == name]
        pool = [player_id for player_id in ELIGIBLE[150:] if expected_stratum(player_id) == name]
        assert PARTS["test_pool"][name] == pool


def test_counts_per_stratum():
    counts = SPLIT["counts"]
    population = Counter(expected_stratum(player_id) for player_id in ELIGIBLE)
    validation = Counter(expected_stratum(player_id) for player_id in ELIGIBLE[:150])
    pool = Counter(expected_stratum(player_id) for player_id in ELIGIBLE[150:])
    for name in STRATA:
        assert counts["population"][name] == population[name]
        assert counts["validation"][name] == validation[name]
        assert counts["test_pool"][name] == pool[name]
        assert counts["population"][name] == counts["validation"][name] + counts["test_pool"][name]
    assert counts["population"]["all"] == len(ELIGIBLE)
    assert counts["validation"]["all"] == 150
    assert counts["test_sample"] == {
        "1": pool["1"], "2-3": pool["2-3"], "4-10": 90, "11+": 60, "all": pool["1"] + pool["2-3"] + 150
    }
    assert SPLIT["graph"] == {
        "name": "ucsd",
        "players": len(GAMES),
        "games": len({appid for appids in GAMES.values() for appid in appids}),
        "played": sum(len(appids) for appids in GAMES.values()),
        "played_sha256": SPLIT["graph"]["played_sha256"],
    }


def test_the_test_sample_takes_every_short_history_and_the_first_players_by_key():
    for name, size in SMALL["test_sample_sizes"].items():
        pool = PARTS["test_pool"][name]
        assert size is None or len(pool) > size  # the fixture has to cut 4-10 and 11+
        assert [record["player_id"] for record in SPLIT["test_sample"][name]] == pool[:size]


def test_each_rep_hides_the_next_game_by_hide_key():
    evaluations = {"validation": 0, "test_sample": 0}
    for part in evaluations:
        for name in STRATA:
            for record in SPLIT[part][name]:
                player_id, games = record["player_id"], GAMES[record["player_id"]]
                by_key = sorted(games, key=lambda appid: sha(f"{SALT}:hide:{player_id}:{appid}"))
                assert record["hidden"] == by_key[: min(3, len(games))]
                evaluations[part] += len(record["hidden"])
    assert SPLIT["counts"]["evaluations"] == evaluations
    reps = Counter(len(record["hidden"]) for name in STRATA for record in SPLIT["test_sample"][name])
    assert set(reps) == {2, 3}
    assert reps[2] == len(SPLIT["test_sample"]["1"])


def test_subsets_are_prefixes_of_the_test_sample():
    subsets = SPLIT["subsets"]
    for name in STRATA:
        sample = [record["player_id"] for record in SPLIT["test_sample"][name]]
        assert subsets["v0"][name] == sample[:40]
        assert subsets["explanations"][name] == sample[:6]
        assert subsets["beyond_accuracy"][name] == sample[: len(subsets["beyond_accuracy"][name])]
    pool = SPLIT["counts"]["test_pool"]
    beyond = SPLIT["counts"]["subsets"]["beyond_accuracy"]
    assert beyond["all"] == 100
    for name in STRATA:
        assert abs(beyond[name] - 100 * pool[name] / pool["all"]) < 1


def test_proportional_allocation_adds_up_and_rounds():
    pool = {"1": 2310, "2-3": 3853, "4-10": 9837, "11+": 47670}
    allocation = proportional_allocation(1000, pool)
    assert sum(allocation.values()) == 1000
    assert all(abs(allocation[name] - 1000 * pool[name] / 63670) < 1 for name in STRATA)
    # round() would give 0 everywhere here; the largest remainders, ties in stratum order, give 2.
    assert proportional_allocation(2, {name: 1 for name in STRATA}) == {"1": 1, "2-3": 1, "4-10": 0, "11+": 0}
    assert proportional_allocation(10, {name: 0 for name in STRATA}) == {name: 0 for name in STRATA}


def test_players_with_one_game_are_left_out():
    one_game = {player_id for player_id, appids in GAMES.items() if len(appids) == 1}
    listed = set(ids(SPLIT["validation"])) | {player_id for name in STRATA for player_id in PARTS["test_pool"][name]}
    assert one_game and not one_game & listed


def test_cases_list_every_rep_of_a_part_and_rep_0_of_a_subset():
    validation = cases(SPLIT, "validation")
    assert len(validation) == SPLIT["counts"]["evaluations"]["validation"]
    reps = Counter(player_id for player_id, _, _, _ in validation)
    for player_id, name, rep, appid in validation:
        assert name == expected_stratum(player_id) and appid in GAMES[player_id]
        assert rep < reps[player_id] == min(3, len(GAMES[player_id]))
    first_hidden = {
        record["player_id"]: record["hidden"][0] for name in STRATA for record in SPLIT["test_sample"][name]
    }
    explanations = cases(SPLIT, "explanations")
    assert [player_id for player_id, _, _, _ in explanations] == [
        player_id for name in STRATA for player_id in SPLIT["subsets"]["explanations"][name]
    ]
    assert all(rep == 0 and appid == first_hidden[player_id] for player_id, _, rep, appid in explanations)
    assert players_by_stratum(SPLIT, "test_sample") == {
        name: [record["player_id"] for record in SPLIT["test_sample"][name]] for name in STRATA
    }
    assert players_by_stratum(SPLIT, "v0") == SPLIT["subsets"]["v0"]


def test_the_default_sizes_are_the_plan():
    split = build_split(RECORDS)
    assert split["sizes"] == {
        "validation": 1000,
        "test_sample": {"1": None, "2-3": None, "4-10": 1500, "11+": 2500},
        "reps": 3,
        "subsets": {
            "v0": {"per_stratum": 500},
            "explanations": {"per_stratum": 75},
            "beyond_accuracy": {"proportional": 1000},
        },
    }
    assert split["counts"]["validation"]["all"] == 1000
    assert split["counts"]["test_sample"] == split["counts"]["test_pool"]  # this pool is smaller than the plan's caps
    assert split["salt"] == SALT


class RecordsReader:
    def __init__(self, graph_name):
        self.graph_name = graph_name

    def all_play_records(self):
        return list(RECORDS)

    def close(self):
        pass


def test_command_line_writes_the_same_bytes_twice(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(static_split, "GraphReader", RecordsReader)
    outputs = [tmp_path / "first" / "split.json", tmp_path / "second.json"]
    for output in outputs:
        monkeypatch.setattr(sys, "argv", ["static_split", "--output", str(output)])
        static_split.main()
    assert outputs[0].read_bytes() == outputs[1].read_bytes() == dumps(build_split(RECORDS)).encode()
    assert load_split(outputs[0]) == json.loads(dumps(build_split(RECORDS)))
    printed = capsys.readouterr().out
    assert "test_sample" in printed and "evaluations (user x rep)" in printed
