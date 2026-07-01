from __future__ import annotations

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    query: str
    user_id: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class HomeRecommendRequest(BaseModel):
    user_id: str
    limit: int = Field(default=10, ge=1, le=50)


class ViewLogRequest(BaseModel):
    user_id: str
    product_id: str
    search_id: str | None = None


class SearchIntent(BaseModel):
    cypher: str
    cypher_explanation: str


class MatchedAttr(BaseModel):
    attr_type: str
    value: str


class Recommendation(BaseModel):
    product_id: str
    title: str
    display_title: str | None = None
    display_language: str = "en"
    image_url: str | None = None
    price: float | None = None
    avg_rating: float | None = None
    rating_count: int | None = None
    score: float
    matched_attrs: list[MatchedAttr] = Field(default_factory=list)
    explanation: str
    display_explanation: str | None = None


class RecommendResponse(BaseModel):
    query: str
    mode: str = "search"  # "search" | "home"
    intent: SearchIntent
    recommendations: list[Recommendation]
    search_id: str
    fallback: bool = False


# ===== 対話型推薦（CRS） =====
class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    limit: int = 10
    lang: str = "ja"  # 表示言語（"ja" | "en"）。LLMの質問・選択肢・サマリの言語に反映


class ChatResponse(BaseModel):
    action: str  # "ask"（聞き返す） | "search"（推薦する）
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    preference_summary: list[str] = Field(default_factory=list)
    intent: SearchIntent | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)


# ===== レビュー取得 =====
class ReviewItem(BaseModel):
    title: str | None = None
    text: str
    rating: float | None = None
    helpful_vote: int | None = None
    verified_purchase: bool | None = None


class ReviewsResponse(BaseModel):
    product_id: str
    reviews: list[ReviewItem]


# ===== レコメンド理由フィードバック =====
class RecommendationFeedbackRequest(BaseModel):
    query: str | None = None
    lang: str = "ja"
    helpful: bool | None = None
    reason_rating: int | None = None
    selected_reasons: list[str] = Field(default_factory=list)
    comment: str | None = None


class RecommendationFeedbackResponse(BaseModel):
    status: str
    product_id: str
