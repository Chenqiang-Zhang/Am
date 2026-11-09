"""The rule-based simulated user of the A layer and one simulated dialogue (E1_PLAN.md).

The user knows only the hidden target's attribute set A*, never its title or appid, and writes slots straight
into the state, so a dialogue measures the retriever and the questioner and nothing else. Selection of users,
targets and opening tags is by SHA-256 of fixed strings, so every run picks the same ones.
"""

import hashlib
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from app.api.chat_questioner import PRICE_BANDS, price_key
from app.api.chat_state import (
    Candidate,
    ChatState,
    Questioner,
    Retriever,
    TurnUpdate,
    apply_update,
    attribute_kind,
    attribute_value,
    record_question,
    record_shown,
    tag_key,
)
from app.api.chat_state import Relation
from eval.crs.constraints import GameFacts
from utils.graph_reader import TAG_MAX_RANK

MAX_TURNS = 5
LIST_SIZE = 10
OPENING_TAGS = 2
DEV_SHARE = 0.3
# a "similar" anchor shares at least this many user tags of rank within TAG_MAX_RANK with the target
ANCHOR_SHARED_TAGS = 2

Answer = Literal["yes", "no", "no_preference"]


def hash_value(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def is_dev(player_id: str) -> bool:
    """The fixed 30% development split; the rest is the test split."""
    return hash_value("e1-split:" + player_id) < DEV_SHARE * 2**256


def sample_order(player_ids: list[str]) -> list[str]:
    return sorted(player_ids, key=lambda player_id: hash_value("e1-sample:" + player_id))


def choose_target(player_id: str, hours_by_appid: dict[str, float], median_hours: dict[str, float]) -> str | None:
    """A game the player played at least as long as the median player of that game, picked by hash."""
    eligible = [appid for appid, hours in hours_by_appid.items()
                if appid in median_hours and hours >= median_hours[appid]]
    if not eligible:
        return None
    return min(eligible, key=lambda appid: hash_value("e1-target:" + player_id + appid))


@dataclass(frozen=True)
class Target:
    """The hidden game as the simulated user knows it."""

    player_id: str
    appid: str
    known: frozenset[str]  # A*: counted tag groups, developers and the price bands the price is within
    any_rank_tags: frozenset[str]  # every tag group of the game, at any rank
    price: float | None

    def answer(self, key: str) -> Answer:
        kind = attribute_kind(key)
        if kind == "price":
            if self.price is None:
                return "no_preference"
            return "yes" if self.price <= float(attribute_value(key)) else "no"
        if key in self.known:
            return "yes"
        if kind == "tag" and key in self.any_rank_tags:
            return "no_preference"
        return "no"


def make_target(player_id: str, appid: str, game: GameFacts) -> Target:
    """A* from the independent dump of the graph (constraints.py), not from the retrievers' queries."""
    bands = set()
    if game.price is not None:
        bands = {price_key(band) for band in PRICE_BANDS if game.price <= band}
    return Target(
        player_id=player_id,
        appid=appid,
        known=game.keys | bands,
        any_rank_tags=frozenset(tag_key(tag_id) for tag_id, _ in game.tag_edges),
        price=game.price,
    )


def askable_tags(target: Target, whitelist: tuple[str, ...]) -> list[str]:
    return [key for key in whitelist if attribute_kind(key) == "tag" and key in target.known]


def opening_tags(target: Target, whitelist: tuple[str, ...], seed: int) -> list[str]:
    """The OPENING_TAGS whitelisted tags of A* the user states first; only these change with the seed."""
    tags = askable_tags(target, whitelist)
    tags.sort(key=lambda key: hash_value(f"e1-open:{seed}:{target.player_id}:{key}"))
    return tags[:OPENING_TAGS]


@dataclass(frozen=True)
class Anchor:
    """A game the simulated user names in the opening, and how the wanted game relates to it."""

    appid: str
    relation: Relation
    same_series: bool  # the anchor's title and the target's look like one series (reported apart)


def ranked_tags(game: GameFacts) -> frozenset[str]:
    """User tag groups with a rank within TAG_MAX_RANK; genres and features have no rank and do not count."""
    return frozenset(tag_key(tag_id) for tag_id, rank in game.tag_edges if rank is not None and rank <= TAG_MAX_RANK)


def developers(game: GameFacts) -> set[str]:
    return {studio for studio in game.developers if studio}


_SERIES_CUT = re.compile(r"[:(]|\s[-–—]\s")
_SERIES_NUMBER = re.compile(r"(\s+(\d+|[ivx]+))+$")


def series_base(title: str) -> str:
    """The title up to a colon, a spaced dash or a bracket, lower-cased, without a trailing number or Roman
    numeral. A hyphen inside a word ("Half-Life") is kept."""
    base = _SERIES_CUT.split(title.lower(), maxsplit=1)[0]
    base = re.sub(r"[^\w\s]", " ", base)
    base = " ".join(base.split())
    return _SERIES_NUMBER.sub("", base).strip()


def same_series(a: str, b: str) -> bool:
    """The titles match up to the series base, e.g. "Portal" and "Portal 2", or "Borderlands 2" and "Borderlands:
    The Pre-Sequel", but not "Portal 2" and "Portal Knights". Bases shorter than 4 characters never match."""
    base_a, base_b = series_base(a), series_base(b)
    return len(base_a) >= 4 and base_a == base_b


def choose_anchor(player_id: str, target_appid: str, played: Iterable[str], facts: dict[str, GameFacts],
                  titles: dict[str, str]) -> Anchor | None:
    """Another game the player played that shares a developer with the target (same_developer) or at least
    ANCHOR_SHARED_TAGS ranked user tags (similar), picked by hash. None puts the player in the no-anchor layer."""
    target = facts[target_appid]
    target_developers, target_tags = developers(target), ranked_tags(target)
    eligible: dict[str, Relation] = {}
    for appid in played:
        if appid == target_appid or appid not in facts:
            continue
        game = facts[appid]
        if developers(game) & target_developers:
            eligible[appid] = "same_developer"
        elif len(ranked_tags(game) & target_tags) >= ANCHOR_SHARED_TAGS:
            eligible[appid] = "similar"
    if not eligible:
        return None
    appid = min(eligible, key=lambda appid: hash_value("e1-anchor:" + player_id + appid))
    return Anchor(appid, eligible[appid], same_series(titles.get(appid, ""), titles.get(target_appid, "")))


def appid_order(appid: str) -> tuple[int, str]:
    return (int(appid), appid) if appid.isdigit() else (2**63, appid)


def choose_co_play_anchor(target_appid: str, hours: dict[str, float], facts: dict[str, GameFacts],
                          titles: dict[str, str]) -> Anchor | None:
    """The co-play layer's anchor: the player's longest-played other game (ties by the smaller appid)."""
    others = [appid for appid in hours if appid != target_appid and appid in facts]
    if not others:
        return None
    appid = min(others, key=lambda appid: (-hours[appid], appid_order(appid)))
    return Anchor(appid, "co_played", same_series(titles.get(appid, ""), titles.get(target_appid, "")))


def flipped(answer: Answer) -> Answer:
    """A wrong answer: yes and no swap, no preference stays."""
    return {"yes": "no", "no": "yes"}.get(answer, answer)  # type: ignore[return-value]


def error_answers(target: Target, seed: int, error_rate: float) -> Callable[[int, str], Answer]:
    """Target.answer, but each answer is wrong with probability `error_rate`, decided by hash.

    The hash depends on the seed, the player, the turn and the question, not on the group, so two groups that
    ask the same question at the same turn get the same error.
    """
    def answer(turn: int, key: str) -> Answer:
        true = target.answer(key)
        wrong = hash_value(f"e1-error:{seed}:{target.player_id}:{turn}:{key}") < error_rate * 2**256
        return flipped(true) if wrong else true
    return answer


def answer_update(key: str, answer: Answer) -> TurnUpdate:
    if answer == "yes":
        return TurnUpdate(include=[key])
    if answer == "no":
        return TurnUpdate(exclude=[key])
    return TurnUpdate(no_preference=[key])


@dataclass
class TurnRecord:
    turn: int
    state_before: ChatState  # the state before this turn's user update
    update: TurnUpdate
    state: ChatState  # the state the list was retrieved for
    shown: list[Candidate]
    hit_rank: int | None  # 1-based rank of the target in the list, if it is there
    pool_size: int | None = None
    question: str | None = None
    answer: Answer | None = None
    true_answer: Answer | None = None  # differs from `answer` when the simulated user answered wrongly
    seconds: float = 0.0  # retrieval and question choice of this turn
    explanation: dict = field(default_factory=dict)


def simulate(
    target: Target,
    opening: list[str],
    retriever: Retriever,
    questioner: Questioner | None,
    pool_size: int,
    explain: Callable[[ChatState, list[Candidate]], dict] | None = None,
    answer: Callable[[int, str], Answer] | None = None,
    anchor: Anchor | None = None,
) -> list[TurnRecord]:
    """One dialogue: each turn updates the state, shows LIST_SIZE games, and before the last turn asks one question.

    Shown games are recorded at once and never come back. The dialogue ends at the first list with the target.
    `explain` (the entropy table, for logs) runs outside the timed part. `answer(turn, key)` replaces the
    target's own answers, e.g. with error_answers. An `anchor` is named in the opening with its relation.
    """
    state = ChatState()
    update = TurnUpdate(include=opening)
    if anchor is not None:
        update = TurnUpdate(include=opening, anchors=[anchor.appid], relation=anchor.relation)
    records = []
    for turn in range(1, MAX_TURNS + 1):
        before = state
        state = apply_update(state, update)
        start = time.perf_counter()
        shown = retriever.retrieve(state, LIST_SIZE)
        seconds = time.perf_counter() - start
        appids = [candidate.appid for candidate in shown]
        hit_rank = appids.index(target.appid) + 1 if target.appid in appids else None
        record = TurnRecord(turn, before, update, state, shown, hit_rank)
        records.append(record)
        state = record_shown(state, appids)
        update = TurnUpdate()
        if hit_rank is None and turn < MAX_TURNS and questioner is not None:
            start = time.perf_counter()
            pool = retriever.retrieve(state, pool_size)
            question = questioner.choose(state, pool)
            seconds += time.perf_counter() - start
            record.pool_size = len(pool)
            if explain is not None:
                record.explanation = explain(state, pool)
            if question is not None:
                state = record_question(state, question)
                record.question, record.true_answer = question, target.answer(question)
                record.answer = answer(turn, question) if answer is not None else record.true_answer
                update = answer_update(question, record.answer)
        record.seconds = seconds
        if hit_rank is not None:
            break
    return records
