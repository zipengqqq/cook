"""日志配置：终端 + 轮转文件。

终端那份一关就没了，而排查问题时最想回头的恰恰是过去的那几行——哪道菜选中了
哪条视频、B 站是不是被风控拦了、投币维度有没有整体失效。所以同时写一份到文件。

三个坑，都踩过或者差点踩到：

1. **文件处理器必须显式写 `encoding="utf-8"`。** Windows 上 `open()` 的默认编码是
   GBK，菜名写进去要么变成乱码，要么直接抛 UnicodeEncodeError 把日志本身打挂——
   而日志挂掉是静默的，你不会收到任何提示。
2. **uvicorn 有自己的一套 logging 配置。** 它跑 `dictConfig` 时会把 `uvicorn.*`
   那几个 logger 的 handler 全部换掉。命令行 `uvicorn main:app` 时它先配置再导入
   app，所以在这里覆盖是有效的；而 `python main.py` 是反过来（先导入再启动），
   得给 `uvicorn.run()` 传 `log_config=None` 让它别插手，日志才会顺到 root 上。
3. **uvicorn 的 logger 默认不往 root 传。** 不单独给它们挂文件处理器的话，500 的
   traceback 只出现在终端里，关掉就没了——那是最需要留下来的一类日志。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Settings, get_settings

# app/ 的上一层，也就是项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
# 终端上日期是噪音，看文件时日期是刚需——所以两边用不同的格式
_CONSOLE_DATEFMT = "%H:%M:%S"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 这些库把每次 HTTP 连接的细节都打成 INFO/DEBUG，一次请求能刷几十行。
# 不压掉的话，无论 INFO 还是 DEBUG 档都看不见自己的日志。
# httpx2 / httpcore2 是较新的包名，与 httpx / httpcore 并列写上以防环境差异。
NOISY_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "openai")

# uvicorn 的日志要单独接管，理由见模块开头的第 3 条。
# 连 "uvicorn" 一起列上：命令行启动时 uvicorn.error 是往它上面传的
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# 打在 handler 上的记号，用来认出「哪些是我们装的」。
# 重复调用 setup_logging 时先摘掉旧的，否则日志会被写两遍。
_MARK = "_cook_handler"


# uvicorn.error 是 uvicorn 的万能 logger：启动、lifespan、协议错误、reload 监管
# 全往它上面打（见 uvicorn/server.py 与 protocols/http/*）。名字里的 error 是早期
# 遗留——当初只有「access」和「其它」两类，另一类被叫成了 error，跟级别没有半点关系。
# 原样打出来有两个坏处：看着像出错了；`grep -i error` 会命中每一行正常的启动信息。
# 名字对 app.* 的日志才有价值（一眼看出是哪个模块打的），uvicorn 一共就三个 logger，
# 看消息本身就知道是谁，所以这里只把显示名收短。
_NAME_ALIASES = {"uvicorn.error": "uvicorn"}


class _Formatter(logging.Formatter):
    """只改 logger 名字的显示，其余原样交给父类。"""

    def format(self, record: logging.LogRecord) -> str:
        alias = _NAME_ALIASES.get(record.name)
        if alias is None:
            return super().format(record)
        # 改回去是必要的：同一条 record 会被终端和文件两个处理器各格式化一次，
        # 留在这个对象上会让后来者拿到被改过的名字。
        original, record.name = record.name, alias
        try:
            return super().format(record)
        finally:
            record.name = original


def _attach(logger: logging.Logger, handler: logging.Handler) -> None:
    setattr(handler, _MARK, True)
    logger.addHandler(handler)


def _drop_ours(logger: logging.Logger) -> None:
    """摘掉之前由本模块装上的处理器，顺手关掉文件句柄。"""
    for handler in list(logger.handlers):
        if getattr(handler, _MARK, False):
            logger.removeHandler(handler)
            handler.close()


def resolve_log_path(settings: Settings) -> Path:
    """把配置里的 log_dir / log_file 解析成绝对路径。

    相对路径按**项目根目录**算而不是当前工作目录：从别处运行
    `python D:/code/cook/main.py` 是文档里支持的用法，跟着 CWD 走会把日志
    散落在各个启动目录里，回头根本找不着。
    """
    directory = Path(settings.log_dir).expanduser()
    if not directory.is_absolute():
        directory = _PROJECT_ROOT / directory
    return directory / settings.log_file


def setup_logging(settings: Settings | None = None) -> Path:
    """装好日志处理器，返回日志文件的路径。重复调用是安全的。

    不传 settings 就用全局配置。测试里传一个把 log_dir 指向临时目录的实例。
    """
    settings = settings or get_settings()
    log_path = resolve_log_path(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    root = logging.getLogger()
    _drop_ours(root)
    root.setLevel(settings.log_level)

    console = logging.StreamHandler()
    console.setFormatter(_Formatter(_FORMAT, datefmt=_CONSOLE_DATEFMT))
    _attach(root, console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        # 少这一行，中文在 Windows 上就是乱码，见模块开头第 1 条
        encoding="utf-8",
    )
    file_handler.setFormatter(_Formatter(_FORMAT, datefmt=_FILE_DATEFMT))
    _attach(root, file_handler)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        # 连 uvicorn 自己装的处理器一起摘掉：命令行启动时它已经配过一遍，
        # 留着同一条日志会在终端出现两次
        for handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(handler)
        _drop_ours(uvicorn_logger)
        # 两个处理器都要挂。只挂文件的话，把它跟 root 的关系一断，
        # uvicorn 的启动信息和 traceback 就从终端上消失了
        _attach(uvicorn_logger, console)
        _attach(uvicorn_logger, file_handler)
        # 已经自己处理了，再往 root 传就是重复写
        uvicorn_logger.propagate = False

    return log_path
