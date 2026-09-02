"""Order router: the only code path that turns an approved intent into a
venue order.

Responsibility: submission sequencing with the write-ahead journal contract
-- intent journaled before the POST, venue order id journaled after, an
ambiguous outcome freezes the book and stops the batch. Dry-run short
circuits here, before any client call.
Not responsible for: deciding quantities (strategy + risk gate), polling
order state (order_monitor.py), or resolving ambiguity (reconciler.py).

Red-line wiring (CLAUDE.md section 3.1):
    - execution.dry_run defaults to True; a dry run journals the intent and
      never touches the order endpoints.
    - Even with dry_run false, each run must be armed with an explicit
      --allow-orders flag (per-run arming); an unarmed run downgrades to dry
      run and logs CRITICAL so the downgrade cannot pass unnoticed.
    - The client itself re-asserts dry_run is off and, in the live
      environment, common.config.assert_live_allowed (defense in depth).

Non-idempotency contract (S4: every order endpoint in the OpenAPI mirror
carries the beta non-idempotency warning, and there is no client order id):
one intent gets AT MOST one POST, ever. A transport timeout does not prove
absence; it raises OrderSubmitAmbiguousError, the ledger freezes, remaining
intents are NOT submitted, and only reconciliation may resolve the freeze.

Public classes:
    SubmitReport

Public functions:
    intent_id_for(strategy_id, decision_day, symbol, quantity)
    submit_intents(intents, ledger, client, decision_day, dry_run, armed,
                   halt_path)
"""

from __future__ import annotations

__all__ = ["SubmitReport", "intent_id_for", "submit_intents"]

import contextlib
import re
import signal
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from common.alerts import notify
from common.logging_setup import get_logger
from common.net import PermanentError
from trading212.client import OrderSubmitAmbiguousError, T212Client
from trading212.execution import risk_gate
from trading212.execution.risk_gate import OrderIntent

log = get_logger("t212.execution")


@dataclass
class SubmitReport:
    """Outcome of one submission batch."""
    submitted: list[tuple[OrderIntent, int]] = field(default_factory=list)
    dry_run: list[OrderIntent] = field(default_factory=list)
    rejected: list[tuple[OrderIntent, str]] = field(default_factory=list)
    skipped_duplicates: list[OrderIntent] = field(default_factory=list)
    ambiguous: OrderIntent | None = None
    not_attempted: list[OrderIntent] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"submitted={len(self.submitted)}", f"dry_run={len(self.dry_run)}",
                 f"rejected={len(self.rejected)}",
                 f"duplicates={len(self.skipped_duplicates)}"]
        if self.ambiguous is not None:
            parts.append(f"AMBIGUOUS={self.ambiguous.symbol} "
                         f"(+{len(self.not_attempted)} not attempted)")
        return " ".join(parts)


@contextlib.contextmanager
def _signals_held():
    """Defer SIGINT/SIGTERM delivery for the enclosed critical section.

    Only effective on the main thread (signal masks are process-wide but
    Python delivers handlers on the main thread); on any other thread this
    is a no-op, which is safe because the run_a0 CLI is single-threaded.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held = {signal.SIGINT, signal.SIGTERM}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def intent_id_for(strategy_id: str, decision_day, symbol: str,
                  quantity: Decimal) -> str:
    """Deterministic intent id: same decision recomputed gives the same id.

    The strategy is a pure function of the decision day's data, so a re-run
    of the same day reproduces the same quantities and collides with the
    journaled intent instead of producing a second order. The daily cycle
    additionally refuses to re-decide a day outright.
    """
    return f"{strategy_id}|{decision_day.date()}|{symbol}|{quantity}"


_PRECISION_MARKER = "quantity-precision-mismatch"
_PRECISION_DECIMALS = re.compile(r"(?:precision|decimal[s]?)\D{0,20}?(\d+)",
                                 re.IGNORECASE)
_PRECISION_STEP = re.compile(r"0\.0*1\b")


def _learn_quantity_step(state_dir: Path | None, intent: OrderIntent,
                         exc: Exception) -> Decimal | None:
    """Turn a quantity-precision rejection into a stored per-symbol step.

    The venue publishes no precision field -- the OpenAPI spec dropped
    minTradeQuantity -- so the only way this system can learn that a name
    trades in thousandths rather than ten-thousandths is to be told off for
    getting it wrong. Recording the answer makes that a one-off cost; without
    it the same order is rejected every session for as long as the strategy
    wants the name.

    Returns the learned step, or None when the rejection was about something
    else or the response carried no readable precision.
    """
    if state_dir is None:
        return None
    detail = f"{getattr(exc, 'detail', '')} {exc}"
    if _PRECISION_MARKER not in detail.lower().replace("_", "-"):
        return None
    step = None
    found = _PRECISION_DECIMALS.search(detail)
    if found:
        places = int(found.group(1))
        if 0 <= places <= 8:
            step = Decimal(1).scaleb(-places)
    if step is None:
        literal = _PRECISION_STEP.search(detail)
        if literal:
            step = Decimal(literal.group(0))
    if step is None or step <= 0:
        log.error("[router] %s was rejected for quantity precision but the "
                  "response carried no readable step: %s",
                  intent.symbol, detail[:200])
        return None
    return risk_gate.record_qty_step(state_dir, intent.symbol, step)


def submit_intents(intents: list[OrderIntent], ledger, client: T212Client | None,
                   decision_day, dry_run: bool, armed: bool,
                   halt_path: Path | None = None,
                   state_dir: Path | None = None) -> SubmitReport:
    """Submit approved intents in sequence, honoring the ambiguity contract.

    Args:
        intents: Risk-gate-approved intents.
        ledger: ShadowLedger.
        client: Required when actually submitting; may be None in dry run.
        decision_day: The decision day (naive Timestamp), for logging.
        dry_run: execution.dry_run from configuration.
        armed: The per-run --allow-orders flag.
        halt_path: Halt flag file; when it appears mid-batch the remaining
            intents are not attempted. None disables the per-intent check.
        state_dir: Where the learned quantity steps are stored. When given, a
            quantity-precision rejection is parsed and the step it implies is
            written there, so the next session sizes that name correctly
            instead of paying the same rejection again. None keeps the old
            behaviour of simply recording the rejection.
    """
    report = SubmitReport()
    effective_dry = dry_run or not armed
    if not dry_run and not armed:
        log.critical("[router] dry_run is off but the run is NOT armed with "
                     "--allow-orders; downgrading to dry run")
    if not effective_dry and client is None:
        raise ValueError("live submission requires a client")

    open_intent_ids = {record["intent_id"]
                       for record in ledger.open_orders.values()}

    for index, intent in enumerate(intents):
        if ledger.is_frozen:
            report.not_attempted.extend(intents[index:])
            break
        if halt_path is not None and halt_path.exists():
            # The flag file is the emergency stop; re-reading it before every
            # POST means a halt raised while the batch is running stops the
            # remaining orders, not just the next cycle. Orders already at
            # the venue are NOT canceled here -- they fill or die on their
            # own; canceling is a manual act (operations card).
            log.critical("[router] halt flag appeared mid-batch; %d intents "
                         "not attempted", len(intents) - index)
            report.not_attempted.extend(intents[index:])
            break
        if intent.intent_id in open_intent_ids:
            # A previous run already carried this intent to the venue.
            log.warning("[router] intent already has an open order, skipping: %s",
                        intent.intent_id)
            report.skipped_duplicates.append(intent)
            continue

        ledger.record_intent(intent.intent_id, intent.symbol, intent.ticker,
                             intent.quantity, intent.ref_price_usd,
                             intent.fx_usd_per_gbp, dry_run=effective_dry)
        log.info("[order] intent=%s ticker=%s qty=%s ref_usd=%s dry_run=%s",
                 intent.intent_id, intent.ticker, intent.quantity,
                 intent.ref_price_usd, effective_dry)

        if effective_dry:
            report.dry_run.append(intent)
            continue

        # The POST-to-outcome stretch is the one window where an interrupt
        # (Ctrl-C, SIGTERM) can strand a live venue order the book does not
        # know about. The mask must cover EVERY branch through its terminal
        # journal write -- an exception path is the likeliest moment for a
        # human to be killing a hung process, and releasing the mask before
        # record_submit_ambiguous would lose exactly that record. Holding
        # signals to the branch end turns "killed mid-POST" into "killed
        # between orders", which the design already survives. SIGKILL
        # cannot be held; that path is covered by the dangling-intent
        # freeze in shadow_ledger.freeze_dangling_live_intents.
        with _signals_held():
            try:
                order = client.place_market_order(intent.ticker,
                                                  intent.quantity,
                                                  extended_hours=False)
                # The venue accepted; from here every failure (missing id,
                # ledger write error, ...) leaves a LIVE order behind and
                # must therefore land in the ambiguity branch, never the
                # rejection branch. Only PermanentError proves the venue
                # refused.
                order_id = int(order["id"])
                ledger.record_submitted(intent.intent_id, order_id,
                                        intent.symbol, intent.ticker,
                                        intent.quantity,
                                        intent.ref_notional_gbp,
                                        str(order.get("status", "NEW")))
            except PermanentError as exc:
                learned = _learn_quantity_step(state_dir, intent, exc)
                reason = repr(exc) if learned is None else \
                    f"precision_learned step={learned}: {exc!r}"
                ledger.record_submit_rejected(intent.intent_id, reason)
                log.error("[router] rejected %s: %s", intent.ticker, reason)
                report.rejected.append((intent, reason))
                continue
            except Exception as exc:
                detail = exc.detail \
                    if isinstance(exc, OrderSubmitAmbiguousError) \
                    else f"unexpected failure after POST: {exc!r}"
                ledger.record_submit_ambiguous(intent.intent_id,
                                               intent.symbol, intent.ticker,
                                               intent.quantity, detail,
                                               intent.ref_notional_gbp)
                log.critical("[router] AMBIGUOUS submit for %s: %s -- book "
                             "frozen, remaining %d intents not attempted",
                             intent.ticker, detail, len(intents) - index - 1)
                notify(f"{getattr(ledger, 'strategy_id', 'strategy')} order AMBIGUOUS -- book frozen",
                       f"{intent.ticker}: {detail[:120]}; check the venue "
                       f"and run settle")
                report.ambiguous = intent
                report.not_attempted.extend(intents[index + 1:])
                break

        log.info("[order] submitted order_id=%s ticker=%s qty=%s status=%s "
                 "dry_run=False", order_id, intent.ticker, intent.quantity,
                 order.get("status"))
        report.submitted.append((intent, order_id))

    log.info("[router] %s decision_day=%s", report.summary(), decision_day.date())
    return report
