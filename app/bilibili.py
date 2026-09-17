"""B 站检索与视频选优。

两段式检索，原因是搜索接口不返回投币数：

  1. search/type 粗筛 —— 返回 play/like/favorites，没有 coin。
     按 play+like 粗排，只留下少量候选。
  2. view 接口补 coin —— 只对粗筛后的候选调用。每条候选多一次请求，
     所以候选数由 coin_lookup_top 控制，不要对整个结果集拉详情。

未登录即可访问，但搜索接口要求带 buvid3 cookie（先调 finger/spi 拿）。
实测无需 WBI 签名，若后续接口收紧，这里是要改的地方。
"""

from __future__ import annotations

import asyncio
import html
import math
import re
from dataclasses import dataclass, field

import httpx

from .models import VideoInfo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

# 三个维度的权重，是唯一需要反复调参的地方。
# 投币和点赞是主动表态，比播放量更能反映视频质量；播放量作为热度权重最低。
WEIGHTS = {"coin": 0.4, "like": 0.4, "play": 0.2}


class BilibiliError(RuntimeError):
    pass


def _clean_title(raw: str) -> str:
    """搜索结果标题里带 <em class="keyword"> 高亮标签和 HTML 实体。"""
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def parse_duration(raw: object) -> int | None:
    """B 站时长格式为 "5:6" / "12:34" / "1:02:03"，也见过直接给秒数。"""
    if isinstance(raw, (int, float)):
        return int(raw)
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds


def relevance(dish: str, title: str) -> float:
    """菜名字符在标题中的覆盖率。

    注意这个指标本身不足以过滤跑题视频：「番茄烧豆腐」和「葱烧豆腐」共用
    「烧豆腐」三字，覆盖率 0.6，但那是另一道菜。必须配合 has_leading_part 用。
    """
    core = [c for c in dish if not c.isspace()]
    if not core:
        return 0.0
    return sum(1 for c in core if c in title) / len(core)


def has_leading_part(dish: str, title: str) -> bool:
    """菜名开头的限定语是否出现在标题里。

    中文菜名的结构是「限定语 + 主料」：番茄|烧豆腐、麻婆|豆腐、葱油|拌面。
    主料（豆腐、蛋、面）在同类菜里高度重复，限定语才是区分不同菜的部分。
    只按字符覆盖率过滤时，主料对上、限定语缺失的标题会漏进来——
    搜「番茄烧豆腐」返回「葱烧豆腐」就是这么来的。

    菜名太短（不足 3 字）时没有可分离的限定语，不做这道门。
    """
    core = [c for c in dish if not c.isspace()]
    if len(core) < 3:
        return True
    return "".join(core[:2]) in title


def _log_norm(values: list[int]) -> list[float]:
    """先取对数再 min-max 归一化。

    播放量通常是投币的几十上百倍，直接加权等于只看播放量。取对数压缩量级后
    再归一化，三个维度才能在同一尺度上相加。
    """
    logs = [math.log10(1 + max(0, v)) for v in values]
    lo, hi = min(logs), max(logs)
    if hi - lo < 1e-9:
        # 所有候选项在该维度上一样，这一维不提供区分度
        return [1.0] * len(logs)
    return [(x - lo) / (hi - lo) for x in logs]


@dataclass
class VideoCandidate:
    bvid: str
    title: str
    author: str
    play: int = 0
    like: int = 0
    coin: int = 0
    duration: int | None = None
    # 默认 1.0 表示不惩罚。生产路径由 _build_candidates 显式赋值
    relevance: float = 1.0
    score: float = 0.0

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    def to_info(self) -> VideoInfo:
        return VideoInfo(
            title=self.title,
            url=self.url,
            author=self.author,
            play=self.play,
            like=self.like,
            coin=self.coin,
            score=round(self.score, 4),
        )


def score_candidates(candidates: list[VideoCandidate]) -> None:
    """就地为候选打分。分数只在同一道菜的候选之间可比。

    最后乘上相关性系数。这是软处理，不是硬过滤：标题只沾了一半菜名的视频
    （比如搜「番茄烧豆腐」返回「番茄烧茄子」）能留下，但要赢得先有足够好的数据。
    用硬阈值拦这类近似标题会误杀「西红柿炒鸡蛋」这种同义写法。
    """
    if not candidates:
        return
    norms = {dim: _log_norm([getattr(c, dim) for c in candidates]) for dim in WEIGHTS}
    for i, c in enumerate(candidates):
        metrics = sum(WEIGHTS[dim] * norms[dim][i] for dim in WEIGHTS)
        c.score = metrics * c.relevance


def coarse_rank_key(c: VideoCandidate) -> float:
    """粗排依据。投币数此时还拿不到，只能用 play+like。"""
    return math.log10(1 + c.play) + math.log10(1 + c.like)


class BilibiliClient:
    """异步客户端。一次请求内复用一个实例，复用 session 和 buvid3。"""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_concurrency: int = 6,
        min_relevance: float = 0.6,
        max_duration_seconds: int = 20 * 60,
        coin_lookup_top: int = 6,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"},
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._buvid_ready = False
        self._buvid_lock = asyncio.Lock()
        self._min_relevance = min_relevance
        self._max_duration = max_duration_seconds
        self._coin_lookup_top = coin_lookup_top

    async def __aenter__(self) -> "BilibiliClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def _ensure_buvid(self) -> None:
        """搜索接口要求带 buvid3。拿不到就继续，让请求自己失败并暴露问题。"""
        if self._buvid_ready:
            return
        async with self._buvid_lock:
            if self._buvid_ready:
                return
            try:
                resp = await self._client.get(SPI_URL)
                b3 = ((resp.json().get("data") or {})).get("b_3")
                if b3:
                    self._client.cookies.set("buvid3", b3, domain=".bilibili.com")
                    self._buvid_ready = True
            except Exception:
                pass

    async def _search(self, keyword: str) -> list[dict]:
        await self._ensure_buvid()
        async with self._sem:
            resp = await self._client.get(
                SEARCH_URL,
                params={"search_type": "video", "keyword": keyword, "page": 1},
            )
        payload = resp.json()
        if payload.get("code") != 0:
            raise BilibiliError(
                f"搜索失败 code={payload.get('code')} msg={payload.get('message')}"
            )
        return (payload.get("data") or {}).get("result") or []

    def _build_candidates(self, dish: str, items: list[dict]) -> list[VideoCandidate]:
        out: list[VideoCandidate] = []
        seen: set[str] = set()
        for it in items:
            bvid = it.get("bvid")
            # 结果里偶尔混入直播等非视频条目
            if not bvid or bvid in seen or it.get("type") != "video":
                continue
            title = _clean_title(it.get("title", ""))
            duration = parse_duration(it.get("duration"))
            if duration is not None and duration > self._max_duration:
                continue
            rel = relevance(dish, title)
            if rel < self._min_relevance or not has_leading_part(dish, title):
                continue
            seen.add(bvid)
            out.append(
                VideoCandidate(
                    bvid=bvid,
                    title=title,
                    author=it.get("author", ""),
                    play=int(it.get("play") or 0),
                    like=int(it.get("like") or 0),
                    coin=0,
                    duration=duration,
                    relevance=rel,
                )
            )
        return out

    async def _fill_coin(self, candidates: list[VideoCandidate]) -> None:
        """补投币数，顺带用 view 接口的数值覆盖搜索结果里的 play/like（更准）。

        单个候选失败只丢它自己的统计值，不影响整道菜。
        """

        async def one(c: VideoCandidate) -> None:
            async with self._sem:
                try:
                    resp = await self._client.get(VIEW_URL, params={"bvid": c.bvid})
                    payload = resp.json()
                    if payload.get("code") != 0:
                        return
                    stat = (payload.get("data") or {}).get("stat") or {}
                    c.coin = int(stat.get("coin") or 0)
                    c.like = int(stat.get("like") or c.like)
                    c.play = int(stat.get("view") or c.play)
                except Exception:
                    return

        if candidates:
            await asyncio.gather(*(one(c) for c in candidates))

    async def pick_best(self, dish: str) -> VideoInfo | None:
        """给一道菜选一条最合适的视频，搜不到返回 None。"""
        try:
            items = await self._search(dish)
        except Exception:
            return None

        candidates = self._build_candidates(dish, items)
        if not candidates:
            return None

        # 粗排后只对少量候选补投币，避免每个菜打 20 次详情请求
        candidates.sort(key=coarse_rank_key, reverse=True)
        shortlist = candidates[: self._coin_lookup_top]
        await self._fill_coin(shortlist)

        score_candidates(shortlist)
        best = max(shortlist, key=lambda c: c.score)
        return best.to_info()
