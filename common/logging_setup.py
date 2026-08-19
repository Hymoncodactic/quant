"""Logger initialisation (quant-error-handling section 3).

Conventions:
    - Timestamps are UTC, ISO 8601. Local time is a display concern only.
    - One logger per module; files land in logs/<name>_YYYYMMDD.log as UTF-8.
    - The console handler only surfaces WARNING and above.
    - Anything that might carry a credential goes through common.secrets.mask()
      before it reaches a log record.

Public functions:
    get_logger(name, level=INFO)  Configured logger; repeat calls do not duplicate handlers
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
    """Return a configured logger.

    Args:
        name: Dotted module name, e.g. "okx.ingest.klines". The first segment
            selects the log file, so related modules share one file.
        level: Level for the file handler. The console stays at CONSOLE_LEVEL.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:
        return logger

    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
    formatter.converter = time.gmtime          # render asctime in UTC, not local time

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

    # Records are emitted once, by this logger's own handlers.
    logger.propagate = False
    return logger
