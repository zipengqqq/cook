# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目目标

「今天晚饭吃什么」推荐服务。纯后端（无前端），FastAPI 暴露接口，入参是一句自然语言的食材/需求描述，出参是若干道菜 + 每道菜一条 B 站做饭视频链接。

## 常用命令

依赖装在项目内的 `.venv`。**注意这台机器上裸 `pip` 指向的是 conda 的 Python**，不是这个 venv——先激活或用 `.venv/Scripts/python.exe -m pip`，否则包装错地方。

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 安装依赖（Windows）
.venv/Scripts/python.exe main.py                              # 直接启动（PyCharm 点绿三角同此）
.venv/Scripts/python.exe -m uvicorn main:app --reload         # 启动并热重载
.venv/Scripts/python.exe -m pytest                            # 跑全部测试
.venv/Scripts/python.exe -m pytest tests/test_bilibili.py -v  # 跑单个文件
.venv/Scripts/python.exe -m pytest tests/test_bilibili.py::test_parse_duration -v  # 单个测试
```

三种启动方式都验过：`python main.py`、`uvicorn main:app`，以及从**其它工作目录**运行 `python D:/code/cook/main.py`（脚本所在目录会进 `sys.path`，所以照样能跑）。监听地址改 `app_host` / `app_port`（在 `app/config.py`）。

**`main.py` 要留在项目根目录，别挪进 `app/` 里。** 根目录天然在 `sys.path` 上，`from app.xxx import` 开箱可用。放进包里就得改用相对导入，脚本模式下会报 `attempted relative import with no known parent package`，必须额外加 `sys.path` 补丁才能直接运行——这个弯路已经走过一次，别再绕回去。

`__main__` 块里传的是 app 对象而非导入字符串，所以直接运行没有热重载——这是为了让 PyCharm 的断点能命中。要热重载走 uvicorn 命令行。

激活 venv 后（`source .venv/Scripts/activate`）直接用 `pytest` / `uvicorn` 亦可。两套解释器（conda 与 `.venv`）都验过，结果一致。

跑一次真实链路（会消耗模型额度、会请求 B 站）：

```bash
python -c "import app.agent as a, json; print(json.dumps(a.recommend_dishes('我有番茄豆腐葱'), ensure_ascii=False))"
```

## 架构

三段链路，彼此独立、可分别调试：

```
POST /api/recommend
   └─ main.py            并发编排（菜与菜之间用 asyncio.gather）
        ├─ app/agent.py      ① LLM：食材 → 菜名数组
        └─ app/bilibili.py   ②③ 每个菜并发检索并选出一条视频
```

`app/agent.py` 的 LLM 调用是同步阻塞的，`main.py` 里用 `asyncio.to_thread` 扔进线程池——**不要**直接 `await`，也不要改成同步路由，否则并发检索退化成串行。

### 关键实现约束

**B 站搜索接口不返回投币数。** 可用字段只有 `play` / `like` / `favorites`(收藏) / `danmaku`。投币必须另调 `view` 接口取 `stat.coin`。所以检索是两段式：搜索粗排（用 play+like）→ 只对前 `coin_lookup_top` 条补投币 → 精排。候选数直接乘以请求数，别对整个结果集拉详情。

**无需 WBI 签名，只要 `buvid3`。** 先调 `x/frontend/finger/spi` 拿 `b_3` 塞进 cookie 即可，实测裸接口可用。若 B 站后续收紧，`_ensure_buvid` 是需要改的地方。

**视频筛选有两道门，缺一不可**，改动前先读函数上的注释：

- `relevance()` 字符覆盖率——**单独用不够**。「番茄烧豆腐」和「葱烧豆腐」共用「烧豆腐」三字，覆盖率 0.6 照样过线，但那是另一道菜。
- `has_leading_part()` 限定语门——中文菜名是「限定语 + 主料」，主料（豆腐/蛋/面）在同类菜里高度重复，限定语才是区分菜的部分。
- `score_candidates()` 末尾乘相关性系数是**软惩罚不是硬过滤**。搜「番茄烧豆腐」返回「番茄烧茄子」这类近似标题，硬阈值拦不住，收紧了又会误杀「西红柿炒鸡蛋」这种同义写法。让数据更好的那个赢。

**`WEIGHTS` 是唯一需要反复调参的地方**，别把权重散进逻辑里。三个维度量级悬殊（播放量常是投币的几十上百倍），必须先 `_log_norm` 取对数再归一化，不能直接相加。

**接口契约在 `app/models.py`，改动要同步 README。** 单个菜搜不到视频返回 `video: null`，检索失败也不该让整个请求挂掉（`main.py` 里 `return_exceptions=True` + 逐个降级）。

## 配置

`.env`（已存在，勿提交）：`DEEPSEEK_API`、`DEEPSEEK_MODEL`。其余可调参数在 `app/config.py`，都有默认值。

DeepSeek 是 OpenAI 兼容接口，走 `langchain-openai` 的 `ChatOpenAI` 覆写 `base_url`，不要引入单独的 DeepSeek SDK。

## 日志

各模块用 `logging.getLogger(__name__)`，级别由 `log_level` 控制（`app/config.py`，可用 `.env` 的 `LOG_LEVEL` 覆盖，默认 `INFO`）。

**INFO 每次请求约 7 行**：模型请求、模型返回（含菜名和耗时）、每道菜选中的视频（含 score 与三个维度原始值）、请求完成汇总（含总耗时和几道菜有视频）。

**DEBUG 用于调参**：多出模型原始输出、每个菜的搜索条数与过滤后条数、落选候选及其分数、buvid3 获取情况。调 `WEIGHTS` 时开这一档，落选候选那行能直接看出权重是否合理。

```bash
LOG_LEVEL=DEBUG .venv/Scripts/python.exe main.py
```

**`main.py` 里压第三方 logger 的那段循环不能删。** `httpx`/`httpcore`/`openai` 会把每次 HTTP 连接的细节打成 INFO/DEBUG，本项目一次推荐要发二三十个请求，不压掉的话 INFO 档被冲没、DEBUG 档完全不可用。

**B 站链路上的失败路径必须留日志。** 这些地方原本是静默 `return None`：搜索失败、没有候选通过过滤、拿不到 buvid3。它们共同的表现是「每道菜都没有视频」，没有任何线索指向原因，B 站一改接口就只能靠猜。`_fill_coin` 末尾那条「投币数全为 0」的告警也是同理——它意味着投币维度整体失效、打分已悄悄退化成两个维度。

## 提交约定

提交信息用 `类型: 内容` 格式，中文描述，一行写完：

```
feat: 支持直接运行 main.py 启动服务
fix: 修正同一主料不同菜的误匹配
docs: 补充直接启动与端口配置说明
test: 增加限定语过滤的回归用例
chore: 忽略临时目录与生成物
```

类型取 `feat` / `fix` / `docs` / `test` / `chore`。冒号后用**中文**，冒号后跟一个空格。

## 测试策略

打分与筛选是纯函数，是测试重点，已覆盖归一化、量级悬殊、全零、并列、限定语等边界。两个回归用例直接对应上面那两个 bug，别删。

网络调用（B 站搜索/view、DeepSeek）不进单测，在模块方法边界打桩。根目录 `conftest.py` 只为把项目根加进 `sys.path`。
