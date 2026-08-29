"""The dashboard's sampling loop: start with the dashboard, stop with it.

Responsibility: while running, take one consolidated sample of everything
the dashboard shows -- account cash and positions from the venue, delayed
prices from the quote vendor, and the strategy's own book from the execution
ledger -- and hand it to snapshots.py. The loop owns its own thread and can
be stopped and restarted without touching the strategy, which runs in a
separate scheduled process and never learns the dashboard exists.

Cost control and responsiveness: each source runs on its own worker with its
own interval, and the sampling tick only reads what those workers last
produced. A slow or unreachable source therefore delays nothing: the tick
stays on its few-second cadence and simply reports that source as stale or
unavailable. Polling stops entirely while the dashboard is stopped.

Degradation: a source that fails is recorded as unavailable, with its error,
and the sample is still written. A dashboard that shows "cannot reach the
broker" is useful; one that crashes or silently shows the last good number
as if it were current is not.

Out of scope: persistence, which belongs to snapshots.py; quote fetching,
which belongs to quotes.py; anything that places an order, which belongs to
trading212/execution/. This module never writes to the ledger and never
submits.

Public classes:
    Collector   The sampling thread: start(), stop(), state().

Constants:
    DEFAULT_TICK_SEC      float  5.0. How often a sample is written. The
                                 tick only reads cached values, so this
                                 cadence holds even when a source is down.
    DEFAULT_ACCOUNT_SEC   float  10.0. Account and position polling period;
                                 the venue allows one summary per 5 s and one
                                 positions call per second, so this sits well
                                 inside both.
    DEFAULT_QUOTE_SEC     float  30.0. Quote polling period. The vendor's
                                 finest bar is one minute, so polling faster
                                 buys nothing.
    DEFAULT_ARCHIVE_SEC   float  300.0. How often the account's own history
                                 is harvested into the archive. The history
                                 endpoints are metered at six requests a
                                 minute, and the records they return change
                                 only when something trades, so five minutes
                                 is frequent enough to lose nothing.

Inputs:
    GET /api/v0/equity/account/summary, /equity/positions   (through client)
    the quote vendor                                        (through quotes)
    data/t212/execution_state/<strategy_id>_snapshot.json   (through ledger)
Outputs:
    data/t212/dashboard/live_snapshot.json
    data/t212/dashboard/samples/YYYY-MM-DD.jsonl

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["Collector", "DEFAULT_TICK_SEC", "DEFAULT_ACCOUNT_SEC",
           "DEFAULT_QUOTE_SEC", "DEFAULT_ARCHIVE_SEC"]

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from common.logging_setup import get_logger
from urllib.parse import urlsplit

from common.paths import records_dir
from trading212 import archive
from trading212.dashboard import diagnostics, snapshots
from trading212.dashboard.quotes import fetch_quotes

log = get_logger("t212.dashboard")

DEFAULT_TICK_SEC = 5.0
DEFAULT_ACCOUNT_SEC = 10.0
DEFAULT_QUOTE_SEC = 30.0
DEFAULT_ARCHIVE_SEC = 300.0

_VENUE = "t212"


class Collector:
    """Samples account, quotes and ledger on a background thread."""

    def __init__(self, context, tick_sec: float = DEFAULT_TICK_SEC,
                 account_sec: float = DEFAULT_ACCOUNT_SEC,
                 quote_sec: float = DEFAULT_QUOTE_SEC,
                 archive_sec: float = DEFAULT_ARCHIVE_SEC) -> None:
        self._ctx = context
        self._tick = tick_sec
        self._account_every = account_sec
        self._quote_every = quote_sec
        self._archive_every = archive_sec
        self._archive_result: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at: str | None = None
        self._ticks = 0
        self._last_error: str | None = None
        self._account: dict[str, Any] = {"ok": False, "reason": "not polled yet"}
        self._quotes: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # [1] Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start sampling. A second call while running is a no-op."""
        if self.running:
            return
        self._stop.clear()
        self._started_at = _now_iso()
        snapshots.mark_gap(_VENUE, "collector started", env=self._ctx.env)
        self._workers = [
            threading.Thread(target=self._poll_loop,
                             args=("account", self._account_every,
                                   self._refresh_account),
                             name="dash-account", daemon=True),
            threading.Thread(target=self._poll_loop,
                             args=("quotes", self._quote_every,
                                   self._refresh_quotes),
                             name="dash-quotes", daemon=True),
            threading.Thread(target=self._poll_loop,
                             args=("archive", self._archive_every,
                                   self._refresh_archive),
                             name="dash-archive", daemon=True),
        ]
        for worker in self._workers:
            worker.start()
        self._thread = threading.Thread(target=self._run, name="dash-collector",
                                        daemon=True)
        self._thread.start()
        log.info("[collector] started tick=%.1fs account=%.1fs quote=%.1fs",
                 self._tick, self._account_every, self._quote_every)

    def stop(self) -> None:
        """Stop sampling only. The strategy process is untouched."""
        if not self.running:
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        thread.join(timeout=self._tick + 5.0)
        for worker in self._workers:
            worker.join(timeout=1.0)   # a worker inside a timeout finishes alone
        self._workers = []
        snapshots.mark_gap(_VENUE, "collector stopped", env=self._ctx.env)
        log.info("[collector] stopped after %d ticks", self._ticks)

    def state(self) -> dict[str, Any]:
        """What the interface shows about the sampler itself."""
        with self._lock:
            return {"running": self.running, "started_at": self._started_at,
                    "ticks": self._ticks, "last_error": self._last_error,
                    "tick_sec": self._tick,
                    "account_sec": self._account_every,
                    "quote_sec": self._quote_every,
                    "archive_sec": self._archive_every,
                    "archive": dict(self._archive_result)}

    # ------------------------------------------------------------------
    # [2] Sampling
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """The sampling tick: assemble and write, never fetch."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick_once()
            except Exception as exc:            # never let the thread die
                with self._lock:
                    self._last_error = repr(exc)[:300]
                log.error("[collector] tick failed: %r", exc)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self._tick - elapsed))

    def _poll_loop(self, name: str, every: float, work) -> None:
        """One source's worker: fetch on its own interval, store, repeat.

        Running each source here rather than inside the tick is what keeps
        the interface responsive when a source is slow: an unreachable venue
        costs one worker its timeout, not every sample its cadence.
        """
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                work()
            except Exception as exc:
                log.error("[collector] %s poll crashed: %r", name, exc)
            self._stop.wait(max(1.0, every - (time.monotonic() - started)))

    def _refresh_account(self) -> None:
        self._account = self._poll_account()

    def _refresh_quotes(self) -> None:
        self._quotes = fetch_quotes(self._ctx.watch_symbols())

    def _refresh_archive(self) -> None:
        """Harvest the account's own history into the archive.

        Kept on the collector because the archive should fill while anyone is
        watching, and stop when they stop watching, exactly like the rest of
        the polling. The strategy's own runs harvest independently.
        """
        self._archive_result = archive.harvest_all(
            self._ctx.client(),
            root=records_dir(_VENUE, self._ctx.env))

    def _tick_once(self) -> None:
        book = self._ctx.book_state()
        sample = self._build_sample(book)
        thin = _thin(sample)
        snapshots.write_snapshot(_VENUE, sample, env=self._ctx.env)
        snapshots.append_sample(_VENUE, thin, env=self._ctx.env)
        snapshots.update_rollup(_VENUE, thin, env=self._ctx.env)
        with self._lock:
            self._ticks += 1
            self._last_error = None

    def _poll_account(self) -> dict[str, Any]:
        """Account summary and positions, or a recorded reason they failed."""
        try:
            client = self._ctx.client()
            summary = client.account_summary()
            positions = client.positions()
            return {"ok": True, "summary": summary, "positions": positions,
                    "at": _now_iso()}
        except Exception as exc:
            reason = repr(exc)[:300]
            log.warning("[collector] account poll failed: %s", reason)
            # A bare "cannot reach the broker" is true and useless: a hijacked
            # DNS answer, a dead network, the wrong environment and a rate
            # limit each need a different action. Diagnose once per failure so
            # the interface can say which one it is.
            try:
                host = urlsplit(self._ctx.client().base).hostname or ""
                verdict = diagnostics.diagnose(host, reason,
                                               env=self._ctx.env)
            except Exception as diag_exc:
                verdict = {"cause": "unknown", "evidence": repr(diag_exc)[:200]}
            log.warning("[collector] diagnosis: %s", verdict.get("cause"))
            return {"ok": False, "reason": reason, "at": _now_iso(),
                    "diagnosis": verdict}

    def _build_sample(self, book: dict[str, Any]) -> dict[str, Any]:
        """Assemble one sample; strategy equity is priced from the quotes."""
        positions = book.get("positions") or {}
        fx = self._quotes.get(self._ctx.fx_symbol, {})
        fx_rate = fx.get("price")
        marked: dict[str, Any] = {}
        holdings_gbp = 0.0
        priced_all = True
        for symbol, qty in positions.items():
            quote = self._quotes.get(symbol) or {}
            price = quote.get("price")
            value = None
            if price is not None and fx_rate:
                value = float(qty) * float(price) / float(fx_rate)
                holdings_gbp += value
            else:
                priced_all = False
            marked[symbol] = {"qty": float(qty), "price_usd": price,
                              "value_gbp": value,
                              "stale": bool(quote.get("stale", True))}
        cash = float(book.get("cash_gbp") or 0.0)
        equity = cash + holdings_gbp if priced_all else None
        return {"ts": _now_iso(),
                "collector": self.state(),
                "account": self._account,
                "quotes": self._quotes,
                "fx_rate": fx_rate,
                "book": {**book, "marked": marked,
                         "holdings_gbp": holdings_gbp if priced_all else None,
                         "equity_gbp": equity,
                         "priced_all": priced_all}}


def _thin(sample: dict[str, Any]) -> dict[str, Any]:
    """The subset worth keeping per tick for charting.

    Full snapshots are large and mostly repeat; the sample file keeps only
    the series a chart draws, so a day of ticks stays a few hundred
    kilobytes instead of tens of megabytes.
    """
    book = sample.get("book") or {}
    account = sample.get("account") or {}
    summary = (account.get("summary") or {}) if account.get("ok") else {}
    cash = summary.get("cash") or {}
    return {"ts": sample["ts"],
            "equity_gbp": book.get("equity_gbp"),
            "cash_gbp": book.get("cash_gbp"),
            "holdings_gbp": book.get("holdings_gbp"),
            "fx_rate": sample.get("fx_rate"),
            "account_ok": bool(account.get("ok")),
            "account_total": summary.get("totalValue"),
            "account_free": cash.get("availableToTrade"),
            "positions": {s: v.get("value_gbp")
                          for s, v in (book.get("marked") or {}).items()}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
