from __future__ import annotations

from pathlib import Path

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
    lang: str = "en"  # explanationの出力言語（"ja" | "en"）


class HomeRecommendRequest(BaseModel):
    user_id: str | None = None  # None = 非個人化モード（人気商品フォールバックのみ）
    limit: int = Field(default=RECOMMEND_LIMIT_DEFAULT, ge=1, le=RECOMMEND_LIMIT_MAX)
    lang: str = "en"  # explanationの出力言語（"ja" | "en"）


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
    display_title: str | None = None  # titleの日本語訳（lang="ja"かつ翻訳済みの場合のみ）
    image_url: str | None = None
    price: float | None = None
    avg_rating: float | None = None
    rating_count: int | None = None
    score: float
    matched_attrs: list[MatchedAttr] = Field(default_factory=list)
    explanation: str


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
    lang: str = "ja"  # 表示言語（"ja" | "en"）。LLMの質問・選択肢・サマリの言語に反映
    user_id: str | None = None


class ChatResponse(BaseModel):
    action: str  # "ask"（聞き返す） | "search"（推薦する）
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    preference_summary: list[str] = Field(default_factory=list)
    intent: SearchIntent | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    search_id: str | None = None  # action="search"のとき、VIEWEDと紐付けるためのSearchLog ID


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


# ===== デモ用テストユーザー選択 =====
class SampleUser(BaseModel):
    user_id: str
    rated_count: int


class SampleUsersResponse(BaseModel):
    users: list[SampleUser]
