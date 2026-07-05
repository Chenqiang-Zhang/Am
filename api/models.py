from __future__ import annotations

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    query: str
    limit: int = 10
    lang: str = "en"
    user_id: str | None = None


class HomeRecommendRequest(BaseModel):
    user_id: str | None = None
    limit: int = 10
    lang: str = "en"


class AttributeFilter(BaseModel):
    attribute_type: str
    value: str
    weight: float = 1.0


class SearchIntent(BaseModel):
    attribute_filters: list[AttributeFilter]
    keywords: list[str]
    price_max: float | None = None
    min_rating: float | None = None


class QueryAction(BaseModel):
    name: str
    enabled: bool = True
    reason: str
    cypher_template: str | None = None


class QueryPlan(BaseModel):
    source: str = "controlled_query_plan"
    objective: str = "product_recommendation"
    user_input: str
    history_policy: str = "none"
    constraints: dict[str, str | int | float | bool | None | list[str]] = Field(default_factory=dict)
    actions: list[QueryAction] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)


class MatchedAttribute(BaseModel):
    attribute_type: str
    name: str
    value: str
    confidence: float
    evidence: str | None = None


class Recommendation(BaseModel):
    product_id: str
    title: str
    display_title: str | None = None
    display_language: str = "en"
    image_url: str | None = None
    price: float | None = None
    price_display: str | None = None
    availability_status: str = "available"
    data_quality_score: float | None = None
    average_rating: float | None = None
    rating_number: int | None = None
    score: float
    matched_attributes: list[MatchedAttribute]
    matched_terms: list[str] = Field(default_factory=list)
    matched_feature_evidence: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    reason_quantification: dict[str, float] = Field(default_factory=dict)
    explanation: str
    display_explanation: str | None = None


class RecommendResponse(BaseModel):
    query: str
    intent: SearchIntent
    query_plan: QueryPlan | None = None
    recommendations: list[Recommendation]


# ===== 対話型推薦（CRS） =====
class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    limit: int = 10
    lang: str = "ja"  # 表示言語（"ja" | "en"）。LLMの質問・選択肢・サマリの言語に反映
    user_id: str | None = None


class ChatResponse(BaseModel):
    action: str  # "ask"（聞き返す） | "search"（推薦する）
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    preference_summary: list[str] = Field(default_factory=list)
    intent: SearchIntent | None = None
    query_plan: QueryPlan | None = None
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


class BehaviorEventRequest(BaseModel):
    user_id: str
    event_type: str
    product_id: str | None = None
    product_ids: list[str] = Field(default_factory=list)
    query: str | None = None
    rank: int | None = None
    source: str = "chat"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class BehaviorEventResponse(BaseModel):
    status: str
    event_count: int
