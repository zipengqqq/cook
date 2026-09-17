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
import logging
import math
import re
from dataclasses import dataclass

import httpx

from .models import Dish, VideoInfo

logger = logging.getLogger(__name__)

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

# 烹饪动词。中文菜名是「限定语 + 动词 + 主料」，一个菜名通常只带一个动词。
COOK_VERBS = frozenset("蒸炒烧炖拌煮焖煎炸烤溜爆卤腌焗煨扒烩焯汆熬酱熏醉")
# 菜名的收尾字。菜名几乎都以主料收尾，用来确认这一段确实是个菜名，
# 而不是「焯水」「炒糖色」这种步骤描述。
DISH_TAILS = frozenset(
    "丝片块丁条汤菜面饭粥饼卷丸饺包羹煲锅肉鱼虾鸡鸭蛋骨豆腐瓜茄笋藕菇菌"
    "米粉丝皮排翅爪肝肚肠舌尾头掌筋酥糕团"
)
# 一次教好几道菜的合集，标题里常见的词。人工看过的高置信信号，不在多而在准。
COMPILATION_MARKERS = ("合集", "大全", "不重样", "今日食谱", "一周食谱", "每日食谱")


class BilibiliError(RuntimeError):
    pass


def looks_like_compilation(title: str) -> bool:
    """标题像「一条视频教好几道菜」的合集吗？

    这类视频点进去还得自己拖进度条找对应的菜，所以不要——这是明确的产品要求。

    判据是数「带烹饪动词、且以主料字收尾」的片段：单道菜的标题最多出现一两个
    （「番茄炒蛋的6种做法」是一个动词），并列好几道菜时每道菜各带各的动词。
    「焯水」「炒糖色」这种罗列步骤的标题靠「以主料字收尾」这半条挡住。

    这是启发式，不是解析。宁可漏掉几个合集，也别把正常的单菜视频误杀成
    「没找到合适的视频」——那是更糟的失败。
    """
    if any(marker in title for marker in COMPILATION_MARKERS):
        return True
    segments = (s.strip() for s in re.split(r"[，,、；;｜|/＋+和与及]", title))
    hits = sum(
        1
        for seg in segments
        if seg and seg[-1] in DISH_TAILS and any(v in seg for v in COOK_VERBS)
    )
    return hits >= 3


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


def composite_scores(candidates: list) -> list[float]:
    """按 WEIGHTS 加权算综合分，不修改候选本身。

    只要求对象有 play/like/coin 三个属性，所以 VideoCandidate 和 VideoInfo 都能传。
    录到最后一步跨菜排序时传进来的正是各道菜选中的 VideoInfo。
    """
    if not candidates:
        return []
    norms = {dim: _log_norm([getattr(c, dim) for c in candidates]) for dim in WEIGHTS}
    return [
        sum(WEIGHTS[dim] * norms[dim][i] for dim in WEIGHTS)
        for i in range(len(candidates))
    ]


def score_candidates(candidates: list[VideoCandidate]) -> None:
    """就地为候选打分。分数只在同一道菜的候选之间可比。

    最后乘上相关性系数。这是软处理，不是硬过滤：标题只沾了一半菜名的视频
    （比如搜「番茄烧豆腐」返回「番茄烧茄子」）能留下，但要赢得先有足够好的数据。
    用硬阈值拦这类近似标题会误杀「西红柿炒鸡蛋」这种同义写法。
    """
    for cand, metrics in zip(candidates, composite_scores(candidates)):
        cand.score = metrics * cand.relevance


def rank_by_popularity(dishes: list[Dish]) -> list[Dish]:
    """把菜按所选视频的热度重排，热的在前；没视频的排最后。

    不能直接拿 VideoInfo.score 排：那个分数是每道菜在自己那批候选里归一化出来的，
    各家的第一名都接近 1.0，跨菜比没有意义。得在「选中的这些视频」之间重新算一遍。
    """
    head = [d for d in dishes if d.video is not None]
    tail = [d for d in dishes if d.video is None]
    if len(head) < 2:
        # 只有一道菜有视频就没得比，但「没视频的垫底」照样要成立
        return head + tail
    scores = composite_scores([d.video for d in head])
    return [head[i] for i in sorted(range(len(head)), key=lambda i: -scores[i])] + tail


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
                    logger.debug("已取得 buvid3")
                else:
                    logger.warning("finger/spi 未返回 b_3，搜索可能被风控拦截")
            except Exception as exc:
                # 这里原来是静默 pass：拿不到 buvid3 时每道菜都会没有视频，
                # 但日志上一片安静，根本查不出原因
                logger.warning("获取 buvid3 失败，搜索可能被风控拦截：%s", exc)

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
        dropped_compilation = 0
        for it in items:
            bvid = it.get("bvid")
            # 结果里偶尔混入直播等非视频条目
            if not bvid or bvid in seen or it.get("type") != "video":
                continue
            title = _clean_title(it.get("title", ""))
            if looks_like_compilation(title):
                dropped_compilation += 1
                continue
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
        if dropped_compilation:
            # 合集被整批滤掉时会连带把「没找到合适的视频」也带出来，
            # 不留一行日志的话，看上去就像搜索坏了
            logger.debug("「%s」滤掉 %d 条合集类视频", dish, dropped_compilation)
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
                        logger.debug(
                            "取投币失败 %s：code=%s %s",
                            c.bvid,
                            payload.get("code"),
                            payload.get("message"),
                        )
                        return
                    stat = (payload.get("data") or {}).get("stat") or {}
                    c.coin = int(stat.get("coin") or 0)
                    c.like = int(stat.get("like") or c.like)
                    c.play = int(stat.get("view") or c.play)
                except Exception as exc:
                    logger.debug("取投币异常 %s：%s", c.bvid, exc)

        if not candidates:
            return
        await asyncio.gather(*(one(c) for c in candidates))

        # 全员投币数为 0 通常意味着 view 接口整体失效了，此时打分实际只剩
        # 点赞和播放两个维度在起作用。不报出来的话，这个维度静默失效没人会发现。
        if candidates and all(c.coin == 0 for c in candidates):
            logger.warning(
                "%d 条候选的投币数全为 0，投币维度可能已失效，打分退化为点赞+播放",
                len(candidates),
            )

    async def pick_best(self, dish: str) -> VideoInfo | None:
        """给一道菜选一条最合适的视频，搜不到返回 None。"""
        try:
            items = await self._search(dish)
        except Exception as exc:
            # 原本这里是静默 return None：B 站一改接口，所有菜集体没视频却查不出原因
            logger.warning("搜索「%s」失败：%s", dish, exc)
            return None

        candidates = self._build_candidates(dish, items)
        if not candidates:
            logger.warning(
                "「%s」搜到 %d 条，但没有一条通过相关性/时长/合集过滤", dish, len(items)
            )
            return None
        logger.debug("「%s」搜到 %d 条，过滤后剩 %d 条", dish, len(items), len(candidates))

        # 粗排后只对少量候选补投币，避免每个菜打 20 次详情请求
        candidates.sort(key=coarse_rank_key, reverse=True)
        shortlist = candidates[: self._coin_lookup_top]
        await self._fill_coin(shortlist)

        score_candidates(shortlist)
        ranked = sorted(shortlist, key=lambda c: c.score, reverse=True)
        best = ranked[0]

        # 这行是调 WEIGHTS 时最该看的：选中的是谁、三个维度各是多少
        logger.info(
            "「%s」选中 %s（score=%.3f play=%d like=%d coin=%d）%s",
            dish, best.bvid, best.score, best.play, best.like, best.coin, best.title,
        )
        logger.debug(
            "「%s」落选候选：%s",
            dish,
            "；".join(f"{c.bvid}({c.score:.3f})" for c in ranked[1:]) or "无",
        )
        return best.to_info()
