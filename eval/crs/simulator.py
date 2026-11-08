"""The rule-based simulated user of the A layer and one simulated dialogue (E1_PLAN.md).

The user knows only the hidden target's attribute set A*, never its title or appid, and writes slots straight
into the state, so a dialogue measures the retriever and the questioner and nothing else. Selection of users,
targets and opening tags is by SHA-256 of fixed strings, so every run picks the same ones.
"""

import hashlib
import time
from collections.abc import Callable
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
from eval.crs.constraints import GameFacts

MAX_TURNS = 5
LIST_SIZE = 10
OPENING_TAGS = 2
DEV_SHARE = 0.3

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
    seconds: float = 0.0  # retrieval and question choice of this turn
    explanation: dict = field(default_factory=dict)


def simulate(
    target: Target,
    opening: list[str],
    retriever: Retriever,
    questioner: Questioner | None,
    pool_size: int,
    explain: Callable[[ChatState, list[Candidate]], dict] | None = None,
) -> list[TurnRecord]:
    """One dialogue: each turn updates the state, shows LIST_SIZE games, and before the last turn asks one question.

    Shown games are recorded at once and never come back. The dialogue ends at the first list with the target.
    `explain` (the entropy table, for logs) runs outside the timed part.
    """
    state = ChatState()
    update = TurnUpdate(include=opening)
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
                record.question, record.answer = question, target.answer(question)
                update = answer_update(question, record.answer)
        record.seconds = seconds
        if hit_rank is not None:
            break
    return records
