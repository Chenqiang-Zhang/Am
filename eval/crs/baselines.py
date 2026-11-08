"""Retrievers that do not use the graph's structure, for the dialogue evaluation (E1_PLAN.md).

P: the most played games, ignoring every condition the user states. It only leaves out games already shown,
rejected or named as anchors, like every retriever. V (vector search) will be added here.
"""

from collections.abc import Iterable

from app.api.chat_state import Candidate, ChatState


class PopularityRetriever:
    """Retriever P. Popularity is the number of players, without the evaluated player's own plays."""

    def __init__(self, players_by_appid: dict[str, int]) -> None:
        self.players_by_appid = dict(players_by_appid)
        self._order = sorted(self.players_by_appid, key=lambda appid: (-self.players_by_appid[appid], appid))

    def excluding_player(self, played_appids: Iterable[str]) -> "PopularityRetriever":
        counts = dict(self.players_by_appid)
        for appid in set(played_appids):
            if appid in counts:
                counts[appid] -= 1
        return PopularityRetriever(counts)

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]:
        unavailable = set(state.unavailable_appids())
        ranked = []
        for appid in self._order:
            if len(ranked) == k:
                break
            if appid not in unavailable:
                ranked.append(Candidate(appid=appid, score=float(self.players_by_appid[appid])))
        return ranked
