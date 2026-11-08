"""E1-pre: the A-layer simulation on the development users (E1_PLAN.md).

Groups: G (T1 only) and G-static, V-entropy and V-static (vector search, vector.py), and P. V needs the
embedding model on the Ollama server at EMBED_BASE_URL; its document vectors are cached under data/eval/vectors/.

    python -m eval.crs.run_e1 --users 300            # inside the app container, GRAPH=ucsd
    python -m eval.crs.run_e1 --users 20 --seeds 1   # a quick smoke run

Only the dev split can be selected: there is no option for test, which the main E1 uses once after the plan
is fixed. A turn log's pool_size is 0 when no pool was retrieved (the list hit, or it was the last turn).
Before any dialogue it checks that popularity leaves the evaluated player out (the plan's acceptance
conditions) and stops on a mismatch. Writes manifest.json, dialogues.jsonl, turns.jsonl and summary.json under
data/eval/e1_pre/<time>/ and prints the summary.
"""

import argparse
import hashlib
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.api.chat_queries import T1_QUERY, GraphRetriever, t1_parameters
from app.api.chat_questioner import (
    MIN_ENTROPY,
    POOL_SIZE,
    STOP_POOL_SIZE,
    EntropyQuestioner,
    FixedOrderQuestioner,
    QuestionCatalog,
    entropy_table,
    load_catalog,
)
from app.api.chat_state import ChatState, TurnLog
from eval.crs.baselines import PopularityRetriever
from eval.crs.constraints import GameFacts, check, dump_games
from eval.crs.simulator import (
    LIST_SIZE,
    MAX_TURNS,
    OPENING_TAGS,
    Target,
    askable_tags,
    choose_target,
    is_dev,
    make_target,
    opening_tags,
    sample_order,
    simulate,
)
from eval.crs.vector import (
    EMBED_MODEL,
    EXCLUDE_WEIGHT,
    VectorRetriever,
    document_vectors,
    load_corpus,
    model_identity,
    ollama_embed,
)
from eval.metrics import hdcg, success_by_turn, turns_taken
from eval.stats import holm, mcnemar_exact, paired_bootstrap_ci, wilcoxon_signed_rank
from utils.graph_reader import GraphReader

GROUPS = ("G", "G-static", "V-entropy", "V-static", "P")
# the first is the primary comparison; Holm adjusts over all of them
COMPARISONS = (("G", "V-entropy"), ("G", "G-static"), ("V-entropy", "V-static"), ("G", "P"))
JACCARD_PAIR = ("G", "V-entropy")
MIN_PLAYED = 5
POPULAR_TOP = 50
ENTROPY_LOG_TOP = 10
BATCH = 200
OUT_ROOT = Path("data/eval/e1_pre")

PLAYERS_QUERY = """
MATCH (p:Player) WHERE COUNT { (p)-[:PLAYED]->(:Game) } >= $min_played
RETURN p.player_id AS player_id
"""
MEDIAN_QUERY = """
MATCH (g:Game)<-[r:PLAYED]-(:Player)
RETURN g.appid AS appid, percentileCont(r.hours, 0.5) AS median
"""
PLAYED_QUERY = """
UNWIND $player_ids AS id
MATCH (:Player {player_id: id})-[r:PLAYED]->(g:Game)
RETURN id AS player_id, g.appid AS appid, r.hours AS hours
"""
# Counted with MATCH and count(), not with the COUNT {} subquery the retrievers use.
PLAYER_COUNT_QUERY = """
UNWIND $appids AS a
MATCH (g:Game {appid: a})<-[r:PLAYED]-(:Player)
RETURN a AS appid, count(r) AS n
"""


@dataclass(frozen=True)
class User:
    player_id: str
    played: frozenset[str]
    target: Target


def select_users(reader: GraphReader, facts: dict[str, GameFacts], whitelist: tuple[str, ...], count: int):
    players = [row["player_id"] for row in reader.query(PLAYERS_QUERY, min_played=MIN_PLAYED)]
    dev = sample_order([player_id for player_id in players if is_dev(player_id)])
    medians = {row["appid"]: row["median"] for row in reader.query(MEDIAN_QUERY)}
    users: list[User] = []
    skipped = {"no_target": 0, "few_whitelisted_tags": 0}
    for start in range(0, len(dev), BATCH):
        batch = dev[start:start + BATCH]
        hours: dict[str, dict[str, float]] = {player_id: {} for player_id in batch}
        for row in reader.query(PLAYED_QUERY, player_ids=batch):
            hours[row["player_id"]][row["appid"]] = row["hours"]
        for player_id in batch:
            appid = choose_target(player_id, hours[player_id], medians)
            if appid is None or appid not in facts:
                skipped["no_target"] += 1
                continue
            target = make_target(player_id, appid, facts[appid])
            if len(askable_tags(target, whitelist)) < OPENING_TAGS:
                skipped["few_whitelisted_tags"] += 1
                continue
            users.append(User(player_id, frozenset(hours[player_id]), target))
            if len(users) == count:
                return users, {"players_min_played": len(players), "dev_players": len(dev), "skipped": skipped}
    return users, {"players_min_played": len(players), "dev_players": len(dev), "skipped": skipped}


def check_leakage(reader, retriever: GraphRetriever, popularity: PopularityRetriever, users, whitelist, workers):
    """The target's popularity, as G and P use it, must be its true player count minus the evaluated player."""
    appids = sorted({user.target.appid for user in users})
    true_count = {row["appid"]: row["n"] for row in reader.query(PLAYER_COUNT_QUERY, appids=appids)}

    def g_count(user: User) -> int | None:
        state = ChatState(include=opening_tags(user.target, whitelist, seed=0))
        rows = reader.query(T1_QUERY, **t1_parameters(state, retriever.vocabulary, user.player_id))
        return next((row["players"] for row in rows if row["appid"] == user.target.appid), None)

    with ThreadPoolExecutor(workers) as pool:
        g_counts = list(pool.map(g_count, users))
    mismatches = []
    for user, g in zip(users, g_counts):
        expected = true_count.get(user.target.appid, 0) - 1
        p = popularity.excluding_player(user.played).players_by_appid[user.target.appid]
        if g != expected or p != expected:
            mismatches.append({"player_id": user.player_id, "appid": user.target.appid,
                               "expected": expected, "G": g, "P": p})
    return {"users": len(users), "matched": len(users) - len(mismatches), "mismatches": mismatches[:20]}


def entropy_top(catalog: QuestionCatalog):
    def explain(state, pool):
        return dict(list(entropy_table(state, pool, catalog).items())[:ENTROPY_LOG_TOP])
    return explain


def run_user(user: User, seeds: int, retriever, vector, popularity, catalog, facts, popular: set[str]):
    systems = {
        "G": (retriever.excluding_player(user.player_id), EntropyQuestioner(catalog), entropy_top(catalog)),
        "G-static": (retriever.excluding_player(user.player_id), FixedOrderQuestioner(catalog), None),
        "V-entropy": (vector, EntropyQuestioner(catalog), entropy_top(catalog)),
        "V-static": (vector, FixedOrderQuestioner(catalog), None),
        "P": (popularity.excluding_player(user.played), None, None),
    }
    dialogues, logs = [], []
    for seed in range(seeds):
        opening = opening_tags(user.target, catalog.keys, seed)
        for group, (system, questioner, explain) in systems.items():
            records = simulate(user.target, opening, system, questioner, POOL_SIZE, explain)
            last = records[-1]
            success_turn = last.turn if last.hit_rank is not None else None
            checks = [check(facts[c.appid], record.state) for record in records for c in record.shown]
            dialogue_id = f"{group}|{user.player_id}|{seed}"
            dialogues.append({
                "dialogue_id": dialogue_id, "group": group, "player_id": user.player_id, "seed": seed,
                "target": user.target.appid, "popular": user.target.appid in popular, "opening": opening,
                "success_turn": success_turn, "success_rank": last.hit_rank,
                "hdcg": hdcg(success_turn, last.hit_rank),
                "questions": sum(record.question is not None for record in records),
                "answers": [[record.question, record.answer] for record in records if record.question],
                "shown": len(checks),
                "first_list": [c.appid for c in records[0].shown],
                "constraints": {
                    "all": sum(c.all for c in checks), "include": sum(c.include for c in checks),
                    "exclude": sum(c.exclude for c in checks), "budget": sum(c.budget for c in checks),
                },
                "turn_seconds": [record.seconds for record in records],
            })
            for record in records:
                logs.append(TurnLog(
                    dialogue_id=dialogue_id, turn=record.turn, state_before=record.state_before,
                    update=record.update, state_after=record.state, pool_size=record.pool_size or 0,
                    shown=record.shown, question=record.question, entropy=record.explanation,
                ).model_dump())
    return dialogues, logs


def per_user(dialogues: list[dict], group: str, value) -> dict[str, float]:
    """A dialogue value averaged over the seeds of each user: the unit of analysis."""
    values: dict[str, list[float]] = {}
    for dialogue in dialogues:
        if dialogue["group"] == group:
            values.setdefault(dialogue["player_id"], []).append(value(dialogue))
    return {player_id: sum(items) / len(items) for player_id, items in values.items()}


METRICS = {
    **{f"SR@{t}": (lambda t: lambda d: success_by_turn(d["success_turn"], t))(t) for t in range(1, MAX_TURNS + 1)},
    "AT": lambda d: turns_taken(d["success_turn"], MAX_TURNS),
    "hDCG@(5,10)": lambda d: d["hdcg"],
    "Recall@10 (final list)": lambda d: float(d["success_turn"] is not None),
    "questions": lambda d: d["questions"],
}


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * len(ordered)) - 1))] if ordered else 0.0


def summarize(dialogues: list[dict]) -> dict:
    strata = {
        "all": dialogues,
        "popular": [d for d in dialogues if d["popular"]],
        "long_tail": [d for d in dialogues if not d["popular"]],
    }
    summary: dict = {"groups": {}, "comparisons": {}}
    for stratum, items in strata.items():
        for group in GROUPS:
            mine = [d for d in items if d["group"] == group]
            if not mine:
                continue
            row = {"users": len({d["player_id"] for d in mine}), "dialogues": len(mine)}
            for name, value in METRICS.items():
                by_user = per_user(mine, group, value)
                row[name] = sum(by_user.values()) / len(by_user)
            shown = sum(d["shown"] for d in mine)
            for kind in ("all", "include", "exclude", "budget"):
                row[f"constraints.{kind}"] = sum(d["constraints"][kind] for d in mine) / shown if shown else None
            seconds = [s for d in mine for s in d["turn_seconds"]]
            row["latency_p50_ms"] = statistics.median(seconds) * 1000
            row["latency_p95_ms"] = percentile(seconds, 0.95) * 1000
            summary["groups"].setdefault(stratum, {})[group] = row

    summary["jaccard@10_turn1"] = {stratum: jaccard_first_lists(items, *JACCARD_PAIR)
                                   for stratum, items in strata.items()}
    for name in ("SR@5", "AT", "hDCG@(5,10)"):
        tests, p_values = {}, {}
        for a, b in COMPARISONS:
            ua, ub = per_user(dialogues, a, METRICS[name]), per_user(dialogues, b, METRICS[name])
            differences = [ua[player_id] - ub[player_id] for player_id in sorted(ua) if player_id in ub]
            _, p = wilcoxon_signed_rank(differences)
            low, high = paired_bootstrap_ci(differences)
            key = f"{a} vs {b}"
            tests[key] = {"mean_difference": sum(differences) / len(differences), "ci95": [low, high],
                          "wilcoxon_p": p}
            p_values[key] = p
            if name == "SR@5":
                tests[key]["mcnemar_p_by_seed"] = mcnemar_by_seed(dialogues, a, b)
        for key, adjusted in holm(p_values).items():
            tests[key]["holm_p"] = adjusted
        summary["comparisons"][name] = tests
    return summary


def jaccard_first_lists(dialogues: list[dict], a: str, b: str) -> float | None:
    """Overlap of the first lists of a and b in the same dialogue (user and seed), averaged per user."""
    lists = {(d["group"], d["player_id"], d["seed"]): set(d["first_list"]) for d in dialogues}
    by_user: dict[str, list[float]] = {}
    for (group, player_id, seed), first in lists.items():
        other = lists.get((b, player_id, seed))
        if group == a and other is not None and (first or other):
            by_user.setdefault(player_id, []).append(len(first & other) / len(first | other))
    values = [sum(items) / len(items) for items in by_user.values()]
    return sum(values) / len(values) if values else None


def mcnemar_by_seed(dialogues: list[dict], a: str, b: str) -> list[float]:
    success = {(d["group"], d["player_id"], d["seed"]): d["success_turn"] is not None for d in dialogues}
    p_values = []
    for seed in sorted({d["seed"] for d in dialogues}):
        players = sorted({d["player_id"] for d in dialogues if d["seed"] == seed})
        only_a = sum(success[(a, p, seed)] and not success[(b, p, seed)] for p in players)
        only_b = sum(success[(b, p, seed)] and not success[(a, p, seed)] for p in players)
        p_values.append(mcnemar_exact(only_a, only_b))
    return p_values


def graph_identity(reader: GraphReader, facts: dict[str, GameFacts]) -> dict:
    digest = hashlib.sha256()
    for appid in sorted(facts):
        game = facts[appid]
        digest.update(json.dumps([appid, game.price, game.tag_edges, game.developers]).encode())
    played = reader.query("MATCH ()-[r:PLAYED]->() RETURN count(r) AS n")[0]["n"]
    return {"games": len(facts), "played_edges": played, "games_tags_studios_sha256": digest.hexdigest()}


def git_commit() -> str | None:
    head = Path(".git/HEAD")
    if not head.exists():
        return None
    ref = head.read_text().strip()
    if ref.startswith("ref: "):
        path = Path(".git") / ref[5:]
        return path.read_text().strip() if path.exists() else ref
    return ref


def print_summary(summary: dict) -> None:
    columns = ["SR@1", "SR@3", "SR@5", "AT", "hDCG@(5,10)", "questions", "constraints.all", "latency_p50_ms"]
    for stratum, groups in summary["groups"].items():
        print(f"\n[{stratum}]")
        print(f"{'group':10s} {'users':>5s} " + " ".join(f"{c[:12]:>12s}" for c in columns))
        for group, row in groups.items():
            cells = " ".join(f"{row[c]:12.3f}" if row[c] is not None else f"{'-':>12s}" for c in columns)
            print(f"{group:10s} {row['users']:5d} {cells}")
    jaccard = ", ".join(f"{stratum} {value:.3f}" for stratum, value in summary["jaccard@10_turn1"].items()
                        if value is not None)
    print(f"\nJaccard@10 of the first lists, {' vs '.join(JACCARD_PAIR)}: {jaccard}")
    print("\npaired comparisons (per-user means over seeds)")
    for name, tests in summary["comparisons"].items():
        for key, test in tests.items():
            low, high = test["ci95"]
            print(f"  {name:12s} {key:21s} diff {test['mean_difference']:+.3f} [{low:+.3f}, {high:+.3f}] "
                  f"Wilcoxon p {test['wilcoxon_p']:.4f} Holm {test['holm_p']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    started = time.time()
    reader = GraphReader()
    retriever = GraphRetriever(reader)
    catalog = load_catalog(reader)
    facts = dump_games(reader)
    popularity = PopularityRetriever({appid: game.players for appid, game in catalog.games.items()})
    popular = set(sorted(catalog.games, key=lambda appid: (-catalog.games[appid].players, appid))[:POPULAR_TOP])
    embedding = model_identity()
    corpus = load_corpus(reader)
    if set(corpus.appids) != set(facts):
        raise SystemExit("V's documents and the graph dump cover different games")
    vectors, documents = document_vectors(corpus, ollama_embed, embedding["digest"])
    vector = VectorRetriever(corpus, vectors, ollama_embed)
    embedding.update(documents, dimension=int(vectors.shape[1]), exclude_weight=EXCLUDE_WEIGHT)
    print(f"V: {EMBED_MODEL} {embedding['digest'][:12]}, {documents['documents']} documents"
          f"{' (cached)' if documents['cached'] else ''}, {time.time() - started:.0f} s")

    users, selection = select_users(reader, facts, catalog.keys, args.users)
    print(f"users: {len(users)} (dev), selection {selection}")
    leakage = check_leakage(reader, retriever, popularity, users, catalog.keys, args.workers)
    print(f"leakage check: {leakage['matched']}/{leakage['users']} match")
    if leakage["mismatches"]:
        print(json.dumps(leakage["mismatches"], indent=2))
        raise SystemExit("popularity still counts the evaluated player; not running")

    out = OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    dialogues = []
    with ThreadPoolExecutor(args.workers) as pool, \
            (out / "turns.jsonl").open("w", encoding="utf-8") as turns_file:
        jobs = pool.map(lambda user: run_user(user, args.seeds, retriever, vector, popularity, catalog, facts, popular), users)
        for done, (user_dialogues, logs) in enumerate(jobs, 1):
            dialogues += user_dialogues
            for log in logs:
                turns_file.write(json.dumps(log, ensure_ascii=False) + "\n")
            if done % 25 == 0:
                print(f"  {done}/{len(users)} users, {time.time() - started:.0f} s", flush=True)
    with (out / "dialogues.jsonl").open("w", encoding="utf-8") as file:
        for dialogue in dialogues:
            file.write(json.dumps(dialogue, ensure_ascii=False) + "\n")

    summary = summarize(dialogues)
    manifest = {
        "experiment": "E1-pre", "split": "dev", "users": len(users), "seeds": args.seeds, "groups": GROUPS,
        "selection": selection, "leakage_check": leakage, "graph": graph_identity(reader, facts),
        "embedding": embedding, "git_commit": git_commit(),
        "constants": {"MAX_TURNS": MAX_TURNS, "LIST_SIZE": LIST_SIZE, "POOL_SIZE": POOL_SIZE,
                      "STOP_POOL_SIZE": STOP_POOL_SIZE, "MIN_ENTROPY": MIN_ENTROPY, "OPENING_TAGS": OPENING_TAGS,
                      "MIN_PLAYED": MIN_PLAYED, "POPULAR_TOP": POPULAR_TOP, "whitelist": len(catalog.keys)},
        "seconds": time.time() - started,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print_summary(summary)
    print(f"\nwrote {out} in {time.time() - started:.0f} s")
    reader.close()


if __name__ == "__main__":
    main()
