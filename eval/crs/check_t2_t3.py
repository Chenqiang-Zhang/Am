"""Check retriever G's T2 (anchor similarity) and T3 (same developer, co-play) against the raw UCSD files,
independently of their Cypher.

Tags, developers and plays come from data/ucsd/games.jsonl and played.jsonl, counted in Python. The T1 part of
each case (tags, price, shown games) is decided with the graph dump that check_t1 already checks. For each case
the script compares the whole result set; for T2 also each game's IDF sum and the top of the ranking, and for
co-play each game's fan count and the top of the ranking.

    python -m eval.crs.check_t2_t3         # inside the app container, GRAPH=ucsd
"""

import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path

from app.api.chat_queries import (
    CO_PLAY_MIN_HOURS,
    POPULARITY_WEIGHT,
    GraphRetriever,
    co_play_lift,
    popularity_scale,
)
from app.api.chat_state import ChatState, tag_key
from eval.crs.constraints import dump_games, satisfies
from utils.graph_reader import TAG_MAX_RANK, GraphReader

UCSD = Path("data/ucsd")
SIMILAR_CASES: dict[str, ChatState] = {
    "Portal 2みたいなもの": ChatState(anchors=["620"]),
    "Terrariaみたいで、ストーリー重視": ChatState(anchors=["105600"], include=["tag:story_rich"]),
    "Stardew Valleyみたいで10ドル以下、ホラー以外": ChatState(
        anchors=["413150"], max_price=10, exclude=["tag:horror"]),
    "Portal 2とTerrariaみたいなもの": ChatState(anchors=["620", "105600"]),
}
STUDIO_CASES: dict[str, ChatState] = {
    "Portal 2と同じ開発元で10ドル以下": ChatState(anchors=["620"], relation="same_developer", max_price=10),
    "Portal 2と同じ開発元": ChatState(anchors=["620"], relation="same_developer"),
    "Portal 2と同じ開発元で協力プレイ": ChatState(anchors=["620"], relation="same_developer", include=["tag:co_op"]),
    "Terrariaと同じ開発元": ChatState(anchors=["105600"], relation="same_developer"),
    "Portal 2と同じ開発元、FPS以外": ChatState(anchors=["620"], relation="same_developer", exclude=["tag:fps"]),
    "Civilization Vと同じ開発元": ChatState(anchors=["8930"], relation="same_developer"),
    "開発元が空のアンカー（Portal Stories: VR）": ChatState(anchors=["446750"], relation="same_developer"),
}
# Each co-play case leaves out one fan of the anchor, as the evaluation does.
CO_PLAY_CASES: dict[str, ChatState] = {
    "Portal 2をよくやる人が遊んでいるもの": ChatState(anchors=["620"], relation="co_played"),
    "CS:GOをよくやる人が遊んでいるシングルプレイ": ChatState(
        anchors=["730"], relation="co_played", include=["tag:single_player"]),
    "Terrariaをよくやる人が遊んでいる20ドル以下": ChatState(anchors=["105600"], relation="co_played", max_price=20),
}
TOP = 20
TIMING_RUNS = 3
# T3 must return the whole of a case: no case here has more games than this.
ALL = 100_000


def raw_keys() -> dict[str, set[str]]:
    """Each game's counted tag groups (no rank, or rank within TAG_MAX_RANK) and developers, as T2 compares.
    Groups whose tags are all Steam features (kind "feature" in every game) are left out."""
    games = [json.loads(line) for line in (UCSD / "games.jsonl").open(encoding="utf-8")]
    kinds: dict[str, set[str]] = {}
    for game in games:
        for tag in game.get("tags") or []:
            kinds.setdefault(tag_key(tag["tag_id"]), set()).add(tag["tag_id"].split(":", 1)[0])
    feature_only = {key for key, found in kinds.items() if found == {"feature"}}
    keys = {}
    for game in games:
        keys[game["appid"]] = {
            tag_key(tag["tag_id"]) for tag in game.get("tags") or []
            if (tag.get("rank") is None or tag["rank"] <= TAG_MAX_RANK) and tag_key(tag["tag_id"]) not in feature_only
        } | {
            "studio:" + studio["studio_id"] for studio in game.get("studios") or []
            if studio.get("role") == "developer" and studio.get("studio_id")
        }
    return keys


def raw_developers() -> dict[str, set[str]]:
    developers = {}
    with (UCSD / "games.jsonl").open(encoding="utf-8") as file:
        for line in file:
            game = json.loads(line)
            developers[game["appid"]] = {
                studio["studio_id"] for studio in game.get("studios") or []
                if studio.get("role") == "developer" and studio.get("studio_id")
            }
    return developers


def raw_plays() -> dict[str, dict[str, float]]:
    """player_id -> {appid: hours}, keeping the larger hours if a pair repeats."""
    plays: dict[str, dict[str, float]] = {}
    with (UCSD / "played.jsonl").open(encoding="utf-8") as file:
        for line in file:
            row = json.loads(line)
            games = plays.setdefault(row["player_id"], {})
            games[row["appid"]] = max(row["hours"] or 0.0, games.get(row["appid"], 0.0))
    return plays


def first_fan(plays, anchor: str) -> str:
    return min(player for player, games in plays.items() if games.get(anchor, 0.0) >= CO_PLAY_MIN_HOURS)


def main() -> None:
    reader = GraphReader()
    retriever = GraphRetriever(reader)
    games = dump_games(reader)
    missing = [key for state in [*SIMILAR_CASES.values(), *STUDIO_CASES.values(), *CO_PLAY_CASES.values()]
               for key in state.include + state.exclude if key not in retriever.vocabulary.attribute_keys]
    if missing:
        raise SystemExit(f"keys the graph does not have: {missing}")
    developers = raw_developers()
    all_ok, timings = True, []

    keys = raw_keys()
    document_frequency = Counter(key for game_keys in keys.values() for key in game_keys)
    idf = {key: math.log(len(keys) / df) for key, df in document_frequency.items()}
    print("T2 similar")
    print(f"{'case':44s} {'expected':>8s} {'T2':>5s} {'extra':>5s} {'missed':>6s} {'idf':>4s} {'top':>4s}  top 3")
    for name, state in SIMILAR_CASES.items():
        expected = {appid for appid, game in games.items() if satisfies(appid, game, state)}
        result = retriever.retrieve(state, k=ALL)
        got = {candidate.appid for candidate in result}

        def shared_idf(appid: str) -> float:
            return sum(idf[key] for anchor in state.anchors for key in keys.get(appid, set()) & keys[anchor])

        def anchor_part(candidate) -> float:
            return sum(item.share for item in candidate.evidence if item.path in ("anchor_tag", "studio"))

        idf_ok = all(math.isclose(anchor_part(candidate), shared_idf(candidate.appid), abs_tol=1e-9)
                     for candidate in result)
        # the T1 part (included tags, popularity) is the returned score minus the anchor part
        ranked = sorted(result, key=lambda candidate: (
            -(candidate.score - anchor_part(candidate) + shared_idf(candidate.appid)), candidate.appid))
        top_ok = [candidate.appid for candidate in ranked[:TOP]] == [candidate.appid for candidate in result[:TOP]]
        all_ok &= got == expected and idf_ok and top_ok
        for _ in range(TIMING_RUNS):
            start = time.perf_counter()
            retriever.retrieve(state, k=100)
            timings.append(("similar", time.perf_counter() - start))
        print(f"{name:44s} {len(expected):8d} {len(got):5d} {len(got - expected):5d} {len(expected - got):6d} "
              f"{'ok' if idf_ok else 'NG':>4s} {'ok' if top_ok else 'NG':>4s}")
        rows = reader.query("MATCH (g:Game) WHERE g.appid IN $ids RETURN g.appid AS appid, g.name AS name",
                            ids=[candidate.appid for candidate in result[:5]])
        names = {row["appid"]: row["name"] for row in rows}
        print("   top 5: " + "; ".join(names.get(candidate.appid, candidate.appid) for candidate in result[:5]))
    print()

    print("T3 same_developer")
    print(f"{'case':44s} {'expected':>8s} {'T3':>5s} {'extra':>5s} {'missed':>6s}  top 3")
    for name, state in STUDIO_CASES.items():
        anchor_devs = set().union(*(developers.get(anchor, set()) for anchor in state.anchors))
        expected = {appid for appid, game in games.items()
                    if satisfies(appid, game, state) and developers.get(appid, set()) & anchor_devs}
        result = retriever.retrieve(state, k=ALL)
        got = {candidate.appid for candidate in result}
        studios_ok = all(
            {item.bridge_id for item in candidate.evidence if item.path == "studio"}
            == developers.get(candidate.appid, set()) & anchor_devs
            for candidate in result
        )
        ok = got == expected and studios_ok
        all_ok &= ok
        for _ in range(TIMING_RUNS):
            start = time.perf_counter()
            retriever.retrieve(state, k=100)
            timings.append(("same_developer", time.perf_counter() - start))
        top = ", ".join(candidate.appid for candidate in result[:3])
        print(f"{name:44s} {len(expected):8d} {len(got):5d} {len(got - expected):5d} {len(expected - got):6d}  "
              f"{top}{'' if studios_ok else '  (studio evidence differs)'}")
    portal = retriever.retrieve(STUDIO_CASES["Portal 2と同じ開発元で10ドル以下"], k=ALL)
    print(f"  Portal (400) in the first case: {'400' in {candidate.appid for candidate in portal}}")

    print("\nreading played.jsonl ...")
    plays = raw_plays()
    edges = sum(len(games_) for games_ in plays.values())
    graph_edges = reader.query("MATCH ()-[e:PLAYED]->() RETURN count(e) AS n")[0]["n"]
    print(f"players {len(plays)}, distinct plays {edges}, graph PLAYED {graph_edges}")
    all_ok &= edges == graph_edges

    print("\nT3 co_played (one fan of the anchor left out)")
    print(f"{'case':44s} {'expected':>8s} {'T3':>5s} {'extra':>5s} {'missed':>6s} {'counts':>6s} {'top':>4s}")
    scale = popularity_scale(retriever.max_players)
    for name, state in CO_PLAY_CASES.items():
        left_out = first_fan(plays, state.anchors[0])
        mine = retriever.excluding_player(left_out)
        others = {player: games_ for player, games_ in plays.items() if player != left_out}
        players = Counter(appid for games_ in others.values() for appid in games_)
        lifts: dict[str, float] = {}
        cos: dict[str, int] = {}
        for anchor in state.anchors:
            fans = [games_ for games_ in others.values() if games_.get(anchor, 0.0) >= CO_PLAY_MIN_HOURS]
            co = Counter(appid for games_ in fans for appid in games_ if appid not in state.anchors)
            for appid, count in co.items():
                lift = co_play_lift(count, len(fans), players[appid], len(others))
                if lift > lifts.get(appid, 0.0):
                    lifts[appid], cos[appid] = lift, count
        expected = {appid for appid in lifts if appid in games and satisfies(appid, games[appid], state)}
        result = mine.retrieve(state, k=ALL)
        got = {candidate.appid for candidate in result}
        counts_ok = all(
            max(item.count for item in candidate.evidence if item.path == "co_play") == cos[candidate.appid]
            for candidate in result if candidate.appid in cos
        )

        def expected_score(candidate) -> float:
            tags = sum(item.share for item in candidate.evidence if item.path == "tag")
            popularity = POPULARITY_WEIGHT * math.log1p(players[candidate.appid]) / scale
            return tags + popularity + math.log(lifts[candidate.appid])

        ranked = sorted((candidate for candidate in result if candidate.appid in lifts),
                        key=lambda candidate: (-expected_score(candidate), candidate.appid))
        top_ok = [candidate.appid for candidate in ranked[:TOP]] == [candidate.appid for candidate in result[:TOP]]
        ok = got == expected and counts_ok and top_ok
        all_ok &= ok
        for _ in range(TIMING_RUNS):
            start = time.perf_counter()
            mine.retrieve(state, k=100)
            timings.append(("co_played", time.perf_counter() - start))
        print(f"{name:44s} {len(expected):8d} {len(got):5d} {len(got - expected):5d} {len(expected - got):6d} "
              f"{'ok' if counts_ok else 'NG':>6s} {'ok' if top_ok else 'NG':>4s}")
        rows = reader.query("MATCH (g:Game) WHERE g.appid IN $ids RETURN g.appid AS appid, g.name AS name",
                            ids=[candidate.appid for candidate in result[:5]])
        names = {row["appid"]: row["name"] for row in rows}
        print("   top 5: " + "; ".join(names.get(candidate.appid, candidate.appid) for candidate in result[:5]))

    for template in ("similar", "same_developer", "co_played"):
        values = sorted(seconds for kind, seconds in timings if kind == template)
        print(f"\n{template} latency over {len(values)} calls: p50 {statistics.median(values) * 1000:.0f} ms, "
              f"max {values[-1] * 1000:.0f} ms", end="")
    print("\n" + ("ALL MATCH" if all_ok else "MISMATCH"))
    reader.close()
    if not all_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
