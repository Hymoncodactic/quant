"""Column layouts for each Binance bulk-archive dataset.

Binance publishes no schema documentation for most of these datasets, so every
layout below was established by downloading a file and inspecting it. Two traps
are encoded here because they will silently corrupt an ingest otherwise:

    1. Spot CSVs carry NO header row. Futures CSVs DO. Reading a spot file with
       header inference silently discards the first trade of every day.
    2. Spot timestamps became microsecond-native on 2025-01-01. Before that they
       are milliseconds. Futures stayed milliseconds throughout. Parsing a 2025
       spot file as milliseconds puts the data 55,000 years in the future.

Public functions:
    spec_for(market, dataset)    Return the DatasetSpec for one dataset
    timestamp_unit(market, day)  Resolve the timestamp unit for a given date

Public classes:
    DatasetSpec                  Column names, dtypes and header presence
"""

from __future__ import annotations

__all__ = ["DatasetSpec", "spec_for", "timestamp_unit", "SPECS", "MICROSECOND_SWITCH"]

from dataclasses import dataclass, field

# Spot bulk files switched from millisecond to microsecond timestamps on this date.
# Verified: in a 2026 spot trades file only 0.094% of timestamps are divisible by
# 1000, and there are more distinct microsecond values than millisecond values, so
# the sub-millisecond digits carry real information rather than padding.
MICROSECOND_SWITCH = "2025-01-01"


@dataclass(frozen=True)
class DatasetSpec:
    """Layout of one archive dataset.

    Attributes:
        columns: Column names in file order.
        has_header: Whether the CSV carries a header row. Spot files do not.
        ts_column: Name of the primary timestamp column, or None.
        drop: Columns to discard at ingest because they are exactly derivable or
            constant. Dropping quote_qty alone is a 1.9x size win and is lossless:
            it equals price * qty exactly, verified with decimal arithmetic over
            300,000 rows with zero mismatches.
        interval_in_path: Whether the path carries a bar interval segment.
    """

    columns: list[str]
    has_header: bool
    ts_column: str | None = "ts"
    drop: list[str] = field(default_factory=list)
    interval_in_path: bool = False


# Kline layout is shared by klines, markPriceKlines, indexPriceKlines and
# premiumIndexKlines. For the three price-index variants the volume, quote and
# taker columns are always zero and `count` is a tick count rather than a trade
# count, so they are dropped rather than stored as misleading zeros.
_KLINE_COLUMNS = [
    "ts", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

SPECS: dict[tuple[str, str], DatasetSpec] = {
    # ---- Spot: three datasets exist, and no order book data of any kind ----
    ("spot", "klines"): DatasetSpec(
        columns=_KLINE_COLUMNS, has_header=False,
        drop=["ignore", "close_time"], interval_in_path=True),
    ("spot", "aggTrades"): DatasetSpec(
        columns=["agg_id", "price", "qty", "first_id", "last_id", "ts",
                 "is_buyer_maker", "is_best_match"],
        has_header=False, drop=["is_best_match"]),
    ("spot", "trades"): DatasetSpec(
        columns=["trade_id", "price", "qty", "quote_qty", "ts",
                 "is_buyer_maker", "is_best_match"],
        has_header=False, drop=["quote_qty", "is_best_match"]),

    # ---- USD-M futures: where every interesting microstructure dataset lives ----
    ("um", "klines"): DatasetSpec(
        columns=_KLINE_COLUMNS, has_header=True,
        drop=["ignore", "close_time"], interval_in_path=True),
    ("um", "markPriceKlines"): DatasetSpec(
        columns=_KLINE_COLUMNS, has_header=True,
        drop=["ignore", "close_time", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"],
        interval_in_path=True),
    ("um", "indexPriceKlines"): DatasetSpec(
        columns=_KLINE_COLUMNS, has_header=True,
        drop=["ignore", "close_time", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"],
        interval_in_path=True),
    ("um", "premiumIndexKlines"): DatasetSpec(
        columns=_KLINE_COLUMNS, has_header=True,
        drop=["ignore", "close_time", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"],
        interval_in_path=True),
    ("um", "aggTrades"): DatasetSpec(
        columns=["agg_id", "price", "qty", "first_id", "last_id", "ts",
                 "is_buyer_maker"],
        has_header=True),
    ("um", "trades"): DatasetSpec(
        columns=["trade_id", "price", "qty", "quote_qty", "ts", "is_buyer_maker"],
        has_header=True, drop=["quote_qty"]),
    # Cumulative resting depth and notional at twelve fixed distances from mid,
    # sampled roughly every 30 seconds. This is a liquidity profile, not a book:
    # it cannot be replayed, and the reference price defining the bands is
    # undocumented.
    ("um", "bookDepth"): DatasetSpec(
        columns=["ts", "percentage", "depth", "notional"], has_header=True),
    # Open interest, top-trader long/short ratios and taker buy/sell ratio at
    # 5-minute resolution. Roughly 11.5 KB per day, the best signal per byte in
    # the whole archive.
    ("um", "metrics"): DatasetSpec(
        columns=["ts", "symbol", "sum_open_interest", "sum_open_interest_value",
                 "count_toptrader_long_short_ratio",
                 "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                 "sum_taker_long_short_vol_ratio"],
        has_header=True),
    ("um", "fundingRate"): DatasetSpec(
        columns=["calc_time", "funding_interval_hours", "last_funding_rate"],
        has_header=True, ts_column="calc_time"),
    # Event-driven best bid and ask WITH quantities. Discontinued 2024-03-30 for
    # USD-M; the window is finite and will never grow.
    ("um", "bookTicker"): DatasetSpec(
        columns=["update_id", "best_bid_price", "best_bid_qty",
                 "best_ask_price", "best_ask_qty", "transaction_time", "ts"],
        has_header=True),
}


def spec_for(market: str, dataset: str) -> DatasetSpec:
    """Return the layout for one dataset.

    Raises:
        KeyError: The combination is not one this project ingests.
    """
    try:
        return SPECS[(market, dataset)]
    except KeyError:
        raise KeyError(
            f"no schema for market={market!r} dataset={dataset!r}; "
            f"known: {sorted(SPECS)}"
        ) from None


def timestamp_unit(market: str, day: str) -> str:
    """Return the pandas timestamp unit for a file covering the given date.

    Args:
        market: "spot", "um" or "cm".
        day: Date as "YYYY-MM-DD" or "YYYY-MM".

    Returns:
        Either "us" or "ms".
    """
    if market != "spot":
        return "ms"
    return "us" if day >= MICROSECOND_SWITCH[:len(day)] else "ms"
