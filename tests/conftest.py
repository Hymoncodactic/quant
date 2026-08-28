"""Root test fixtures: keep tests from touching the real desktop.

The alert channel (common/alerts.py) fires real macOS notifications; test
runs exercising reconcile-mismatch and ambiguity paths must not spray
banners indistinguishable from genuine trading alerts, nor stall on
osascript in headless sessions. Each importing module binds its own
`notify` name, so every binding is patched, not just the source.

Change log:
    2026-08-29  Created with the alert channel.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _silence_alerts(monkeypatch):
    import common.alerts
    calls: list[tuple[str, str]] = []

    def _capture(title: str, message: str) -> bool:
        calls.append((title, message))
        return True

    monkeypatch.setattr(common.alerts, "notify", _capture)
    for module_name in ("trading212.execution.order_router",
                        "trading212.execution.reconciler",
                        "trading212.execution.session_cycle"):
        try:
            module = __import__(module_name, fromlist=["notify"])
        except ImportError:  # pragma: no cover - optional in partial runs
            continue
        if hasattr(module, "notify"):
            monkeypatch.setattr(module, "notify", _capture)
    yield calls
