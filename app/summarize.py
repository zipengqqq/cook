"""第四步：把结果写成给人读的文本。

结构化数据是给程序看的，人直接读 JSON 太费劲。这里渲染成一段能直接看的文字。

输出只要三样：**菜名、推荐理由、视频链接**。播放量、点赞、投币这些数字不进输出，
用户不关心，写在推荐里只是噪音；视频标题和 UP 主同理，也去掉了。

润色交给模型，但**链接不交给它"理解"**：链接错一个字就打不开，所以要求原样照抄，
产出后再校验一遍每个菜名和链接还在不在。任何一步不对就退回模板渲染——
模板是纯代码拼的，链接一定对。
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import get_settings
from .models import Dish

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你把菜谱推荐结果整理成一段给人读的话。

要求：
1. 平实口语，像朋友推荐今天吃什么。不要用 markdown 符号（** ## - 等一律不要）。
2. 开头一句话总述，然后逐道菜写。
3. 每道菜写全三样：菜名、推荐理由、链接。此外不要写别的——
   不要提视频标题、UP 主，也不要提播放量、点赞数、投币数。
4. 链接必须与给定数据**一字不差地照抄**，不要改写、不要补全、不要省略。
5. 只输出这段文字本身，前后不要加任何说明。"""


def _facts(dish: Dish) -> dict[str, str]:
    """把一道菜摊平成待照抄的字段。

    只留菜名、理由、链接三样。数字不进输出，视频标题和 UP 主也没人看，
    一起去掉——给模型的字段越少，它能写歪的地方就越少。
    """
    if dish.video is None:
        return {"菜名": dish.name, "推荐理由": dish.reason, "视频": "没找到合适的视频"}
    return {
        "菜名": dish.name,
        "推荐理由": dish.reason,
        "链接": dish.video.url,
    }


def render_text(query: str, dishes: list[Dish]) -> str:
    """模板渲染。既是默认输出，也是模型写崩时的兜底。"""
    lines = [f"关于「{query}」，推荐这 {len(dishes)} 道菜：", ""]
    for i, dish in enumerate(dishes, 1):
        head = f"{i}. {dish.name}"
        if dish.reason:
            head += f"：{dish.reason}"
        lines.append(head)
        if dish.video is None:
            lines.append("   （没找到合适的视频）")
        else:
            lines.append(f"   链接：{dish.video.url}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def looks_intact(text: str, dishes: list[Dish]) -> bool:
    """校验润色结果有没有漏掉或改坏关键信息。

    只查链接和菜名：链接错一个字就打不开，菜名丢了整道菜就没了。
    这两个是最不能出错的，也是最好判定的。
    """
    for dish in dishes:
        if dish.name not in text:
            return False
        if dish.video is not None and dish.video.url not in text:
            return False
    return True


def _build_user_message(query: str, dishes: list[Dish]) -> str:
    parts = [f"用户的情况：{query}", "", "推荐结果："]
    for i, dish in enumerate(dishes, 1):
        parts.append(f"\n【第 {i} 道】")
        for key, value in _facts(dish).items():
            parts.append(f"{key}：{value}")
    return "\n".join(parts)


def summarize(query: str, dishes: list[Dish]) -> str:
    """同步接口。调用方负责放进线程池。润色失败一律退回模板，不抛异常。"""
    fallback = render_text(query, dishes)

    settings = get_settings()
    if not settings.deepseek_key:
        logger.warning("未配置 DEEPSEEK_KEY，跳过润色直接用模板")
        return fallback

    llm = ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_key,
        base_url=settings.deepseek_base_url,
        # 润色要的是稳定，不是发挥
        temperature=0.3,
        timeout=settings.request_timeout,
    )
    try:
        resp = llm.invoke(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=_build_user_message(query, dishes)),
            ]
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = content.strip()
        if not text:
            raise ValueError("模型返回空文本")
        if not looks_intact(text, dishes):
            logger.warning("润色结果缺少菜名或链接，退回模板渲染")
            return fallback
        logger.debug("润色结果：%s", text[:800])
        return text + "\n"
    except Exception as exc:
        logger.warning("润色失败，退回模板渲染：%s", exc)
        return fallback
