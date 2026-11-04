import random
import re

from utils.graph_reader import CO_PLAY_CONTEXT, GraphReader, co_play_count_statement

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


class CoPlayReader(RecordingGraphReader):
    """Answers the statements of GraphReader.co_play_scores from the plays of a FakeGraphReader, as
    Neo4j would. `shuffle_seed` shuffles the rows and the order of the candidates in the columns."""

    def __init__(self, graph, shuffle_seed: int | None = None) -> None:
        super().__init__()
        self.graph = graph
        self.random = random.Random(shuffle_seed) if shuffle_seed is not None else None

    def query(self, cypher, **params):
        self.statements.append(cypher)
        if cypher == CO_PLAY_CONTEXT:
            return self.shuffled(self.context(**params))
        words = len(params["seeds"][0]["units"])
        assert cypher == co_play_count_statement(words), cypher
        return [self.counts(words, **params)]

    def shuffled(self, rows):
        rows = list(rows)
        if self.random is not None:
            self.random.shuffle(rows)
        return rows

    def context(self, player_id, played_appids, appids):
        own = self.graph.games_by_player.get(player_id)
        if not own:
            return []
        own_unseen = self.shuffled(own - set(played_appids))
        return [
            {
                "appid": appid,
                "name": self.graph.games[appid]["name"],
                "degree": len(self.graph.players_by_game[appid]),
                "mine": appid in own,
                "own_unseen": own_unseen,
            }
            for appid in appids
            if appid in self.graph.games
        ]

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


def order_by_clauses(cypher: str) -> list[str]:
    clauses = re.findall(r"ORDER BY (.+?)(?= LIMIT | WITH | RETURN | UNWIND | MATCH |$)", cypher)
    return [clause.strip() for clause in clauses]


def last_sort_key(clause: str) -> str:
    return clause.split(",")[-1].strip()
