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
