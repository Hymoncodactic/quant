"""T212 Invest (GBP) cost model: FX fee, UK and US pass-through taxes,
spread/slippage price adjustment, and GBP conversion helpers.

Responsibility: every GBP amount a fill moves, itemized under the venue's own
Tax.name vocabulary so backtest costs reconcile line-by-line against the real
account's GET /equity/history/orders walletImpact.taxes.
Not responsible for: deciding whether/when an order fills (matching.py,
broker_sim.py).

Fact sources (full citations in fixplans/framework/04_cost_model.md and
data/reference/t212_research_20260820/fees_costs_calendar.json):
    - FX fee 0.15%, charged by worsening the conversion rate against the
      customer on both sides (helpcentre 360018909758, official).
    - Commission zero, no added spread (helpcentre 11471996799517 and
      order-execution-policy.pdf section 2, official).
    - SDRT 0.5% on LSE STOCK buys, ETF/ETC exempt (helpcentre 360007081637).
    - PTM levy GBP 1.50 per trade over GBP 10,000, both sides (same page).
      Whether T212 really applies it to ETFs is UNVERIFIED; config flag
      ptm_levy_on_etf defaults True (conservative).
    - FINRA TAF USD 0.000195 x shares sold; SEC fee 0.00206% of sell value
      (same page; SEC wording ambiguous between % and $, the percent reading
      is taken and both readings are tiny).

Public functions and classes:
    CostConfig                       Tunable cost parameters with defaults
    price_to_gbp(price, ccy, fx)     Mid conversion for valuation, no fee
    apply_spread(price, is_buy, half_spread_bps, extra_bps)  Execution price
    fill_cash_and_costs(...)         Signed GBP cash delta + itemized costs
"""

from __future__ import annotations

__all__ = ["CostConfig", "price_to_gbp", "apply_spread", "fill_cash_and_costs",
           "FX_FEE_RATE", "STAMP_DUTY_RATE", "PTM_LEVY_GBP",
           "PTM_THRESHOLD_GBP", "FINRA_TAF_USD_PER_SHARE", "SEC_FEE_RATE",
           "FRENCH_FTT_RATE", "MIN_ORDER_VALUE_GBP"]

from dataclasses import dataclass
from decimal import Decimal

# ============================================================================
# [1] Constants (each with its source; see module docstring for URLs)
# ============================================================================

# helpcentre 360018909758: "spot exchange rate + 0.15% FX fee", worked example
# 1.41 -> 1.412115 on the sell side.
FX_FEE_RATE = Decimal("0.0015")

# helpcentre 360007081637: 0.5% on LSE share purchases; ETF/gilt/bond exempt.
STAMP_DUTY_RATE = Decimal("0.005")

# helpcentre 360007081637: GBP 1.50 per trade for orders over GBP 10,000,
# charged on both purchase and sale.
PTM_LEVY_GBP = Decimal("1.50")
PTM_THRESHOLD_GBP = Decimal("10000")

# helpcentre 360007081637: USD 0.000195 x quantity sold, US covered sales.
FINRA_TAF_USD_PER_SHARE = Decimal("0.000195")

# helpcentre 360007081637: "$0.00206% of the value of the sell order";
# percent reading = 0.0000206 of sell value.
SEC_FEE_RATE = Decimal("0.0000206")

# helpcentre 360007081637: French FTT 0.4% on purchases of qualifying French
# large-cap shares. No such instrument is in the current universe; the rule
# activates via security_kind() == "stock_fr".
FRENCH_FTT_RATE = Decimal("0.004")

# Wayback 2024-02-24 of helpcentre 360008095497 + staff forum confirmation
# 2020-08 (weak evidence: live page now gated). Configurable.
MIN_ORDER_VALUE_GBP = Decimal("1.00")

_PENCE_PER_POUND = Decimal("100")
_BPS = Decimal("10000")


@dataclass(frozen=True)
class CostConfig:
    """Cost parameters of one run. Defaults are the worst-tier settings
    (fixplans/framework/04_cost_model.md section 6)."""
    fx_fee_rate: Decimal = FX_FEE_RATE
    slippage_bps: Decimal = Decimal("5")   # zipline FixedBasisPointsSlippage default
    spread_session_multiplier: Decimal = Decimal("2")  # INFERRED, section 4.4
    ptm_levy_on_etf: bool = True           # unverified whether T212 charges; conservative
    min_order_value_gbp: Decimal = MIN_ORDER_VALUE_GBP
    # Cooldown (backtest-discipline hard list item 7): minimum bar intervals
    # between fills of DIFFERENT orders on one symbol. 1 is the structural
    # floor (bar granularity plus the no-same-bar rule already spaces fills
    # one interval apart); the WORST tier defaults to 2 because per-bar
    # volume budgets reset every bar and consecutive-bar re-entry would
    # otherwise re-eat a book the replay can never deplete. A result that
    # improves sharply when this drops is capacity illusion and must be
    # downgraded (the skill's 教训条款).
    cooldown_bars: int = 2
    # Same-close execution (EngineConfig.fill_timing == "same_close"): the
    # order is placed about one minute before the close but the fill is
    # modeled at the official close, so the gap between the two is charged
    # adversely. Calibrated 2026-08-22 on local 1m data (51 symbols x 21
    # sessions, 1,061 samples): |price 1 min before close - daily close|
    # median 4.8 bps, P75 10.7, P90 22.3; US single names P75 16.4. Worst
    # tier takes P75 rounded up, actual tier the median. Short calibration
    # window (30-day 1m history cap) -- rerun when more 1m data accrues.
    close_gap_bps: Decimal = Decimal("11")
    # Seconds before the close inside which the order must reach the venue
    # to count as a same-close fill; a latency draw beyond it spills the
    # order to the next open (user statement: last minute is feasible).
    close_window_sec: int = 60

    @staticmethod
    def actual_tier() -> "CostConfig":
        """The measured-costs comparison tier: no extra slippage, no session
        widening, structural-floor cooldown, median close gap. Spread itself
        stays on (it is a measurement, not a stress)."""
        return CostConfig(slippage_bps=Decimal("0"),
                          spread_session_multiplier=Decimal("1"),
                          cooldown_bars=1, close_gap_bps=Decimal("5"))


# ============================================================================
# [2] Conversion and price adjustment
# ============================================================================

def price_to_gbp(price: Decimal, quote_ccy: str, fx_mid: Decimal | None) -> Decimal:
    """Convert one price to GBP at MID (valuation only, no FX fee).

    Args:
        price: Price in the quote currency. GBp is pence: 1 GBp = GBP 0.01.
        quote_ccy: "USD" | "GBP" | "GBp".
        fx_mid: GBPUSD mid (USD per GBP); required for USD.
    """
    if quote_ccy == "GBP":
        return price
    if quote_ccy == "GBp":
        return price / _PENCE_PER_POUND
    if quote_ccy == "USD":
        if fx_mid is None or fx_mid <= 0:
            raise ValueError("USD price needs a positive GBPUSD mid")
        return price / fx_mid
    raise ValueError(f"unknown quote currency {quote_ccy!r}")


def apply_spread(raw_price: Decimal, is_buy: bool, half_spread_bps: Decimal,
                 extra_bps: Decimal) -> Decimal:
    """Worsen a raw bar price by half-spread plus slippage, in bps.

    Buys pay up, sells receive less. The bar price is treated as mid; the
    venue itself adds no spread, so this models crossing the reference
    exchange's touch (fixplans/framework/04_cost_model.md section 1).
    """
    adj = (half_spread_bps + extra_bps) / _BPS
    return raw_price * ((1 + adj) if is_buy else (1 - adj))


# ============================================================================
# [3] Fill cash flow
# ============================================================================

def fill_cash_and_costs(quantity: Decimal, exec_price: Decimal, quote_ccy: str,
                        fx_mid: Decimal | None, kind: str, is_lse: bool,
                        cfg: CostConfig,
                        prior_order_principal_gbp: Decimal = Decimal("0"),
                        ptm_already_charged: bool = False,
                        ) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    """Signed GBP cash delta of one fill, its principal, and itemized costs.

    Args:
        quantity: Signed shares (negative = sell).
        exec_price: Execution price in quote currency, spread already applied.
        quote_ccy: "USD" | "GBP" | "GBp".
        fx_mid: GBPUSD mid at fill time; required for USD fills.
        kind: security_kind(symbol) -- "stock" | "stock_fr" | "etf" | "etc".
        is_lse: Whether the symbol is LSE-listed (drives SDRT / PTM).
        cfg: Cost parameters.
        prior_order_principal_gbp: GBP principal already filled on the SAME
            order. The PTM levy is per ORDER over GBP 10,000, so partial
            fills must accumulate before testing the threshold.
        ptm_already_charged: Whether an earlier fill of this order already
            paid the levy; it is never charged twice.

    Returns:
        (cash_delta_gbp, principal_gbp, costs_gbp). cash_delta_gbp is
        negative for buys; principal_gbp is this fill's unsigned principal
        for the caller to accumulate. costs_gbp keys use the venue Tax.name
        vocabulary in lower case; the FX fee is both embedded in the
        effective rate (official mechanics) and reported as its own line.
    """
    is_buy = quantity > 0
    qty_abs = abs(quantity)
    gross_native = qty_abs * exec_price
    costs: dict[str, Decimal] = {}

    if quote_ccy == "USD":
        if fx_mid is None or fx_mid <= 0:
            raise ValueError("USD fill needs a positive GBPUSD mid")
        # Official mechanics: rate worsened 0.15% against the customer.
        # Buy: GBP -> USD at mid*(1-fee), so the GBP cost of a USD amount is
        # usd / (mid*(1-fee)). Sell: USD -> GBP at mid*(1+fee).
        if is_buy:
            effective = fx_mid * (1 - cfg.fx_fee_rate)
            principal_gbp = gross_native / effective
        else:
            effective = fx_mid * (1 + cfg.fx_fee_rate)
            principal_gbp = gross_native / effective
        costs["currency_conversion_fee"] = abs(principal_gbp
                                               - gross_native / fx_mid)
    else:
        principal_gbp = price_to_gbp(gross_native, quote_ccy, None)

    if is_lse and is_buy and kind == "stock":
        costs["stamp_duty_reserve_tax"] = principal_gbp * STAMP_DUTY_RATE
    if kind == "stock_fr" and is_buy:
        costs["french_transaction_tax"] = principal_gbp * FRENCH_FTT_RATE
    order_principal = prior_order_principal_gbp + principal_gbp
    if is_lse and order_principal > PTM_THRESHOLD_GBP and not ptm_already_charged:
        if kind == "stock" or cfg.ptm_levy_on_etf:
            costs["ptm_levy"] = PTM_LEVY_GBP
    if quote_ccy == "USD" and not is_buy:
        finra_usd = FINRA_TAF_USD_PER_SHARE * qty_abs
        sec_usd = gross_native * SEC_FEE_RATE
        costs["finra_fee"] = finra_usd / fx_mid
        costs["transaction_fee"] = sec_usd / fx_mid

    # The FX fee line is informational (already inside principal_gbp); every
    # other line is an addition on buys and a deduction on sells.
    extra = sum((v for k, v in costs.items()
                 if k != "currency_conversion_fee"), Decimal("0"))
    if is_buy:
        cash_delta = -(principal_gbp + extra)
    else:
        cash_delta = principal_gbp - extra
    return cash_delta, principal_gbp, costs
