"""Build the dialogue screen's mock data from recorded E1-pre dialogues of retriever G.

Each picked dialogue becomes a list of turns in the shape the dialogue API is meant to return (see
src/types/dialogue.ts): what the user said, the state, the ten games shown with their graph evidence, and the
question asked. Names, prices and images come from the graph. Run inside the api container:

    python app/web/scripts/build_dialogue_mock.py data/eval/e1_pre/<run> [<run> ...]

Writes app/web/src/mocks/dialogues.json.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.api.chat_queries import GraphRetriever  # noqa: E402
from app.api.chat_questioner import POOL_SIZE, QuestionCatalog, _weights, load_catalog  # noqa: E402
from app.api.chat_state import ChatState, attribute_kind, attribute_value, record_shown, tag_key  # noqa: E402
from app.api.presenters import build_image_url, build_store_url  # noqa: E402
from utils.graph_reader import GraphReader  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "src" / "mocks" / "dialogues.json"
PER_RELATION = 2  # dialogues per relation (and for no anchor)
ALTERNATIVES = 3  # runner-up questions shown next to the one asked


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8")]


def length(dialogue: dict) -> int:
    """How much of the dialogue there is to show: 2 for a hit at turn 3 or later after two questions, 1 for a
    hit after at least one question, 0 for any other hit, -1 for a miss."""
    if not dialogue["success_turn"]:
        return -1
    if dialogue["success_turn"] >= 3 and dialogue["questions"] >= 2:
        return 2
    return 1 if dialogue["questions"] >= 1 else 0


def pick(dialogues: list[dict], relation: str | None) -> list[dict]:
    """G hits with this relation, the longest dialogues first (a strong anchor often hits at turn 1), one per
    player. Players come in id order, so runs on the same users tend to show the same people."""
    mine = [d for d in dialogues if d["group"] == "G" and d.get("relation") == relation
            and (relation is None or d.get("anchored", True)) and length(d) >= 0]
    mine.sort(key=lambda d: (-length(d), d["dialogue_id"]))
    picked, players = [], set()
    for dialogue in mine:
        if dialogue["player_id"] not in players:
            picked.append(dialogue)
            players.add(dialogue["player_id"])
    return picked[:PER_RELATION]


class Names:
    def __init__(self, reader: GraphReader) -> None:
        self.games = {row["appid"]: row for row in reader.query(
            "MATCH (g:Game) RETURN g.appid AS appid, g.name AS name, g.price AS price")}
        self.tags: dict[str, str] = {}
        self.tag_by_id: dict[str, str] = {}
        for row in reader.query("MATCH (t:Tag) RETURN t.tag_id AS tag_id, t.name AS name ORDER BY tag_id"):
            # a group's label is its user tag's name when it has one, as players know the tag by that name
            self.tag_by_id[row["tag_id"]] = row["name"]
            key = tag_key(row["tag_id"])
            if key not in self.tags or row["tag_id"].startswith("usertag:"):
                self.tags[key] = row["name"]
        self.studios = {row["studio_id"]: row["name"] for row in reader.query(
            "MATCH (s:Studio) WHERE s.studio_id <> '' RETURN s.studio_id AS studio_id, s.name AS name")}

    def game(self, appid: str) -> dict:
        row = self.games.get(appid, {})
        return {"appid": appid, "name": row.get("name") or appid}

    def attribute(self, key: str) -> dict:
        kind = attribute_kind(key)
        value = attribute_value(key)
        if kind == "price":
            label = f"${float(value):g}"
        elif kind == "studio":
            label = self.studios.get(value, value)
        else:
            label = self.tags.get(key, value)
        return {"key": key, "kind": kind, "label": label}

    def evidence(self, item: dict) -> dict:
        path = item["path"]
        bridge = item.get("bridge_id")
        if path == "tag":
            label = self.tag_by_id.get(bridge, bridge)
        elif path == "anchor_tag":
            label = self.tags.get(bridge, bridge)
        elif path == "studio":
            label = self.studios.get(bridge, bridge)
        else:
            label = ""
        result = {"path": path, "label": label, "share": round(item.get("share") or 0.0, 4)}
        if item.get("anchor_appid"):
            result["anchor"] = self.game(item["anchor_appid"])
        if item.get("count") is not None:
            result["count"] = item["count"]
        return result


GAME_INFO_QUERY = """
UNWIND $appids AS id
MATCH (g:Game {appid: id})
CALL {
  WITH g
  OPTIONAL MATCH (g)<-[p:PLAYED]-(:Player)
  RETURN count(p) AS players, percentileCont(p.hours, 0.5) AS median_hours
}
CALL {
  WITH g
  OPTIONAL MATCH (r:Review)-[:ABOUT]->(g)
  RETURN count(r.recommended) AS rated, sum(CASE WHEN r.recommended THEN 1 ELSE 0 END) AS positive,
         count(CASE WHEN r.text IS NOT NULL AND size(r.text) > 10 THEN 1 END) AS readable
}
RETURN g.appid AS appid, g.release_date AS released, players, median_hours, rated, positive, readable,
       [(g)-[:DEVELOPED_BY]->(s:Studio) WHERE s.studio_id <> '' | s.name] AS developers,
       [(g)-[ht:HAS_TAG]->(t:Tag) WHERE ht.rank IS NOT NULL AND ht.rank <= $top_rank | {rank: ht.rank, name: t.name}]
         AS top_tags
"""
TOP_TAG_RANK = 5


def game_info(reader: GraphReader, appids: list[str]) -> dict[str, dict]:
    """What a card shows about a game besides the evidence: developers, year, play and review numbers, top tags."""
    info = {}
    for row in reader.query(GAME_INFO_QUERY, appids=sorted(set(appids)), top_rank=TOP_TAG_RANK):
        info[row["appid"]] = {
            "developers": sorted(set(row["developers"])),
            "year": int(row["released"][:4]) if row["released"] else None,
            "players": row["players"],
            "median_hours": round(row["median_hours"], 1) if row["median_hours"] is not None else None,
            "reviews": row["rated"],
            "positive": round(row["positive"] / row["rated"], 3) if row["rated"] else None,
            # what GET /games/{appid}/reviews can show: it leaves out texts of 10 characters or fewer
            "readable_reviews": row["readable"],
            "top_tags": [tag["name"] for tag in sorted(row["top_tags"], key=lambda tag: tag["rank"])],
        }
    return info


def checks(appid: str, state: dict, price: float | None, context: "Context", names: Names) -> list[dict]:
    """Each condition of the state, and whether this game meets it (by the questioner's catalog)."""
    result = [{"kind": "include", "attribute": names.attribute(key), "ok": context.catalog.has(appid, key)}
              for key in state["include"]]
    result += [{"kind": "exclude", "attribute": names.attribute(key), "ok": not context.catalog.has(appid, key)}
               for key in state["exclude"]]
    low, high = state["min_price"], state["max_price"]
    if low is not None or high is not None:
        ok = price is not None and (high is None or price <= high) and (low is None or price > low)
        result.append({"kind": "budget", "min": low, "max": high, "ok": ok})
    return result


def state_view(state: dict, names: Names) -> dict:
    return {
        "include": [names.attribute(key) for key in state["include"]],
        "exclude": [names.attribute(key) for key in state["exclude"]],
        "anchors": [names.game(appid) for appid in state["anchors"]],
        "relation": state["relation"],
        "max_price": state["max_price"],
        "min_price": state["min_price"],
    }


class Context:
    """What it takes to rebuild the questioner's pool: G's retriever and the question catalog."""

    def __init__(self, reader: GraphReader) -> None:
        self.reader = reader
        self.retriever = GraphRetriever(reader)
        self.catalog: QuestionCatalog = load_catalog(reader)
        self.info: dict[str, dict] = {}  # game_info for the games shown, filled before building


def question_info(turn: dict, question: str, player_id: str, context: Context, names: Names) -> dict:
    """Why this question: how the pool splits on it, and the runner-ups by entropy (from the turn log).

    The pool is retrieved again for the state after the list was shown, as the simulator did, and must have
    the logged size; otherwise the run was made with other code and the script stops.
    """
    state = record_shown(ChatState(**turn["state_after"]), [c["appid"] for c in turn["shown"]])
    pool = context.retriever.excluding_player(player_id).retrieve(state, POOL_SIZE)
    if len(pool) != turn["pool_size"]:
        raise SystemExit(f"{turn['dialogue_id']} turn {turn['turn']}: pool {len(pool)} != logged {turn['pool_size']}")
    weights = _weights(pool)
    has = [context.catalog.has(candidate.appid, question) for candidate in pool]
    entropy = turn["entropy"] or {}
    return {
        "pool": len(pool),
        "yes": sum(has),
        "no": len(pool) - sum(has),
        "share": round(sum(w for w, h in zip(weights, has) if h), 4),  # weighted by the retrieval scores
        "entropy": round(entropy.get(question, 0.0), 4),
        "alternatives": [
            {"attribute": names.attribute(key), "entropy": round(value, 4)}
            for key, value in entropy.items() if key != question
        ][:ALTERNATIVES],
    }


def build(dialogue: dict, turns: list[dict], names: Names, context: Context, run: str) -> dict:
    answers = {question: answer for question, answer in dialogue["answers"]}
    view_turns = []
    previous_question = None
    for turn in turns:
        update = turn["update"]
        user: dict = {
            "include": [names.attribute(key) for key in update["include"]],
            "exclude": [names.attribute(key) for key in update["exclude"]],
            "anchors": [names.game(appid) for appid in update["anchors"]],
            "relation": update["relation"],
        }
        if previous_question is not None:
            user["answer"] = {"question": names.attribute(previous_question),
                              "answer": answers.get(previous_question)}
        candidates = []
        for candidate in turn["shown"]:
            game = names.games.get(candidate["appid"], {})
            candidates.append({
                "appid": candidate["appid"],
                "name": game.get("name") or candidate["appid"],
                "image_url": build_image_url(candidate["appid"]),
                "store_url": build_store_url(candidate["appid"]),
                "price": game.get("price"),
                "score": round(candidate["score"], 4),
                "is_target": candidate["appid"] == dialogue["target"],
                "evidence": [names.evidence(item) for item in candidate["evidence"]],
                "checks": checks(candidate["appid"], turn["state_after"], game.get("price"), context, names),
                "info": context.info.get(candidate["appid"]),
            })
        question = turn["question"]
        why = question_info(turn, question, dialogue["player_id"], context, names) if question else None
        view_turns.append({
            "turn": turn["turn"],
            "user": user,
            "state": state_view(turn["state_after"], names),
            "candidates": candidates,
            "question": names.attribute(question) if question else None,
            "answer": answers.get(question) if question else None,
            "why": why,
            "hit_rank": next((i + 1 for i, c in enumerate(candidates) if c["is_target"]), None),
        })
        previous_question = question
    return {
        "id": dialogue["dialogue_id"],
        "run": run,
        "relation": dialogue.get("relation"),
        "anchor": names.game(dialogue["anchor"]) if dialogue.get("anchor") else None,
        "target": names.game(dialogue["target"]),
        "turns": view_turns,
    }


def main() -> None:
    runs = [Path(arg) for arg in sys.argv[1:]]
    if not runs:
        raise SystemExit(__doc__)
    reader = GraphReader()
    names = Names(reader)
    context = Context(reader)
    result = []
    taken: dict[str | None, int] = {}  # per relation over all runs: the first runs given win
    for run in runs:
        dialogues = load_jsonl(run / "dialogues.jsonl")
        relations = sorted({d.get("relation") for d in dialogues}, key=lambda r: r or "")
        picked = []
        for relation in relations:
            room = PER_RELATION - taken.get(relation, 0)
            mine = pick(dialogues, relation)[:max(room, 0)]
            taken[relation] = taken.get(relation, 0) + len(mine)
            picked += mine
        wanted = {d["dialogue_id"] for d in picked}
        turns: dict[str, list[dict]] = {}
        for line in (run / "turns.jsonl").open(encoding="utf-8"):
            if any(dialogue_id in line for dialogue_id in wanted):
                turn = json.loads(line)
                if turn["dialogue_id"] in wanted:
                    turns.setdefault(turn["dialogue_id"], []).append(turn)
        shown = [c["appid"] for dialogue_turns in turns.values() for turn in dialogue_turns for c in turn["shown"]]
        context.info.update(game_info(reader, [appid for appid in shown if appid not in context.info]))
        for dialogue in picked:
            ordered = sorted(turns[dialogue["dialogue_id"]], key=lambda turn: turn["turn"])
            result.append(build(dialogue, ordered, names, context, run.name))
    reader.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"dialogues": result}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(result)} dialogues to {OUT}")


if __name__ == "__main__":
    main()
