"""
横向推荐算法对比实验。

目标：
- 用同一份 leave-one-out 数据比较多个推荐策略。
- 保存中间结果、summary 和图，便于多轮迭代。
- 不调用 LLM、不修改 Neo4j 图；所有实验在内存中完成。

方法：
- popularity: 全局热门/高评分
- item_cf: item-item collaborative filtering
- user_cf: peer overlap collaborative filtering
- kg_meta_path_v1: User -> Product -> Attribute -> Product
- hybrid_v2: item_cf + user_cf + kg_meta_path_v1 + popularity 的 rank fusion
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.recommender import Recommender  # noqa: E402


IGNORED_ATTR_TYPES = {
    "batteries",
    "color",
    "customer_reviews",
    "date_first_available",
    "item_weight",
    "language",
    "package_dimensions",
    "pricing",
    "release_date",
    "return_policy",
    "terms_of_use",
}


@dataclass
class GraphData:
    products: dict[str, dict[str, Any]]
    ratings_by_user: dict[str, list[dict[str, Any]]]
    attrs_by_product: dict[str, set[str]]
    cats_by_product: dict[str, set[str]]


@dataclass
class EvalTarget:
    user_id: str
    target_product_id: str
    target_ts: int
    to_remove: set[tuple[str, int]]
    train_pids: set[str]
    train_positive_pids: set[str]


def fetch_graph(recommender: Recommender) -> GraphData:
    products: dict[str, dict[str, Any]] = {}
    ratings_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attrs_by_product: dict[str, set[str]] = defaultdict(set)
    cats_by_product: dict[str, set[str]] = defaultdict(set)

    with recommender._driver.session(database=recommender._neo4j_db) as session:
        for row in session.run(
            "MATCH (p:Product) "
            "RETURN p.product_id AS pid, p.avg_rating AS avg_rating, "
            "p.rating_count AS rating_count, p.price AS price, p.image_url AS image_url"
        ):
            products[row["pid"]] = {
                "avg_rating": float(row["avg_rating"] or 0.0),
                "rating_count": int(row["rating_count"] or 0),
                "price": row["price"],
                "image_url": row["image_url"],
            }

        for row in session.run(
            "MATCH (u:User)-[r:RATED]->(p:Product) "
            "RETURN u.user_id AS uid, p.product_id AS pid, "
            "toFloat(r.rating) AS rating, toInteger(r.timestamp) AS ts"
        ):
            ratings_by_user[row["uid"]].append(
                {"product_id": row["pid"], "rating": float(row["rating"]), "timestamp": int(row["ts"])}
            )

        for row in session.run(
            "MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute) "
            "RETURN p.product_id AS pid, a.attr_type AS attr_type, a.value AS value"
        ):
            attr_type = str(row["attr_type"] or "")
            value = str(row["value"] or "")
            if attr_type and value and attr_type not in IGNORED_ATTR_TYPES:
                attrs_by_product[row["pid"]].add(f"{attr_type}:{value}")

        for row in session.run(
            "MATCH (p:Product)-[:BELONGS_TO]->(c:Category) "
            "RETURN p.product_id AS pid, c.name AS name"
        ):
            if row["name"]:
                cats_by_product[row["pid"]].add(str(row["name"]).lower())

    for edges in ratings_by_user.values():
        edges.sort(key=lambda x: x["timestamp"])
    return GraphData(
        products=products,
        ratings_by_user=dict(ratings_by_user),
        attrs_by_product=dict(attrs_by_product),
        cats_by_product=dict(cats_by_product),
    )


def build_targets(data: GraphData) -> list[EvalTarget]:
    targets: list[EvalTarget] = []
    for uid, edges in data.ratings_by_user.items():
        positives = [e for e in edges if e["rating"] >= 4.0]
        if not positives:
            continue
        target = max(positives, key=lambda e: e["timestamp"])
        target_ts = int(target["timestamp"])
        to_remove = {(e["product_id"], int(e["timestamp"])) for e in edges if int(e["timestamp"]) >= target_ts}
        train = [e for e in edges if (e["product_id"], int(e["timestamp"])) not in to_remove]
        if not train:
            continue
        targets.append(
            EvalTarget(
                user_id=uid,
                target_product_id=target["product_id"],
                target_ts=target_ts,
                to_remove=to_remove,
                train_pids={e["product_id"] for e in train},
                train_positive_pids={e["product_id"] for e in train if e["rating"] >= 4.0},
            )
        )
    return targets


def build_train_edges(data: GraphData, targets: list[EvalTarget]) -> dict[str, list[dict[str, Any]]]:
    remove_by_user = {t.user_id: t.to_remove for t in targets}
    train_by_user: dict[str, list[dict[str, Any]]] = {}
    for uid, edges in data.ratings_by_user.items():
        remove = remove_by_user.get(uid, set())
        train_by_user[uid] = [
            e for e in edges if (e["product_id"], int(e["timestamp"])) not in remove
        ]
    return train_by_user


class RankingContext:
    def __init__(self, data: GraphData, targets: list[EvalTarget]):
        self.data = data
        self.targets = targets
        self.train_by_user = build_train_edges(data, targets)
        self.all_pids = list(data.products.keys())
        self.popularity_rank = self._build_popularity_rank()
        self.item_scores_by_user = self._build_item_cf_scores()
        self.user_cf_index = self._build_user_cf_index()

    def _build_popularity_rank(self) -> list[str]:
        return sorted(
            self.all_pids,
            key=lambda pid: (
                math.log1p(self.data.products[pid]["rating_count"]),
                self.data.products[pid]["avg_rating"],
            ),
            reverse=True,
        )

    def _positive_train_pids(self, uid: str) -> set[str]:
        return {
            e["product_id"]
            for e in self.train_by_user.get(uid, [])
            if e["rating"] >= 4.0
        }

    def _build_item_cf_scores(self) -> dict[str, dict[str, float]]:
        user_index: dict[str, int] = {}
        item_index: dict[str, int] = {}
        entries: list[tuple[int, int, float]] = []

        for uid, edges in self.train_by_user.items():
            for e in edges:
                if e["rating"] < 4.0:
                    continue
                u_idx = user_index.setdefault(uid, len(user_index))
                i_idx = item_index.setdefault(e["product_id"], len(item_index))
                entries.append((u_idx, i_idx, 1.0))

        n_users, n_items = len(user_index), len(item_index)
        R = np.zeros((n_users, n_items), dtype=np.float32)
        for u_idx, i_idx, value in entries:
            R[u_idx, i_idx] = value

        norms = np.linalg.norm(R, axis=0, keepdims=True)
        norms[norms == 0] = 1.0
        item_sim = (R / norms).T @ (R / norms)
        np.fill_diagonal(item_sim, 0.0)
        idx_to_item = {v: k for k, v in item_index.items()}

        scores_by_user: dict[str, dict[str, float]] = {}
        for uid, u_idx in user_index.items():
            vec = R[u_idx]
            scores = item_sim @ vec
            scores[vec > 0] = -np.inf
            user_scores = {
                idx_to_item[i]: float(s)
                for i, s in enumerate(scores)
                if np.isfinite(s) and s > 0
            }
            scores_by_user[uid] = user_scores
        return scores_by_user

    def _build_user_cf_index(self) -> dict[str, set[str]]:
        item_users: dict[str, set[str]] = defaultdict(set)
        for uid, edges in self.train_by_user.items():
            for e in edges:
                if e["rating"] >= 4.0:
                    item_users[e["product_id"]].add(uid)
        return dict(item_users)

    def _rank_from_scores(self, scores: dict[str, float], exclude: set[str], k: int) -> list[str]:
        ranked = sorted(
            (pid for pid, score in scores.items() if pid not in exclude and score > 0),
            key=lambda pid: scores[pid],
            reverse=True,
        )
        return ranked[:k]

    def recommend_popularity(self, target: EvalTarget, k: int) -> list[str]:
        return [pid for pid in self.popularity_rank if pid not in target.train_pids][:k]

    def recommend_item_cf(self, target: EvalTarget, k: int) -> list[str]:
        return self._rank_from_scores(
            self.item_scores_by_user.get(target.user_id, {}),
            target.train_pids,
            k,
        )

    def recommend_user_cf(self, target: EvalTarget, k: int) -> list[str]:
        peers: Counter[str] = Counter()
        for seed in target.train_positive_pids:
            peers.update(self.user_cf_index.get(seed, set()))
        peers.pop(target.user_id, None)

        scores: Counter[str] = Counter()
        for peer_uid, peer_weight in peers.items():
            for e in self.train_by_user.get(peer_uid, []):
                if e["rating"] >= 4.0 and e["product_id"] not in target.train_pids:
                    scores[e["product_id"]] += peer_weight
        return [pid for pid, _ in scores.most_common(k)]

    def _kg_scores(self, target: EvalTarget) -> dict[str, float]:
        profile = Counter()
        category_profile = Counter()
        for seed in target.train_positive_pids:
            profile.update(self.data.attrs_by_product.get(seed, set()))
            category_profile.update(self.data.cats_by_product.get(seed, set()))

        scores: dict[str, float] = {}
        for pid in self.all_pids:
            if pid in target.train_pids:
                continue
            attrs = self.data.attrs_by_product.get(pid, set())
            cats = self.data.cats_by_product.get(pid, set())
            shared_attrs = sum(profile[a] for a in attrs)
            shared_cats = sum(category_profile[c] for c in cats)
            if shared_attrs <= 0 and shared_cats <= 0:
                continue
            meta = self.data.products[pid]
            scores[pid] = (
                shared_attrs * 1.35
                + shared_cats * 0.4
                + meta["avg_rating"] * 0.45
                + math.log1p(meta["rating_count"]) * 0.12
            )
        return scores

    def recommend_kg_meta_path_v1(self, target: EvalTarget, k: int) -> list[str]:
        return self._rank_from_scores(self._kg_scores(target), target.train_pids, k)

    @staticmethod
    def _rrf(ranked: list[str], weight: float, base: int = 60) -> dict[str, float]:
        return {pid: weight / (base + rank) for rank, pid in enumerate(ranked, start=1)}

    def recommend_hybrid_v2(self, target: EvalTarget, k: int) -> list[str]:
        pool_k = max(k, 100)
        lists = {
            "item_cf": self.recommend_item_cf(target, pool_k),
            "user_cf": self.recommend_user_cf(target, pool_k),
            "kg": self.recommend_kg_meta_path_v1(target, pool_k),
            "pop": self.recommend_popularity(target, pool_k),
        }
        weights = {"item_cf": 3.0, "user_cf": 2.3, "kg": 1.5, "pop": 0.4}
        fused: Counter[str] = Counter()
        for name, ranked in lists.items():
            fused.update(self._rrf(ranked, weights[name]))
        for pid in target.train_pids:
            fused.pop(pid, None)
        return [pid for pid, _ in fused.most_common(k)]


METHODS = {
    "popularity": RankingContext.recommend_popularity,
    "item_cf": RankingContext.recommend_item_cf,
    "user_cf": RankingContext.recommend_user_cf,
    "kg_meta_path_v1": RankingContext.recommend_kg_meta_path_v1,
    "hybrid_v2": RankingContext.recommend_hybrid_v2,
}


def hit_rank(target_pid: str, ranked: list[str]) -> int | None:
    try:
        return ranked.index(target_pid) + 1
    except ValueError:
        return None


def ndcg(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / math.log2(rank + 1)


def mrr(rank: int | None, cutoff: int) -> float:
    return 0.0 if rank is None or rank > cutoff else 1.0 / rank


def diversity_for_list(pids: list[str], data: GraphData) -> float:
    if len(pids) < 2:
        return 0.0
    dists: list[float] = []
    for i in range(len(pids)):
        a = data.attrs_by_product.get(pids[i], set())
        for j in range(i + 1, len(pids)):
            b = data.attrs_by_product.get(pids[j], set())
            if not a and not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            dists.append(1.0 - inter / union if union else 0.0)
    return sum(dists) / len(dists) if dists else 0.0


def summarize(records: list[dict[str, Any]], data: GraphData, cutoffs: list[int]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "n_users": len(records),
        "n_products": len(data.products),
        "cutoffs": cutoffs,
        "methods": {},
    }
    report_k = min(cutoffs)
    for method in METHODS:
        ranks = [r["ranks"][method] for r in records]
        rec_lists = [r["recommendations"][method][:report_k] for r in records]
        all_recommended = {pid for recs in rec_lists for pid in recs}
        ratings = [
            data.products[pid]["avg_rating"]
            for recs in rec_lists for pid in recs
            if pid in data.products
        ]
        method_summary: dict[str, float] = {
            f"coverage@{report_k}": round(len(all_recommended) / max(1, len(data.products)), 4),
            f"avg_rating@{report_k}": round(float(np.mean(ratings)), 4) if ratings else 0.0,
            f"diversity@{report_k}": round(float(np.mean([diversity_for_list(x, data) for x in rec_lists])), 4),
        }
        for c in cutoffs:
            method_summary[f"HR@{c}"] = round(sum(1 for rk in ranks if rk is not None and rk <= c) / len(ranks), 4)
            method_summary[f"NDCG@{c}"] = round(sum(ndcg(rk) if rk is not None and rk <= c else 0.0 for rk in ranks) / len(ranks), 4)
            method_summary[f"MRR@{c}"] = round(sum(mrr(rk, c) for rk in ranks) / len(ranks), 4)
        summary["methods"][method] = method_summary
    return summary


def plot_summary(summary: dict[str, Any], out_dir: Path) -> None:
    methods = list(summary["methods"].keys())
    labels = {
        "popularity": "Popularity",
        "item_cf": "Item-CF",
        "user_cf": "User-CF",
        "kg_meta_path_v1": "KG Meta-path v1",
        "hybrid_v2": "Hybrid v2",
    }
    cut = max(summary["cutoffs"])
    top_k = min(summary["cutoffs"])

    def values(metric: str) -> list[float]:
        return [summary["methods"][m][metric] for m in methods]

    x = np.arange(len(methods))
    fig, ax = plt.subplots(figsize=(10, 5))
    width = 0.25
    ax.bar(x - width, values(f"HR@{cut}"), width, label=f"HR@{cut}")
    ax.bar(x, values(f"NDCG@{cut}"), width, label=f"NDCG@{cut}")
    ax.bar(x + width, values(f"MRR@{cut}"), width, label=f"MRR@{cut}")
    ax.set_title("Offline Ranking Accuracy")
    ax.set_ylabel("Score")
    ax.set_xticks(x, [labels[m] for m in methods], rotation=20, ha="right")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "accuracy_metrics.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    width = 0.25
    ax.bar(x - width, values(f"coverage@{top_k}"), width, label=f"Catalog Coverage@{top_k}")
    ax.bar(x, values(f"avg_rating@{top_k}"), width, label=f"Avg Rating@{top_k}")
    ax.bar(x + width, values(f"diversity@{top_k}"), width, label=f"Diversity@{top_k}")
    ax.set_title("Operational Quality Metrics")
    ax.set_ylabel("Score")
    ax.set_xticks(x, [labels[m] for m in methods], rotation=20, ha="right")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "quality_metrics.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    for method in methods:
        ax.plot(summary["cutoffs"], [summary["methods"][method][f"HR@{c}"] for c in summary["cutoffs"]], marker="o", label=labels[method])
    ax.set_title("Hit Rate by Cutoff")
    ax.set_xlabel("K")
    ax.set_ylabel("HR@K")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "hr_by_cutoff.png", dpi=180)
    plt.close(fig)


def write_report(summary: dict[str, Any], out_dir: Path, phase_note: str) -> None:
    methods = summary["methods"]
    cut = max(summary["cutoffs"])
    top_k = min(summary["cutoffs"])
    best = max(methods, key=lambda m: methods[m][f"NDCG@{cut}"])

    lines = [
        "# 推荐算法横向对比与进化报告",
        "",
        "## 实验设置",
        "",
        f"- 评估用户数：{summary['n_users']}",
        f"- 商品数：{summary['n_products']}",
        f"- 留出方式：每个用户最新一条 4 星及以上评分作为未来目标商品。",
        f"- 指标：HR@K、NDCG@K、MRR@K、Catalog Coverage@{top_k}、Avg Rating@{top_k}、Diversity@{top_k}。",
        f"- 本轮说明：{phase_note}",
        "",
        "## 横向结果",
        "",
        f"![accuracy](accuracy_metrics.png)",
        "",
        f"![quality](quality_metrics.png)",
        "",
        f"![hr](hr_by_cutoff.png)",
        "",
        "| 方法 | HR@{} | NDCG@{} | MRR@{} | Coverage@{} | Diversity@{} |".format(cut, cut, cut, top_k, top_k),
        "|---|---:|---:|---:|---:|---:|",
    ]
    for method, vals in methods.items():
        lines.append(
            f"| {method} | {vals[f'HR@{cut}']:.4f} | {vals[f'NDCG@{cut}']:.4f} | "
            f"{vals[f'MRR@{cut}']:.4f} | {vals[f'coverage@{top_k}']:.4f} | {vals[f'diversity@{top_k}']:.4f} |"
        )

    lines.extend([
        "",
        "## 客观判断",
        "",
        f"- 当前最优方法按 NDCG@{cut} 判断为：`{best}`。",
        "- 如果 `kg_meta_path_v1` 低于 Item-CF/User-CF，这是合理现象：KG 属性路径更擅长解释和语义泛化，不一定擅长精确预测未来同一个 ASIN。",
        "- 如果 `hybrid_v2` 高于单一 KG 方法，说明应把协同行为信号纳入线上排序，而不是只依赖属性元路径。",
        "",
        "## 结论与改进方向",
        "",
        "- 保留 KG 元路径作为解释层和语义召回层。",
        "- 引入 Item-CF/User-CF 作为行为协同排序信号，提高 exact-ASIN 离线预测能力。",
        "- 继续增加 VIEWED/点击数据后，可重新评估在线行为对排序的贡献。",
    ])
    (out_dir / "算法进化报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    recommender = Recommender(config_path=args.config)
    try:
        data = fetch_graph(recommender)
    finally:
        recommender.close()

    targets = build_targets(data)
    rng = random.Random(args.seed)
    targets.sort(key=lambda t: t.user_id)
    if args.sample:
        targets = rng.sample(targets, min(args.sample, len(targets)))
    targets.sort(key=lambda t: t.user_id)

    preflight = {
        "products": len(data.products),
        "users_with_ratings": len(data.ratings_by_user),
        "eligible_users": len(build_targets(data)),
        "evaluated_users": len(targets),
        "price_coverage": sum(1 for p in data.products.values() if p["price"] is not None) / max(1, len(data.products)),
        "image_coverage": sum(1 for p in data.products.values() if p["image_url"] is not None) / max(1, len(data.products)),
        "avg_rating_coverage": sum(1 for p in data.products.values() if p["avg_rating"] > 0) / max(1, len(data.products)),
    }
    (out_dir / "preflight.json").write_text(json.dumps(preflight, indent=2, ensure_ascii=False), encoding="utf-8")

    ctx = RankingContext(data, targets)
    max_k = max(args.cutoffs)
    records: list[dict[str, Any]] = []
    for idx, target in enumerate(targets, start=1):
        recommendations: dict[str, list[str]] = {}
        ranks: dict[str, int | None] = {}
        for method, func in METHODS.items():
            recs = func(ctx, target, max_k)
            recommendations[method] = recs
            ranks[method] = hit_rank(target.target_product_id, recs)
        records.append({
            "user_id": target.user_id,
            "target_product_id": target.target_product_id,
            "train_count": len(target.train_pids),
            "recommendations": recommendations,
            "ranks": ranks,
        })
        if idx % 100 == 0 or idx == len(targets):
            print(f"evaluated {idx}/{len(targets)} users")

    result_path = out_dir / "per_user_results.jsonl"
    with result_path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = summarize(records, data, args.cutoffs)
    summary["preflight"] = preflight
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    plot_summary(summary, out_dir)
    write_report(summary, out_dir, args.phase_note)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"saved to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.yaml")
    parser.add_argument("--out-dir", type=Path, default=Path("reports/algorithm_evolution"))
    parser.add_argument("--cutoffs", type=int, nargs="+", default=[10, 20, 50])
    parser.add_argument("--sample", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--phase-note", default="第一轮横向实验 + hybrid_v2 改进方案。")
    args = parser.parse_args()
    args.cutoffs = sorted(set(args.cutoffs))
    run(args)


if __name__ == "__main__":
    main()
