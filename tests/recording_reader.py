import re

from utils.graph_reader import GraphReader

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


def order_by_clauses(cypher: str) -> list[str]:
    clauses = re.findall(r"ORDER BY (.+?)(?= LIMIT | WITH | RETURN | UNWIND | MATCH |$)", cypher)
    return [clause.strip() for clause in clauses]


def last_sort_key(clause: str) -> str:
    return clause.split(",")[-1].strip()
