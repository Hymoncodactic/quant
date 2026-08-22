"""Static instrument facts for the T212 backtest adapter.

Responsibility: per-symbol exchange time zone, measured half spreads, security
kind (which drives stamp-duty and PTM applicability), the venue's annualization
factor, and the US/LSE simultaneous-open test that decides when an LSE half
spread is widened. Sources are stated per constant. Where a value is an
inference rather than a measurement it is marked INFERRED and must be treated
as a sensitivity parameter, not a fact (docs/backtest/framework/04_cost_model.md
section 4).

Out of scope: fee arithmetic, which belongs to backtest/t212/costs.py; fill
logic, which belongs to backtest/t212/broker_sim.py; fault parameters, which
belong to backtest/t212/faults.py.

Public functions:
    exchange_tz(symbol)
        IANA zone of the symbol's exchange.
    half_spread_bps(symbol)
        Half of the touch spread in basis points: measured where available,
        the INFERRED default otherwise.
    security_kind(symbol)
        "stock" | "etf" | "etc" | "fx". An unmapped symbol defaults to
        "stock", the conservative (taxed) assumption for UK lines.
    in_us_overlap(ts_utc)
        Whether an instant falls inside the US/LSE simultaneous-open window.

Constants:
    T212_ANNUALIZATION_DAYS
        int, 252. UK equities trade 252 days a year. The factor lives in the
        venue adapter so the engine cannot silently reuse it for crypto
        (backtest-discipline section 5.2 and section 10).
    EXCHANGE_TZ_LONDON, EXCHANGE_TZ_NEW_YORK
        "Europe/London" and "America/New_York". The mapping was verified on
        real data 2026-08-20: daily bars stamp exchange-local midnight, and the
        observed midnights place .L suffixes and GBPUSD=X in London and
        everything else in New York (docs/backtest/framework/02_data_layer.md
        section 3.1).
    HALF_SPREAD_BPS
        dict of 10 LSE lines, from 0.36 bps (CSPX.L) to 8.94 bps (IBTL.L).
        MEASURED: half of the touch spreads captured from the LSE's own
        interface on 2026-08-19 at about 08:47 London time and recorded in
        research/notes/20260819_t212_execution_and_liquidity.md section 2. T212
        itself adds no spread; the fill pays the reference exchange's touch
        (order-execution-policy.pdf sections 2 and 7, cited in
        docs/backtest/framework/04_cost_model.md section 1).
    DEFAULT_HALF_SPREAD_BPS_US
        Decimal, 1.0 bps. INFERRED: large-cap US names quote around 1 to 2 bps
        touch and no local measurement exists.
    DEFAULT_HALF_SPREAD_BPS_LSE
        Decimal, 9.0 bps. INFERRED: the worst measured LSE half spread
        (IBTL.L, 8.94 bps) rounded up, so an unmeasured LSE line is never
        assumed tighter than anything actually observed.
    SECURITY_KIND
        dict of 11 symbols. Security kind drives UK tax applicability: SDRT
        hits LSE STOCK buys only, while ETFs, ETCs, gilts and bonds are exempt
        (helpcentre article 360007081637, official, cited in
        docs/backtest/framework/04_cost_model.md section 3). Every current
        uk_tradable symbol is an ETF or ETC (trading212/ingest/yahoo_bars.py
        UNIVERSE), so SDRT is structurally zero for the present pool; the rule
        stays implemented for future single-name UK stocks.
    US_SESSION_OPEN_LOCAL, LSE_SESSION_CLOSE_LOCAL
        09:30 America/New_York and 16:30 Europe/London. The overlap window is
        the stretch in which US underlyings have live US quotes while the LSE
        is still open, so London market makers stop pricing off futures
        (research/notes/20260819_t212_execution_and_liquidity.md section 4.1).
        It is defined by the two venues' LOCAL sessions because the UTC window
        shifts twice a year on each side; a constant UTC window is wrong for
        the GMT months.

Inputs: None. Pure lookups over module-level tables; no file or network access.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec; each constant's source
                note is carried over from the inline comments.
"""

from __future__ import annotations

__all__ = ["exchange_tz", "half_spread_bps", "security_kind", "in_us_overlap",
           "T212_ANNUALIZATION_DAYS", "EXCHANGE_TZ_LONDON",
           "EXCHANGE_TZ_NEW_YORK", "HALF_SPREAD_BPS",
           "DEFAULT_HALF_SPREAD_BPS_US", "DEFAULT_HALF_SPREAD_BPS_LSE",
           "SECURITY_KIND"]

from decimal import Decimal

import pandas as pd

# ============================================================================
# [1] Constants
# ============================================================================

# UK equities trade 252 days a year; the annualization factor lives in the
# venue adapter so the engine cannot silently reuse it for crypto
# (backtest-discipline section 5.2 / section 10).
T212_ANNUALIZATION_DAYS = 252

EXCHANGE_TZ_LONDON = "Europe/London"
EXCHANGE_TZ_NEW_YORK = "America/New_York"

# Half of the touch spread, in basis points of the mid price. Measured values
# are HALF of the touch spreads captured from the LSE's own interface on
# 2026-08-19 ~08:47 London time and recorded in
# research/notes/20260819_t212_execution_and_liquidity.md section 2.
# T212 itself adds no spread; the fill pays the reference-exchange touch
# (order-execution-policy.pdf sections 2 and 7, cited in
# docs/backtest/framework/04_cost_model.md section 1).
HALF_SPREAD_BPS: dict[str, Decimal] = {
    "CSPX.L": Decimal("0.36"),
    "VUSA.L": Decimal("0.70"),
    "IB01.L": Decimal("0.83"),
    "IGLN.L": Decimal("1.04"),
    "EQQQ.L": Decimal("1.32"),
    "SGLN.L": Decimal("1.61"),
    "XSPS.L": Decimal("1.84"),
    "IDTL.L": Decimal("6.59"),
    "IUCS.L": Decimal("7.31"),
    "IBTL.L": Decimal("8.94"),
}

# INFERRED defaults for symbols without a measurement: large-cap US names
# quote around 1-2 bps touch (no local measurement exists); unmeasured LSE
# lines get the WORST measured LSE half-spread (IBTL.L 8.94 bps, table above)
# rounded up, so an unmeasured line is never assumed tighter than anything
# actually observed. Sensitivity parameters, not facts.
DEFAULT_HALF_SPREAD_BPS_US = Decimal("1.0")
DEFAULT_HALF_SPREAD_BPS_LSE = Decimal("9.0")

# Security kind drives UK tax applicability: SDRT hits LSE STOCK buys only;
# ETFs, ETCs, gilts and bonds are exempt (helpcentre article 360007081637,
# official, cited in docs/backtest/framework/04_cost_model.md section 3).
# Every current uk_tradable symbol is an ETF or ETC (trading212/ingest/
# yahoo_bars.py UNIVERSE), so SDRT is structurally zero for the present pool;
# the rule stays implemented for future single-name UK stocks.
SECURITY_KIND: dict[str, str] = {
    "SGLN.L": "etc", "IGLN.L": "etc",
    "IB01.L": "etf", "IDTL.L": "etf", "IBTL.L": "etf", "XSPS.L": "etf",
    "VUSA.L": "etf", "CSPX.L": "etf", "EQQQ.L": "etf", "IUCS.L": "etf",
    "GBPUSD=X": "fx",
}

# The overlap window in which US underlyings have live US quotes while the
# LSE is still open, so London market makers stop pricing off futures
# (research/notes/20260819_t212_execution_and_liquidity.md section 4.1).
# Defined by the two venues' LOCAL sessions -- US regular open 09:30
# America/New_York, LSE close 16:30 Europe/London -- because the UTC window
# shifts twice a year on each side; a constant UTC window is wrong for the
# GMT months.
US_SESSION_OPEN_LOCAL = pd.Timestamp("09:30").time()
LSE_SESSION_CLOSE_LOCAL = pd.Timestamp("16:30").time()


# ============================================================================
# [2] Lookups
# ============================================================================

def exchange_tz(symbol: str) -> str:
    """IANA zone of the symbol's exchange.

    Mapping verified on real data 2026-08-20: daily bars stamp exchange-local
    midnight, and the observed midnights place .L suffixes and GBPUSD=X in
    Europe/London, everything else in America/New_York
    (docs/backtest/framework/02_data_layer.md section 3.1).
    """
    if symbol.endswith(".L") or symbol == "GBPUSD=X":
        return EXCHANGE_TZ_LONDON
    return EXCHANGE_TZ_NEW_YORK


def half_spread_bps(symbol: str) -> Decimal:
    """Half touch spread in bps: measured where available, INFERRED default
    otherwise."""
    if symbol in HALF_SPREAD_BPS:
        return HALF_SPREAD_BPS[symbol]
    if symbol.endswith(".L"):
        return DEFAULT_HALF_SPREAD_BPS_LSE
    return DEFAULT_HALF_SPREAD_BPS_US


def security_kind(symbol: str) -> str:
    """Security kind for tax applicability; unmapped symbols default to
    "stock", which is the conservative (taxed) assumption for UK lines."""
    if symbol in SECURITY_KIND:
        return SECURITY_KIND[symbol]
    return "stock"


def in_us_overlap(ts_utc: pd.Timestamp) -> bool:
    """Whether an instant falls inside the US/LSE simultaneous-open window.

    Evaluated in each venue's LOCAL clock: New York time at or past the US
    09:30 open AND London time before the LSE 16:30 close. This tracks both
    DST regimes; a fixed UTC window drifts one hour off during the months
    when only one side has switched. Daily bars carry exchange-midnight
    stamps and never fall in the window, which is intended: a daily-bar fill
    happens at the LSE open auction and deserves the widened spread
    (docs/backtest/framework/04_cost_model.md section 4.4).
    """
    ny = ts_utc.tz_convert(EXCHANGE_TZ_NEW_YORK).time()
    ldn = ts_utc.tz_convert(EXCHANGE_TZ_LONDON).time()
    return ny >= US_SESSION_OPEN_LOCAL and ldn < LSE_SESSION_CLOSE_LOCAL
