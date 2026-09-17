"""FastAPI 入口。

链路：LLM 出菜名 → 每个菜并发去 B 站检索 → 各自选一条最合适的视频。
两道菜之间彼此独立，所以用 asyncio.gather 并发；LLM 调用是同步阻塞的，
扔进线程池别挡住事件循环。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException

from .agent import LLMError, recommend_dishes
from .bilibili import BilibiliClient
from .config import get_settings
from .models import Dish, RecommendRequest, RecommendResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="今天晚饭吃什么",
    description="给一句「我有什么食材」，换回几道菜，每道菜配一条 B 站做饭视频。",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/recommend", response_model=RecommendResponse)
async def recommend(req: RecommendRequest) -> RecommendResponse:
    settings = get_settings()

    try:
        dishes = await asyncio.to_thread(recommend_dishes, req.query, settings.dish_count)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async with BilibiliClient(
        timeout=settings.request_timeout,
        max_concurrency=settings.max_concurrency,
        min_relevance=settings.min_relevance,
        max_duration_seconds=settings.max_duration_seconds,
        coin_lookup_top=settings.coin_lookup_top,
    ) as client:
        videos = await asyncio.gather(
            *(client.pick_best(d["name"]) for d in dishes),
            return_exceptions=True,
        )

    result: list[Dish] = []
    for dish, video in zip(dishes, videos):
        if isinstance(video, BaseException):
            # 单个菜检索失败不该让整个请求挂掉
            logger.warning("检索「%s」的视频失败：%s", dish["name"], video)
            video = None
        result.append(Dish(**dish, video=video))

    return RecommendResponse(query=req.query, dishes=result)
