from typing import Literal

from pydantic import BaseModel, Field

LIMIT_DEFAULT = 10
LIMIT_MAX = 50

Lang = Literal["ja", "en"]


class Reason(BaseModel):
    kind: Literal["tag", "aspect", "game", "studio"]
    label: str


EvidencePath = Literal["peer", "tag", "studio", "popular"]
StudioRole = Literal["developer", "publisher"]


class Evidence(BaseModel):
    """One graph path instance behind a static recommendation. "This game" is the recommended game,
    the seed is a game the player has played, and every count leaves the player out.

    - peer: seed <-PLAYED- `count` other players -PLAYED-> this game
    - tag: seed -HAS_TAG-> tag `bridge_id` <-HAS_TAG- this game
    - studio: seed -[`seed_role`]-> studio `bridge_id` <-[`candidate_role`]- this game;
      `count` is the number of games of the studio
    - popular: `count` other players -PLAYED-> this game

    `share` is this instance's part of the recommendation's score (0 when the score is 0).
    """

    path: EvidencePath
    seed_appid: str | None = None
    seed_name: str | None = None
    bridge_id: str | None = None
    bridge_name: str | None = None
    count: int | None = None
    seed_role: StudioRole | None = None
    candidate_role: StudioRole | None = None
    share: float = 0.0


class Recommendation(BaseModel):
    appid: str
    name: str
    description: str | None
    image_url: str
    store_url: str
    price: float | None
    currency: str | None
    tags: list[str]
    review_count: int
    recommend_rate: float | None
    score: float
    reasons: list[Reason]
    explanation: str
    # Static recommendations only: the path instances behind the score, headline first, and the
    # score split by path (the values add up to `score`).
    evidence: list[Evidence] | None = None
    contributions: dict[EvidencePath, float] | None = None


class RecommendRequest(BaseModel):
    player_id: str | None = None
    limit: int = Field(default=LIMIT_DEFAULT, ge=1, le=LIMIT_MAX)
    lang: Lang = "ja"


class RecommendResponse(BaseModel):
    recommendations: list[Recommendation]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    player_id: str | None = None
    limit: int = Field(default=LIMIT_DEFAULT, ge=1, le=LIMIT_MAX)
    lang: Lang = "ja"


class ChatResponse(BaseModel):
    reply: str
    options: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class ReviewItem(BaseModel):
    text: str
    recommended: bool | None
    votes_up: int | None
    created_at: str | None
    hours: float | None


class ReviewsResponse(BaseModel):
    appid: str
    reviews: list[ReviewItem]


class SamplePlayer(BaseModel):
    player_id: str
    played_count: int


class SamplePlayersResponse(BaseModel):
    players: list[SamplePlayer]
