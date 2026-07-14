from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query

from .models import (
    ChatRequest,
    ChatResponse,
    ClearHistoryResponse,
    DescriptionResponse,
    FeedbackRequest,
    FeedbackResponse,
    GraphReadinessResponse,
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


@app.get("/ready", response_model=GraphReadinessResponse)
async def ready() -> GraphReadinessResponse:
    """Readiness check for the currently connected Neo4j graph, not just the API process."""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    report = await asyncio.to_thread(_recommender.graph_readiness)
    if report["status"] != "ready":
        raise HTTPException(status_code=503, detail=report)
    return GraphReadinessResponse(**report)


@app.post("/recommend", response_model=RecommendResponse)
async def recommend(req: RecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results, fallback = await asyncio.to_thread(
        _recommender.recommend, req.query, req.user_id, req.limit, req.lang
    )
    return RecommendResponse(
        query=req.query, mode="search", intent=intent, recommendations=results,
        search_id=search_id, fallback=fallback,
    )


@app.post("/recommend/home", response_model=RecommendResponse)
async def recommend_home(req: HomeRecommendRequest) -> RecommendResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    search_id, intent, results, fallback = await asyncio.to_thread(
        _recommender.recommend_home, req.user_id, req.limit, req.lang
    )
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
    await asyncio.to_thread(_recommender.warm_home_cache, req.user_id, req.limit, req.lang)


@app.post("/behavior/view", status_code=204)
async def log_view(req: ViewLogRequest) -> None:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    await asyncio.to_thread(_recommender.log_view, req.user_id, req.product_id, req.search_id)


@app.post("/users/{user_id}/clear_history", response_model=ClearHistoryResponse)
async def clear_history(user_id: str) -> ClearHistoryResponse:
    """RATED（データセット由来の評価履歴）以外の永続データ——VIEWED行動ログと
    SearchLog検索履歴——をこのユーザーについて削除する。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    counts = await asyncio.to_thread(_recommender.clear_behavior_history, user_id)
    return ClearHistoryResponse(**counts)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    # 対話型推薦は会話条件だけを使う。リクエストにuser_idが含まれていても、
    # 個人化プロフィールや評価・閲覧履歴を検索へ混ぜないよう常にNoneを渡す。
    result = await asyncio.to_thread(
        _recommender.chat, [m.model_dump() for m in req.messages], req.limit, req.lang, None
    )
    return ChatResponse(**result)


@app.get("/users/sample", response_model=SampleUsersResponse)
async def sample_users(limit: int = Query(default=10, ge=1, le=50)) -> SampleUsersResponse:
    """デモ用: 評価履歴を持つ実ユーザーを何件か返す（テストユーザー選択に使用）。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    rows = await asyncio.to_thread(_recommender.sample_users, limit)
    return SampleUsersResponse(users=[SampleUser(**r) for r in rows])


@app.get("/products/{product_id}/reviews", response_model=ReviewsResponse)
async def get_reviews(
    product_id: str,
    limit: int = Query(default=5, ge=1, le=20),
    lang: Literal["ja", "en"] = "en",
) -> ReviewsResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    rows = await asyncio.to_thread(_recommender.get_reviews, product_id, limit, lang)
    reviews = [ReviewItem(**r) for r in rows]
    translated_count = sum(1 for review in reviews if review.translated)
    return ReviewsResponse(
        product_id=product_id,
        reviews=reviews,
        requested_language=lang,
        translated_count=translated_count,
        fallback_count=len(reviews) - translated_count if lang == "ja" else 0,
    )


@app.get("/products/{product_id}/description", response_model=DescriptionResponse)
async def get_description(product_id: str, lang: Literal["ja", "en"] = "en") -> DescriptionResponse:
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    row = await asyncio.to_thread(_recommender.get_description, product_id, lang)
    if row is None:
        return DescriptionResponse(product_id=product_id, description=None, translated=False)
    return DescriptionResponse(product_id=product_id, **row)


@app.post("/recommendations/{product_id}/feedback", response_model=FeedbackResponse)
async def recommendation_feedback(product_id: str, req: FeedbackRequest) -> FeedbackResponse:
    """「この推薦は役に立ちましたか？」のはい/いいえをNeo4jに記録する。"""
    if _recommender is None:
        raise HTTPException(status_code=503, detail="Recommender not initialized")
    try:
        saved = await asyncio.to_thread(
            _recommender.save_feedback, product_id, req.user_id, req.search_id, req.helpful, req.lang
        )
    except Exception as exc:
        # DB障害の詳細をクライアントへ露出せず、失敗を明示する。
        print(f"[api] recommendation feedback failed: {exc}")
        raise HTTPException(status_code=503, detail="Could not save feedback")
    if not saved:
        raise HTTPException(status_code=404, detail="Product not found")
    return FeedbackResponse(status="ok", product_id=product_id)


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
