"""Pre-trade risk gate: rejects or shrinks intents, never enlarges them.

Responsibility: the last decision layer before the order router. Every check
either passes an intent through unchanged, trims it DOWN, or rejects it.
Nothing here may raise a quantity, flip a side or invent an order, which is
what makes the gate safe to tighten without re-reasoning about the strategy
(.claude/skills/live-trading-architecture/SKILL.md, RiskGate contract).

Fail-closed configuration: every limit must be present and positive in the
venue configuration's risk section. A missing or zero limit rejects
everything rather than meaning "unlimited", because an unfilled risk file
must never read as permission (CLAUDE.md section 1.4: parameters that shape
live orders belong to the user).

Out of scope: computing targets, which belongs to
trading212/strategy/a0_v0_0_1.py; submitting, which belongs to
trading212/execution/order_router.py; account truth, which belongs to
trading212/execution/reconciler.py; deciding when the submission window is
open, which belongs to trading212/execution/session_cycle.py using the
venue calendar in trading212/execution/instruments.py.

Public classes:
    OrderIntent   One desired order in the strategy-symbol domain.
    GateReport    Approved intents, per-rejection reasons, and whether a
                  fatal precondition closed the gate for the whole batch.

Public functions:
    check_intents(intents, ledger_view, positions_ref_notional_gbp, cfg_risk,
                  orders_today, in_submit_window, halt_path)
                  Run every check; returns a GateReport.
    halt_active(halt_path)   Whether the halt flag file exists.

Constants:
    T212_QTY_STEP     Decimal  Order quantity step, 0.0001 shares. NOT
                               officially documented: the current OpenAPI
                               spec carries no precision field and
                               minTradeQuantity was removed from it, so 4 dp
                               is the conservative floor taken from community
                               and staff reports (WORKING_MEMORY open item
                               13). The strategy quantizes to the same step.
    REQUIRED_RISK_KEYS tuple   The five risk limits that must all be set
                               positive before any order is approved.

Inputs:
    The halt flag file, existence only: data/t212/execution_state/halt
Outputs:
    None.

Change log:
    2026-08-21  Created for the daily A0 cycle.
    2026-08-22  Session semantics inverted for the hourly arm: the gate now
                requires the caller-supplied submission window to be OPEN,
                where the daily version required the market to be CLOSED.
                The insufficient-cash rejection adopted the backtest's
                reason string so both audit tables use one vocabulary.
    2026-08-23  Cash budget now rolls with approved sells, matching the
                backtest, which books a same-close sell inside submit()
                so its proceeds reach later intents in the same batch
                (research/decisions/20260823_a0_live_execution_calibers.md).
"""

from __future__ import annotations

__all__ = ["OrderIntent", "GateReport", "check_intents", "halt_active",
           "T212_QTY_STEP", "QTY_STEP_OVERRIDES", "QTY_STEP_DEFAULTS",
           "QTY_STEP_FILE", "qty_step", "qty_steps_path", "load_qty_steps",
           "record_qty_step", "REQUIRED_RISK_KEYS"]

import json
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from common.logging_setup import get_logger

log = get_logger("t212.execution")

# ============================================================================
# [1] Constants
# ============================================================================

# Order quantity step. NOT officially documented: the current OpenAPI spec
# carries no precision field (minTradeQuantity was removed from it), and the
# only evidence is community/staff posts of a historical 4-decimal cap
# (WORKING_MEMORY open item 13). 4 dp is therefore the conservative floor;
# the strategy already quantizes to the same step.
T212_QTY_STEP = Decimal("0.0001")

# Per-symbol overrides where the venue rejects the default 4-decimal step.
# The venue publishes NO precision metadata (instruments endpoint carries
# only maxOpenQuantity); every entry here is an EMPIRICAL fact from a real
# rejection, recorded with its evidence. First live session 2026-08-31:
# INTC order of 0.8326 rejected with "quantity-precision-mismatch: invalid
# quantity precision 3" (traceId 695304f478dbe5e5fe98f642539baa6b) while
# fifteen 4-decimal siblings were accepted -- precision is per instrument.
# Steps learned the hard way, from a venue rejection. INTC is the one the
# first live night produced (2026-08-31). The table is persisted per
# environment so a rejection is paid for ONCE: without that, the same order is
# rejected every session forever, because nothing else in the system knows the
# venue's per-instrument precision -- the OpenAPI spec carries no such field.
QTY_STEP_FILE = "qty_steps.json"
QTY_STEP_DEFAULTS: dict[str, str] = {"INTC": "0.001"}
QTY_STEP_OVERRIDES: dict[str, Decimal] = {
    symbol: Decimal(step) for symbol, step in QTY_STEP_DEFAULTS.items()}


def qty_step(symbol: str) -> Decimal:
    """The order-quantity step for one symbol (override or default)."""
    return QTY_STEP_OVERRIDES.get(symbol, T212_QTY_STEP)


def qty_steps_path(state_dir: Path) -> Path:
    """Where one environment's learned quantity steps are stored."""
    return Path(state_dir) / QTY_STEP_FILE


def load_qty_steps(state_dir: Path) -> dict[str, Decimal]:
    """Install the learned steps for one environment and return the table.

    Called once per cycle, before any intent is priced. The defaults are the
    floor: a stored file may add names or widen a step, never drop one, so a
    corrupted or truncated file cannot quietly restore a precision that was
    already proven wrong.
    """
    merged = {symbol: Decimal(step)
              for symbol, step in QTY_STEP_DEFAULTS.items()}
    path = qty_steps_path(state_dir)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("[risk] %s is unreadable (%s); using defaults only",
                      path, exc)
            stored = {}
        for symbol, step in (stored or {}).items():
            try:
                value = Decimal(str(step))
            except Exception:                      # noqa: BLE001
                continue
            if value > 0:
                merged[str(symbol)] = max(value, merged.get(str(symbol),
                                                            value))
    QTY_STEP_OVERRIDES.clear()
    QTY_STEP_OVERRIDES.update(merged)
    return dict(merged)


def record_qty_step(state_dir: Path, symbol: str, step: Decimal) -> Decimal:
    """Persist one learned step atomically and install it immediately.

    Widening only. A venue that rejected 0.0001 will reject it again, so a
    later reading that suggests a finer step is not evidence and is ignored.
    """
    path = qty_steps_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored: dict[str, str] = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            stored = {}
    previous = Decimal(str(stored.get(symbol, "0")))
    widened = max(previous, Decimal(str(step)))
    stored[symbol] = str(widened)
    tmp = path.with_suffix(".writing")
    tmp.write_text(json.dumps(stored, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)
    QTY_STEP_OVERRIDES[symbol] = max(QTY_STEP_OVERRIDES.get(symbol, widened),
                                     widened)
    log.warning("[risk] learned quantity step for %s: %s (was %s)",
                symbol, widened, previous or T212_QTY_STEP)
    return widened

# Every one of these must be present and positive in cfg["risk"]; the gate
# fails closed otherwise. Values are the user's call, not code defaults.
REQUIRED_RISK_KEYS = ("max_order_notional_gbp", "max_gross_notional_gbp",
                      "max_daily_orders", "min_order_value_gbp", "fee_buffer")


@dataclass(frozen=True)
class OrderIntent:
    """One desired order before venue translation.

    quantity is SIGNED shares (negative sells, mirroring the venue
    convention); ref_price_usd is the decision close, fx is USD per GBP.
    ref_notional_gbp is derived once at construction so every layer prices
    the intent identically.
    """
    intent_id: str
    symbol: str
    ticker: str
    quantity: Decimal
    ref_price_usd: Decimal
    fx_usd_per_gbp: Decimal

    @property
    def ref_notional_gbp(self) -> Decimal:
        return abs(self.quantity) * self.ref_price_usd / self.fx_usd_per_gbp


@dataclass
class GateReport:
    approved: list[OrderIntent]
    rejected: list[tuple[OrderIntent, str]]
    # True when a fatal precondition (halt, unset limits, open session)
    # closed the gate for the whole batch. The cycle treats a closed gate as
    # an ABORTED run, not a completed decision day, so fixing the
    # configuration allows a same-day retry.
    closed: bool = False

    def summary(self) -> str:
        state = "CLOSED " if self.closed else ""
        return (f"{state}{len(self.approved)} approved, "
                f"{len(self.rejected)} rejected"
                + ("" if not self.rejected else ": "
                   + "; ".join(f"{i.symbol} {r}" for i, r in self.rejected)))


# ============================================================================
# [2] Gate
# ============================================================================

def halt_active(halt_path: Path) -> bool:
    """The halt flag is a plain file; existence means stop. Creating it takes
    one shell command in an emergency; removal is deliberate -- the dashboard
    clear route (which re-checks the book first) or a manual delete."""
    return halt_path.exists()


def check_intents(intents: list[OrderIntent], ledger_view,
                  positions_ref_notional_gbp: Decimal, cfg_risk: dict,
                  orders_today: int, in_submit_window: bool,
                  halt_path: Path) -> GateReport:
    """Run every gate over the intents; tighten-only.

    Args:
        intents: Desired orders from the target diff.
        ledger_view: LedgerPortfolioView (strategy-owned book).
        positions_ref_notional_gbp: GBP value of held strategy positions at
            decision prices, for the gross-exposure check.
        cfg_risk: The venue configuration's risk mapping.
        orders_today: Orders already submitted this trading day.
        in_submit_window: Whether NOW is inside the session's submission
            window, which the caller derives from the venue calendar. A0
            fills at the session close, so an order sent outside that window
            would either miss the close or execute at an unmodeled price.
        halt_path: Halt-flag file.
    """
    fatal = _fatal_precondition(cfg_risk, in_submit_window, halt_path)
    if fatal:
        log.error("[risk] gate closed: %s", fatal)
        return GateReport(approved=[], rejected=[(i, fatal) for i in intents],
                          closed=True)

    fee_buffer = Decimal(str(cfg_risk["fee_buffer"]))
    max_order = Decimal(str(cfg_risk["max_order_notional_gbp"]))
    max_gross = Decimal(str(cfg_risk["max_gross_notional_gbp"]))
    min_value = Decimal(str(cfg_risk["min_order_value_gbp"]))
    max_daily = int(cfg_risk["max_daily_orders"])

    approved: list[OrderIntent] = []
    rejected: list[tuple[OrderIntent, str]] = []
    budget = ledger_view.available_cash_gbp
    gross = positions_ref_notional_gbp

    for intent in intents:
        verdict = _check_one(intent, ledger_view, max_order, min_value)
        if isinstance(verdict, str):
            rejected.append((intent, verdict))
            continue
        intent = verdict
        cost = intent.ref_notional_gbp * (Decimal("1") + fee_buffer)
        if intent.quantity < 0:
            # A sell approved earlier in this batch funds the buys that come
            # after it. Under the authoritative same_close timing the
            # backtest books a sell inside broker.submit() itself
            # (backtest/t212/same_close.py via fills.py apply_fill), so its
            # cash is available to later intents in the same batch. Holding a
            # static budget here would reject buys the backtest accepts and
            # push the live rejection rate above the recorded baseline.
            # Proceeds are credited NET of the same buffer rather than gross,
            # so the estimate stays on the conservative side of the real fill.
            budget += intent.ref_notional_gbp * (Decimal("1") - fee_buffer)
            # Gross exposure falls by exactly what is sold. Without this the
            # running total only ever rises, so a session that rotates a book
            # -- sell twenty, buy twenty -- is measured as if it had bought
            # forty. Starting from the full held value, a ceiling set at
            # roughly the account size then rejects the second half of every
            # rotation while the real post-session exposure sits well inside
            # the limit.
            gross -= intent.ref_notional_gbp
            if gross < 0:
                gross = Decimal("0")
        if intent.quantity > 0:
            if cost > budget:
                # Same reason string the backtest's admission layer records
                # (backtest/t212/admission.py), so the live audit table and
                # the backtest's reconcile line by line.
                rejected.append((intent, f"insufficient_free_for_stocks_buy: "
                                         f"cost {cost:.2f} over available "
                                         f"{budget:.2f}"))
                continue
            if gross + intent.ref_notional_gbp > max_gross:
                rejected.append((intent, f"gross would exceed "
                                         f"max_gross_notional_gbp {max_gross}"))
                continue
            budget -= cost
            gross += intent.ref_notional_gbp
        if orders_today + len(approved) + 1 > max_daily:
            rejected.append((intent, f"max_daily_orders {max_daily} reached"))
            continue
        approved.append(intent)

    report = GateReport(approved=approved, rejected=rejected)
    log.info("[risk] %s", report.summary())
    return report


# ============================================================================
# [3] Internals
# ============================================================================

def _fatal_precondition(cfg_risk: dict, in_submit_window: bool,
                        halt_path: Path) -> str | None:
    """Conditions that close the gate for every intent at once."""
    if halt_active(halt_path):
        return f"halt flag present at {halt_path}"
    for key in REQUIRED_RISK_KEYS:
        raw = cfg_risk.get(key)
        try:
            value = Decimal(str(raw))
        except Exception:
            return f"risk.{key} is not a number ({raw!r}); gate fails closed"
        if key != "fee_buffer" and value <= 0:
            return f"risk.{key} must be set positive by the user; gate fails closed"
        if key == "fee_buffer" and not (0 <= value < Decimal("0.2")):
            return f"risk.fee_buffer {raw!r} outside [0, 0.2); gate fails closed"
    if not in_submit_window:
        return ("outside the session submission window; A0 fills at the "
                "close, so orders are sent only shortly before it")
    return None


def _check_one(intent: OrderIntent, ledger_view, max_order: Decimal,
               min_value: Decimal) -> "OrderIntent | str":
    """Per-intent checks. Returns the (possibly trimmed) intent or a reason."""
    if intent.quantity == 0:
        return "zero quantity"
    if intent.ref_price_usd <= 0 or intent.fx_usd_per_gbp <= 0:
        return "non-positive reference price or FX"

    quantity = intent.quantity
    step = qty_step(intent.symbol)
    magnitude = abs(quantity).quantize(step, rounding=ROUND_DOWN)
    if magnitude == 0:
        return f"below quantity step {step}"

    if quantity < 0:
        held = ledger_view.positions.get(intent.symbol, Decimal("0"))
        pending = ledger_view.pending_signed_qty.get(intent.symbol, Decimal("0"))
        # Quantity already committed to in-flight sell orders is not sellable
        # again; pending is signed, so only its negative part subtracts.
        sellable = held + min(pending, Decimal("0"))
        if sellable <= 0:
            return "sell with no strategy-owned position (never short)"
        if magnitude > sellable:
            magnitude = sellable.quantize(qty_step(intent.symbol),
                                          rounding=ROUND_DOWN)
            if magnitude == 0:
                return "held quantity rounds to zero at the venue step"
        # Residual below the minimum: a partial sell that would leave a stub
        # worth less than one order is enlarged into a full exit instead. The
        # stub could never be sold afterwards -- every future order for it
        # falls under the same floor -- so the position would be stranded at
        # the venue while the strategy believed it had left. Enlarging a SELL
        # only reduces exposure, which is the direction the gate is allowed to
        # move in.
        residual = sellable - magnitude
        residual_gbp = residual * intent.ref_price_usd / intent.fx_usd_per_gbp
        if residual > 0 and residual_gbp < min_value:
            magnitude = sellable.quantize(qty_step(intent.symbol),
                                          rounding=ROUND_DOWN)
            log.info("[risk] %s sell enlarged to a full exit: the residual "
                     "would have been %.2f GBP, under min_order_value_gbp %s",
                     intent.symbol, residual_gbp, min_value)

    trimmed = replace(intent, quantity=magnitude.copy_sign(quantity))
    if trimmed.ref_notional_gbp < min_value:
        # Minimum order value: helpcentre article 360008095497 said 1
        # GBP/EUR; page now login-gated, so the config value is the
        # conservative, user-owned floor (unverified venue fact).
        return f"notional {trimmed.ref_notional_gbp:.2f} below " \
               f"min_order_value_gbp {min_value}"
    if trimmed.quantity > 0 and trimmed.ref_notional_gbp > max_order:
        # The per-order ceiling applies to BUYS only. It exists to cap how
        # much new exposure one order may add; applied to a sell it does the
        # opposite of its purpose -- a position that grew past the ceiling
        # could never be exited, and the strategy's zero targets would be
        # rejected every session forever while the position stayed on.
        return f"notional {trimmed.ref_notional_gbp:.2f} exceeds " \
               f"max_order_notional_gbp {max_order}"
    return trimmed
