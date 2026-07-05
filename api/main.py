from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .models import (
    BehaviorEventRequest,
    BehaviorEventResponse,
    ChatRequest,
    ChatResponse,
    HomeRecommendRequest,
    RecommendRequest,
    RecommendResponse,
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
    ReviewItem,
    ReviewsResponse,
)
from .recommender import Recommender

_recommender: Recommender | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _recommender
    _recommender = Recommender()
    yield
    if _recommender:
        _recommender.close()


app = FastAPI(title="KG Recommender API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/recommend", response_model=RecommendResponse)
async def recommend(req: RecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    intent, results = _recommender.recommend(req.query, req.limit, req.lang, req.user_id)
    return RecommendResponse(query=req.query, intent=intent, recommendations=results)


@app.post("/recommend/home", response_model=RecommendResponse)
async def recommend_home(req: HomeRecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    intent, results = _recommender.recommend_home(req.user_id, req.limit, req.lang)
    return RecommendResponse(query="[home]", intent=intent, recommendations=results)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    result = _recommender.chat([m.model_dump() for m in req.messages], req.limit, req.lang, req.user_id)
    return ChatResponse(**result)


@app.get("/products/{product_id}/reviews", response_model=ReviewsResponse)
async def get_reviews(product_id: str, limit: int = 5) -> ReviewsResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    rows = _recommender.get_reviews(product_id, limit)
    reviews = [ReviewItem(**r) for r in rows]
    return ReviewsResponse(product_id=product_id, reviews=reviews)


@app.post("/recommendations/{product_id}/feedback", response_model=RecommendationFeedbackResponse)
async def recommendation_feedback(
    product_id: str,
    req: RecommendationFeedbackRequest,
) -> RecommendationFeedbackResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    _recommender.save_feedback(product_id, req.model_dump())
    return RecommendationFeedbackResponse(status="ok", product_id=product_id)


@app.post("/behavior/events", response_model=BehaviorEventResponse)
async def behavior_event(req: BehaviorEventRequest) -> BehaviorEventResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    count = _recommender.log_behavior_event(req.model_dump())
    return BehaviorEventResponse(status="ok", event_count=count)
