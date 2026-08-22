"""Project path constants and data partition path construction.

This is the single source of truth for paths: every other module takes its paths
from here rather than assembling them itself. Layout is documented in
ARCHITECTURE.md section 3.

A trading venue and a data source are deliberately separate concepts. OKX and
Trading 212 are venues: orders are sent to them. Binance is a data source only,
because UK retail cannot trade crypto derivatives (FCA, in force since
2021-01-06) and api.binance.com answers HTTP 451 from this host anyway. Keeping
the two registries apart stops a data-only source from acquiring an empty
execution directory, and stops venue code from assuming every source can trade.

Everything that DESCRIBES the data lives under docs/data/<source>/ inside the
repository, never under data/. The data tree is excluded from version control in
full and is expected to move to an external disk, so a specification or a
manifest kept beside the bytes would walk off with the disk and leave the
repository with no record of what the data was or how to rebuild it.

Public functions:
    venue_dir(venue)                                Venue source directory
    config_dir(venue)                               Venue configuration directory
    data_dir(source, layer)                         Data layer of one source (raw / curated)
    bar_path(source, layer, inst, period, date_str) Single-day bar file
    binance_partition_path(market, dataset, symbol, period, stamp)  Binance partition
    binance_partition_dir(market, dataset, symbol, period)          Its parent directory
    equity_daily_path(group, symbol, year)          Daily bars for one calendar year
    equity_intraday_path(group, symbol, interval, start, end)  Intraday bars for one month
    equity_curated_root(data_root=None)             t212 curated tree, root injectable
    equity_interval_dir(group, symbol, interval, data_root=None)  One symbol-interval dir
    execution_state_dir(venue)                      Live execution ledger and cycle state
    month_bounds(period_start, latest)              Calendar-anchored start and end labels
    stamp_freq(stamp)                               Classify a partition stamp daily/monthly
    docs_data_dir(source)                           Versioned documentation for one source
    data_spec_path(source)                          Field and unit specification
    manifest_path(source)                           Rebuild manifest (JSONL)
    gaps_path(source)                               Gap register (CSV)

Public constants:
    ROOT, DIR_DATA, DIR_DOCS, DIR_DOCS_DATA, DIR_REFERENCE, DIR_SECRETS,
    DIR_LOGS, DIR_REPORTS, DIR_RESEARCH, DIR_SCRIPTS, DIR_BACKTEST_RESULTS,
    VENUE_DIRS, VENUES, DATA_SOURCES, LAYERS
"""

from __future__ import annotations

__all__ = [
    "venue_dir", "config_dir", "data_dir", "bar_path",
    "binance_partition_path", "binance_partition_dir",
    "equity_daily_path", "equity_intraday_path",
    "equity_curated_root", "equity_interval_dir", "execution_state_dir",
    "month_bounds", "stamp_freq",
    "docs_data_dir", "data_spec_path", "manifest_path", "gaps_path",
    "ROOT", "DIR_DATA", "DIR_DOCS", "DIR_DOCS_DATA", "DIR_REFERENCE",
    "DIR_SECRETS", "DIR_LOGS", "DIR_REPORTS",
    "DIR_RESEARCH", "DIR_SCRIPTS", "DIR_BACKTEST_RESULTS",
    "VENUE_DIRS", "VENUES", "DATA_SOURCES", "LAYERS",
]

from datetime import date
from pathlib import Path

# ============================================================================
# [1] Root paths
# ============================================================================

ROOT = Path(__file__).resolve().parent.parent

DIR_DATA = ROOT / "data"
DIR_REFERENCE = DIR_DATA / "reference"
DIR_SECRETS = ROOT / "secrets"
DIR_LOGS = ROOT / "logs"
DIR_REPORTS = ROOT / "reports"
DIR_RESEARCH = ROOT / "research"
DIR_SCRIPTS = ROOT / "scripts"
DIR_BACKTEST_RESULTS = ROOT / "backtest" / "results"

# Versioned counterpart of DIR_DATA. The bytes are excluded from git; what the
# bytes mean and how to rebuild them is kept here and is committed.
DIR_DOCS = ROOT / "docs"
DIR_DOCS_DATA = DIR_DOCS / "data"

# Venue slug -> source directory. The slug is the only legal venue identifier in
# code; see quant-code-standards section 1.3.
VENUE_DIRS: dict[str, Path] = {
    "okx": ROOT / "crypto_trading",
    "t212": ROOT / "trading212",
}
VENUES = tuple(VENUE_DIRS)

# Slugs that may appear under data/. A superset of VENUES: a source supplies
# data whether or not orders can be sent to it. "binance" is research data only.
DATA_SOURCES = ("binance", "okx", "t212")

LAYERS = ("raw", "curated")


# ============================================================================
# [2] Path construction
# ============================================================================

def venue_dir(venue: str) -> Path:
    """Return the source directory for a venue."""
    _check_venue(venue)
    return VENUE_DIRS[venue]


def config_dir(venue: str) -> Path:
    """Return the venue's configuration directory. Never holds credentials."""
    return venue_dir(venue) / "config"


def data_dir(source: str, layer: str) -> Path:
    """Return the directory for one data layer of one data source.

    Takes a data-source slug, not a venue slug: data/binance/ is legitimate even
    though nothing is ever traded on Binance from this project.

    Args:
        source: Data-source slug, see DATA_SOURCES.
        layer: Either "raw" or "curated".
    """
    _check_source(source)
    if layer not in LAYERS:
        raise ValueError(f"unknown data layer {layer!r}, expected one of {LAYERS}")
    return DIR_DATA / source / layer


def binance_partition_dir(market: str, dataset: str, symbol: str,
                          period: str | None, *,
                          data_root: Path | str | None = None) -> Path:
    """Return the directory holding one Binance dataset for one symbol.

    Layout: binance/curated/<market>/<dataset>/<symbol>/<leaf>/

    The leaf is the bar interval for the kline family and the dataset name for
    everything else, mirroring the archive itself, where a kline file is named by
    interval and only its directory says which kline variant it is.

    Args:
        market: "spot", "um" or "cm".
        dataset: Archive dataset name, e.g. "klines", "metrics", "bookTicker".
        symbol: Venue-native symbol, e.g. "BTCUSDT".
        period: Bar interval for the kline family, otherwise None.
        data_root: Optional data-lake root override (code may run from a git
            worktree while the lake lives beside the main working copy).
    """
    leaf = period if period else dataset
    base = (Path(data_root) / "binance" / "curated") if data_root is not None \
        else data_dir("binance", "curated")
    return base / market / dataset / symbol / leaf


def binance_partition_path(market: str, dataset: str, symbol: str,
                           period: str | None, stamp: str) -> Path:
    """Return the partition file for one Binance archive file.

    Args:
        stamp: Period label as the archive names it: "YYYY-MM-DD" for a daily
            file and "YYYY-MM" for a monthly one. The bookTicker partitions
            written by the 2026-08-19 loader carry the older "YYYYMMDD" form;
            both are accepted on read, and stamp_freq() classifies either.
    """
    return (binance_partition_dir(market, dataset, symbol, period)
            / f"year={stamp[:4]}" / f"{stamp}.parquet")


def execution_state_dir(venue: str) -> Path:
    """Return the directory for one venue's live-execution state.

    Holds the execution layer's event-sourced ledger (journal + snapshot),
    cycle state and the halt flag. Kept under data/ because it is
    machine-local mutable state that must never enter version control, but
    it is NOT market data: neither the raw nor the curated layer applies.
    """
    _check_venue(venue)
    return DIR_DATA / venue / "execution_state"


def bar_path(venue: str, layer: str, instrument: str, period: str, date_str: str) -> Path:
    """Return the path of a single-day bar file.

    Partitioning is <venue>/<layer>/<instrument>/<period>/year=YYYY/YYYYMMDD.parquet.
    The date identifies the data the file contains (UTC), not the download date.

    Args:
        instrument: Venue-native identifier, e.g. "BTC-USDT" or "AAPL". Never remapped.
        period: Lower-case bar period, e.g. "1m", "1h", "1d".
        date_str: Date as "YYYYMMDD".
    """
    if len(date_str) != 8 or not date_str.isdigit():
        raise ValueError(f"date_str must be YYYYMMDD, received {date_str!r}")
    return (data_dir(venue, layer) / instrument / period
            / f"year={date_str[:4]}" / f"{date_str}.parquet")


def equity_daily_path(group: str, symbol: str, year: int | str) -> Path:
    """Return the daily-bar file for one symbol and one calendar year.

    Daily history is split by year rather than kept in one file per symbol so that
    a refresh rewrites only the current year. A single 45-year file has to be
    rewritten in full on every update, which makes incremental maintenance both
    slow and risky.

    Layout: <group>/<symbol>/1d/<symbol>_<year>.parquet
    """
    return (DIR_DATA / "t212" / "curated" / group / symbol / "1d"
            / f"{_file_slug(symbol)}_{year}.parquet")


def equity_intraday_path(group: str, symbol: str, interval: str,
                         start: str, end: str) -> Path:
    """Return the intraday-bar file for one symbol, interval and month.

    The start is always the first of the calendar month, never the first date on
    which the instrument happened to trade. Anchoring to the calendar keeps the
    name predictable: a month whose first trading day is the 2nd because the 1st
    was a holiday still reads as 01, so a file name can be constructed from a
    date without consulting a trading calendar.

    The end is the last day of the calendar month, except for the month still in
    progress, where it is the last date actually present. A partial month is
    therefore self-describing.

    Layout: <group>/<symbol>/<interval>/<symbol>_<start>_<end>_<interval>.parquet
    Examples:
        us_equity/AAPL/1h/AAPL_20231001_20231031_1h.parquet   complete month
        us_equity/AAPL/1m/AAPL_20260801_20260820_1m.parquet   month in progress

    Args:
        interval: Bar interval as the source names it, e.g. "1m", "5m", "1h".
        start: First of the month, "YYYYMMDD".
        end: Month end, or the latest date present for the current month.
    """
    for label, value in (("start", start), ("end", end)):
        if len(value) != 8 or not value.isdigit():
            raise ValueError(f"{label} must be YYYYMMDD, received {value!r}")
    slug = _file_slug(symbol)
    return (DIR_DATA / "t212" / "curated" / group / symbol / interval
            / f"{slug}_{start}_{end}_{interval}.parquet")


def equity_curated_root(data_root: Path | str | None = None) -> Path:
    """Return the t212 curated tree, optionally under an injected data root.

    The injection exists because code may run from a git worktree while the
    data lake lives beside the main working copy; every consumer still gets
    the layout from here rather than assembling "t212/curated" itself.
    """
    if data_root is not None:
        return Path(data_root) / "t212" / "curated"
    return data_dir("t212", "curated")


def equity_interval_dir(group: str, symbol: str, interval: str,
                        data_root: Path | str | None = None) -> Path:
    """Return the directory of one symbol's bars for one interval.

    Layout: <curated>/<group>/<symbol>/<interval>/ (files named per
    equity_daily_path / equity_intraday_path).
    """
    return equity_curated_root(data_root) / group / symbol / interval


def month_bounds(period_start, latest) -> tuple[str, str]:
    """Return the start and end labels for one month's intraday file.

    Args:
        period_start: Any date within the month, as a date or Timestamp.
        latest: The most recent date present anywhere in this dataset. It caps
            the end label so the month in progress is not labelled with a future
            date.

    Returns:
        (start, end) as "YYYYMMDD" strings.
    """
    import calendar
    year, month = period_start.year, period_start.month
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    latest_date = latest.date() if hasattr(latest, "date") else latest
    if last > latest_date:
        last = latest_date
    return first.strftime("%Y%m%d"), last.strftime("%Y%m%d")


def _file_slug(symbol: str) -> str:
    """Make a symbol safe for a file name.

    Only the FX pseudo-tickers carry a character that is awkward in a path, and
    the substitution is reversible because no real ticker contains an underscore.
    """
    return symbol.replace("=", "_")


def stamp_freq(stamp: str) -> str:
    """Classify a partition stamp as "daily" or "monthly".

    Three forms occur in the tree. "YYYY-MM" is a monthly archive file.
    "YYYY-MM-DD" and the older dash-free "YYYYMMDD" are both daily. The
    distinction is what decides which archive URL rebuilds the partition, so it
    is derived from the label rather than guessed per dataset.

    Raises:
        ValueError: The label matches none of the three forms.
    """
    if len(stamp) == 7 and stamp[4] == "-":
        return "monthly"
    if len(stamp) == 10 and stamp[4] == "-" and stamp[7] == "-":
        return "daily"
    if len(stamp) == 8 and stamp.isdigit():
        return "daily"
    raise ValueError(f"unrecognised partition stamp {stamp!r}")


# ============================================================================
# [3] Versioned data documentation
# ============================================================================

def docs_data_dir(source: str) -> Path:
    """Return the committed documentation directory for one data source."""
    _check_source(source)
    return DIR_DOCS_DATA / source


def data_spec_path(source: str) -> Path:
    """Return the field, unit and time-zone specification for one data source."""
    return docs_data_dir(source) / "DATA_SPEC.md"


def manifest_path(source: str) -> Path:
    """Return the rebuild manifest for one data source.

    JSONL, one record per stored partition, holding the coordinates and the
    upstream URL needed to fetch that partition again plus the local size and
    row count needed to detect a damaged copy. This file is the entire reason a
    64 GiB tree can be excluded from git without becoming unaccountable, so it
    lives in the repository rather than beside the data.
    """
    return docs_data_dir(source) / "MANIFEST.jsonl"


def gaps_path(source: str) -> Path:
    """Return the gap register for one data source.

    CSV: dataset, symbol, from, to, cause, state. Records stretches known to be
    absent upstream, so a later run does not keep re-requesting them.
    """
    return docs_data_dir(source) / "GAPS.csv"


def _check_venue(venue: str) -> None:
    if venue not in VENUE_DIRS:
        raise ValueError(f"unknown venue {venue!r}, expected one of {VENUES}")


def _check_source(source: str) -> None:
    if source not in DATA_SOURCES:
        raise ValueError(f"unknown data source {source!r}, "
                         f"expected one of {DATA_SOURCES}")
