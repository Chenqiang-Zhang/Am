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
