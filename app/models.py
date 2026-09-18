"""内部数据结构，以及对外 JSON 的那一层。

链路中间走结构化对象：既方便在模块之间传递，也让渲染和校验都能写成纯函数来测。

**内部对象 ≠ 对外契约。** `Dish` / `VideoInfo` 里带着 score、播放量、标题、UP 主，
这些是排序和调试要用的，不该顺着接口漏出去。HTML 页面要的是卡片，所以这里另有
一组 `*Card` 对象：只有菜名、理由、链接、封面四样，`to_card()` 负责把前者裁成后者。
漏字段的责任就落在这一个函数上，比在每个路由里手工拼 dict 好查。
"""

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="自然语言描述，例如：我现在有番茄，豆腐，葱，今天吃什么",
    )


class VideoInfo(BaseModel):
    title: str
    url: str
    author: str
    # 封面图地址。文字输出用不上，只有卡片要，所以放在最后并给了默认值——
    # 单测里到处在手工构造 VideoInfo，多一个必填字段会把它们全打红
    cover: str = ""
    # 三个维度的原始数值，保留原样便于核对打分是否符合直觉
    play: int
    like: int
    coin: int
    # 归一化加权后的综合分。只用于同一道菜的候选之间比较，跨菜不可比
    score: float

    def to_card(self) -> "VideoCard":
        return VideoCard(url=self.url, cover=self.cover)


class Dish(BaseModel):
    name: str
    reason: str = ""
    ingredients_used: list[str] = Field(default_factory=list)
    # 该菜确实搜不到合适视频时为 None，不影响其它菜返回
    video: VideoInfo | None = None

    def to_card(self) -> "DishCard":
        return DishCard(
            name=self.name,
            reason=self.reason,
            video=self.video.to_card() if self.video is not None else None,
        )


# --- 对外的那一层 ---
#
# 四个字段，一个不多。播放量/点赞/投币不进输出是明确要求，用户不看这些；
# 视频标题和 UP 主同理。封面图是唯一的例外——卡片上要有图，而图里不含任何文字信息。


class VideoCard(BaseModel):
    url: str
    cover: str = ""


class DishCard(BaseModel):
    name: str
    reason: str = ""
    # 没搜到合适视频时为 None，前端渲染成「没找到合适的视频」，不是错误
    video: VideoCard | None = None


class RecommendResponse(BaseModel):
    query: str
    dishes: list[DishCard]
