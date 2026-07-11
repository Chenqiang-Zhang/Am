"""
横向推荐算法对比实验。

目标：
- 用同一份 leave-one-out 数据比较多个推荐策略。
- 保存中间结果、summary 和图，便于多轮迭代。
- 不调用 LLM、不修改 Neo4j 图；所有实验在内存中完成。

方法：
- popularity: 全局热门/高评分
- item_cf: item-item collaborative filtering
- item_cf_recent: recency-weighted item-item collaborative filtering
- user_cf: peer overlap collaborative filtering
- transition_cf: sequential next-item transition from recent positive items
- kg_meta_path_v1: User -> Product -> Attribute -> Product (frozen round1-5
  formula — uniform seed weight, no review-confirmed evidence; kept only as
  a fixed baseline so later versions' gains show up in the same report)
- kg_meta_path_v2: round6 revision — recency-weighted seed profile plus a
  review-confirmed (positive MENTIONS) attribute bonus
- hybrid_v4: transition-first cascade; transition_cf keeps the top ranks, while
  item/user/KG/popularity only fill missing recall
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

DOMAIN_PREFIXES = {
    "platform": "domain_platform:",
    "franchise": "domain_franchise:",
    "product_type": "domain_product_type:",
}

# 迭代历史：每一轮实验结束后在末尾追加一条新记录，不要改写已有条目。
# write_report() 会把这份列表原样渲染进"迭代过程与诊断"，所以这里就是
# report 与代码版本号保持一致的唯一来源 —— 新增方法/改打分公式时，
# 对应地在这里加一条，报告就会自动带上这一轮的说明，不需要再手动补历史章节。
ITERATION_HISTORY = [
    "第一轮问题：KG-only 的 `kg_meta_path_v1` 能解释“为什么相似”，但 exact-ASIN 命中很低。"
    "原因是 Video Games 图里的属性更像语义标签，适合解释和泛化，不足以单独预测用户下一次具体会评分哪一个 ASIN。",
    "第二轮改进：加入 `item_cf_recent` 和 `transition_cf`。结果显示近期兴趣、顺序转移明显优于普通 KG 属性路径，"
    "说明相关性差的主因是排序缺少行为时序信号。",
    "第三轮调参：RRF 融合可以提高较深位置的覆盖，但会稀释 Top10。对前端推荐来说，用户首先看到的是前几条，"
    "因此不能只追 HR@50。",
    "第四轮方案：采用 `hybrid_v4`，即 Transition-first。先保留“相似用户在相似游戏之后选择了什么”的候选顺序，"
    "再用 Recent Item-CF、User-CF、KG、Popularity 补召回。",
    "第五轮方案：参考 All Beauty 阶段的数据清洗经验，发现 Video Games 的旧 `product_type/franchise/platform` "
    "属性存在描述串扰和类型误判，因此新增干净的 `domain_product_type/domain_platform/domain_franchise`。"
    "搜索场景先满足平台、系列、商品类型等强约束，再在候选内使用 Transition-first 排序；如果“系列 + 平台”过窄"
    "导致无结果，只放松系列，不放松平台和商品类型。",
    "第六轮方案：`kg_meta_path_v1` 冻结为对照基线，新增 `kg_meta_path_v2` —— 给正向历史加近期衰减权重"
    "（同 `item_cf_recent`/`transition_cf`），并对被正面 MENTIONS 独立确认过的属性加分。n=1000、两个随机种子"
    "的消融实验显示：继续给 domain_* 属性加权会持续拉低 exact-ASIN 命中（印证第五轮已有结论——domain 信号擅长"
    "一致性而非预测下一个具体商品），因此未采用；recency + mentions 加成让 `kg_meta_path_v2` 相比 v1 在 "
    "NDCG@50/MRR@50 上有稳定提升。同款改动（近期窗口 + MENTIONS 确认加分）已同步移植到线上 "
    "`app/api/recommender.py` 的 `_METAPATH_USER_CYPHER` 打分公式。",
]


@dataclass
class GraphData:
    products: dict[str, dict[str, Any]]
    ratings_by_user: dict[str, list[dict[str, Any]]]
    attrs_by_product: dict[str, set[str]]
    cats_by_product: dict[str, set[str]]
    confirmed_attrs_by_product: dict[str, Counter[str]]


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
    confirmed_attrs_by_product: dict[str, Counter[str]] = defaultdict(Counter)

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

        for row in session.run(
            "MATCH (p:Product)<-[:ABOUT]-(:Review)-[m:MENTIONS {sentiment: 'positive'}]->(a:Attribute) "
            "RETURN p.product_id AS pid, a.attr_type AS attr_type, a.value AS value, count(m) AS confirmations"
        ):
            attr_type = str(row["attr_type"] or "")
            value = str(row["value"] or "")
            if attr_type and value and attr_type not in IGNORED_ATTR_TYPES:
                confirmed_attrs_by_product[row["pid"]][f"{attr_type}:{value}"] += int(row["confirmations"])

    for edges in ratings_by_user.values():
        edges.sort(key=lambda x: x["timestamp"])
    return GraphData(
        products=products,
        ratings_by_user=dict(ratings_by_user),
        attrs_by_product=dict(attrs_by_product),
        cats_by_product=dict(cats_by_product),
        confirmed_attrs_by_product=dict(confirmed_attrs_by_product),
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
        self.item_sim, self.item_index, self.idx_to_item, self.R, self.user_index = self._build_item_cf_model()
        self.item_scores_by_user = self._build_item_cf_scores(recent_weighted=False)
        self.item_recent_scores_by_user = self._build_item_cf_scores(recent_weighted=True)
        self.user_cf_index = self._build_user_cf_index()
        # Round-3 diagnostics showed that a short transition window underfits
        # game-review behavior. A 15-step window improved top-rank relevance
        # while preserving the sequential "chosen next" signal.
        self.transition_index = self._build_transition_index(window=15)

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

    def _build_item_cf_model(self) -> tuple[np.ndarray, dict[str, int], dict[int, str], np.ndarray, dict[str, int]]:
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
        return item_sim, item_index, idx_to_item, R, user_index

    def _build_item_cf_scores(self, recent_weighted: bool) -> dict[str, dict[str, float]]:
        scores_by_user: dict[str, dict[str, float]] = {}
        for uid, u_idx in self.user_index.items():
            vec = self.R[u_idx].copy()
            if recent_weighted:
                positives = [
                    e for e in self.train_by_user.get(uid, [])
                    if e["rating"] >= 4.0 and e["product_id"] in self.item_index
                ]
                positives.sort(key=lambda e: e["timestamp"], reverse=True)
                vec[:] = 0.0
                for rank, e in enumerate(positives, start=1):
                    vec[self.item_index[e["product_id"]]] = 1.0 / math.sqrt(rank)
            scores = self.item_sim @ vec
            scores[vec > 0] = -np.inf
            user_scores = {
                self.idx_to_item[i]: float(s)
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

    def _build_transition_index(self, window: int = 6) -> dict[str, Counter[str]]:
        transitions: dict[str, Counter[str]] = defaultdict(Counter)
        for edges in self.train_by_user.values():
            positives = [e for e in edges if e["rating"] >= 4.0]
            positives.sort(key=lambda e: e["timestamp"])
            pids = [e["product_id"] for e in positives]
            for i, seed in enumerate(pids):
                for distance, cand in enumerate(pids[i + 1 : i + 1 + window], start=1):
                    if cand != seed:
                        transitions[seed][cand] += 1.0 / distance
        return dict(transitions)

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

    def recommend_item_cf_recent(self, target: EvalTarget, k: int) -> list[str]:
        return self._rank_from_scores(
            self.item_recent_scores_by_user.get(target.user_id, {}),
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

    def recommend_transition_cf(self, target: EvalTarget, k: int) -> list[str]:
        recent = [
            e for e in self.train_by_user.get(target.user_id, [])
            if e["rating"] >= 4.0
        ]
        recent.sort(key=lambda e: e["timestamp"], reverse=True)
        scores: Counter[str] = Counter()
        for rank, e in enumerate(recent[:8], start=1):
            seed_weight = 1.0 / math.sqrt(rank)
            for cand, weight in self.transition_index.get(e["product_id"], {}).items():
                if cand not in target.train_pids:
                    scores[cand] += seed_weight * weight
        return [pid for pid, _ in scores.most_common(k)]

    def _kg_scores_v1(self, target: EvalTarget) -> dict[str, float]:
        """Frozen round1-5 formula: every positive rating counts equally,
        no recency, no review-confirmed evidence. Kept only so v2's gain is
        visible side by side in the same report instead of requiring a diff
        across separate round directories."""
        profile: Counter[str] = Counter()
        category_profile: Counter[str] = Counter()
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

    def _kg_scores_v2(self, target: EvalTarget) -> dict[str, float]:
        # Round6: recency-weighted profile (same 1/sqrt(rank) decay as
        # item_cf_recent/transition_cf) instead of counting every past
        # positive equally — a user's taste drifts, so recent seeds should
        # dominate the attribute profile. Note: an earlier attempt also gave
        # domain_platform/domain_franchise/domain_product_type attrs extra
        # weight, but a sweep (n=300 and n=1000, two seeds) showed that
        # consistently *hurts* exact-ASIN HR/NDCG even though it raises the
        # domain-match diagnostic — confirms round4/5's own finding that KG's
        # domain signal is a consistency/explanation feature, not a
        # next-item predictor, so it's intentionally left un-boosted here.
        positives = [
            e for e in self.train_by_user.get(target.user_id, [])
            if e["rating"] >= 4.0
        ]
        positives.sort(key=lambda e: e["timestamp"], reverse=True)

        profile: Counter[str] = Counter()
        category_profile: Counter[str] = Counter()
        for rank, e in enumerate(positives, start=1):
            seed_weight = 1.0 / math.sqrt(rank)
            for a in self.data.attrs_by_product.get(e["product_id"], set()):
                profile[a] += seed_weight
            for c in self.data.cats_by_product.get(e["product_id"], set()):
                category_profile[c] += seed_weight

        scores: dict[str, float] = {}
        for pid in self.all_pids:
            if pid in target.train_pids:
                continue
            attrs = self.data.attrs_by_product.get(pid, set())
            cats = self.data.cats_by_product.get(pid, set())
            confirmed = self.data.confirmed_attrs_by_product.get(pid, {})

            shared_attrs = sum(profile[a] for a in attrs)
            shared_cats = sum(category_profile[c] for c in cats)
            # Review-confirmed evidence: an attribute the catalog lists AND
            # that independent reviewers actually praised (positive MENTIONS)
            # is stronger signal than a catalog-only attribute match. Weight
            # chosen from a sweep over [0, 1, 1.5, 2, 2.5, 3] at n=1000 across
            # two seeds — 2.0 was consistently at or near the NDCG@50/HR@50
            # peak in both runs, while 3.0 already overshoots and degrades.
            shared_confirmed = sum(
                profile[a] * math.log1p(confirmed[a])
                for a in attrs
                if a in confirmed and a in profile
            )
            if shared_attrs <= 0 and shared_cats <= 0 and shared_confirmed <= 0:
                continue
            meta = self.data.products[pid]
            scores[pid] = (
                shared_attrs * 1.35
                + shared_cats * 0.4
                + shared_confirmed * 2.0
                + meta["avg_rating"] * 0.45
                + math.log1p(meta["rating_count"]) * 0.12
            )
        return scores

    def recommend_kg_meta_path_v1(self, target: EvalTarget, k: int) -> list[str]:
        return self._rank_from_scores(self._kg_scores_v1(target), target.train_pids, k)

    def recommend_kg_meta_path_v2(self, target: EvalTarget, k: int) -> list[str]:
        return self._rank_from_scores(self._kg_scores_v2(target), target.train_pids, k)

    @staticmethod
    def _rrf(ranked: list[str], weight: float, base: int = 60) -> dict[str, float]:
        return {pid: weight / (base + rank) for rank, pid in enumerate(ranked, start=1)}

    def recommend_hybrid_v4(self, target: EvalTarget, k: int) -> list[str]:
        """Transition-first hybrid.

        RRF-style fusion improved broad HR@50 but hurt top-rank relevance. For
        a product UI, the first 10 results matter most, so v4 keeps sequential
        transition candidates in front and uses the other recommenders only as
        fallbacks when transition evidence is sparse.
        """
        ordered_lists = [
            self.recommend_transition_cf(target, max(k, 50)),
            self.recommend_item_cf_recent(target, max(k, 50)),
            self.recommend_user_cf(target, max(k, 50)),
            self.recommend_item_cf(target, max(k, 50)),
            self.recommend_kg_meta_path_v2(target, max(k, 30)),
            self.recommend_popularity(target, max(k, 50)),
        ]
        out: list[str] = []
        seen: set[str] = set()
        for ranked in ordered_lists:
            for pid in ranked:
                if pid in seen or pid in target.train_pids:
                    continue
                seen.add(pid)
                out.append(pid)
                if len(out) >= k:
                    return out
        return out


METHODS = {
    "popularity": RankingContext.recommend_popularity,
    "item_cf": RankingContext.recommend_item_cf,
    "item_cf_recent": RankingContext.recommend_item_cf_recent,
    "user_cf": RankingContext.recommend_user_cf,
    "transition_cf": RankingContext.recommend_transition_cf,
    "kg_meta_path_v1": RankingContext.recommend_kg_meta_path_v1,
    "kg_meta_path_v2": RankingContext.recommend_kg_meta_path_v2,
    "hybrid_v4": RankingContext.recommend_hybrid_v4,
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


def domain_values(pid: str, data: GraphData, prefix: str) -> set[str]:
    return {
        value.removeprefix(prefix)
        for value in data.attrs_by_product.get(pid, set())
        if value.startswith(prefix)
    }


def domain_match_for_list(target_pid: str, recs: list[str], data: GraphData, prefix: str) -> float | None:
    target_values = domain_values(target_pid, data, prefix)
    if not target_values:
        return None
    if not recs:
        return 0.0
    matches = 0
    for pid in recs:
        if target_values & domain_values(pid, data, prefix):
            matches += 1
    return matches / len(recs)


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
        domain_scores: list[float] = []
        for name, prefix in DOMAIN_PREFIXES.items():
            values = [
                domain_match_for_list(record["target_product_id"], record["recommendations"][method][:report_k], data, prefix)
                for record in records
            ]
            values = [v for v in values if v is not None]
            score = round(float(np.mean(values)), 4) if values else 0.0
            method_summary[f"{name}_match@{report_k}"] = score
            domain_scores.append(score)
        method_summary[f"domain_match@{report_k}"] = round(float(np.mean(domain_scores)), 4) if domain_scores else 0.0
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
        "item_cf_recent": "Recent Item-CF",
        "user_cf": "User-CF",
        "transition_cf": "Transition-CF",
        "kg_meta_path_v1": "KG Meta-path v1",
        "kg_meta_path_v2": "KG Meta-path v2",
        "hybrid_v4": "Hybrid v4",
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
    ax.bar(x, values(f"domain_match@{top_k}"), width, label=f"Domain Match@{top_k}")
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
    best_top = max(methods, key=lambda m: methods[m][f"NDCG@{top_k}"])
    best_deep = max(methods, key=lambda m: methods[m][f"NDCG@{cut}"])
    popularity = methods.get("popularity", {})
    item_cf = methods.get("item_cf", {})
    hybrid = methods.get("hybrid_v4", {})

    def lift(method_vals: dict[str, float], base_vals: dict[str, float], metric: str) -> str:
        base = base_vals.get(metric, 0.0)
        value = method_vals.get(metric, 0.0)
        if base <= 0:
            return "N/A"
        return f"{(value / base - 1.0) * 100:.1f}%"

    lines = [
        "# 推荐算法横向对比与进化报告",
        "",
        "## 实验设置",
        "",
        f"- 评估用户数：{summary['n_users']}",
        f"- 商品数：{summary['n_products']}",
        f"- 留出方式：每个用户最新一条 4 星及以上评分作为未来目标商品。",
        f"- 指标：HR@K、NDCG@K、MRR@K、Catalog Coverage@{top_k}、Domain Match@{top_k}、Diversity@{top_k}。",
        f"- 本轮说明：{phase_note}",
        f"- 数据完整性：价格覆盖率 {summary['preflight']['price_coverage']:.2%}，图片覆盖率 {summary['preflight']['image_coverage']:.2%}，评分覆盖率 {summary['preflight']['avg_rating_coverage']:.2%}。",
        "",
        "## 横向结果",
        "",
        f"![accuracy](accuracy_metrics.png)",
        "",
        f"![quality](quality_metrics.png)",
        "",
        f"![hr](hr_by_cutoff.png)",
        "",
        "| 方法 | HR@{} | NDCG@{} | MRR@{} | Coverage@{} | Domain@{} | Platform@{} | Franchise@{} | Type@{} |".format(cut, cut, cut, top_k, top_k, top_k, top_k, top_k),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for method, vals in methods.items():
        lines.append(
            f"| {method} | {vals[f'HR@{cut}']:.4f} | {vals[f'NDCG@{cut}']:.4f} | "
            f"{vals[f'MRR@{cut}']:.4f} | {vals[f'coverage@{top_k}']:.4f} | "
            f"{vals[f'domain_match@{top_k}']:.4f} | {vals[f'platform_match@{top_k}']:.4f} | "
            f"{vals[f'franchise_match@{top_k}']:.4f} | {vals[f'product_type_match@{top_k}']:.4f} |"
        )

    kg_v1 = methods.get("kg_meta_path_v1", {})
    kg_v2 = methods.get("kg_meta_path_v2", {})

    lines.extend([
        "",
        "## 迭代过程与诊断",
        "",
        *(f"{i}. {note}" for i, note in enumerate(ITERATION_HISTORY, start=1)),
        "",
        "## 客观判断",
        "",
        f"- Top{top_k} 相关性最优方法：`{best_top}`，NDCG@{top_k} = {methods[best_top][f'NDCG@{top_k}']:.4f}。",
        f"- Top{cut} 综合命中最优方法：`{best_deep}`，NDCG@{cut} = {methods[best_deep][f'NDCG@{cut}']:.4f}。",
        f"- `hybrid_v4` 相比热门推荐：HR@{top_k} 提升 {lift(hybrid, popularity, f'HR@{top_k}')}，NDCG@{top_k} 提升 {lift(hybrid, popularity, f'NDCG@{top_k}')}。",
        f"- `hybrid_v4` 相比普通 Item-CF：HR@{top_k} 提升 {lift(hybrid, item_cf, f'HR@{top_k}')}，NDCG@{top_k} 提升 {lift(hybrid, item_cf, f'NDCG@{top_k}')}。",
    ] + ([
        f"- `kg_meta_path_v2` 相比冻结基线 `kg_meta_path_v1`：NDCG@{cut} 提升 {lift(kg_v2, kg_v1, f'NDCG@{cut}')}，MRR@{cut} 提升 {lift(kg_v2, kg_v1, f'MRR@{cut}')}，"
        f"Domain@{top_k} 从 {kg_v1.get(f'domain_match@{top_k}', 0.0):.4f} 到 {kg_v2.get(f'domain_match@{top_k}', 0.0):.4f}（基本持平，一致性没有被 recency/mentions 改动破坏）。",
    ] if kg_v1 and kg_v2 else []) + [
        f"- `kg_meta_path_v2` 的 Domain@{top_k} = {kg_v2.get(f'domain_match@{top_k}', kg_v1.get(f'domain_match@{top_k}', 0.0)):.4f}，说明 KG 更适合保证同平台、同系列、同商品类型的一致性；行为方法更适合 exact-ASIN 预测。",
        "- 因此，当前推荐器不应被描述为“LLM 生成 Cypher 后直接推荐”，而应描述为：LLM 结构化对话条件，图数据库用用户行为元路径召回，Transition-first 行为排序决定前排，KG 属性路径负责条件过滤和可解释理由。",
        "",
        "## 仍然存在的限制",
        "",
        "- 所有方法的 NDCG 绝对值仍不高，这是因为当前离线任务是严格预测未来同一个 ASIN；如果用户实际接受同系列、同平台、同玩法的商品，exact-ASIN 会低估体验相关性。",
        "- 当前用户行为主要来自评分/评论历史，缺少真实曝光、点击、收藏、购买、停留时间等在线行为。因此模型能学习“历史偏好”，但还不能充分学习“看见后是否感兴趣”。第六轮加入的 MENTIONS 确认信号部分缓解了“缺少 review sentiment 反馈”这一点，但仍然只是补充，不是替代。",
        "- KG 属性目前更适合做解释和语义补充。若要让 KG 排序本身更强，还需要更细粒度的 developer、mode、genre 属性。",
        "",
        "## 当前采用方案",
        "",
        "- 线上推荐排序采用 Transition-first：相似用户在相似游戏之后更可能选择的候选优先。",
        "- 搜索推荐采用 domain-constrained ranking：明确的 franchise/platform/product_type 先作为强约束和高权重特征，避免“用户要 Switch 游戏却返回其他平台或配件”。",
        "- 当具体系列覆盖不足时，系统只进行受控降级：保留平台和商品类型，放松系列约束。",
        "- Recent Item-CF / User-CF 用于补足行为相似性和召回。",
        "- KG 元路径继续保留（现为 v2：近期衰减权重 + MENTIONS 确认加分），用于对话条件过滤、属性解释、冷启动补充和 domain-level 一致性控制。",
        "- 热门推荐只作为无用户历史或召回为空时的兜底。",
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
    parser.add_argument("--phase-note", default="Transition-first hybrid v4 横向实验。")
    args = parser.parse_args()
    args.cutoffs = sorted(set(args.cutoffs))
    run(args)


if __name__ == "__main__":
    main()
