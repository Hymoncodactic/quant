"""Local desktop notification for events that must reach a human NOW.

Responsibility: one best-effort push channel for CRITICAL-grade trading
events (ambiguity freeze, reconcile mismatch, fill-timing breach, negative
strategy cash). A log line alone is a pull channel: nobody reads a log file
at 20:00 UTC unless something told them to. This module is that something.

Out of scope: deciding WHAT is critical (call sites decide); log files
(common/logging_setup.py); any network delivery -- notifications stay on
this machine by design, nothing leaves it.

Public functions:
    notify(title, message)   Fire one desktop notification, never raise.

Constants:
    OSASCRIPT_TIMEOUT_SEC  float  5.0. A notification is a courtesy; it must
                                  never stall or crash the trading process.

Inputs:
    None.
Outputs:
    A macOS Notification Center banner via /usr/bin/osascript.

Change log:
    2026-08-29  Created after the pre-live audit found CRITICAL events had
                no channel beyond the log file (finding E5).
"""

from __future__ import annotations

__all__ = ["notify", "OSASCRIPT_TIMEOUT_SEC"]

import subprocess
import sys

from common.logging_setup import get_logger

log = get_logger("common.alerts")

OSASCRIPT_TIMEOUT_SEC = 5.0


def notify(title: str, message: str) -> bool:
    """Show a desktop notification; return whether the attempt succeeded.

    Failure is logged and swallowed: the caller has already logged the
    underlying event at CRITICAL, and a broken notification channel must
    never abort a trading process mid-flight.
    """
    if sys.platform != "darwin":
        log.warning("[alerts] no notification backend on %s", sys.platform)
        return False
    script = 'display notification "{}" with title "{}"'.format(
        _escape(message[:200]), _escape(title[:80]))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=True,
                       capture_output=True, timeout=OSASCRIPT_TIMEOUT_SEC)
        return True
    except Exception as exc:
        log.warning("[alerts] notification failed: %r", exc)
        return False


def _escape(text: str) -> str:
    """Make the text safe inside an AppleScript double-quoted literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')
