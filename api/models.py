from __future__ import annotations

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    query: str
    limit: int = 10


class AttributeFilter(BaseModel):
    attribute_type: str
    value: str
    weight: float = 1.0


class SearchIntent(BaseModel):
    attribute_filters: list[AttributeFilter]
    keywords: list[str]
    price_max: float | None = None
    min_rating: float | None = None


class MatchedAttribute(BaseModel):
    attribute_type: str
    name: str
    value: str
    confidence: float
    evidence: str | None = None


class Recommendation(BaseModel):
    product_id: str
    title: str
    image_url: str | None = None
    price: float | None = None
    average_rating: float | None = None
    rating_number: int | None = None
    score: float
    matched_attributes: list[MatchedAttribute]
    matched_terms: list[str] = Field(default_factory=list)
    matched_feature_evidence: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    explanation: str


class RecommendResponse(BaseModel):
    query: str
    intent: SearchIntent
    recommendations: list[Recommendation]


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
