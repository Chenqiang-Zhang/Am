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
    fallback: bool = False  # 条件検索が0件で人気商品へフォールバックしたか


# ===== レビュー取得 =====
class ReviewItem(BaseModel):
    title: str | None = None
    text: str
    rating: float | None = None
    helpful_vote: int | None = None
    translated: bool = False
    display_language: str = "en"


class ReviewsResponse(BaseModel):
    product_id: str
    reviews: list[ReviewItem]
    requested_language: str = "en"
    translated_count: int = 0
    fallback_count: int = 0


# ===== 商品説明文取得 =====
class DescriptionResponse(BaseModel):
    product_id: str
    description: str | None = None
    translated: bool = False


# ===== デモ用テストユーザー選択 =====
class SampleUser(BaseModel):
    user_id: str
    rated_count: int


class SampleUsersResponse(BaseModel):
    users: list[SampleUser]


# ===== 推薦理由のグラフ可視化 =====
class GraphNode(BaseModel):
    id: str  # "{type}:{自然キー}" 形式。例: "Product:B0001234"
    type: str  # "Product" | "User" | "Attribute" | "Brand" | "Category"
    label: str
    role: str | None = None  # "recommended" | "anchor" | "context"


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str  # "RATED" | "HAS_ATTRIBUTE" | "MADE_BY" | "BELONGS_TO"


class GraphData(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class ExplainGraphRequest(BaseModel):
    product_id: str
    user_id: str | None = None
    matched_attrs: list[MatchedAttr] = Field(default_factory=list)
    lang: str = "en"
