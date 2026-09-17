"""打分与筛选逻辑的单测。这些是纯函数，不碰网络。

网络调用（搜索、view 接口）不进单测，在 BilibiliClient 的方法边界打桩。
"""

import pytest

from app.bilibili import (
    COMPILATION_MARKERS,
    VideoCandidate,
    _clean_title,
    coarse_rank_key,
    composite_scores,
    has_leading_part,
    looks_like_compilation,
    parse_duration,
    rank_by_popularity,
    relevance,
    score_candidates,
)
from app.models import Dish, VideoInfo

# --- parse_duration ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5:6", 306),
        ("12:34", 754),
        ("1:02:03", 3723),
        ("0:45", 45),
        ("1:00:00", 3600),
        # view 接口偶尔直接给秒数
        (306, 306),
        (None, None),
        ("", None),
        ("abc", None),
        # 倒计时、多段拼接等异常格式，宁可解析失败也不能算成合法时长
        ("--:--", None),
        ("1:2:3:4", 223384),
    ],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


# --- relevance ---


def test_relevance_exact_match():
    assert relevance("番茄炒蛋", "番茄炒蛋，记住一个关键点") == 1.0


def test_relevance_partial():
    # 「番茄」「蛋」命中，「炒」不命中
    assert relevance("番茄炒蛋", "番茄鸡蛋汤") == pytest.approx(0.75)


def test_relevance_off_topic():
    # 美食探店 vlog，几乎不含菜名字符
    assert relevance("番茄炒蛋", "探店｜这家苍蝇馆子排队两小时") < 0.3


def test_relevance_empty_dish():
    assert relevance("", "随便什么标题") == 0.0


def test_relevance_ignores_whitespace_in_dish():
    assert relevance("番茄 炒蛋", "番茄炒蛋教程") == 1.0


# --- has_leading_part：这条是修「番茄烧豆腐 → 葱烧豆腐」那个 bug 的 ---


def test_leading_part_rejects_shared_main_ingredient():
    """共用「烧豆腐」但限定语对不上，是另一道菜。

    这个标题的字符覆盖率是 3/5 = 0.6，刚好过 min_relevance，
    光靠覆盖率拦不住，必须靠限定语这道门。
    """
    title = "【葱烧豆腐】5块钱带来的神仙美味！做法简单用料少！"
    assert relevance("番茄烧豆腐", title) >= 0.6
    assert not has_leading_part("番茄烧豆腐", title)


def test_leading_part_accepts_matching_dish():
    title = "厨师长分享：“番茄炒蛋”的6种做法"
    assert has_leading_part("番茄炒蛋", title)


def test_leading_part_rejects_葱烧豆腐_for_小葱拌豆腐():
    assert not has_leading_part("小葱拌豆腐", "隋卞一做|家常菜之王？！葱烧豆腐！")


def test_leading_part_passes_麻婆豆腐():
    assert has_leading_part("麻婆豆腐", "厨师长教你：“麻婆豆腐”的正宗做法")


def test_leading_part_skipped_for_short_names():
    """3 字以下没有可分离的限定语，不该被这道门拦掉。"""
    assert has_leading_part("炒饭", "黄金炒饭，粒粒分明")
    assert has_leading_part("红烧肉", "红烧肉的家常做法")


def test_leading_part_ignores_whitespace_in_dish():
    assert has_leading_part("番茄 烧豆腐", "番茄烧豆腐，酸甜下饭")


# --- _clean_title ---


def test_clean_title_strips_highlight_tag():
    raw = '<em class="keyword">番茄炒蛋</em>，记住一个关键点'
    assert _clean_title(raw) == "番茄炒蛋，记住一个关键点"


def test_clean_title_unescapes_entities():
    assert _clean_title("A&amp;B &quot;引号&quot;") == 'A&B "引号"'


# --- 归一化与打分 ---


def _cand(play=0, like=0, coin=0, bvid="BV1"):
    return VideoCandidate(bvid=bvid, title="t", author="a", play=play, like=like, coin=coin)


def test_score_ranks_engagement_over_raw_play_count():
    """播放量高但没人投币的，不该赢过播放量低但互动好的。"""
    viral = _cand(play=5_000_000, like=1000, coin=50, bvid="viral")
    solid = _cand(play=200_000, like=30_000, coin=20_000, bvid="solid")
    score_candidates([viral, solid])
    assert solid.score > viral.score


def test_score_all_zero_is_uniform():
    """三个维度全为 0 时不该出现除零或 NaN。"""
    cands = [_cand(), _cand(), _cand()]
    score_candidates(cands)
    assert all(c.score == pytest.approx(1.0) for c in cands)


def test_score_identical_candidates_tie():
    cands = [_cand(play=100, like=10, coin=1), _cand(play=100, like=10, coin=1)]
    score_candidates(cands)
    assert cands[0].score == pytest.approx(cands[1].score)


def test_score_empty_list():
    score_candidates([])  # 不应抛异常


def test_score_penalizes_partial_title_match():
    """数据相同，标题更贴合菜名的胜出。"""
    exact = _cand(play=1000, like=100, coin=10)
    exact.relevance = 1.0
    partial = _cand(play=1000, like=100, coin=10)
    partial.relevance = 0.6
    score_candidates([exact, partial])
    assert exact.score > partial.score


def test_score_partial_match_can_win_on_better_data():
    """软惩罚不是硬过滤：数据明显更好时，沾一半菜名的仍能赢。"""
    exact = _cand(play=1000, like=100, coin=10)
    exact.relevance = 1.0
    partial = _cand(play=5_000_000, like=200_000, coin=80_000)
    partial.relevance = 0.6
    score_candidates([exact, partial])
    assert partial.score > exact.score


def test_score_within_unit_range():
    cands = [
        _cand(play=10, like=1, coin=0),
        _cand(play=1_000_000, like=90_000, coin=30_000),
    ]
    score_candidates(cands)
    assert all(0.0 <= c.score <= 1.0 for c in cands)


def test_score_extreme_magnitude_does_not_overflow():
    """播放量与投币量级悬殊是常态，取对数就是为了不溢出。"""
    cands = [_cand(play=10**12, like=0, coin=0), _cand(play=1, like=10**9, coin=10**9)]
    score_candidates(cands)
    assert all(c.score == c.score for c in cands)  # 非 NaN


# --- 粗排 ---


def test_coarse_rank_prefers_higher_play_and_like():
    low = _cand(play=100, like=10)
    high = _cand(play=100_000, like=5_000)
    assert coarse_rank_key(high) > coarse_rank_key(low)


def test_coarse_rank_handles_zero():
    assert coarse_rank_key(_cand()) == 0.0


def test_composite_scores_does_not_mutate_candidates():
    """跨菜排序要的是分数，不该顺手把候选自己的 score 也改了。"""
    cands = [_cand(play=100, like=10, coin=1), _cand(play=1, like=1, coin=0)]
    composite_scores(cands)
    assert all(c.score == 0.0 for c in cands)


# --- 合集过滤：一条视频教好几道菜的不要 ---

# 真实搜到的一条，搜「酸辣土豆丝」时排在前面
_COMPILATION = "今日食谱：芋头蒸排骨，酸辣土豆丝，青椒炒鱿鱼，清炒荷兰豆，青瓜肉片汤"


def test_compilation_marker_detected():
    assert looks_like_compilation(_COMPILATION)


def test_compilation_detected_without_marker_word():
    """标题里没有「合集」这类词，但并列了好几道各带动词的菜名，一样算。"""
    title = "今晚做了三道菜：红烧肉炖土豆，青椒炒肉丝，番茄炒鸡蛋"
    assert not any(m in title for m in COMPILATION_MARKERS)
    assert looks_like_compilation(title)


def test_single_dish_videos_are_not_compilations():
    """这几条都是实测搜到并选中过的正常单菜视频，一个都不能误杀——

    误杀的代价是「没找到合适的视频」，比漏掉几个合集更糟。
    """
    for title in (
        "厨师长分享：“番茄炒蛋”的6种做法，多种版本适合各类人群",
        "了不起的中国菜，大爷的麻婆豆腐，麻辣鲜香，配上米饭超好吃。",
        "小葱拌豆腐，为什么饭店做的更好吃？最后一步很关键，老做法更香",
        "番茄菌菇豆腐汤！喝一口仿佛全身开了暖气！",
        "【土豆鸡蛋饼】外脆里嫩",
    ):
        assert not looks_like_compilation(title), title


def test_step_list_from_one_recipe_is_not_a_compilation():
    """罗列做菜步骤的标题不是合集。「以主料字收尾」这半条判据就是为它们设的。"""
    for title in (
        "红烧肉怎么做？焯水、煸炒、慢炖，一步都不能少",
        "五花肉先焯水，再炒糖色，最后小火慢炖四十分钟",
    ):
        assert not looks_like_compilation(title), title


# --- 跨菜排序 ---


def _info(play=0, like=0, coin=0, url="https://www.bilibili.com/video/BV1"):
    return VideoInfo(
        title="t", url=url, author="a", play=play, like=like, coin=coin, score=0.0
    )


def _result(name: str, video: VideoInfo | None) -> Dish:
    return Dish(name=name, reason="", ingredients_used=[], video=video)


def test_rank_by_popularity_orders_hottest_first():
    cold = _result("凉菜", _info(play=1_000, like=10, coin=1, url="https://b/cold"))
    hot = _result("热菜", _info(play=5_000_000, like=200_000, coin=80_000, url="https://b/hot"))
    mid = _result("中菜", _info(play=100_000, like=5_000, coin=1_000, url="https://b/mid"))
    ranked = rank_by_popularity([cold, hot, mid])
    assert [d.name for d in ranked] == ["热菜", "中菜", "凉菜"]


def test_rank_by_popularity_recomputes_across_dishes():
    """per-dish 的 score 是各自归一化出来的，各家的第一名都接近 1.0，
    跨菜比毫无意义。这条锁住「必须重算」：内部 score 高的不一定排前面。
    """
    weak = _info(play=1_000, like=10, coin=1, url="https://b/weak")
    weak.score = 1.0
    strong = _info(play=9_000_000, like=300_000, coin=90_000, url="https://b/strong")
    strong.score = 0.4
    ranked = rank_by_popularity([_result("弱的", weak), _result("强的", strong)])
    assert [d.name for d in ranked] == ["强的", "弱的"]


def test_rank_by_popularity_puts_dishes_without_video_last():
    hot = _result("热菜", _info(play=5_000_000, like=200_000, coin=80_000))
    ranked = rank_by_popularity([_result("没搜到的", None), hot])
    assert [d.name for d in ranked] == ["热菜", "没搜到的"]


def test_rank_by_popularity_handles_short_lists():
    only = _result("独苗", _info(play=1, like=1, coin=1))
    assert rank_by_popularity([only]) == [only]
    assert rank_by_popularity([]) == []
    assert [d.name for d in rank_by_popularity([_result("全没视频", None)])] == ["全没视频"]
