"""Run the two questioners on the live graph: the whitelist, the first questions for example states, and timing.

    python -m eval.crs.check_questioner      # inside the app container, GRAPH=ucsd
"""

import statistics
import time

from app.api.chat_queries import GraphRetriever
from app.api.chat_questioner import EntropyQuestioner, FixedOrderQuestioner, entropy_table, load_catalog
from app.api.chat_state import ChatState
from utils.graph_reader import GraphReader

EXAMPLES: dict[str, ChatState] = {
    "条件なし": ChatState(),
    "インディー": ChatState(include=["tag:indie"]),
    "ローグライク、ホラーは無理": ChatState(include=["tag:rogue_like"], exclude=["tag:horror"]),
    "協力プレイ、20ドル以下": ChatState(include=["tag:co_op"], max_price=20),
    "アニメ調のターン制JRPG": ChatState(include=["tag:anime", "tag:jrpg", "tag:turn_based"]),
}


def main() -> None:
    reader = GraphReader()
    start = time.perf_counter()
    retriever = GraphRetriever(reader)
    catalog = load_catalog(reader)
    print(f"setup {time.perf_counter() - start:.1f} s, games {len(catalog.games)}, whitelist {len(catalog.keys)} "
          f"(tags {sum(k.startswith('tag:') for k in catalog.keys)}, "
          f"studios {sum(k.startswith('studio:') for k in catalog.keys)}, "
          f"prices {sum(k.startswith('price:') for k in catalog.keys)})")
    print("fixed order head:", ", ".join(catalog.keys[:12]))

    entropy, fixed = EntropyQuestioner(catalog), FixedOrderQuestioner(catalog)
    timings = []
    for name, state in EXAMPLES.items():
        pool = retriever.retrieve(state, k=len(catalog.games))
        begin = time.perf_counter()
        choice = entropy.choose(state, pool)
        timings.append(time.perf_counter() - begin)
        top = list(entropy_table(state, pool, catalog).items())[:3]
        print(f"\n{name}  pool={len(pool)}")
        print(f"  G        -> {choice}   top3 " + ", ".join(f"{key} {value:.2f}" for key, value in top))
        print(f"  G-static -> {fixed.choose(state, pool)}")
    print(f"\nentropy choice: p50 {statistics.median(timings) * 1000:.0f} ms, max {max(timings) * 1000:.0f} ms")
    reader.close()


if __name__ == "__main__":
    main()
