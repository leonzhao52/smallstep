"""应用日志配置。

- 控制台：INFO 级别，简洁格式，适合开发时盯着看。
- 文件：DEBUG 级别，滚动保留，记录完整内容（含发给 LLM 的消息和回复），便于排查。

用法：在模块里 `logger = logging.getLogger(__name__)`，直接用即可。
调用 setup_logging() 一次完成配置（main.py 启动时调用）。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "app.log"

_CONSOLE_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """配置根 'app' logger：控制台 INFO + 文件 DEBUG 滚动。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("app")
    root.setLevel(logging.DEBUG)
    # 避免重复添加 handler（uvicorn --reload 会多次 import）
    if getattr(root, "_configured", False):
        return

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(file_handler)

    root._configured = True
    # 防止日志向上传播到 uvicorn 的 root logger 造成重复输出
    root.propagate = False
