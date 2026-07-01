from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .models import (
    ChatRequest,
    ChatResponse,
    HomeRecommendRequest,
    RecommendRequest,
    RecommendResponse,
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
    ReviewItem,
    ReviewsResponse,
    ViewLogRequest,
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


app = FastAPI(title="KG Recommender API", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/recommend", response_model=RecommendResponse)
async def recommend(req: RecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results, fallback = _recommender.recommend(req.query, req.user_id, req.limit)
    return RecommendResponse(
        query=req.query, mode="search", intent=intent, recommendations=results,
        search_id=search_id, fallback=fallback,
    )


@app.post("/recommend/home", response_model=RecommendResponse)
async def recommend_home(req: HomeRecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results, fallback = _recommender.recommend_home(req.user_id, req.limit)
    return RecommendResponse(
        query="[home]", mode="home", intent=intent, recommendations=results,
        search_id=search_id, fallback=fallback,
    )


@app.post("/behavior/view", status_code=204)
async def log_view(req: ViewLogRequest) -> None:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    _recommender.log_view(req.user_id, req.product_id, req.search_id)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    result = _recommender.chat([m.model_dump() for m in req.messages], req.limit, req.lang)
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
