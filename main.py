"""FastAPI 入口。

链路：LLM 出菜名 → 每个菜并发去 B 站检索 → 各自选一条最合适的视频。
两道菜之间彼此独立，所以用 asyncio.gather 并发；LLM 调用是同步阻塞的，
扔进线程池别挡住事件循环。

放在项目根目录而不是 app/ 里，是为了 `python main.py` 能直接跑：脚本所在目录
天然在 sys.path 上，`from app.xxx import` 开箱可用，不需要任何路径补丁。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException

from app.agent import LLMError, recommend_dishes
from app.bilibili import BilibiliClient
from app.config import get_settings
from app.models import Dish, RecommendRequest, RecommendResponse

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 这些库把每次 HTTP 连接的细节都打成 INFO/DEBUG，一次请求能刷几十行。
# 不压掉的话，无论 INFO 还是 DEBUG 档都看不见自己的日志。
# httpx2 / httpcore2 是较新的包名，与 httpx / httpcore 并列写上以防环境差异。
for _noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "openai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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
    started = time.perf_counter()

    try:
        dishes = await asyncio.to_thread(recommend_dishes, req.query, settings.dish_count)
    except LLMError as exc:
        logger.warning("推荐失败（模型环节）：%s", exc)
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

    logger.info(
        "请求完成：%d 道菜，%d 道有视频，总耗时 %.1fs",
        len(result),
        sum(1 for d in result if d.video is not None),
        time.perf_counter() - started,
    )
    return RecommendResponse(query=req.query, dishes=result)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    print(
        f"\n  服务已启动，打开 http://{settings.app_host}:{settings.app_port}/docs "
        f"可以直接试接口\n  按 Ctrl+C 停止\n"
    )
    # 传 app 对象而不是导入字符串：改了代码不会自动重启，但 PyCharm 的断点能正常命中。
    # 需要热重载就用命令行：uvicorn main:app --reload
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_level="info")
