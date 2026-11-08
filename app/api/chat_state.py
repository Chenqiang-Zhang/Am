"""Dialogue state and the interfaces between the parts of the conversational recommender.

The state holds graph node IDs only, never names or free text, so every turn's search is a pure function of
it. Names are linked to IDs before they reach the state (chat_nlu.py), and the state comes back from the
browser on every turn (ChatRequest.state), so it is validated as untrusted input: malformed keys are dropped,
lists are capped, and `restricted_to` drops IDs the graph does not know.

Attributes share one key space, "<kind>:<value>":

- tag:<group>    a tag group, the part of Tag.tag_id after the kind ("genre:action" and "usertag:action" are
                 both "tag:action"), so a Genre and a UserTag of the same name count once
- studio:<id>    Studio.studio_id
- price:<amount> a price band "at most <amount>" in the graph's currency (settings.CURRENCY: USD on UCSD,
                 JPY on live); including it sets max_price instead of filtering
"""

import json
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ATTRIBUTES = 12
MAX_ANCHORS = 3
MAX_SEEN_GAMES = 300
MAX_ASKED = 30

Relation = Literal["similar", "same_developer", "co_played"]
AttributeKind = Literal["tag", "studio", "price"]

ATTRIBUTE_KEY = re.compile(r"^(tag|studio):[a-z0-9_]+$|^price:\d+(\.\d+)?$")
APPID = re.compile(r"^\d+$")


def tag_key(tag_id: str) -> str:
    """The attribute key of a Tag.tag_id: "usertag:rogue_like" -> "tag:rogue_like"."""
    return "tag:" + tag_id.split(":", 1)[-1]


def attribute_kind(key: str) -> AttributeKind:
    return key.split(":", 1)[0]  # type: ignore[return-value]


def attribute_value(key: str) -> str:
    return key.split(":", 1)[1]


def _clean(values: list[Any], pattern: re.Pattern[str], cap: int) -> list[str]:
    """Keep well-formed strings, drop duplicates (first one wins) and keep the latest `cap` of them."""
    seen: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and pattern.match(value) and value not in seen:
            seen[value] = None
    return list(seen)[-cap:]


class ChatState(BaseModel):
    """What the user has said so far, as graph IDs.

    include / exclude are hard conditions. A key never sits in both: exclude wins, so a game the user said
    no to cannot come back. Price keys never stay in include or exclude; they become max_price.
    `shown` are games already presented and `rejected` games the user turned down; neither is offered again.
    `asked` are attribute keys the system has asked about or the user said they do not care about.
    """

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    relation: Relation | None = None
    max_price: float | None = Field(default=None, ge=0)
    shown: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    asked: list[str] = Field(default_factory=list)
    turn: int = Field(default=0, ge=0)

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _clean_attributes(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], ATTRIBUTE_KEY, MAX_ATTRIBUTES)

    @field_validator("asked", mode="before")
    @classmethod
    def _clean_asked(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], ATTRIBUTE_KEY, MAX_ASKED)

    @field_validator("anchors", mode="before")
    @classmethod
    def _clean_anchors(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], APPID, MAX_ANCHORS)

    @field_validator("shown", "rejected", mode="before")
    @classmethod
    def _clean_games(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], APPID, MAX_SEEN_GAMES)

    @model_validator(mode="after")
    def _consistent(self) -> "ChatState":
        prices = [float(attribute_value(key)) for key in self.include if attribute_kind(key) == "price"]
        if prices:
            self.max_price = min(prices + ([self.max_price] if self.max_price is not None else []))
        self.exclude = [key for key in self.exclude if attribute_kind(key) != "price"]
        self.include = [key for key in self.include if attribute_kind(key) != "price" and key not in self.exclude]
        if not self.anchors:
            self.relation = None
        elif self.relation is None:
            self.relation = "similar"
        return self

    def restricted_to(self, known_attributes: set[str], known_appids: set[str]) -> "ChatState":
        """Drop IDs the graph does not have. Price keys are not graph nodes and always pass."""

        def known(key: str) -> bool:
            return attribute_kind(key) == "price" or key in known_attributes

        return ChatState(
            include=[key for key in self.include if known(key)],
            exclude=[key for key in self.exclude if known(key)],
            anchors=[appid for appid in self.anchors if appid in known_appids],
            relation=self.relation,
            max_price=self.max_price,
            shown=[appid for appid in self.shown if appid in known_appids],
            rejected=[appid for appid in self.rejected if appid in known_appids],
            asked=[key for key in self.asked if known(key)],
            turn=self.turn,
        )

    def unavailable_appids(self) -> list[str]:
        """Games a search must not return: anchors, games already shown and games the user rejected."""
        return list(dict.fromkeys(self.anchors + self.shown + self.rejected))


class TurnUpdate(BaseModel):
    """What one user turn adds, already linked to IDs (by chat_nlu.py, or written directly by a simulator).

    - include / exclude: attribute keys the user wants / does not want
    - no_preference: keys the user does not care about; they leave include and exclude and are not asked again
    - anchors / relation: reference games and how candidates should relate to them
    - max_price: a new budget in USD, or None if the turn says nothing about price
    - reject_shown: the user turned down everything shown so far ("さっきの以外で")
    """

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    no_preference: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    relation: Relation | None = None
    max_price: float | None = Field(default=None, ge=0)
    reject_shown: bool = False

    @field_validator("include", "exclude", "no_preference", mode="before")
    @classmethod
    def _clean_attributes(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], ATTRIBUTE_KEY, MAX_ATTRIBUTES)

    @field_validator("anchors", mode="before")
    @classmethod
    def _clean_anchors(cls, values: Any) -> list[str]:
        return _clean(values if isinstance(values, list) else [], APPID, MAX_ANCHORS)


def apply_update(state: ChatState, update: TurnUpdate) -> ChatState:
    """The state after a user turn. A later statement about a key overrides an earlier one."""
    include = [key for key in state.include if key not in update.exclude and key not in update.no_preference]
    exclude = [key for key in state.exclude if key not in update.include and key not in update.no_preference]
    include += [key for key in update.include if key not in update.exclude]
    exclude += update.exclude
    anchors = state.anchors + update.anchors if update.anchors else state.anchors
    relation = update.relation or (state.relation if not update.anchors else None)
    max_price = update.max_price if update.max_price is not None else state.max_price
    rejected = state.rejected + state.shown if update.reject_shown else state.rejected
    return ChatState(
        include=include,
        exclude=exclude,
        anchors=anchors,
        relation=relation,
        max_price=max_price,
        shown=state.shown,
        rejected=rejected,
        asked=state.asked + update.no_preference,
        turn=state.turn + 1,
    )


def record_question(state: ChatState, key: str) -> ChatState:
    return state.model_copy(update={"asked": _clean(state.asked + [key], ATTRIBUTE_KEY, MAX_ASKED)})


def record_shown(state: ChatState, appids: list[str]) -> ChatState:
    return state.model_copy(update={"shown": _clean(state.shown + appids, APPID, MAX_SEEN_GAMES)})


EvidencePath = Literal["tag", "anchor_tag", "studio", "co_play"]


class ChatEvidence(BaseModel):
    """One graph path behind a dialogue candidate. Field names follow Evidence of the static recommender
    (PR #13: path, seed, bridge, count, share) so the two can be shown with the same card.

    - tag: candidate -HAS_TAG-> tag `bridge_id`, a tag the user asked for
    - anchor_tag: anchor `anchor_appid` -HAS_TAG-> tag `bridge_id` <-HAS_TAG- candidate
    - studio: anchor `anchor_appid` -DEVELOPED_BY-> studio `bridge_id` <-DEVELOPED_BY- candidate
    - co_play: anchor <-PLAYED- `count` players -PLAYED-> candidate
    """

    path: EvidencePath
    bridge_id: str | None = None
    anchor_appid: str | None = None
    count: int | None = None
    share: float = 0.0


class Candidate(BaseModel):
    appid: str
    score: float
    evidence: list[ChatEvidence] = Field(default_factory=list)


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Score descending, then appid ascending as a string, so equal scores always come out in one order."""
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.appid))


class Retriever(Protocol):
    """Graph templates (G), vectors (V), the LLM alone (L) or popularity (P), behind one call.

    Returns at most `k` candidates that satisfy the state's hard conditions, ranked by `rank_candidates`, and
    never a game from `state.unavailable_appids()`. The pipeline passes a large `k` to get the candidate pool
    the questioner looks at, and shows the first ones.
    """

    def retrieve(self, state: ChatState, k: int) -> list[Candidate]: ...


class Questioner(Protocol):
    """Picks the attribute key to ask about next, or None to stop asking and recommend.

    `candidates` is the whole pool from the retriever, not just the games shown. A questioner never returns a
    key in `state.include`, `state.exclude` or `state.asked`.
    """

    def choose(self, state: ChatState, candidates: list[Candidate]) -> str | None: ...


class TurnLog(BaseModel):
    """One line of the per-turn JSONL log the dialogue evaluation reads."""

    dialogue_id: str
    turn: int
    state_before: ChatState
    update: TurnUpdate
    state_after: ChatState
    pool_size: int
    shown: list[Candidate]
    question: str | None
    entropy: dict[str, float] = Field(default_factory=dict)


def append_turn_log(path: Path, log: TurnLog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(log.model_dump(), ensure_ascii=False) + "\n")
