"""打分与筛选逻辑的单测。这些是纯函数，不碰网络。

网络调用（搜索、view 接口）不进单测，在 BilibiliClient 的方法边界打桩。
"""

import pytest

from app.bilibili import (
    VideoCandidate,
    _clean_title,
    coarse_rank_key,
    has_leading_part,
    parse_duration,
    relevance,
    score_candidates,
)

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
