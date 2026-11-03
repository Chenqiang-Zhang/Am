from collections import defaultdict
from typing import Callable, Mapping

import numpy as np

from app.api.static_recommend import DEFAULT_SEED_SCHEME, SeedScheme, recommend_games
from app.api.static_recommend_v0 import recommend_games_v0
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

    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        own_appids = appids_by_player[player_id]
        visible_appids = own_appids - hidden_appids
        counts_without_player: dict[str, int] = {}
        for appid, count in player_counts.items():
            if appid in own_appids:
                count -= 1
            counts_without_player[appid] = count
        appids_by_popularity = sorted(
            counts_without_player, key=lambda appid: counts_without_player[appid], reverse=True
        )
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
    play_counts = np.diag(co_play_counts).copy()

    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        own_vector = played_matrix[player_row[player_id]]
        visible_appids = appids_by_player[player_id] - hidden_appids
        visible_columns = [appid_column[appid] for appid in visible_appids]
        norms = np.sqrt(play_counts - own_vector)
        rows = co_play_counts[visible_columns] - np.outer(own_vector[visible_columns], own_vector)
        similarity = rows / np.maximum(np.outer(norms[visible_columns], norms), 1e-9)
        scores = similarity.sum(axis=0)
        scores[visible_columns] = -np.inf
        return [appids[column] for column in np.argsort(-scores)[:top_k]]

    return rank


def build_graph_ranker(
    reader: GraphReader, weights: Mapping[str, float] | None = None, scheme: SeedScheme = DEFAULT_SEED_SCHEME
) -> Ranker:
    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        recommendations = recommend_games(
            reader, player_id, top_k, "en", frozenset(hidden_appids), weights=weights, scheme=scheme
        )
        return [recommendation.appid for recommendation in recommendations]

    return rank


def build_graph_v0_ranker(reader: GraphReader) -> Ranker:
    """The scoring before M3, for the v0 row of the comparison."""

    def rank(player_id: str, top_k: int, hidden_appids: set[str]) -> list[str]:
        recommendations = recommend_games_v0(reader, player_id, top_k, "en", frozenset(hidden_appids))
        return [recommendation.appid for recommendation in recommendations]

    return rank
