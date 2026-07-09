"""
オフライン評価（leave-one-out）: RATEDベースの個人化（Text2Cypher, recommend_home）が、
定番のItem-based Collaborative Filtering（Sarwar et al. 2001）と比べてどうかを測る。

対象ユーザー: rating>=4のRATEDエッジを1件以上持つユーザー。
ホールドアウト方式:
  1. 各ユーザーについて、rating>=4のうち最もタイムスタンプが新しいものを
     ターゲット（当てるべき商品）とする。
  2. そのユーザーの、ターゲットのタイムスタンプ以降のRATEDエッジを全て
     （ターゲット自身 + それより後の自分の低評価商品も含めて）一時的に削除する。
     こうすることで「未来の自分の低評価商品が文脈に残る」問題を、
     _get_user_context() のCypher自体には一切手を加えずに解消する。
     他ユーザーの未来のデータについては許容する（設計判断として確定済み）。
  3. 削除後に残る文脈が0件になるユーザーは評価対象から除外する。
  4. recommend_home() を実際に呼び、ターゲットがtop-Kに入るか(HR@K)、
     何位に入るか(NDCG@K)を記録する。
  5. 評価が終わったら削除したエッジを必ず復元する。

Item-KNNベースラインは、全対象ユーザーについて「自分のtarget+未来を除いた残り」だけを
使ってアイテム-アイテムのコサイン類似度行列を1回だけ構築し（自分のターゲットが類似度計算
自体に混ざらないようにする）、各ユーザーへのスコアリングだけを個別に行う。numpyのみで実装し、
scipy/scikit-learn等の追加依存は増やさない。

使い方:
    python eval/eval_offline.py --k 10
    python eval/eval_offline.py --k 10 --sample 100  # 動作確認用
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.recommender import Recommender  # noqa: E402


# ── データ取得 ────────────────────────────────────────────────────────────────

def fetch_all_rated(recommender: Recommender) -> dict[str, list[dict]]:
    """全ユーザーのRATEDエッジを {user_id: [{product_id, rating, timestamp}]} で返す。"""
    by_user: dict[str, list[dict]] = {}
    with recommender._driver.session(database=recommender._neo4j_db) as session:
        res = session.run(
            "MATCH (u:User)-[r:RATED]->(p:Product) "
            "RETURN u.user_id AS uid, p.product_id AS pid, r.rating AS rating, r.timestamp AS ts"
        )
        for row in res:
            by_user.setdefault(row["uid"], []).append(
                {"product_id": row["pid"], "rating": float(row["rating"]), "timestamp": int(row["ts"])}
            )
    return by_user


def build_eval_targets(by_user: dict[str, list[dict]]) -> list[dict]:
    """各ユーザーについて、ターゲットと削除すべきエッジ一覧・残り文脈数を決定する。
    残り文脈が0件になるユーザーは除外する。"""
    targets: list[dict] = []
    for uid, edges in by_user.items():
        ge4 = [e for e in edges if e["rating"] >= 4]
        if not ge4:
            continue
        target = max(ge4, key=lambda e: e["timestamp"])
        t_cut = target["timestamp"]
        to_remove = [e for e in edges if e["timestamp"] >= t_cut]
        remaining = [e for e in edges if e["timestamp"] < t_cut]
        if not remaining:
            continue
        targets.append({
            "user_id": uid,
            "target_product_id": target["product_id"],
            "to_remove": to_remove,  # ターゲット自身 + 自分の未来の低評価商品
            "remaining_count": len(remaining),
        })
    return targets


# ── Neo4j操作（削除・復元） ──────────────────────────────────────────────────

def remove_edges(recommender: Recommender, user_id: str, edges: list[dict]) -> None:
    with recommender._driver.session(database=recommender._neo4j_db) as session:
        for e in edges:
            session.run(
                "MATCH (u:User {user_id: $uid})-[r:RATED]->(p:Product {product_id: $pid}) "
                "WHERE r.timestamp = $ts "
                "DELETE r",
                uid=user_id, pid=e["product_id"], ts=e["timestamp"],
            )


def restore_edges(recommender: Recommender, user_id: str, edges: list[dict]) -> None:
    with recommender._driver.session(database=recommender._neo4j_db) as session:
        for e in edges:
            session.run(
                "MATCH (u:User {user_id: $uid}), (p:Product {product_id: $pid}) "
                "CREATE (u)-[:RATED {rating: $rating, timestamp: $ts}]->(p)",
                uid=user_id, pid=e["product_id"], rating=e["rating"], ts=e["timestamp"],
            )


# ── Item-KNNベースライン ────────────────────────────────────────────────────

class ItemKnnModel:
    def __init__(self, R: np.ndarray, user_index: dict[str, int], idx_to_item: dict[int, str], item_sim: np.ndarray):
        self.R = R
        self.user_index = user_index
        self.idx_to_item = idx_to_item
        self.item_sim = item_sim

    def recommend(self, user_id: str, k: int) -> list[str]:
        if user_id not in self.user_index:
            return []
        u_idx = self.user_index[user_id]
        user_ratings = self.R[u_idx]
        scores = self.item_sim @ user_ratings
        scores[user_ratings > 0] = -np.inf  # 既に評価済みの商品は候補から除く
        top_idx = np.argsort(-scores)[:k]
        return [self.idx_to_item[i] for i in top_idx if scores[i] > -np.inf]


def build_item_knn_model(by_user: dict[str, list[dict]], targets: list[dict]) -> ItemKnnModel:
    """全ユーザーの評価データからアイテム×アイテムのコサイン類似度を1回だけ構築する。
    評価対象ユーザー(targets内)については、自分のtarget+未来のエッジを除いた「残り」
    だけを類似度計算に使い、自分のターゲットが類似度自体に混入しないようにする。"""
    to_remove_by_uid = {
        t["user_id"]: {(e["product_id"], e["timestamp"]) for e in t["to_remove"]} for t in targets
    }

    user_index: dict[str, int] = {}
    item_index: dict[str, int] = {}
    entries: list[tuple[int, int, float]] = []

    for uid, edges in by_user.items():
        remove_keys = to_remove_by_uid.get(uid)
        use_edges = edges if remove_keys is None else [
            e for e in edges if (e["product_id"], e["timestamp"]) not in remove_keys
        ]
        for e in use_edges:
            u_idx = user_index.setdefault(uid, len(user_index))
            i_idx = item_index.setdefault(e["product_id"], len(item_index))
            entries.append((u_idx, i_idx, e["rating"]))

    n_users, n_items = len(user_index), len(item_index)
    R = np.zeros((n_users, n_items), dtype=np.float32)
    for u_idx, i_idx, rating in entries:
        R[u_idx, i_idx] = rating

    norms = np.linalg.norm(R, axis=0, keepdims=True)
    norms[norms == 0] = 1.0
    R_norm = R / norms
    item_sim = R_norm.T @ R_norm  # コサイン類似度 (n_items, n_items)
    np.fill_diagonal(item_sim, 0.0)  # 自分自身との類似度は除外

    idx_to_item = {v: k for k, v in item_index.items()}
    print(f"Item-KNN model: {n_users} users x {n_items} items, {len(entries)} ratings used")
    return ItemKnnModel(R, user_index, idx_to_item, item_sim)


# ── 指標 ──────────────────────────────────────────────────────────────────────

def hit_and_rank(target_pid: str, ranked_pids: list[str]) -> tuple[bool, int | None]:
    if target_pid in ranked_pids:
        rank = ranked_pids.index(target_pid) + 1
        return True, rank
    return False, None


def ndcg_from_rank(rank: int | None) -> float:
    if rank is None:
        return 0.0
    return 1.0 / math.log2(rank + 1)


# ── メイン評価ループ ────────────────────────────────────────────────────────

def run_eval(
    recommender: Recommender, targets: list[dict], knn_model: ItemKnnModel,
    k: int, out_path: Path, resume: bool,
) -> None:
    done_ids: set[str] = set()
    if resume and out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    done_ids.add(json.loads(line)["user_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"resume: {len(done_ids)} users already evaluated, skipping them")

    mode = "a" if resume else "w"
    with out_path.open(mode, encoding="utf-8") as out:
        for i, t in enumerate(targets):
            uid = t["user_id"]
            if uid in done_ids:
                continue

            remove_edges(recommender, uid, t["to_remove"])
            try:
                _, _, recs, fallback = recommender.recommend_home(uid, limit=k)
            except Exception as exc:
                print(f"[{i}/{len(targets)}] {uid}: recommend_home failed: {exc}", file=sys.stderr)
                recs, fallback = [], True
            finally:
                restore_edges(recommender, uid, t["to_remove"])

            llm_pids = [r.product_id for r in recs]
            llm_hit, llm_rank = hit_and_rank(t["target_product_id"], llm_pids)
            knn_pids = knn_model.recommend(uid, k)
            knn_hit, knn_rank = hit_and_rank(t["target_product_id"], knn_pids)

            record = {
                "user_id": uid,
                "target_product_id": t["target_product_id"],
                "remaining_context_count": t["remaining_count"],
                "llm_fallback": fallback,
                "llm_hit": llm_hit, "llm_rank": llm_rank,
                "knn_hit": knn_hit, "knn_rank": knn_rank,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(f"[{i+1}/{len(targets)}] {uid}: llm_hit={llm_hit}(rank={llm_rank}) "
                  f"knn_hit={knn_hit}(rank={knn_rank}) fallback={fallback}")

    # 万一ここで例外が起きても、remove/restoreはユーザー単位でtry/finally済みなので
    # グラフに削除しっぱなしのエッジは残らない。


def summarize(out_path: Path, k: int) -> dict[str, Any]:
    records = []
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    n = len(records)
    llm_hr = sum(r["llm_hit"] for r in records) / n
    knn_hr = sum(r["knn_hit"] for r in records) / n
    llm_ndcg = sum(ndcg_from_rank(r["llm_rank"]) for r in records) / n
    knn_ndcg = sum(ndcg_from_rank(r["knn_rank"]) for r in records) / n
    fallback_rate = sum(r["llm_fallback"] for r in records) / n

    summary = {
        "n_users": n,
        "k": k,
        f"HR@{k}_llm_personalized": round(llm_hr, 4),
        f"HR@{k}_item_knn_baseline": round(knn_hr, 4),
        f"NDCG@{k}_llm_personalized": round(llm_ndcg, 4),
        f"NDCG@{k}_item_knn_baseline": round(knn_ndcg, 4),
        "llm_fallback_rate": round(fallback_rate, 4),
    }
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--sample", type=int, default=None, help="デバッグ用: 対象をN人に限定する")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    recommender = Recommender(config_path=args.config)
    out_path = args.out or (args.config.resolve().parent / "eval_results.jsonl")

    print("fetching RATED edges from Neo4j...")
    by_user = fetch_all_rated(recommender)
    targets = build_eval_targets(by_user)
    print(f"eligible users: {len(targets)} (excluded users with zero remaining context)")

    if args.sample:
        targets = targets[: args.sample]
        print(f"--sample指定により {len(targets)} 人に限定")

    knn_model = build_item_knn_model(by_user, targets)

    try:
        run_eval(recommender, targets, knn_model, args.k, out_path, args.resume)
    finally:
        recommender._home_cache.clear()  # 切り詰めたグラフでの結果が本番用途に混入しないようにする
        recommender.close()

    summary = summarize(out_path, args.k)
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved: {out_path}")
    print(f"saved: {summary_path}")


if __name__ == "__main__":
    main()
