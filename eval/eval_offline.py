"""
オフライン評価（leave-one-out）: RATEDベースの個人化（Text2Cypher, recommend_home）が、
Item-based Collaborative Filtering（Sarwar et al. 2001）・人気度ベースラインと比べて
どうかを測る。

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
     何位に入るか(NDCG@K)を記録する。K は --cutoffs で指定した全ての値について
     同時に集計する（デフォルト 10/20/50、上位 max(cutoffs) 件を1回取得して
     使い回す）。
  5. 評価が終わったら削除したエッジを必ず復元する。

ベースラインは2つ:
  - Item-KNN: 全対象ユーザーについて「自分のtarget+未来を除いた残り」だけを使って
    アイテム-アイテムのコサイン類似度行列を1回だけ構築し（自分のターゲットが類似度
    計算自体に混ざらないようにする）、各ユーザーへのスコアリングだけを個別に行う。
  - Popularity: rating_count・avg_ratingで全体を1回だけ静的にランキングし、各ユーザーの
    既知（残り）商品を除いて上位を返すだけの非個人化ベースライン。LLM/KNNが「ただ人気の
    ものを勧めているだけ」ではないかを切り分けるために使う。
どちらもnumpyのみで実装し、scipy/scikit-learn等の追加依存は増やさない。

集計結果には、手法ごとにHR@K/NDCG@Kと同列の指標として catalog_coverage@k（全対象
ユーザーへの推薦で実際にカバーできたカタログの割合）と avg_rating@k（推薦商品の平均
評価点）も含む（kは最小カットオフ＝実際にUIへ出す件数相当）。データ規模の健全性チェック
（商品・ユーザー数、価格/画像/評価のカバレッジ）を評価開始前に出力する。

使い方:
    python eval/eval_offline.py --cutoffs 10 20 50
    python eval/eval_offline.py --cutoffs 10 20 50 --sample 100  # 動作確認用
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


# ── データ健全性チェック ────────────────────────────────────────────────────

def print_data_readiness(recommender: Recommender) -> None:
    """評価開始前に、カタログ規模と主要フィールドのカバレッジを出力する（診断のみ、
    中断はしない）。数値が小さすぎる/カバレッジが低い場合に、評価結果をどこまで
    信用してよいか判断する材料にする。"""
    with recommender._driver.session(database=recommender._neo4j_db) as session:
        row = session.run(
            "MATCH (p:Product) "
            "RETURN count(p) AS n_products, "
            "       sum(CASE WHEN p.price IS NOT NULL THEN 1 ELSE 0 END) AS with_price, "
            "       sum(CASE WHEN p.image_url IS NOT NULL THEN 1 ELSE 0 END) AS with_image, "
            "       sum(CASE WHEN p.avg_rating IS NOT NULL THEN 1 ELSE 0 END) AS with_rating"
        ).single()
        n_users = session.run("MATCH (u:User) RETURN count(u) AS n").single()["n"]
        n_reviews = session.run("MATCH (r:Review) RETURN count(r) AS n").single()["n"]

    n_products = row["n_products"] or 0
    print("=== data readiness ===")
    print(f"  products: {n_products:,}  users: {n_users:,}  reviews: {n_reviews:,}")
    if n_products:
        print(
            f"  price coverage: {row['with_price'] / n_products:.1%}  "
            f"image coverage: {row['with_image'] / n_products:.1%}  "
            f"avg_rating coverage: {row['with_rating'] / n_products:.1%}"
        )
    print()


def fetch_product_catalog(recommender: Recommender) -> dict[str, dict[str, Any]]:
    """{product_id: {avg_rating, rating_count}} を全商品分返す。
    catalog_coverage/avg_rating指標と人気度ベースラインの構築に使う。"""
    catalog: dict[str, dict[str, Any]] = {}
    with recommender._driver.session(database=recommender._neo4j_db) as session:
        res = session.run(
            "MATCH (p:Product) "
            "RETURN p.product_id AS pid, p.avg_rating AS avg_rating, "
            "       p.rating_count AS rating_count"
        )
        for row in res:
            catalog[row["pid"]] = {
                "avg_rating": row["avg_rating"],
                "rating_count": row["rating_count"] or 0,
            }
    return catalog


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


# ── 人気度ベースライン ──────────────────────────────────────────────────────

class PopularityModel:
    """rating_count・avg_ratingで全商品を1回だけ静的にランキングする非個人化ベース
    ライン。ユーザーごとに「既に評価済み（残り）」の商品だけを除いて上位を返す。"""

    def __init__(self, ranked_pids: list[str]):
        self._ranked_pids = ranked_pids

    def recommend(self, k: int, exclude_ids: set[str]) -> list[str]:
        out: list[str] = []
        for pid in self._ranked_pids:
            if pid in exclude_ids:
                continue
            out.append(pid)
            if len(out) >= k:
                break
        return out


def build_popularity_model(catalog: dict[str, dict[str, Any]]) -> PopularityModel:
    """rating_count降順、同点はavg_rating降順で全商品をランキングする。"""
    ranked = sorted(
        catalog.keys(),
        key=lambda pid: (catalog[pid]["rating_count"] or 0, catalog[pid]["avg_rating"] or 0.0),
        reverse=True,
    )
    return PopularityModel(ranked)


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

METHODS = ("llm", "knn", "pop")
METHOD_LABELS = {"llm": "llm_personalized", "knn": "item_knn_baseline", "pop": "popularity_baseline"}


def run_eval(
    recommender: Recommender, targets: list[dict], by_user: dict[str, list[dict]],
    knn_model: ItemKnnModel, popularity_model: PopularityModel,
    cutoffs: list[int], out_path: Path, resume: bool,
) -> None:
    max_k = max(cutoffs)
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
                _, _, recs, fallback = recommender.recommend_home(uid, limit=max_k)
            except Exception as exc:
                print(f"[{i}/{len(targets)}] {uid}: recommend_home failed: {exc}", file=sys.stderr)
                recs, fallback = [], True
            finally:
                restore_edges(recommender, uid, t["to_remove"])

            llm_pids = [r.product_id for r in recs]
            knn_pids = knn_model.recommend(uid, max_k)
            removed_pids = {e["product_id"] for e in t["to_remove"]}
            remaining_pids = {e["product_id"] for e in by_user.get(uid, [])} - removed_pids
            pop_pids = popularity_model.recommend(max_k, exclude_ids=remaining_pids)

            _, llm_rank = hit_and_rank(t["target_product_id"], llm_pids)
            _, knn_rank = hit_and_rank(t["target_product_id"], knn_pids)
            _, pop_rank = hit_and_rank(t["target_product_id"], pop_pids)

            record = {
                "user_id": uid,
                "target_product_id": t["target_product_id"],
                "remaining_context_count": t["remaining_count"],
                "llm_fallback": fallback,
                "llm_rank": llm_rank, "knn_rank": knn_rank, "pop_rank": pop_rank,
                "llm_pids": llm_pids, "knn_pids": knn_pids, "pop_pids": pop_pids,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(f"[{i+1}/{len(targets)}] {uid}: llm_rank={llm_rank} knn_rank={knn_rank} "
                  f"pop_rank={pop_rank} fallback={fallback}")

    # 万一ここで例外が起きても、remove/restoreはユーザー単位でtry/finally済みなので
    # グラフに削除しっぱなしのエッジは残らない。


def _catalog_coverage(records: list[dict], method: str, catalog: dict[str, dict[str, Any]], report_k: int) -> float:
    """上位report_k件の推薦で、カタログ全体のうち実際にカバーできた商品の割合。"""
    if not catalog:
        return 0.0
    all_pids = {pid for r in records for pid in r[f"{method}_pids"][:report_k]}
    return round(len(all_pids) / len(catalog), 4)


def _avg_rating(records: list[dict], method: str, catalog: dict[str, dict[str, Any]], report_k: int) -> float:
    """上位report_k件の推薦商品の平均評価点（avg_rating欠損の商品は除く）。"""
    ratings = [
        float(catalog[pid]["avg_rating"])
        for r in records
        for pid in r[f"{method}_pids"][:report_k]
        if catalog.get(pid) and catalog[pid]["avg_rating"] is not None
    ]
    return round(sum(ratings) / len(ratings), 3) if ratings else 0.0


def summarize(
    out_path: Path, cutoffs: list[int], catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    records = []
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    n = len(records)
    report_k = min(cutoffs)  # catalog_coverage/avg_ratingは実際にUIへ出す件数相当（最小カットオフ）で見る
    summary: dict[str, Any] = {"n_users": n, "cutoffs": cutoffs}

    for method in METHODS:
        ranks = [r[f"{method}_rank"] for r in records]
        label = METHOD_LABELS[method]
        for c in cutoffs:
            hr = sum(1 for rk in ranks if rk is not None and rk <= c) / n
            ndcg = sum(ndcg_from_rank(rk) if (rk is not None and rk <= c) else 0.0 for rk in ranks) / n
            summary[f"HR@{c}_{label}"] = round(hr, 4)
            summary[f"NDCG@{c}_{label}"] = round(ndcg, 4)
        summary[f"catalog_coverage@{report_k}_{label}"] = _catalog_coverage(records, method, catalog, report_k)
        summary[f"avg_rating@{report_k}_{label}"] = _avg_rating(records, method, catalog, report_k)

    summary["llm_fallback_rate"] = round(sum(r["llm_fallback"] for r in records) / n, 4)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    ap.add_argument(
        "--cutoffs", type=int, nargs="+", default=[10, 20, 50],
        help="HR@K/NDCG@Kを計算するK値（複数指定可）。上位max(cutoffs)件を1回だけ取得して使い回す。",
    )
    ap.add_argument("--sample", type=int, default=None, help="デバッグ用: 対象をN人に限定する")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    cutoffs = sorted(set(args.cutoffs))

    recommender = Recommender(config_path=args.config)
    out_path = args.out or (args.config.resolve().parent / "eval_results.jsonl")

    print_data_readiness(recommender)
    catalog = fetch_product_catalog(recommender)

    print("fetching RATED edges from Neo4j...")
    by_user = fetch_all_rated(recommender)
    targets = build_eval_targets(by_user)
    print(f"eligible users: {len(targets)} (excluded users with zero remaining context)")

    if args.sample:
        targets = targets[: args.sample]
        print(f"--sample指定により {len(targets)} 人に限定")

    knn_model = build_item_knn_model(by_user, targets)
    popularity_model = build_popularity_model(catalog)

    try:
        run_eval(recommender, targets, by_user, knn_model, popularity_model, cutoffs, out_path, args.resume)
    finally:
        recommender._home_cache.clear()  # 切り詰めたグラフでの結果が本番用途に混入しないようにする
        recommender.close()

    summary = summarize(out_path, cutoffs, catalog)
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved: {out_path}")
    print(f"saved: {summary_path}")


if __name__ == "__main__":
    main()
