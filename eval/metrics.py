import math
from itertools import combinations


def hit_rate_at_k(ranked_appids: list[str], hidden_appid: str, k: int) -> float:
    return 1.0 if hidden_appid in ranked_appids[:k] else 0.0


def ndcg_at_k(ranked_appids: list[str], hidden_appid: str, k: int) -> float:
    if hidden_appid not in ranked_appids[:k]:
        return 0.0
    return 1.0 / math.log2(ranked_appids.index(hidden_appid) + 2)


def coverage_at_k(rankings: list[list[str]], candidate_count: int, k: int) -> float:
    recommended_appids = {appid for ranked_appids in rankings for appid in ranked_appids[:k]}
    return len(recommended_appids) / candidate_count


def diversity_at_k(ranked_appids: list[str], tag_ids_by_game: dict[str, set[str]], k: int) -> float:
    appid_pairs = list(combinations(ranked_appids[:k], 2))
    if not appid_pairs:
        return 0.0
    total_distance = 0.0
    for first_appid, second_appid in appid_pairs:
        first_tags, second_tags = tag_ids_by_game[first_appid], tag_ids_by_game[second_appid]
        all_tags = first_tags | second_tags
        if all_tags:
            total_distance += 1 - len(first_tags & second_tags) / len(all_tags)
    return total_distance / len(appid_pairs)


def novelty_at_k(ranked_appids: list[str], player_counts: dict[str, int], player_total: int, k: int) -> float:
    top_appids = ranked_appids[:k]
    if not top_appids:
        return 0.0
    surprisals = [-math.log2(max(player_counts.get(appid, 0), 1) / player_total) for appid in top_appids]
    return sum(surprisals) / len(top_appids)
