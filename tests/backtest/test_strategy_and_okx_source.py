"""Tests for the strategy plug-in loader and the crypto kline loader.

Responsibility: pin the two seams that keep the engine independent of both the
strategy code and the venue data layout. The loader seam covers the happy
path, the refusal to load a module whose declared identity disagrees with its
file name, a missing file, a module without the compute_targets entry point,
and the fact that default path resolution goes through common/paths rather
than a hand-built string. The data seam covers backtest/okx/data_source.py
reading a Binance-shaped lake: the loaded frame carries exactly the engine's
seven columns with the extra source columns dropped, the date window is
inclusive at both ends, a missing symbol raises, and the quote-currency
whitelist can be narrowed by the caller. One case is deliberately opposite to
the equity side: crypto daily bars stamp midnight UTC and must be keyed on the
UTC date, whereas equity daily bars are keyed on the exchange's local date, so
reusing the equity mapping here would shift every crypto day back by one.

Out of scope: the strategies themselves, whose behavior is tested next to
them, for example tests/backtest/test_a0_intraday.py; the download path that
fills the real lake, which belongs with the ingest code; equity daily
alignment, covered by tests/backtest/test_feed.py.

Public functions:
    test_load_strategy_happy_path(tmp_path)
        A well-formed strategy file loads and its entry point returns the
        expected targets.
    test_load_strategy_rejects_identity_mismatch(tmp_path)
        A file whose name claims a version the module does not declare is
        refused.
    test_load_strategy_missing_file(tmp_path)
        A missing strategy file raises FileNotFoundError.
    test_load_strategy_missing_entry_point(tmp_path)
        A module without compute_targets is refused.
    test_strategy_path_uses_venue_dir()
        Default resolution produces the versioned file name inside the venue's
        strategy directory.
    test_load_klines_schema_and_window(tmp_path)
        The loaded frame carries the engine's column list, the inclusive UTC
        date window, the quote currency, and passes the engine's quality gate.
    test_load_klines_missing_symbol(tmp_path)
        A symbol absent from the lake raises FileNotFoundError.
    test_crypto_daily_alignment_is_utc(tmp_path)
        Crypto daily bars key on the UTC date, one key per day and no shift.
    test_quote_ccy_whitelist_is_parameterizable(tmp_path)
        A caller narrowing the accepted currencies makes the gate refuse the
        crypto quote currency.

Public classes: None.

Constants:
    D
        Alias of decimal.Decimal, used so the loader's returned targets are
        compared against the same numeric type the production path uses.
    STRATEGY_SOURCE
        Source text of the fixture strategy written into tmp_path: it declares
        a name and a version and returns a constant one-share target for every
        symbol. It is a string rather than a file in the repository so that
        the loader is exercised on a file it has never seen, and so that no
        fixture strategy is left where a run could pick it up.

Inputs: None from the repository or from data/. Both the strategy file and the
parquet lake are written under pytest's tmp_path and read back from there; the
lake's directory layout comes from common.paths.binance_partition_dir with
data_root pointed at tmp_path, so the real lake is never touched.
Outputs: One strategy source file and one parquet partition per test that
needs them, under pytest's tmp_path and reclaimed by pytest; nothing lands in
the project directory.

Change log:
    2026-08-22  Header expanded to the six-section spec. The em dash in the
                previous first paragraph was the only non-ASCII character in
                tests/, and is gone with it (CLAUDE.md section 2.3).
"""

from __future__ import annotations

import textwrap
from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.feed import BarFeed, validate_frame
from backtest.engine.strategy_loader import load_strategy, strategy_path
from backtest.okx.data_source import load_klines
from common.paths import binance_partition_dir
from common.store import write_table

D = Decimal

STRATEGY_SOURCE = '''
"""Test fixture strategy: constant one-share target."""
from decimal import Decimal

STRATEGY_NAME = "fixture_hold"
STRATEGY_VERSION = "0.0.1"


def compute_targets(view, portfolio, params):
    return {s: Decimal("1") for s in view.symbols()}
'''


def _write_strategy(folder, name="fixture_hold", version="0.0.1",
                    source=STRATEGY_SOURCE):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}_v{version.replace('.', '_')}.py"
    path.write_text(textwrap.dedent(source))
    return path


# ---------------------------------------------------------------------------
# Loader contract: happy path, identity mismatch, missing pieces.
# ---------------------------------------------------------------------------

def test_load_strategy_happy_path(tmp_path):
    _write_strategy(tmp_path)
    fn = load_strategy("t212", "fixture_hold", "0.0.1", strategy_dir=tmp_path)
    class _View:
        def symbols(self):
            return ["AAPL"]
    assert fn(_View(), None, {}) == {"AAPL": D("1")}


def test_load_strategy_rejects_identity_mismatch(tmp_path):
    # File named 0.0.2 but module still declares 0.0.1: the file name lies.
    path = _write_strategy(tmp_path)
    path.rename(tmp_path / "fixture_hold_v0_0_2.py")
    with pytest.raises(ValueError, match="must agree"):
        load_strategy("t212", "fixture_hold", "0.0.2", strategy_dir=tmp_path)


def test_load_strategy_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_strategy("t212", "nope", "0.0.1", strategy_dir=tmp_path)


def test_load_strategy_missing_entry_point(tmp_path):
    source = ('STRATEGY_NAME = "fixture_hold"\n'
              'STRATEGY_VERSION = "0.0.1"\n')
    _write_strategy(tmp_path, source=source)
    with pytest.raises(ValueError, match="compute_targets"):
        load_strategy("t212", "fixture_hold", "0.0.1", strategy_dir=tmp_path)


def test_strategy_path_uses_venue_dir():
    # Default resolution goes through common/paths, not hand-built strings.
    path = strategy_path("t212", "ma_cross", "0.1.2")
    assert path.name == "ma_cross_v0_1_2.py"
    assert path.parent.name == "strategy"
    assert path.parent.parent.name == "trading212"


# ---------------------------------------------------------------------------
# Crypto kline loader: Binance layout in a tmp lake, engine-schema output.
# ---------------------------------------------------------------------------

def _fake_binance_lake(tmp_path) -> None:
    import pyarrow as pa
    days = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    frame = pd.DataFrame({
        "ts": days,
        "open": [100.0 + i for i in range(10)],
        "high": [101.0 + i for i in range(10)],
        "low": [99.0 + i for i in range(10)],
        "close": [100.5 + i for i in range(10)],
        "volume": [10.0] * 10,
        "quote_volume": [1000.0] * 10,     # extras must be dropped on load
        "count": [5] * 10,
        "taker_buy_volume": [4.0] * 10,
        "taker_buy_quote_volume": [400.0] * 10,
    })
    folder = binance_partition_dir("spot", "klines", "BTCUSDT", "1d",
                                   data_root=tmp_path)
    write_table(pa.Table.from_pandas(frame, preserve_index=False),
                folder / "year=2024" / "2024-01.parquet")


def test_load_klines_schema_and_window(tmp_path):
    _fake_binance_lake(tmp_path)
    frames = load_klines(["BTCUSDT"], "1d", "2024-01-03", "2024-01-05",
                         data_root=tmp_path)
    frame = frames["BTCUSDT"]
    assert list(frame.columns) == ["ts", "open", "high", "low", "close",
                                   "volume", "quote_ccy"]
    assert len(frame) == 3                       # inclusive UTC date window
    assert frame["ts"].iloc[0] == pd.Timestamp("2024-01-03", tz="UTC")
    assert (frame["quote_ccy"] == "USDT").all()
    validate_frame(frame, "BTCUSDT")             # engine gate accepts USDT


def test_load_klines_missing_symbol(tmp_path):
    _fake_binance_lake(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_klines(["ETHUSDT"], "1d", "2024-01-03", "2024-01-05",
                    data_root=tmp_path)


def test_crypto_daily_alignment_is_utc(tmp_path):
    # Crypto daily bars stamp 00:00 UTC; the feed must key them on the UTC
    # date, so the okx line passes tz "UTC" -- reusing the equity NY mapping
    # would shift every day to the previous date (00:00 UTC = 19:00/20:00 NY).
    _fake_binance_lake(tmp_path)
    frames = load_klines(["BTCUSDT"], "1d", "2024-01-03", "2024-01-04",
                         data_root=tmp_path)
    feed = BarFeed(frames, lambda s: "UTC", daily=True)
    keys = [key for _, key, _ in feed]
    assert keys == [pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-04")]


def test_quote_ccy_whitelist_is_parameterizable(tmp_path):
    _fake_binance_lake(tmp_path)
    frame = load_klines(["BTCUSDT"], "1d", "2024-01-03", "2024-01-05",
                        data_root=tmp_path)["BTCUSDT"]
    with pytest.raises(ValueError, match="quote_ccy"):
        validate_frame(frame, "BTCUSDT", valid_ccys=("USD", "GBP", "GBp"))