"""接口层的单测：内容协商，以及 JSON 里到底漏了什么出去。

现在有两个出口——默认 text/plain，带 `Accept: application/json` 时给卡片要的
结构化数据。两边共用同一条链路，差别只在最后一步怎么渲染。

链路两端（DeepSeek、B 站）都在模块边界打桩，真正的网络调用不进单测，
这是本项目的既定做法。
"""

import pytest
from fastapi.testclient import TestClient

import main
from app.agent import LLMError
from app.models import VideoInfo

_JSON = {"Accept": "application/json"}


def _video(url: str = "https://www.bilibili.com/video/BV1UhMMzkEqi") -> VideoInfo:
    return VideoInfo(
        title="当你想吃醋溜土豆丝就把这部视频翻出来",
        url=url,
        author="美食强",
        cover="https://i0.hdslb.com/bfs/archive/abc.jpg",
        play=631368,
        like=31917,
        coin=3274,
        score=0.97,
    )


# 有视频的和没视频的各一道，两条分支都要覆盖
_DISHES = [
    {"name": "醋溜土豆丝", "reason": "酸辣开胃", "ingredients_used": ["土豆"]},
    {"name": "土豆鸡蛋饼", "reason": "当主食也行", "ingredients_used": ["土豆", "鸡蛋"]},
]


class _FakeBilibili:
    """替掉 BilibiliClient。不发请求，按菜名给结果。"""

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeBilibili":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def pick_best(self, dish: str) -> VideoInfo | None:
        if dish == "土豆鸡蛋饼":
            raise RuntimeError("B 站抽风了")  # 单道菜失败不该拖垮整个请求
        return _video()


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "recommend_dishes", lambda query, limit: _DISHES)
    monkeypatch.setattr(main, "BilibiliClient", _FakeBilibili)
    monkeypatch.setattr(main, "summarize", lambda query, dishes: "润色过的一段话\n")
    return TestClient(main.app)


# --- 首页 ---


def test_index_serves_the_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "今天吃什么" in res.text


# --- 默认出口仍是文本，老调用方不受影响 ---


def test_default_response_stays_plain_text(client):
    res = client.post("/api/recommend", json={"query": "我有土豆"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert res.text == "润色过的一段话\n"


# --- JSON 出口：卡片要的四样 ---


def test_accept_json_returns_structured_cards(client):
    res = client.post("/api/recommend", json={"query": "我有土豆"}, headers=_JSON)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    assert body["query"] == "我有土豆"
    assert [d["name"] for d in body["dishes"]] == ["醋溜土豆丝", "土豆鸡蛋饼"]


def test_json_card_has_only_the_four_fields(client):
    """播放量、点赞、投币、score、视频标题、UP 主——一个都不该漏出去。"""
    res = client.post("/api/recommend", json={"query": "我有土豆"}, headers=_JSON)
    assert set(res.json()["dishes"][0]) == {"name", "reason", "video"}
    assert set(res.json()["dishes"][0]["video"]) == {"url", "cover"}

    for noise in ("play", "like", "coin", "score", "631368", "3274", "美食强", "当你想吃"):
        assert noise not in res.text, f"JSON 里不该有「{noise}」"


def test_json_keeps_cover_and_url(client):
    card = client.post("/api/recommend", json={"query": "我有土豆"}, headers=_JSON).json()
    video = card["dishes"][0]["video"]
    assert video["url"] == "https://www.bilibili.com/video/BV1UhMMzkEqi"
    assert video["cover"] == "https://i0.hdslb.com/bfs/archive/abc.jpg"


def test_json_marks_dish_without_video_as_null(client):
    """搜不到视频是正常结果，不是错误：video 给 null，菜本身照常返回。"""
    body = client.post("/api/recommend", json={"query": "我有土豆"}, headers=_JSON).json()
    assert body["dishes"][1]["name"] == "土豆鸡蛋饼"
    assert body["dishes"][1]["video"] is None


def test_json_path_skips_the_polish_call(client, monkeypatch):
    """卡片不需要那段润色过的话，省下的是一次模型调用和几秒等待。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("JSON 分支不该调 summarize")

    monkeypatch.setattr(main, "summarize", _boom)
    assert client.post("/api/recommend", json={"query": "我有土豆"}, headers=_JSON).status_code == 200


# --- 失败路径 ---


def test_llm_failure_returns_502(client, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise LLMError("模型环节炸了")

    monkeypatch.setattr(main, "recommend_dishes", _fail)
    res = client.post("/api/recommend", json={"query": "我有土豆"})
    assert res.status_code == 502


def test_empty_query_is_rejected(client):
    assert client.post("/api/recommend", json={"query": ""}).status_code == 422
