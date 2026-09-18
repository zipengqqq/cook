# 项目介绍

给它一句「我手头有什么」，它给你几道菜，每道菜配一条 B 站做饭视频。

> 我现在有番茄，豆腐，葱，请你推荐我今天吃什么

菜名和推荐理由由大模型给出，视频则按**投币、点赞、播放量**三个维度打分选出——不是搜索结果里的第一条，也不是播放量最高的那条。

起服务后访问 http://127.0.0.1:8000/ 是个提问页：说一句手头有什么，结果以卡片列出来，每张卡带视频封面，点开就是对应的 B 站视频。


## 快速开始

**需要**：Python 3.10+（实测 3.13 / 3.14 均可）、一个 DeepSeek API Key（去 https://platform.deepseek.com 的「API Keys」页创建，新账号有免费额度）。不需要 node、数据库、Docker。

```bash
git clone https://github.com/zipengqqq/cook.git
cd cook
```

**建虚拟环境、装依赖**（下面每条命令在 macOS/Linux 上把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`，`python` 换成 `python3`）：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**建 `.env`**——在项目根目录，和 `main.py` 平级，内容两行：

```ini
DEEPSEEK_KEY=密钥
DEEPSEEK_MODEL=deepseek-flash
```

**启动**：

```bash
.venv\Scripts\python.exe main.py
```

## 说明

仅供个人学习与自用。项目不含任何绕过 B 站访问限制的手段，请遵守对应网站的服务条款。
