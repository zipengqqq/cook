"""文本渲染与校验的单测。都是纯函数，不碰网络也不调模型。"""

import pytest

from app import summarize as summarize_mod
from app.config import Settings
from app.models import Dish, VideoInfo
from app.summarize import format_count, looks_intact, render_text, summarize


def _dish(name="醋溜土豆丝", reason="酸辣开胃", video=True):
    v = (
        VideoInfo(
            title="当你想吃醋溜土豆丝就把这部视频翻出来",
            url="https://www.bilibili.com/video/BV1UhMMzkEqi",
            author="美食强",
            play=631368,
            like=31917,
            coin=3274,
            score=0.97,
        )
        if video
        else None
    )
    return Dish(name=name, reason=reason, ingredients_used=["土豆"], video=v)


# --- format_count ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, "0"),
        (1, "1"),
        (9999, "9999"),
        # 一万是「万」的起点
        (10_000, "1.0万"),
        (63_1368, "63.1万"),
        (3_1917, "3.2万"),
        # 一亿是「亿」的起点
        (100_000_000, "1.0亿"),
        (123_456_789, "1.2亿"),
    ],
)
def test_format_count(raw, expected):
    assert format_count(raw) == expected


def test_format_count_boundaries_do_not_fall_through():
    """9999 是「个」的最后一档，10000 该跳到「万」，别漏掉接缝。"""
    assert format_count(9999) == "9999"
    assert format_count(10_000).endswith("万")
    assert format_count(99_999_999).endswith("万")
    assert format_count(100_000_000).endswith("亿")


# --- render_text ---


def test_render_text_includes_everything_essential():
    text = render_text("我有土豆", [_dish()])
    assert "醋溜土豆丝" in text
    assert "酸辣开胃" in text
    assert "美食强" in text
    assert "https://www.bilibili.com/video/BV1UhMMzkEqi" in text
    assert "63.1万" in text
    assert "3.2万" in text
    assert "3274" in text


def test_render_text_does_not_leak_internal_score():
    """score 是内部排序用的，给人看只会干扰。"""
    text = render_text("我有土豆", [_dish()])
    assert "score" not in text
    assert "0.97" not in text


def test_render_text_does_not_include_elapsed_time():
    text = render_text("我有土豆", [_dish()])
    assert "耗时" not in text


def test_render_text_numbers_every_dish():
    dishes = [_dish(name="醋溜土豆丝"), _dish(name="土豆鸡蛋饼")]
    text = render_text("我有土豆", dishes)
    assert "1. 醋溜土豆丝" in text
    assert "2. 土豆鸡蛋饼" in text


def test_render_text_handles_missing_video():
    text = render_text("我有土豆", [_dish(video=False)])
    assert "醋溜土豆丝" in text
    assert "没找到合适的视频" in text


def test_render_text_handles_missing_reason():
    text = render_text("我有土豆", [_dish(reason="")])
    assert "醋溜土豆丝" in text


def test_render_text_empty_dish_list():
    assert "0 道菜" in render_text("我有土豆", [])


def test_render_text_ends_with_single_newline():
    """收尾别留一堆空行，拼接进别的输出时会很难看。"""
    text = render_text("我有土豆", [_dish()])
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


# --- looks_intact：润色结果的校验闸门 ---


def test_looks_intact_accepts_faithful_output():
    dishes = [_dish()]
    text = render_text("我有土豆", dishes)
    assert looks_intact(text, dishes)


def test_looks_intact_rejects_missing_url():
    """链接错一个字就打不开，必须判为不合格。"""
    dishes = [_dish()]
    text = "推荐醋溜土豆丝，视频很好，链接是 https://www.bilibili.com/video/BV1WRONG"
    assert not looks_intact(text, dishes)


def test_looks_intact_rejects_dropped_dish():
    """漏了一整道菜也算不合格。"""
    dishes = [_dish(name="醋溜土豆丝"), _dish(name="土豆鸡蛋饼")]
    text = "推荐醋溜土豆丝 https://www.bilibili.com/video/BV1UhMMzkEqi"
    assert not looks_intact(text, dishes)


def test_looks_intact_allows_dish_without_video():
    """没有视频的菜只需菜名在，不该因为没有链接就被判不合格。"""
    dishes = [_dish(video=False)]
    assert looks_intact("推荐醋溜土豆丝", dishes)


def test_looks_intact_empty_dishes():
    assert looks_intact("随便什么", [])


# --- summarize 的兜底：润色写崩时必须退回模板 ---


class _FakeLLM:
    """替掉 ChatOpenAI。不发请求，只按剧本返回。"""

    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc

    def invoke(self, _messages):
        if self._exc is not None:
            raise self._exc
        return type("Resp", (), {"content": self._content})()


@pytest.fixture
def _with_api(monkeypatch):
    """假装配好了密钥，否则 summarize 会跳过润色直接走模板，测不到校验逻辑。"""
    monkeypatch.setattr(
        summarize_mod,
        "get_settings",
        lambda: Settings(deepseek_api="test-key", deepseek_model="test-model"),
    )


def _patch_llm(monkeypatch, **kw):
    monkeypatch.setattr(summarize_mod, "ChatOpenAI", lambda **_kwargs: _FakeLLM(**kw))


@pytest.mark.usefixtures("_with_api")
def test_summarize_uses_model_output_when_faithful(monkeypatch):
    dishes = [_dish()]
    faithful = render_text("我有土豆", dishes)
    _patch_llm(monkeypatch, content=faithful)
    assert summarize("我有土豆", dishes).strip() == faithful.strip()


@pytest.mark.usefixtures("_with_api")
def test_summarize_falls_back_when_link_dropped(monkeypatch):
    """模型把链接写错或漏掉，必须退回模板——链接错了整个结果就没用了。"""
    dishes = [_dish()]
    _patch_llm(monkeypatch, content="推荐醋溜土豆丝，视频很好，链接我忘了")
    assert summarize("我有土豆", dishes) == render_text("我有土豆", dishes)


@pytest.mark.usefixtures("_with_api")
def test_summarize_falls_back_when_model_raises(monkeypatch):
    dishes = [_dish()]
    _patch_llm(monkeypatch, exc=RuntimeError("连接超时"))
    assert summarize("我有土豆", dishes) == render_text("我有土豆", dishes)


@pytest.mark.usefixtures("_with_api")
def test_summarize_falls_back_on_empty_output(monkeypatch):
    dishes = [_dish()]
    _patch_llm(monkeypatch, content="   ")
    assert summarize("我有土豆", dishes) == render_text("我有土豆", dishes)


def test_summarize_skips_model_without_api_key(monkeypatch):
    """没配密钥时不该去调模型，直接给模板。"""
    dishes = [_dish()]
    monkeypatch.setattr(
        summarize_mod, "get_settings", lambda: Settings(deepseek_api="")
    )
    called = []
    monkeypatch.setattr(
        summarize_mod, "ChatOpenAI", lambda **kw: called.append(kw) or _FakeLLM(content="x")
    )
    assert summarize("我有土豆", dishes) == render_text("我有土豆", dishes)
    assert called == []

