from contextlib import asynccontextmanager
from functools import cache

from fastapi import Depends, FastAPI, Query

from utils.graph_reader import GraphReader
from utils.llm_client import LlmClient

from .chat import chat
from .models import (
    ChatRequest,
    ChatResponse,
    RecommendRequest,
    RecommendResponse,
    ReviewItem,
    ReviewsResponse,
    SamplePlayer,
    SamplePlayersResponse,
)
from .static_recommend import recommend_games


@cache
def get_graph_reader() -> GraphReader:
    return GraphReader()


@cache
def get_llm_client() -> LlmClient:
    return LlmClient("chat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    get_graph_reader().close()


app = FastAPI(title="Steam KG API", lifespan=lifespan)


@app.get("/health")
def get_health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def post_chat(
    request: ChatRequest,
    reader: GraphReader = Depends(get_graph_reader),
    llm_client: LlmClient = Depends(get_llm_client),
) -> ChatResponse:
    return chat(reader, llm_client, request.messages, request.player_id, request.limit, request.lang)


@app.post("/recommend", response_model=RecommendResponse)
def post_recommend(request: RecommendRequest, reader: GraphReader = Depends(get_graph_reader)) -> RecommendResponse:
    return RecommendResponse(
        recommendations=recommend_games(reader, request.player_id, request.limit, request.lang)
    )


@app.get("/players/sample", response_model=SamplePlayersResponse)
def get_sample_players(
    limit: int = Query(default=10, ge=1, le=50), reader: GraphReader = Depends(get_graph_reader)
) -> SamplePlayersResponse:
    return SamplePlayersResponse(players=[SamplePlayer(**row) for row in reader.sample_players(limit)])


@app.get("/games/{appid}/reviews", response_model=ReviewsResponse)
def get_game_reviews(
    appid: str, limit: int = Query(default=100, ge=1, le=5000), reader: GraphReader = Depends(get_graph_reader)
) -> ReviewsResponse:
    return ReviewsResponse(appid=appid, reviews=[ReviewItem(**row) for row in reader.top_reviews(appid, limit)])
