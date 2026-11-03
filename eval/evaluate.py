import argparse
import random
import statistics
from collections import Counter, defaultdict

from utils.graph_reader import GraphReader

from .metrics import coverage_at_k, diversity_at_k, hit_rate_at_k, ndcg_at_k, novelty_at_k
from .rankers import (
    build_graph_ranker,
    build_graph_v0_ranker,
    build_item_knn_ranker,
    build_popularity_ranker,
    group_appids_by_player,
)

METRIC_NAMES = ["HR", "NDCG", "coverage", "diversity", "novelty"]
RANKER_NAMES = ["popularity", "item_knn", "graph_v0", "graph"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--k", type=int, nargs="+", default=[10, 20, 50])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--rankers",
        nargs="+",
        choices=RANKER_NAMES,
        default=["popularity", "item_knn", "graph"],
        help="graph_v0 is the scoring before M3",
    )
    args = parser.parse_args()

    reader = GraphReader("ucsd")
    play_records = reader.all_play_records()
    tag_ids_by_game = reader.tag_ids_by_game()
    appids_by_player = group_appids_by_player(play_records)
    player_counts = Counter(appid for _, appid, _ in play_records)
    eligible_players = sorted(player_id for player_id, appids in appids_by_player.items() if len(appids) >= 2)

    builders = {
        "popularity": lambda: build_popularity_ranker(play_records),
        "item_knn": lambda: build_item_knn_ranker(play_records),
        "graph_v0": lambda: build_graph_v0_ranker(reader),
        "graph": lambda: build_graph_ranker(reader),
    }
    rankers = {name: builders[name]() for name in RANKER_NAMES if name in args.rankers}
    largest_k = max(args.k)
    values_by_setting = defaultdict(list)

    for run in range(args.runs):
        random_generator = random.Random(args.seed + run)
        players = random_generator.sample(eligible_players, min(args.users, len(eligible_players)))
        hidden_appid = {
            player_id: random_generator.choice(sorted(appids_by_player[player_id])) for player_id in players
        }
        for ranker_name, ranker in rankers.items():
            ranked_by_player = {
                player_id: ranker(player_id, largest_k, {hidden_appid[player_id]}) for player_id in players
            }
            for k in args.k:
                hit_rate = sum(hit_rate_at_k(ranked_by_player[p], hidden_appid[p], k) for p in players) / len(players)
                ndcg = sum(ndcg_at_k(ranked_by_player[p], hidden_appid[p], k) for p in players) / len(players)
                diversity = sum(
                    diversity_at_k(ranked_by_player[p], tag_ids_by_game, k) for p in players
                ) / len(players)
                novelty = sum(
                    novelty_at_k(ranked_by_player[p], player_counts, len(appids_by_player), k) for p in players
                ) / len(players)
                coverage = coverage_at_k(list(ranked_by_player.values()), len(tag_ids_by_game), k)
                run_values = {
                    "HR": hit_rate,
                    "NDCG": ndcg,
                    "coverage": coverage,
                    "diversity": diversity,
                    "novelty": novelty,
                }
                for metric_name in METRIC_NAMES:
                    values_by_setting[(ranker_name, k, metric_name)].append(run_values[metric_name])
        print(f"run {run + 1}/{args.runs} done")

    print(f"users={args.users} runs={args.runs} (mean ± standard deviation)")
    for ranker_name in rankers:
        print(ranker_name)
        for k in args.k:
            parts = []
            for metric_name in METRIC_NAMES:
                values = values_by_setting[(ranker_name, k, metric_name)]
                mean = statistics.mean(values)
                deviation = 0.0
                if len(values) >= 2:
                    deviation = statistics.stdev(values)
                parts.append(f"{metric_name}={mean:.4f}±{deviation:.4f}")
            print(f"  K={k}: " + " ".join(parts))
    reader.close()


if __name__ == "__main__":
    main()
