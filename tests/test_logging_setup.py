"""日志配置的单测。

重点是三件容易静默出错的事：文件编码、重复安装处理器、以及 uvicorn 那套
自有配置。它们共同的特点是坏了不会报错，只是日志悄悄少了一半或者变成乱码。
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app import logging_setup
from app.config import Settings


@pytest.fixture(autouse=True)
def _restore_logging():
    """setup_logging 改的是全局 logging 状态，退出时要还回去，否则污染其它用例。"""
    watched = [logging.getLogger()] + [
        logging.getLogger(name) for name in logging_setup._UVICORN_LOGGERS
    ]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in watched]

    yield

    for logger, handlers, level, propagate in saved:
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(log_dir=str(tmp_path), **overrides)


def _file_handler() -> RotatingFileHandler:
    return next(
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    )


# --- 编码：Windows 上最容易踩的一个 ---


def test_chinese_survives_the_round_trip(tmp_path):
    """处理器漏了 encoding="utf-8" 的话，中文在 Windows 上会拿 GBK 去写。

    轻则乱码，重则当场抛 UnicodeEncodeError——而日志自己挂掉是没人告诉你的，
    只会表现为「文件里怎么什么都没有」。
    """
    path = logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("app.bilibili").warning("「番茄烧豆腐」选中 BV1EA41137b3")

    # 这里用 utf-8 读得出来，就说明写进去的确实是 utf-8
    assert "「番茄烧豆腐」选中 BV1EA41137b3" in path.read_text(encoding="utf-8")


def test_file_handler_declares_utf8_explicitly(tmp_path):
    logging_setup.setup_logging(_settings(tmp_path))
    assert _file_handler().encoding.lower().replace("-", "") == "utf8"


# --- 日志文件放哪 ---


def test_relative_log_dir_anchors_to_project_root(tmp_path, monkeypatch):
    """相对路径按项目根目录算，不能跟着当前工作目录跑。

    `python D:/code/cook/main.py` 从别处启动是文档里支持的用法，
    跟着 CWD 走会把日志散落在各个启动目录里，回头根本找不着。
    """
    monkeypatch.chdir(tmp_path)
    path = logging_setup.resolve_log_path(Settings(log_dir="logs", log_file="app.log"))

    assert path.is_absolute()
    assert path.parent.parent == Path(logging_setup.__file__).resolve().parent.parent


def test_absolute_log_dir_is_used_as_is(tmp_path):
    target = tmp_path / "别处"
    path = logging_setup.resolve_log_path(Settings(log_dir=str(target), log_file="a.log"))
    assert path == target / "a.log"


# --- 装两次不能写两遍 ---


def test_setup_twice_does_not_duplicate_lines(tmp_path):
    """热重载和测试里都可能连着装两次，装一次处理器链就长一节。"""
    logging_setup.setup_logging(_settings(tmp_path))
    path = logging_setup.setup_logging(_settings(tmp_path))

    logging.getLogger("app.x").warning("只该出现一次")
    assert path.read_text(encoding="utf-8").count("只该出现一次") == 1


# --- uvicorn 的日志 ---


def test_uvicorn_logs_reach_the_file(tmp_path):
    """500 的 traceback 是 uvicorn.error 打出来的。

    它的 logger 默认不往 root 传，不单独接管的话这类日志只留在终端里，关掉就没了
    ——而这恰恰是排查问题时最需要的那一类。
    """
    path = logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("uvicorn.error").error("请求炸了")

    assert "请求炸了" in path.read_text(encoding="utf-8")


def test_uvicorn_logs_are_not_written_twice(tmp_path):
    """自己挂了处理器就不能再往 root 传，否则同一条写两遍。"""
    path = logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("uvicorn.access").info("POST /api/recommend 200")

    assert path.read_text(encoding="utf-8").count("POST /api/recommend 200") == 1


def test_uvicorn_loggers_stop_propagating(tmp_path):
    logging_setup.setup_logging(_settings(tmp_path))
    assert logging.getLogger("uvicorn.error").propagate is False


def test_uvicorn_error_logger_shows_as_uvicorn(tmp_path):
    """`uvicorn.error` 里的 error 是历史命名，不是级别——启动信息也走它。

    原样打出来，`grep -i error` 会命中每一行正常启动，排错时全是假信号。
    """
    path = logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("uvicorn.error").info("Uvicorn running on http://127.0.0.1:8000")

    line = path.read_text(encoding="utf-8")
    assert "uvicorn |" in line
    assert "uvicorn.error" not in line
    assert "INFO" in line, "级别不能被顺手改掉"


def test_app_logger_names_are_untouched(tmp_path):
    """别名只针对 uvicorn。app.* 的模块名是有用的——一眼看出是谁打的。"""
    path = logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("app.bilibili").info("选中 BV1EA41137b3")

    assert "app.bilibili |" in path.read_text(encoding="utf-8")


def test_formatter_leaves_the_record_alone():
    """格式化不许改坏 record：同一条会被终端和文件两个处理器各格式化一次。"""
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "启动", None, None)
    formatter = logging_setup._Formatter(logging_setup._FORMAT)

    assert "uvicorn |" in formatter.format(record)
    assert record.name == "uvicorn.error"


def test_uvicorn_preinstalled_handlers_are_dropped(tmp_path):
    """命令行启动时 uvicorn 已经配过一遍，留着会让终端出现两条一样的日志。"""
    logging_setup.setup_logging(_settings(tmp_path))

    handlers = logging.getLogger("uvicorn.access").handlers
    assert handlers, "至少要挂上我们自己的处理器"
    assert all(
        getattr(h, "_cook_handler", False) for h in handlers
    ), "uvicorn 自带的那套没摘干净"


# --- 第三方库的噪音要压掉 ---


def test_noisy_libraries_are_suppressed(tmp_path):
    """httpx 一次请求能刷几十行 INFO，不压掉的话自己的日志根本看不见。"""
    logging_setup.setup_logging(_settings(tmp_path))
    logging.getLogger("httpx").info("HTTP Request: POST https://api.bilibili.com")

    assert "HTTP Request" not in (tmp_path / "app.log").read_text(encoding="utf-8")


# --- 轮转 ---


def test_rotation_settings_are_applied(tmp_path):
    logging_setup.setup_logging(_settings(tmp_path, log_max_bytes=1024, log_backup_count=2))
    handler = _file_handler()
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2


def test_log_actually_rotates(tmp_path):
    """光设了参数不算数，得真的滚起来——不轮转文件会一直涨。"""
    path = logging_setup.setup_logging(_settings(tmp_path, log_max_bytes=512, log_backup_count=1))
    log = logging.getLogger("app.loud")
    for i in range(60):
        log.warning("填充 %03d %s", i, "x" * 40)

    assert path.exists()
    assert path.with_name(path.name + ".1").exists()
