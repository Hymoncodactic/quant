"""Project path constants and data partition path construction.

This is the single source of truth for paths: every other module takes its paths
from here rather than assembling them itself. Layout is documented in
ARCHITECTURE.md section 3.

Public functions:
    venue_dir(venue)                                Venue source directory
    config_dir(venue)                               Venue configuration directory
    data_dir(venue, layer)                          Venue data layer (raw / curated)
    bar_path(venue, layer, inst, period, date_str)  Single-day bar file
    manifest_path(venue)                            Raw-layer retrieval manifest
    gaps_path(venue)                                Curated-layer gap register

Public constants:
    ROOT, DIR_DATA, DIR_REFERENCE, DIR_SECRETS, DIR_LOGS, DIR_REPORTS,
    DIR_RESEARCH, DIR_SCRIPTS, DIR_BACKTEST_RESULTS, VENUE_DIRS, VENUES, LAYERS
"""

from __future__ import annotations

__all__ = [
    "venue_dir", "config_dir", "data_dir", "bar_path", "manifest_path", "gaps_path",
    "ROOT", "DIR_DATA", "DIR_REFERENCE", "DIR_SECRETS", "DIR_LOGS", "DIR_REPORTS",
    "DIR_RESEARCH", "DIR_SCRIPTS", "DIR_BACKTEST_RESULTS",
    "VENUE_DIRS", "VENUES", "LAYERS",
]

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

# Venue slug -> source directory. The slug is the only legal venue identifier in
# code; see quant-code-standards section 1.3.
VENUE_DIRS: dict[str, Path] = {
    "okx": ROOT / "crypto_trading",
    "t212": ROOT / "trading212",
}
VENUES = tuple(VENUE_DIRS)

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


def data_dir(venue: str, layer: str) -> Path:
    """Return the directory for one data layer of one venue.

    Args:
        venue: Venue slug, see VENUES.
        layer: Either "raw" or "curated".
    """
    _check_venue(venue)
    if layer not in LAYERS:
        raise ValueError(f"unknown data layer {layer!r}, expected one of {LAYERS}")
    return DIR_DATA / venue / layer


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


def manifest_path(venue: str) -> Path:
    """Return the raw-layer retrieval manifest (JSONL: URL, params, fetch time, rows)."""
    return data_dir(venue, "raw") / "_manifest.jsonl"


def gaps_path(venue: str) -> Path:
    """Return the curated-layer gap register (CSV: instrument, period, from, to, cause, state)."""
    return data_dir(venue, "curated") / "_gaps.csv"


def _check_venue(venue: str) -> None:
    if venue not in VENUE_DIRS:
        raise ValueError(f"unknown venue {venue!r}, expected one of {VENUES}")
