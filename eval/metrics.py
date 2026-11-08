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


# Conversational recommendation (eval/crs/E1_PLAN.md). A dialogue succeeds at `success_turn` (1-based) with the
# target at `success_rank` (1-based) in that turn's list, or fails with both None.


def success_by_turn(success_turn: int | None, turn: int) -> float:
    """SR@turn for one dialogue."""
    return 1.0 if success_turn is not None and success_turn <= turn else 0.0


def turns_taken(success_turn: int | None, max_turns: int) -> int:
    """The dialogue's term in AT: the success turn, or max_turns for a failure (as in SCPR)."""
    return success_turn if success_turn is not None else max_turns


def hdcg(success_turn: int | None, success_rank: int | None) -> float:
    """hDCG@(T,K) of UNICORN (eq. 20) for one target: 1 at turn 1, rank 1. T and K bound the turns and ranks seen."""
    if success_turn is None or success_rank is None:
        return 0.0
    turn_gain = 1 / math.log2(success_turn + 2)
    turn_step = 1 / math.log2(success_turn + 1) - turn_gain
    return turn_gain + turn_step / math.log2(success_rank + 1)
