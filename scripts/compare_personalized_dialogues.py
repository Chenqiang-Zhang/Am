#!/usr/bin/env python3
"""Run the same short dialogue for anonymous and real users, then save a comparison."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow direct execution via `python scripts/compare_personalized_dialogues.py`.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.recommender import Recommender


def _run_dialogue(
    recommender: Recommender,
    user_id: str | None,
    opening: str,
    replies: list[str],
    lang: str,
    limit: int,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = [{"role": "user", "content": opening}]
    turns: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for reply in [*replies, "こだわりなし"]:
        result = recommender.chat(messages, limit=limit, lang=lang, user_id=user_id)
        turns.append({
            "action": result["action"],
            "question": result.get("question"),
            "options": result.get("options", []),
        })
        if result["action"] == "search":
            break
        messages.append({"role": "assistant", "content": result.get("question") or ""})
        messages.append({"role": "user", "content": reply})

    intent = result.get("intent") or {}
    cypher = getattr(intent, "cypher", "") if not isinstance(intent, dict) else intent.get("cypher", "")
    return {
        "user_id": user_id or "anonymous",
        "turns": turns,
        "cypher_sha256": hashlib.sha256(cypher.encode()).hexdigest() if cypher else None,
        "top_products": [
            {"product_id": r.product_id, "title": r.title, "source": r.recommendation_source}
            for r in result.get("recommendations", [])
        ],
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Personalized Dialogue Comparison",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"Opening: {report['opening']}",
        "",
        "| User | First follow-up | First options | Cypher fingerprint | Top products |",
        "| --- | --- | --- | --- | --- |",
    ]
    for run in report["runs"]:
        first = run["turns"][0].get("question") if run["turns"] else "-"
        options = "; ".join(run["turns"][0].get("options", [])[:4]) if run["turns"] else "-"
        products = "; ".join(item["title"] for item in run["top_products"][:3]) or "No result"
        fingerprint = (run["cypher_sha256"] or "-")[:12]
        lines.append(f"| {run['user_id']} | {first or '-'} | {options or '-'} | {fingerprint} | {products} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opening", default="家族で遊べるゲームを探しています")
    parser.add_argument("--reply", action="append", default=["アクション", "協力プレイ"])
    parser.add_argument("--user", action="append", required=True, help="Repeat for two or more real user IDs")
    parser.add_argument("--lang", default="ja", choices=["ja", "en"])
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--output-dir", default="reports/personalization")
    args = parser.parse_args()

    recommender = Recommender()
    try:
        runs = [_run_dialogue(recommender, None, args.opening, args.reply, args.lang, args.limit)]
        runs.extend(_run_dialogue(recommender, user, args.opening, args.reply, args.lang, args.limit) for user in args.user)
    finally:
        recommender.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "opening": args.opening,
        "replies": args.reply,
        "runs": runs,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "dialogue_comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "dialogue_comparison.md").write_text(_markdown(report), encoding="utf-8")
    print(output_dir / "dialogue_comparison.md")


if __name__ == "__main__":
    main()
