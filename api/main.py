from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .models import RecommendRequest, RecommendResponse
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
    intent, results = _recommender.recommend(req.query, req.limit)
    return RecommendResponse(query=req.query, intent=intent, recommendations=results)
