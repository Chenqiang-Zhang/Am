from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

_cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
_api_cfg: dict = {}
if _cfg_path.exists():
    with _cfg_path.open(encoding="utf-8") as f:
        _api_cfg = (yaml.safe_load(f) or {}).get("api", {})

RECOMMEND_LIMIT_DEFAULT: int = _api_cfg.get("recommend_limit_default", 10)
RECOMMEND_LIMIT_MAX: int = _api_cfg.get("recommend_limit_max", 50)


class RecommendRequest(BaseModel):
    query: str
    user_id: str | None = None
    limit: int = Field(default=RECOMMEND_LIMIT_DEFAULT, ge=1, le=RECOMMEND_LIMIT_MAX)
    lang: Literal["ja", "en"] = "en"


class HomeRecommendRequest(BaseModel):
    user_id: str | None = None  # None = 非個人化モード（人気商品フォールバックのみ）
    limit: int = Field(default=RECOMMEND_LIMIT_DEFAULT, ge=1, le=RECOMMEND_LIMIT_MAX)
    lang: Literal["ja", "en"] = "en"


class ViewLogRequest(BaseModel):
    user_id: str
    product_id: str
    search_id: str | None = None


class SearchIntent(BaseModel):
    cypher: str
    cypher_explanation: str
    applied_conditions: list[str] = Field(default_factory=list)
    condition_source: Literal["llm", "heuristic_fallback", "none"] = "none"
    retrieval_status: Literal["matched", "matched_after_relaxation", "no_match", "fallback_popular"] = "matched"
    no_result_reason: str | None = None
    candidate_count: int = 0
    hard_conditions: list[str] = Field(default_factory=list)
    soft_conditions: list[str] = Field(default_factory=list)


class MatchedAttr(BaseModel):
    attr_type: str
    value: str


class ReasonMetrics(BaseModel):
    condition_matches: int = 0
    behavior_matches: int = 0
    transition_peers: int = 0
    collaborative_peers: int = 0
    shared_rated_attributes: int = 0
    shared_viewed_attributes: int = 0
    review_confirmations: int = 0


class Recommendation(BaseModel):
    product_id: str
    title: str
    display_title: str | None = None  # titleの日本語訳（lang="ja"かつ翻訳済みの場合のみ）
    description: str | None = None  # already localized for the request's lang
    image_url: str | None = None
    price: float | None = None
    avg_rating: float | None = None
    rating_count: int | None = None
    score: float
    matched_attrs: list[MatchedAttr] = Field(default_factory=list)
    reason_metrics: ReasonMetrics = Field(default_factory=ReasonMetrics)
    explanation: str
    recommendation_source: str = "dialogue_only"


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
    limit: int = Field(default=RECOMMEND_LIMIT_DEFAULT, ge=1, le=RECOMMEND_LIMIT_MAX)
    lang: Literal["ja", "en"] = "ja"
    user_id: str | None = None


class ChatResponse(BaseModel):
    action: str  # "ask"（聞き返す） | "search"（推薦する）
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    preference_summary: list[str] = Field(default_factory=list)
    intent: SearchIntent | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    search_id: str | None = None  # action="search"のとき、VIEWEDと紐付けるためのSearchLog ID
    fallback: bool = False  # 条件検索が0件で人気商品へフォールバックしたか
    provisional: bool = False  # action="ask"中に表示する暫定推薦か


# ===== レビュー取得 =====
class ReviewItem(BaseModel):
    title: str | None = None
    text: str
    rating: float | None = None
    helpful_vote: int | None = None
    verified_purchase: bool | None = None
    translated: bool = False
    display_language: Literal["ja", "en"] = "en"


class ReviewsResponse(BaseModel):
    product_id: str
    reviews: list[ReviewItem]
    requested_language: Literal["ja", "en"] = "en"
    translated_count: int = 0
    fallback_count: int = 0


# ===== 商品説明文取得 =====
class DescriptionResponse(BaseModel):
    product_id: str
    description: str | None = None
    translated: bool = False


class GraphReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    graph_profile: str
    node_counts: dict[str, int]
    domain_coverage: dict[str, int]
    japanese_description_coverage: float
    japanese_review_coverage: float
    issues: list[str] = Field(default_factory=list)


# ===== 推薦フィードバック =====
class FeedbackRequest(BaseModel):
    user_id: str | None = None
    search_id: str | None = None
    helpful: bool
    lang: Literal["ja", "en"] = "ja"


class FeedbackResponse(BaseModel):
    status: str
    product_id: str


# ===== デモ用テストユーザー選択 =====
class SampleUser(BaseModel):
    user_id: str
    rated_count: int


class SampleUsersResponse(BaseModel):
    users: list[SampleUser]


# ===== 履歴クリア =====
class ClearHistoryResponse(BaseModel):
    viewed_deleted: int
    searches_deleted: int
    feedback_deleted: int = 0
