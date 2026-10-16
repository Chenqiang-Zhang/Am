import argparse
import random
from collections import Counter

from utils.graph_reader import GraphReader

from .metrics import coverage_at_k, diversity_at_k, hit_rate_at_k, ndcg_at_k, novelty_at_k
from .rankers import (
    build_graph_ranker,
    build_item_knn_ranker,
    build_popularity_ranker,
    group_appids_by_player,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--k", type=int, nargs="+", default=[10, 20, 50])
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    reader = GraphReader()
    play_records = reader.all_play_records()
    tag_ids_by_game = reader.tag_ids_by_game()
    appids_by_player = group_appids_by_player(play_records)
    player_counts = Counter(appid for _, appid, _ in play_records)

    random_generator = random.Random(args.seed)
    eligible_players = sorted(player_id for player_id, appids in appids_by_player.items() if len(appids) >= 2)
    players = random_generator.sample(eligible_players, min(args.users, len(eligible_players)))
    hidden_appid = {player_id: random_generator.choice(sorted(appids_by_player[player_id])) for player_id in players}

    rankers = {
        "popularity": build_popularity_ranker(play_records),
        "item_knn": build_item_knn_ranker(play_records),
        "graph": build_graph_ranker(reader),
    }
    largest_k = max(args.k)
    for ranker_name, ranker in rankers.items():
        ranked_by_player = {
            player_id: ranker(player_id, largest_k, {hidden_appid[player_id]}) for player_id in players
        }
        print(ranker_name)
        for k in args.k:
            hit_rate = sum(hit_rate_at_k(ranked_by_player[p], hidden_appid[p], k) for p in players) / len(players)
            ndcg = sum(ndcg_at_k(ranked_by_player[p], hidden_appid[p], k) for p in players) / len(players)
            diversity = sum(diversity_at_k(ranked_by_player[p], tag_ids_by_game, k) for p in players) / len(players)
            novelty = sum(
                novelty_at_k(ranked_by_player[p], player_counts, len(appids_by_player), k) for p in players
            ) / len(players)
            coverage = coverage_at_k(list(ranked_by_player.values()), len(tag_ids_by_game), k)
            print(
                f"  K={k}: HR={hit_rate:.4f} NDCG={ndcg:.4f} coverage={coverage:.4f} "
                f"diversity={diversity:.4f} novelty={novelty:.4f}"
            )
    reader.close()


if __name__ == "__main__":
    main()
