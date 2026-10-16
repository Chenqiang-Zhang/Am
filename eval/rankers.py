from collections import defaultdict
from typing import Callable

import numpy as np

from app.api.static_recommend import recommend_games
from utils.graph_reader import GraphReader

PlayRecord = tuple[str, str, float]
Ranker = Callable[[str, int, set[str]], list[str]]


def group_appids_by_player(play_records: list[PlayRecord]) -> dict[str, set[str]]:
    appids_by_player: dict[str, set[str]] = defaultdict(set)
    for player_id, appid, _ in play_records:
        appids_by_player[player_id].add(appid)
    return appids_by_player


def build_popularity_ranker(play_records: list[PlayRecord]) -> Ranker:
    appids_by_player = group_appids_by_player(play_records)
    player_counts: dict[str, int] = defaultdict(int)
    for _, appid, _ in play_records:
        player_counts[appid] += 1
    appids_by_popularity = sorted(player_counts, key=lambda appid: player_counts[appid], reverse=True)

    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        visible_appids = appids_by_player[player_id] - hidden_appids
        return [appid for appid in appids_by_popularity if appid not in visible_appids][:top_k]

    return rank


def build_item_knn_ranker(play_records: list[PlayRecord]) -> Ranker:
    appids_by_player = group_appids_by_player(play_records)
    player_ids = sorted(appids_by_player)
    appids = sorted({appid for _, appid, _ in play_records})
    player_row = {player_id: row for row, player_id in enumerate(player_ids)}
    appid_column = {appid: column for column, appid in enumerate(appids)}

    played_matrix = np.zeros((len(player_ids), len(appids)), dtype=np.float32)
    for player_id, appid, _ in play_records:
        played_matrix[player_row[player_id], appid_column[appid]] = 1.0
    co_play_counts = played_matrix.T @ played_matrix
    norms = np.sqrt(np.diag(co_play_counts))
    similarity = co_play_counts / np.maximum(np.outer(norms, norms), 1e-9)

    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        visible_appids = appids_by_player[player_id] - hidden_appids
        played_vector = np.zeros(len(appids), dtype=np.float32)
        for appid in visible_appids:
            played_vector[appid_column[appid]] = 1.0
        scores = played_vector @ similarity
        for appid in visible_appids:
            scores[appid_column[appid]] = -np.inf
        return [appids[column] for column in np.argsort(-scores)[:top_k]]

    return rank


def build_graph_ranker(reader: GraphReader) -> Ranker:
    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        recommendations = recommend_games(reader, player_id, top_k, "en", frozenset(hidden_appids))
        return [recommendation.appid for recommendation in recommendations]

    return rank
