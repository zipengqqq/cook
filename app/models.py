"""内部数据结构。

接口只返回文本（见 app/summarize.py），但链路中间仍然走结构化对象：
既方便在模块之间传递，也让渲染和校验都能写成纯函数来测。
"""

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="自然语言描述，例如：我现在有番茄，豆腐，葱，晚饭吃什么",
    )


class VideoInfo(BaseModel):
    title: str
    url: str
    author: str
    # 三个维度的原始数值，保留原样便于核对打分是否符合直觉
    play: int
    like: int
    coin: int
    # 归一化加权后的综合分。只用于同一道菜的候选之间比较，跨菜不可比
    score: float


class Dish(BaseModel):
    name: str
    reason: str = ""
    ingredients_used: list[str] = Field(default_factory=list)
    # 该菜确实搜不到合适视频时为 None，不影响其它菜返回
    video: VideoInfo | None = None
