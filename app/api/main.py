from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .models import (
    ChatRequest,
    ChatResponse,
    HomeRecommendRequest,
    RecommendRequest,
    RecommendResponse,
    ReviewItem,
    ReviewsResponse,
    SampleUser,
    SampleUsersResponse,
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
    search_id, intent, results, fallback = _recommender.recommend(req.query, req.user_id, req.limit, req.lang)
    return RecommendResponse(
        query=req.query, mode="search", intent=intent, recommendations=results,
        search_id=search_id, fallback=fallback,
    )


@app.get("/recommend/trending", response_model=RecommendResponse)
async def recommend_trending(limit: int = 10, lang: str = "en") -> RecommendResponse:
    """LLMを呼ばない高速パス。個人化する材料が無いと分かっている初期表示（匿名ユーザーが
    画面を開いた瞬間など）向け。人気・高評価商品を直接クエリするのでラグがほぼ無い。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results = _recommender.recommend_trending(limit, lang)
    return RecommendResponse(
        query="[trending]", mode="home", intent=intent, recommendations=results,
        search_id=search_id, fallback=False,
    )


@app.post("/recommend/home", response_model=RecommendResponse)
async def recommend_home(req: HomeRecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results, fallback = _recommender.recommend_home(req.user_id, req.limit, req.lang)
    return RecommendResponse(
        query="[home]", mode="home", intent=intent, recommendations=results,
        search_id=search_id, fallback=fallback,
    )


@app.post("/recommend/home/warm", status_code=204)
async def recommend_home_warm(req: HomeRecommendRequest) -> None:
    """fire-and-forget用: タブを閉じる/バックグラウンドに回した時などに呼び、次回
    recommend_home()が即座に返せるようホーム推薦を先読みキャッシュしておく。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    _recommender.warm_home_cache(req.user_id, req.limit, req.lang)


@app.post("/behavior/view", status_code=204)
async def log_view(req: ViewLogRequest) -> None:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    _recommender.log_view(req.user_id, req.product_id, req.search_id)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    result = _recommender.chat([m.model_dump() for m in req.messages], req.limit, req.lang, req.user_id)
    return ChatResponse(**result)


@app.get("/users/sample", response_model=SampleUsersResponse)
async def sample_users(limit: int = 10) -> SampleUsersResponse:
    """デモ用: 評価履歴を持つ実ユーザーを何件か返す（テストユーザー選択に使用）。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    rows = _recommender.sample_users(limit)
    return SampleUsersResponse(users=[SampleUser(**r) for r in rows])


@app.get("/products/{product_id}/reviews", response_model=ReviewsResponse)
async def get_reviews(product_id: str, limit: int = 5) -> ReviewsResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    rows = _recommender.get_reviews(product_id, limit)
    reviews = [ReviewItem(**r) for r in rows]
    return ReviewsResponse(product_id=product_id, reviews=reviews)


if __name__ == "__main__":
    import yaml
    from pathlib import Path

    import uvicorn

    cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
    api_cfg: dict = {}
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            api_cfg = (yaml.safe_load(f) or {}).get("api", {})
    uvicorn.run(
        "app.api.main:app",
        host=api_cfg.get("host", "0.0.0.0"),
        port=api_cfg.get("port", 8000),
    )
