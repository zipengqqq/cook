"""第一步：把用户的食材描述交给 DeepSeek，换回一个菜名数组。

DeepSeek 是 OpenAI 兼容接口，所以用 langchain-openai 覆写 base_url 即可，
不需要单独的 SDK。

这一步的输出契约是下游检索的输入，必须稳定：菜名要能被直接拿去 B 站搜索，
所以提示词里明确要求用常见、标准的菜名写法。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import get_settings

SYSTEM_PROMPT = """你是家常菜推荐助手。用户会告诉你手头有什么食材、或想吃什么。

要求：
1. 推荐 {count} 道菜。优先用上用户已有的食材，缺一两样常见调料没关系。
2. 每道菜给三个字段：
   - name：菜名。必须是常见、标准的写法（例如「番茄炒蛋」而不是「西红柿鸡蛋小炒」），
     因为这个菜名会被直接拿去视频网站搜索。
   - reason：一句话推荐理由，30 字以内，说清楚为什么适合用户。
   - ingredients_used：用到的用户已有食材，字符串数组。
3. 菜品之间要有区分度，不要推荐四道做法雷同的菜。
4. 只输出 JSON 数组，不要 markdown 代码块，不要任何解释文字。

输出格式：
[{{"name":"番茄炒蛋","reason":"十分钟出锅，番茄和葱都能用上","ingredients_used":["番茄","葱"]}}]"""


class LLMError(RuntimeError):
    pass


def _extract_array(content: str) -> list:
    """从模型输出里抠出 JSON 数组。

    模型偶尔会加解释、包 markdown 代码块，或把数组塞进一个对象里。
    这层容错是必要的，不要简化成裸 json.loads。
    """
    text = (content or "").strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出不是合法 JSON：{text[:200]}") from exc
        if isinstance(obj, dict):
            for key in ("dishes", "recommendations", "result", "data"):
                if isinstance(obj.get(key), list):
                    return obj[key]

    raise LLMError(f"没能从模型输出里解析出菜名数组：{text[:200]}")


def _normalize(raw: list, count: int) -> list[dict]:
    """丢掉缺菜名的条目，补齐字段类型。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        used = item.get("ingredients_used") or []
        if not isinstance(used, list):
            used = [str(used)]
        seen.add(name)
        out.append(
            {
                "name": name,
                "reason": str(item.get("reason") or "").strip(),
                "ingredients_used": [str(x) for x in used],
            }
        )
        if len(out) >= count:
            break
    return out


def recommend_dishes(query: str, count: int | None = None) -> list[dict]:
    """同步接口。调用方负责放进线程池，不要阻塞事件循环。"""
    settings = get_settings()
    if not settings.deepseek_api:
        raise LLMError("未配置 DEEPSEEK_API，请检查 .env")

    count = count or settings.dish_count
    llm = ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api,
        base_url=settings.deepseek_base_url,
        temperature=settings.deepseek_temperature,
        timeout=settings.request_timeout,
    )

    try:
        resp = llm.invoke(
            [
                SystemMessage(content=SYSTEM_PROMPT.format(count=count)),
                HumanMessage(content=query),
            ]
        )
    except Exception as exc:
        raise LLMError(f"调用模型失败：{exc}") from exc

    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    dishes = _normalize(_extract_array(content), count)
    if not dishes:
        raise LLMError("模型没有返回任何可用的菜名")
    return dishes
