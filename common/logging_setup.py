"""日志初始化（quant-error-handling §3）。

约定：
    - 时间一律 UTC，ISO8601。
    - 每模块一路 logger，文件落 logs/<name>_YYYYMMDD.log，UTF-8。
    - 控制台只输出 WARNING 以上。
    - 密钥一律先过 common.secrets.mask() 再进日志。

对外函数：
    get_logger(name, level=INFO)  取得已配置的 logger，重复调用不重复挂 handler
"""

from __future__ import annotations

__all__ = ["get_logger"]

import logging
import time
from datetime import datetime, timezone

from common.paths import DIR_LOGS

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"
CONSOLE_LEVEL = logging.WARNING

def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """取得已配置的 logger。重复调用同名不会重复挂 handler。

    Args:
        name: 模块名，用点分层，如 "okx.ingest.klines"。
        level: 文件 handler 的级别。
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:
        return logger

    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
    formatter.converter = time.gmtime          # asctime 用 UTC

    file_handler = logging.FileHandler(
        DIR_LOGS / f"{name.split('.')[0]}_{day}.log", encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(CONSOLE_LEVEL)
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.propagate = False
    return logger
