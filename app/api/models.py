from typing import Literal

from pydantic import BaseModel, Field

LIMIT_DEFAULT = 10
LIMIT_MAX = 50

Lang = Literal["ja", "en"]


class Reason(BaseModel):
    kind: Literal["tag", "aspect", "game", "studio"]
    label: str


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
