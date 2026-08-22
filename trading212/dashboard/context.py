"""Shared state for one dashboard process: configuration, client, book.

Responsibility: hold the things every dashboard request needs, build them
lazily, and keep them cheap to reuse. One venue client is shared by the
sampler and the request handlers so both pass through the same client-side
rate limiter; rebuilding a client per request would defeat it.

The book is read fresh on every call rather than cached, because the
strategy process writes it from outside this process and a cached copy would
go quietly wrong the moment a session filled.

Out of scope: sampling, which belongs to collector.py; serving, which
belongs to server.py; order placement, which belongs to manual_orders.py and
to trading212/execution/.

Public classes:
    AppContext   Configuration, client, book access and the watch list.

Constants:
    VENUE            str    "t212". The dashboard is venue-specific by
                            design: the two venue lines keep their code,
                            configuration and conclusions separate
                            (CLAUDE.md section 6).
    CLIENT_TIMEOUT_SEC float 6.0. An interactive surface must fail fast. The
                            execution layer keeps the patient default,
                            because a batch job would rather wait than miss
                            a session.
    CLIENT_ATTEMPTS  int    1. No retry budget here for the same reason: on
                            an unreachable venue the default budget spends
                            close to a minute in back-off, which would stall
                            the sampling loop and freeze the interface.

Inputs:
    trading212/config/t212.<env>.yaml
    trading212/config/strategies/<strategy_id>.yaml
    data/t212/execution_state/<strategy_id>_snapshot.json
Outputs:
    None.

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["AppContext", "VENUE", "CLIENT_TIMEOUT_SEC", "CLIENT_ATTEMPTS"]

import threading
from typing import Any

import yaml

from common.config import load_config
from common.logging_setup import get_logger
from common.paths import config_dir, execution_state_dir
from trading212.client import T212Client
from trading212.execution.ledger_store import LedgerFrozenError
from trading212.execution.shadow_ledger import ShadowLedger

log = get_logger("t212.dashboard")

VENUE = "t212"
CLIENT_TIMEOUT_SEC = 6.0
CLIENT_ATTEMPTS = 1


class AppContext:
    """Everything one dashboard process shares."""

    def __init__(self, env: str | None = None) -> None:
        self._lock = threading.Lock()
        self._client: T212Client | None = None
        self._env_override = env
        self.reload_config()

    # ------------------------------------------------------------------
    # [1] Configuration
    # ------------------------------------------------------------------

    def reload_config(self) -> dict[str, Any]:
        """Re-read the venue configuration and the strategy parameters."""
        self.cfg = load_config(VENUE, self._env_override)
        execution = self.cfg.get("execution") or {}
        strategy = execution.get("strategy") or {}
        self.signal_name = strategy.get("name", "a0")
        self.signal_version = strategy.get("version", "0.0.1")
        self.strategy_id = f"{self.signal_name}_v" \
                           + self.signal_version.replace(".", "_")
        params_path = config_dir(VENUE) / "strategies" / f"{self.strategy_id}.yaml"
        self.params = yaml.safe_load(params_path.read_text(encoding="utf-8")) or {}
        self.fx_symbol = self.params.get("fx_symbol", "GBPUSD=X")
        self.state_dir = execution_state_dir(VENUE)
        self.halt_path = self.state_dir / "halt"
        # Same file the execution layer refreshes; the dashboard only reads
        # it, so opening the page cannot spend the venue's metadata budget.
        self.calendar_cache = self.state_dir / "exchange_calendar.json"
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None
        return self.cfg

    @property
    def env(self) -> str:
        return str(self.cfg.get("_env"))

    def watch_symbols(self) -> list[str]:
        """Symbols the dashboard prices: the universe plus state and FX."""
        symbols = list(self.params.get("trade_symbols") or [])
        for extra in (self.params.get("state_symbol"), self.fx_symbol):
            if extra and extra not in symbols:
                symbols.append(extra)
        return symbols

    # ------------------------------------------------------------------
    # [2] Venue client
    # ------------------------------------------------------------------

    def client(self) -> T212Client:
        """The shared read-capable client, built on first use."""
        with self._lock:
            if self._client is None:
                secret = (self.cfg.get("endpoints") or {}).get(
                    "secret_name", "trading212_api_key")
                self._client = T212Client(self.env, cfg=self.cfg,
                                          secret_name=secret,
                                          timeout_sec=CLIENT_TIMEOUT_SEC,
                                          max_attempts=CLIENT_ATTEMPTS)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    # ------------------------------------------------------------------
    # [3] The strategy's book
    # ------------------------------------------------------------------

    def ledger(self) -> ShadowLedger | None:
        """The strategy book, or None when it does not exist yet."""
        try:
            return ShadowLedger.load(self.state_dir, self.strategy_id)
        except FileNotFoundError:
            return None

    def book_state(self) -> dict[str, Any]:
        """A plain mapping of the book, including why it is unusable.

        A frozen book is reported as frozen rather than raised, because the
        dashboard's whole job at that moment is to show that it is frozen.
        """
        try:
            ledger = self.ledger()
        except LedgerFrozenError as exc:
            return {"exists": True, "usable": False, "reason": repr(exc)[:300],
                    "cash_gbp": None, "positions": {}}
        if ledger is None:
            return {"exists": False, "usable": False, "reason": "no_ledger",
                    "cash_gbp": None, "positions": {}}
        return {"exists": True, "usable": True, "reason": None,
                "cash_gbp": float(ledger.cash_gbp),
                "positions": {s: float(q) for s, q in ledger.positions.items()},
                "open_orders": ledger.open_orders,
                "frozen": ledger.is_frozen,
                "ambiguous": ledger.ambiguous_intents,
                "strategy_id": self.strategy_id}

    def halted(self) -> bool:
        return self.halt_path.exists()
