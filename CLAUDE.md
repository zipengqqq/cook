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

四段链路，彼此独立、可分别调试：

```
POST /api/recommend  →  纯文本
   └─ main.py            并发编排（菜与菜之间用 asyncio.gather）
        ├─ app/agent.py      ① LLM：食材 → 菜名数组
        ├─ app/bilibili.py   ②③ 每个菜并发检索并选出一条视频
        └─ app/summarize.py  ④ 渲染成文本（模型润色 + 模板兜底）
```

`app/agent.py` 和 `app/summarize.py` 的 LLM 调用都是同步阻塞的，`main.py` 里两处都用 `asyncio.to_thread` 扔进线程池——**不要**直接 `await`，也不要改成同步路由，否则并发检索退化成串行。

### 关键实现约束

**B 站搜索接口不返回投币数。** 可用字段只有 `play` / `like` / `favorites`(收藏) / `danmaku`。投币必须另调 `view` 接口取 `stat.coin`。所以检索是两段式：搜索粗排（用 play+like）→ 只对前 `coin_lookup_top` 条补投币 → 精排。候选数直接乘以请求数，别对整个结果集拉详情。

**无需 WBI 签名，只要 `buvid3`。** 先调 `x/frontend/finger/spi` 拿 `b_3` 塞进 cookie 即可，实测裸接口可用。若 B 站后续收紧，`_ensure_buvid` 是需要改的地方。

**视频筛选有两道门，缺一不可**，改动前先读函数上的注释：

- `relevance()` 字符覆盖率——**单独用不够**。「番茄烧豆腐」和「葱烧豆腐」共用「烧豆腐」三字，覆盖率 0.6 照样过线，但那是另一道菜。
- `has_leading_part()` 限定语门——中文菜名是「限定语 + 主料」，主料（豆腐/蛋/面）在同类菜里高度重复，限定语才是区分菜的部分。
- `score_candidates()` 末尾乘相关性系数是**软惩罚不是硬过滤**。搜「番茄烧豆腐」返回「番茄烧茄子」这类近似标题，硬阈值拦不住，收紧了又会误杀「西红柿炒鸡蛋」这种同义写法。让数据更好的那个赢。
- `looks_like_compilation()` 合集门——「一条视频教好几道菜」的整条滤掉。见下面单独一条。

**`WEIGHTS` 是唯一需要反复调参的地方**，别把权重散进逻辑里。三个维度量级悬殊（播放量常是投币的几十上百倍），必须先 `_log_norm` 取对数再归一化，不能直接相加。

**接口只返回文本，不再返回 JSON。** 响应体是 `text/plain`，由 `app/summarize.py` 渲染。`app/models.py` 里的结构体只在模块之间传值用，不是对外契约。单个菜搜不到视频时文字里写「没找到合适的视频」，检索失败也不该让整个请求挂掉（`main.py` 里 `return_exceptions=True` + 逐个降级）。

**输出只有菜名、推荐理由、链接三样。** 播放量/点赞/投币不进输出，视频标题和 UP 主也不进——这是明确要求，用户不看这些。所以 `_facts()` 只摊平这三个字段：给模型的字段越少，它能写歪的地方越少。**别因为"信息更全"又把数字或视频标题加回去**；真加了，`test_render_text_drops_view_counts_and_video_metadata` 会红。

**合集视频必须整条滤掉。** `looks_like_compilation()` 判的是「一条视频教好几道菜」：这种视频点进去还得自己拖进度条找对应的菜。判据是数「带烹饪动词、且以主料字收尾」的标题片段，≥3 个算合集，另有 `COMPILATION_MARKERS` 作高置信兜底。这是启发式，**宁可漏几个合集，也不能误杀正常单菜视频**——误杀的代价是「没找到合适的视频」，更糟。`test_single_dish_videos_are_not_compilations` 拿实测数据钉住了这条反例，别删。

**菜的排序用 `rank_by_popularity()` 跨菜重算，别直接拿 `VideoInfo.score` 排。** 那个分数是每道菜在自己那批候选里归一化出来的，各家的第一名都接近 1.0，跨菜比排出来的是"谁的相关性高"而不是"谁火"。没视频的菜一律垫底。

**`app/summarize.py` 的兜底链不能拆。** 模型只负责把话说顺，产出后 `looks_intact()` 校验链接和菜名，缺一个就退回 `render_text()` 模板渲染。模型超时、返回空、乱写都会走兜底，`summarize()` 不会抛异常。改这一段时保持"模型润色只是锦上添花、模板永远能出正确结果"这个性质——链接错一个是这套东西最严重的故障。

## 配置

`.env`（已存在，勿提交）：`DEEPSEEK_KEY`、`DEEPSEEK_MODEL`。其余可调参数在 `app/config.py`，都有默认值。

`dish_min` / `dish_max` 是菜数区间（默认 4–10）。具体几道交给模型按食材的实际情况定；**上限在代码里硬截，下限只写进提示词**——模型给少了不硬凑，凑数凑出来的一定是烂菜，但会打一行 WARNING，否则"怎么又只有两道"看起来像配置没生效。注意这个区间直接放大 B 站请求量：菜数 × `coin_lookup_top` 次 view 调用，10 道菜就是 60 次。

DeepSeek 是 OpenAI 兼容接口，走 `langchain-openai` 的 `ChatOpenAI` 覆写 `base_url`，不要引入单独的 DeepSeek SDK。

## 日志

各模块用 `logging.getLogger(__name__)`，级别由 `log_level` 控制（`app/config.py`，可用 `.env` 的 `LOG_LEVEL` 覆盖，默认 `INFO`）。

**INFO 每次请求约 7 行**（菜多时按菜数线性增加）：模型请求、模型返回（含菜名和耗时）、每道菜选中的视频（含 score 与三个维度原始值）、请求完成汇总（含总耗时和几道菜有视频）。

**DEBUG 用于调参**：多出模型原始输出、每个菜的搜索条数与过滤后条数、落选候选及其分数、buvid3 获取情况、润色后的文本。调 `WEIGHTS` 时开这一档，落选候选那行能直接看出权重是否合理。

**耗时只进日志，不进返回给用户的文本。** 这是明确要求，别往 `render_text()` 或润色提示词里加时间。

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
