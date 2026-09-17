"""模型输出解析的容错测试。真实返回什么样由模型决定，这层必须扛得住。"""

import pytest

from app.agent import LLMError, _extract_array, _normalize


def test_extract_plain_array():
    assert _extract_array('[{"name":"番茄炒蛋"}]') == [{"name": "番茄炒蛋"}]


def test_extract_strips_markdown_fence():
    content = '```json\n[{"name":"番茄炒蛋"}]\n```'
    assert _extract_array(content) == [{"name": "番茄炒蛋"}]


def test_extract_strips_bare_fence():
    content = '```\n[{"name":"番茄炒蛋"}]\n```'
    assert _extract_array(content) == [{"name": "番茄炒蛋"}]


def test_extract_ignores_surrounding_prose():
    content = '好的，这是我推荐的菜：\n[{"name":"番茄炒蛋"}]\n希望你喜欢！'
    assert _extract_array(content) == [{"name": "番茄炒蛋"}]


def test_extract_from_wrapped_object():
    content = '{"dishes":[{"name":"番茄炒蛋"}]}'
    assert _extract_array(content) == [{"name": "番茄炒蛋"}]


def test_extract_raises_on_garbage():
    with pytest.raises(LLMError):
        _extract_array("我不知道该推荐什么")


def test_extract_raises_on_empty():
    with pytest.raises(LLMError):
        _extract_array("")


# --- _normalize ---


def test_normalize_drops_items_without_name():
    raw = [{"name": "番茄炒蛋"}, {"reason": "缺菜名"}, "字符串", None]
    assert [d["name"] for d in _normalize(raw, 5)] == ["番茄炒蛋"]


def test_normalize_dedupes_by_name():
    raw = [{"name": "番茄炒蛋"}, {"name": "番茄炒蛋"}]
    assert len(_normalize(raw, 5)) == 1


def test_normalize_truncates_to_count():
    raw = [{"name": f"菜{i}"} for i in range(10)]
    assert len(_normalize(raw, 3)) == 3


def test_normalize_fills_missing_fields():
    out = _normalize([{"name": "番茄炒蛋"}], 5)[0]
    assert out["reason"] == ""
    assert out["ingredients_used"] == []


def test_normalize_coerces_bad_ingredients_type():
    out = _normalize([{"name": "番茄炒蛋", "ingredients_used": "番茄"}], 5)[0]
    assert out["ingredients_used"] == ["番茄"]


def test_normalize_trims_whitespace_in_name():
    assert _normalize([{"name": "  番茄炒蛋  "}], 5)[0]["name"] == "番茄炒蛋"
