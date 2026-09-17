"""第三步：把结果写成给人读的文本。

结构化数据是给程序看的，人直接读 JSON 太费劲。这里渲染成一段能直接看的文字。

润色交给模型，但**数字和链接不交给它"理解"**：先把播放量之类格式化成
「63.1万」这种可读写法再喂给它，要求原样照抄，产出后再校验一遍链接和菜名
有没有丢。任何一步不对就退回模板渲染——模板是纯代码拼的，数字一定准。
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
1. 平实口语，像朋友推荐今晚吃什么。不要用 markdown 符号（** ## - 等一律不要）。
2. 开头一句话总述，然后逐道菜写。
3. 每道菜都要写全：菜名、推荐理由、视频标题、UP 主、播放/点赞/投币三个数字、链接。
4. 数字、链接、UP 主名字必须与给定数据**一字不差地照抄**，不要改写、不要换算单位、
   不要四舍五入、不要补全。
5. 只输出这段文字本身，前后不要加任何说明。"""


def format_count(n: int) -> str:
    """按中文习惯写大数：631368 → 63.1万。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _facts(dish: Dish) -> dict[str, str]:
    """把一道菜摊平成待照抄的字段。数字在这里就已经是可读写法了。"""
    v = dish.video
    if v is None:
        return {"菜名": dish.name, "推荐理由": dish.reason, "视频": "没找到合适的视频"}
    return {
        "菜名": dish.name,
        "推荐理由": dish.reason,
        "视频标题": v.title,
        "UP主": v.author,
        "播放": format_count(v.play),
        "点赞": format_count(v.like),
        "投币": format_count(v.coin),
        "链接": v.url,
    }


def render_text(query: str, dishes: list[Dish]) -> str:
    """模板渲染。既是默认输出，也是模型写崩时的兜底。"""
    lines = [f"关于「{query}」，推荐这 {len(dishes)} 道菜：", ""]
    for i, dish in enumerate(dishes, 1):
        lines.append(f"{i}. {dish.name}")
        if dish.reason:
            lines.append(f"   {dish.reason}")
        if dish.video is None:
            lines.append("   （没找到合适的视频）")
        else:
            v = dish.video
            lines.append("")
            lines.append(f"   视频：{v.title}")
            lines.append(f"   UP 主：{v.author}")
            lines.append(
                f"   播放 {format_count(v.play)} · 点赞 {format_count(v.like)}"
                f" · 投币 {format_count(v.coin)}"
            )
            lines.append(f"   {v.url}")
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
    if not settings.deepseek_api:
        logger.warning("未配置 DEEPSEEK_API，跳过润色直接用模板")
        return fallback

    llm = ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api,
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
