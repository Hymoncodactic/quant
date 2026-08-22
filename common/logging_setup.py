"""Logger initialization with UTC timestamps and one log file per module family.

Responsibility: return loggers configured once and identically across the project
(quant-error-handling section 3). Timestamps are rendered in UTC ISO 8601 because
local time is a display concern only, and a long-running trading process has to
be diagnosable against venue timestamps. Repeat calls for the same name return
the same logger without stacking a second pair of handlers.

Out of scope: deciding what to log and at which level, which belongs to each
calling module; redacting a value that might carry a credential, which belongs to
common.secrets.mask() and must happen before the value reaches a log record; log
rotation and retention, which are not implemented here.

Public functions:
    get_logger(name, level=INFO)  Return a configured logger for one module.

Constants:
    LOG_FORMAT     str  Record layout: time, level, logger name, message.
    LOG_DATEFMT    str  "%Y-%m-%dT%H:%M:%S", ISO 8601 to the second. The formatter
                        converter is set to time.gmtime, so the rendered time is
                        UTC rather than the host's local time.
    CONSOLE_LEVEL  int  logging.WARNING. The console carries only what needs
                        attention; the file carries the full trace.

Inputs:
    None.
Outputs:
    logs/<first dotted segment of name>_YYYYMMDD.log, UTF-8, where the date is the
    UTC date at logger creation. The directory root is common.paths.DIR_LOGS and
    is created on demand.

Change log:
    2026-08-22  Header expanded to the six-section spec.
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
