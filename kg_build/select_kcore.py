"""k-coreフィルタリングで、ユーザー・商品の両方が最低k件の相互作用を持つ部分グラフを
確定し、対象となる user_id / product_id (parent_asin) の一覧を保存する。

このスクリプトが生成するファイルは、既存の属性抽出・KG構築パイプラインの「商品選定」
ステップ（従来は config.yaml の scale.max_meta による上位N件選定）を置き換えるための
対象リストとして使う。

使い方:
    python kg_build/select_kcore.py --config config.yaml --k 14
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path

import yaml

from utils.product_fields import complete_product_ids


def load_edges(review_path: Path) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    with gzip.open(review_path, "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            u = d.get("user_id")
            p = d.get("parent_asin") or d.get("asin")
            if u and p:
                edges.add((u, p))
    return edges


def bipartite_kcore(
    edges: set[tuple[str, str]], k: int
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    user_items: dict[str, set[str]] = defaultdict(set)
    item_users: dict[str, set[str]] = defaultdict(set)
    for u, p in edges:
        user_items[u].add(p)
        item_users[p].add(u)

    changed = True
    while changed:
        changed = False
        for u in list(user_items.keys()):
            if len(user_items[u]) < k:
                for p in user_items[u]:
                    item_users[p].discard(u)
                del user_items[u]
                changed = True
        for p in list(item_users.keys()):
            if len(item_users[p]) < k:
                for u in item_users[p]:
                    user_items[u].discard(p)
                del item_users[p]
                changed = True
        user_items = {u: v for u, v in user_items.items() if v}
        item_users = {p: v for p, v in item_users.items() if v}

    return user_items, item_users


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--k", type=int, default=14,
        help="k-core threshold. 14 is calibrated for the default Video_Games scale, with "
             "the metadata-completeness filter applied (users=861 / items=610 / edges=18,771); "
             "re-sweep with a few --k values before trusting this default on a different "
             "genre/config or with --allow-incomplete-metadata.",
    )
    ap.add_argument("--out-dir", default=None, help="省略時は kg_output/<genre_lower>/kcore_selection")
    ap.add_argument(
        "--allow-incomplete-metadata", action="store_true",
        help="price/avg_rating/rating_count/description/brand/categoryのいずれかが欠損して"
             "いる商品を、k-core選定の対象から除外せずに許容する（デフォルトでは除外する）。",
    )
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.open(encoding="utf-8")) or {}
    review_path = Path(cfg["data"]["review_path"])
    meta_path = Path(cfg["data"]["meta_path"])
    min_feature_len = cfg.get("scale", {}).get("min_feature_len", 8)
    out_dir = Path(args.out_dir) if args.out_dir else Path(cfg["data"]["output_dir"]) / "kcore_selection"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"loading review edges from {review_path} ...")
    edges = load_edges(review_path)
    print(f"raw unique (user,item) edges: {len(edges)}")

    if not args.allow_incomplete_metadata:
        print(f"loading product metadata from {meta_path} to filter incomplete listings...")
        complete_ids = complete_product_ids(meta_path, min_feature_len)
        before = len(edges)
        edges = {(u, p) for (u, p) in edges if p in complete_ids}
        print(
            f"metadata completeness filter: kept {len(edges):,}/{before:,} edges "
            f"(products missing price/avg_rating/rating_count/description/brand/category "
            f"excluded before k-core; pass --allow-incomplete-metadata to skip this)"
        )

    user_items, item_users = bipartite_kcore(edges, args.k)
    n_users, n_items = len(user_items), len(item_users)
    n_edges = sum(len(v) for v in user_items.values())
    if n_users == 0:
        raise SystemExit(f"k={args.k} core is empty — choose a smaller k")

    print(f"k={args.k} core: users={n_users}, items={n_items}, edges={n_edges}, "
          f"avg_edges/user={n_edges/n_users:.2f}, avg_edges/item={n_edges/n_items:.2f}")

    users_path = out_dir / "selected_user_ids.txt"
    items_path = out_dir / "selected_product_ids.txt"
    users_path.write_text("\n".join(sorted(user_items)), encoding="utf-8")
    items_path.write_text("\n".join(sorted(item_users)), encoding="utf-8")

    summary = {
        "k": args.k,
        "users": n_users,
        "items": n_items,
        "edges": n_edges,
        "avg_edges_per_user": n_edges / n_users,
        "avg_edges_per_item": n_edges / n_items,
    }
    (out_dir / "kcore_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"saved: {users_path}")
    print(f"saved: {items_path}")
    print(f"saved: {out_dir / 'kcore_summary.json'}")


if __name__ == "__main__":
    main()
