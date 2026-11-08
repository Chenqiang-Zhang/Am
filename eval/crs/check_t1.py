"""Check retriever G's T1 filter against the live graph, independently of its Cypher.

Dumps every game's tag edges, developers and price with plain queries, decides in Python which games satisfy
each example state, and compares that set with what T1 returns (all of it, not only the top k). Also times T1.

    python -m eval.crs.check_t1            # inside the app container, GRAPH=ucsd
"""

import statistics
import time

from app.api.chat_queries import GraphRetriever
from app.api.chat_state import ChatState
from eval.crs.constraints import dump_games, satisfies
from utils.graph_reader import GraphReader

EXAMPLES: dict[str, ChatState] = {
    "ローグライク、ホラーは無理、10ドル以下": ChatState(include=["tag:rogue_like"], exclude=["tag:horror"], max_price=10),
    "オープンワールドのレース、5ドル以下": ChatState(include=["tag:open_world", "tag:racing"], max_price=5),
    "協力プレイ必須 → 予算20ドル": ChatState(include=["tag:co_op"], max_price=20),
    "難しすぎないメトロイドヴァニア": ChatState(include=["tag:metroidvania"], exclude=["tag:difficult"]),
    "アニメ調のターン制JRPG、20ドル以下": ChatState(include=["tag:anime", "tag:jrpg", "tag:turn_based"], max_price=20),
    "まったりできる街づくり、無料": ChatState(include=["tag:relaxing", "tag:city_builder"], max_price=0),
    "家で協力プレイ、ゾンビ以外": ChatState(include=["tag:local_co_op"], exclude=["tag:zombies"]),
    "Valve の開発、提示済みを除く": ChatState(include=["studio:valve"], shown=["730", "440"]),
    "ローグライク、5ドルより高い（価格帯への「いいえ」）": ChatState(include=["tag:rogue_like"], exclude=["price:5"]),
    "協力プレイ、5ドルより高く20ドル以下": ChatState(include=["tag:co_op"], min_price=5, max_price=20),
    "条件なし": ChatState(),
}
TIMING_RUNS = 5


def main() -> None:
    reader = GraphReader()
    retriever = GraphRetriever(reader)
    games = dump_games(reader)
    missing_keys = [
        key for state in EXAMPLES.values() for key in state.include + state.exclude
        if key not in retriever.vocabulary.attribute_keys
    ]
    if missing_keys:
        raise SystemExit(f"keys the graph does not have: {missing_keys}")

    print(f"games: {len(games)}")
    print(f"{'example':40s} {'expected':>8s} {'T1':>6s} {'extra':>5s} {'missed':>6s}  top 3")
    all_ok, timings = True, []
    for name, state in EXAMPLES.items():
        expected = {appid for appid, game in games.items() if satisfies(appid, game, state)}
        result = retriever.retrieve(state, k=len(games))
        got = {candidate.appid for candidate in result}
        extra, missed = got - expected, expected - got
        all_ok &= not extra and not missed
        for _ in range(TIMING_RUNS):
            start = time.perf_counter()
            retriever.retrieve(state, k=100)
            timings.append(time.perf_counter() - start)
        top = ", ".join(f"{candidate.appid}({candidate.score:.2f})" for candidate in result[:3])
        print(f"{name:40s} {len(expected):8d} {len(got):6d} {len(extra):5d} {len(missed):6d}  {top}")

    timings.sort()
    p95 = timings[min(len(timings) - 1, round(0.95 * len(timings)) - 1)]
    print(f"\nT1 latency over {len(timings)} calls: p50 {statistics.median(timings) * 1000:.0f} ms, "
          f"p95 {p95 * 1000:.0f} ms")
    print("ALL MATCH" if all_ok else "MISMATCH")
    reader.close()
    if not all_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
