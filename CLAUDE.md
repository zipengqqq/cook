# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目目标

「今天晚饭吃什么」推荐服务。纯后端（无前端），FastAPI 暴露接口，入参是一句自然语言的食材/需求描述，出参是若干道菜 + 每道菜一条 B 站做饭视频链接。

## 常用命令

依赖装在项目内的 `.venv`。**注意这台机器上裸 `pip` 指向的是 conda 的 Python**，不是这个 venv——先激活或用 `.venv/Scripts/python.exe -m pip`，否则包装错地方。

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 安装依赖（Windows）
.venv/Scripts/python.exe -m uvicorn app.main:app --reload     # 本地启动，交互文档在 /docs
.venv/Scripts/python.exe -m pytest                            # 跑全部测试
.venv/Scripts/python.exe -m pytest tests/test_bilibili.py -v  # 跑单个文件
.venv/Scripts/python.exe -m pytest tests/test_bilibili.py::test_parse_duration -v  # 单个测试
```

激活 venv 后（`source .venv/Scripts/activate`）直接用 `pytest` / `uvicorn` 亦可。两套解释器都验过，结果一致。

跑一次真实链路（会消耗模型额度、会请求 B 站）：

```bash
python -c "import app.agent as a, json; print(json.dumps(a.recommend_dishes('我有番茄豆腐葱'), ensure_ascii=False))"
```

## 架构

三段链路，彼此独立、可分别调试：

```
POST /api/recommend
   └─ app/main.py        并发编排（菜与菜之间用 asyncio.gather）
        ├─ app/agent.py      ① LLM：食材 → 菜名数组
        └─ app/bilibili.py   ②③ 每个菜并发检索并选出一条视频
```

`app/agent.py` 的 LLM 调用是同步阻塞的，`app/main.py` 里用 `asyncio.to_thread` 扔进线程池——**不要**直接 `await`，也不要改成同步路由，否则并发检索退化成串行。

### 关键实现约束

**B 站搜索接口不返回投币数。** 可用字段只有 `play` / `like` / `favorites`(收藏) / `danmaku`。投币必须另调 `view` 接口取 `stat.coin`。所以检索是两段式：搜索粗排（用 play+like）→ 只对前 `coin_lookup_top` 条补投币 → 精排。候选数直接乘以请求数，别对整个结果集拉详情。

**无需 WBI 签名，只要 `buvid3`。** 先调 `x/frontend/finger/spi` 拿 `b_3` 塞进 cookie 即可，实测裸接口可用。若 B 站后续收紧，`_ensure_buvid` 是需要改的地方。

**视频筛选有两道门，缺一不可**，改动前先读函数上的注释：

- `relevance()` 字符覆盖率——**单独用不够**。「番茄烧豆腐」和「葱烧豆腐」共用「烧豆腐」三字，覆盖率 0.6 照样过线，但那是另一道菜。
- `has_leading_part()` 限定语门——中文菜名是「限定语 + 主料」，主料（豆腐/蛋/面）在同类菜里高度重复，限定语才是区分菜的部分。
- `score_candidates()` 末尾乘相关性系数是**软惩罚不是硬过滤**。搜「番茄烧豆腐」返回「番茄烧茄子」这类近似标题，硬阈值拦不住，收紧了又会误杀「西红柿炒鸡蛋」这种同义写法。让数据更好的那个赢。

**`WEIGHTS` 是唯一需要反复调参的地方**，别把权重散进逻辑里。三个维度量级悬殊（播放量常是投币的几十上百倍），必须先 `_log_norm` 取对数再归一化，不能直接相加。

**接口契约在 `app/models.py`，改动要同步 README。** 单个菜搜不到视频返回 `video: null`，检索失败也不该让整个请求挂掉（`app/main.py` 里 `return_exceptions=True` + 逐个降级）。

## 配置

`.env`（已存在，勿提交）：`DEEPSEEK_API`、`DEEPSEEK_MODEL`。其余可调参数在 `app/config.py`，都有默认值。

DeepSeek 是 OpenAI 兼容接口，走 `langchain-openai` 的 `ChatOpenAI` 覆写 `base_url`，不要引入单独的 DeepSeek SDK。

## 测试策略

打分与筛选是纯函数，是测试重点，已覆盖归一化、量级悬殊、全零、并列、限定语等边界。两个回归用例直接对应上面那两个 bug，别删。

网络调用（B 站搜索/view、DeepSeek）不进单测，在模块方法边界打桩。根目录 `conftest.py` 只为把项目根加进 `sys.path`。
