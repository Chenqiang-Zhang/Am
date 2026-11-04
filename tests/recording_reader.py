import math
import random
import re
from collections import defaultdict

from utils.graph_reader import (
    CO_PLAY_CONTEXT,
    GAME_POPULARITY,
    TAG_CANDIDATES,
    TAG_CONCEPTS,
    TAG_MAX_RANK,
    GraphReader,
    co_play_count_statement,
)

# A sort key that is unique per row, so equally ranked rows always come back in the same order.
UNIQUE_SORT_KEYS = ("appid", "tag_id", "studio_id", "concept", "review_id", "player_id")


class RecordingGraphReader(GraphReader):
    """A GraphReader that records its Cypher instead of running it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def close(self) -> None:
        pass

    def query(self, cypher, **params):
        self.statements.append(cypher)
        return []


class StandInReader(RecordingGraphReader):
    """Answers some statements of GraphReader from a FakeGraphReader, as Neo4j would. `shuffle_seed`
    shuffles the rows and the order of the values in the columns."""

    def __init__(self, graph, shuffle_seed: int | None = None) -> None:
        super().__init__()
        self.graph = graph
        self.random = random.Random(shuffle_seed) if shuffle_seed is not None else None

    def shuffled(self, rows):
        rows = list(rows)
        if self.random is not None:
            self.random.shuffle(rows)
        return rows


class CoPlayReader(StandInReader):
    """Answers the statements of GraphReader.co_play_scores from the plays of the graph."""

    def query(self, cypher, **params):
        self.statements.append(cypher)
        if cypher == CO_PLAY_CONTEXT:
            return self.context(**params)
        words = len(params["seeds"][0]["units"])
        assert cypher == co_play_count_statement(words), cypher
        return [self.counts(words, **params)]

    def context(self, player_id, played_appids, appids):
        """One row with the known seeds, or none for an unknown player or without known seeds."""
        own = self.graph.games_by_player.get(player_id)
        seeds = [
            {
                "appid": appid,
                "name": self.graph.games[appid]["name"],
                "degree": len(self.graph.players_by_game[appid]),
                "mine": appid in own,
            }
            for appid in appids
            if own and appid in self.graph.games
        ]
        if not seeds:
            return []
        return [{"seeds": self.shuffled(seeds), "own_unseen": self.shuffled(own - set(played_appids))}]

    def counts(self, words, player_id, seeds, played_appids):
        """Per other player the units of the seeds they played add up, then per game those of its players."""
        played: dict[str, list[int]] = {}
        if self.graph.games_by_player.get(player_id):
            for seed_row in seeds:
                for peer in self.graph.players_by_game.get(seed_row["appid"], set()) - {player_id}:
                    played[peer] = [a + b for a, b in zip(played.get(peer, [0] * words), seed_row["units"])]
        counts: dict[str, list[int]] = {}
        for peer, units in played.items():
            for appid in self.graph.games_by_player[peer]:
                counts[appid] = [a + b for a, b in zip(counts.get(appid, [0] * words), units)]
        appids = self.shuffled(appid for appid in counts if appid not in played_appids)
        assert all(value < 2**63 for appid in appids for value in counts[appid]), "a Cypher integer overflows"
        return {
            "appids": appids,
            "degrees": [len(self.graph.players_by_game[appid]) for appid in appids],
            **{f"word{k}": [counts[appid][k] for appid in appids] for k in range(words)},
        }


class TagConceptReader(StandInReader):
    """Answers the statements of GraphReader.tag_concept_scores from the tags of the graph."""

    def query(self, cypher, **params):
        self.statements.append(cypher)
        if cypher == TAG_CONCEPTS:
            return self.shuffled(self.concepts(**params))
        assert cypher == TAG_CANDIDATES, cypher
        return [self.candidates(**params)]

    def concepts(self, seeds, max_rank, concept_limit):
        """The concept table, written apart from FakeGraphReader.concept_table: per concept the distinct
        (w_s, seed) of the known seeds that have it, by (-w_s, appid as a string), whose weights add up one by
        one in that order and whose first is the seed; df(c) and the number of games."""
        assert max_rank == TAG_MAX_RANK, "FakeGraphReader.concepts() uses TAG_MAX_RANK"
        holders = defaultdict(set)  # concept -> {(w_s, seed appid)}
        for seed in seeds:
            if seed["appid"] in self.graph.games:
                for concept in self.graph.concepts(seed["appid"]):
                    holders[concept].add((seed["weight"], seed["appid"]))
        games = len(self.graph.games)
        rows = []
        for concept, held in holders.items():
            ordered = sorted(held, key=lambda holder: (-holder[0], holder[1]))
            preference = 0.0
            for seed_weight, _ in ordered:
                preference += seed_weight
            document_frequency = sum(1 for appid in self.graph.games if concept in self.graph.concepts(appid))
            weight = preference * math.log(games / document_frequency)
            if weight > 0:
                via = ordered[0][1]
                name, name_ja = self.graph.concept_label(concept)
                tag_ids = {
                    tag_id
                    for appid in self.graph.games
                    for tag_id, kind, _, _ in self.graph.tag_edges(appid)
                    if kind != "feature" and tag_id.split(":", 1)[1] == concept
                }
                rows.append(
                    {
                        "concept": concept,
                        "weight": weight,
                        "seed_appid": via,
                        "seed_name": self.graph.games[via]["name"],
                        "name": name,
                        "name_ja": name_ja,
                        "tag_ids": sorted(tag_ids),
                        "holders": [
                            {"appid": appid, "name": self.graph.games[appid]["name"], "weight": seed_weight}
                            for seed_weight, appid in ordered
                        ],
                        "document_frequency": document_frequency,
                        "games": games,
                    }
                )
        rows.sort(key=lambda row: (-row["weight"], row["concept"]))
        return rows[:concept_limit]

    def candidates(self, tag_ids, played_appids, max_rank):
        """Per game outside played_appids the indices of the concepts whose tags it has, through an
        edge with rank <= max_rank or no rank."""
        matched = {}
        for appid in self.graph.games:
            if appid not in played_appids:
                own = {tag_id for tag_id, _, _, rank in self.graph.tag_edges(appid) if rank is None or rank <= max_rank}
                matched[appid] = [index for index, ids in enumerate(tag_ids) if own & set(ids)]
        appids = self.shuffled(appid for appid, indices in matched.items() if indices)
        return {
            "appids": appids,
            "matched": [self.shuffled(matched[appid]) for appid in appids],
            "concept_counts": [len(self.graph.concepts(appid)) for appid in appids],
        }


class PopularityReader(StandInReader):
    """Answers the statement of GraphReader.game_popularity from the plays of the graph."""

    def query(self, cypher, player_id, played_appids):
        self.statements.append(cypher)
        assert cypher == GAME_POPULARITY, cypher
        own = self.graph.games_by_player.get(player_id, set())
        appids = self.shuffled(appid for appid in self.graph.games if appid not in played_appids)
        players = [len(self.graph.players_by_game[appid]) - (1 if appid in own else 0) for appid in appids]
        return [{"appids": appids, "players": players}]


def order_by_clauses(cypher: str) -> list[str]:
    clauses = re.findall(r"ORDER BY (.+?)(?= LIMIT | WITH | RETURN | UNWIND | MATCH |$)", cypher)
    return [clause.strip() for clause in clauses]


def last_sort_key(clause: str) -> str:
    return clause.split(",")[-1].strip()
