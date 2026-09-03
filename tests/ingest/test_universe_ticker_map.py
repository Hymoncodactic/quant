"""The venue ticker map's matching rule: shortName first, renames refused.

Trading 212 keeps the ORIGINAL ticker id after a company renames and gives the
plain symbol id to whoever holds that symbol now. A prefix-first rule therefore
maps a pool symbol onto a different company; measured 2026-09-03, five pool
symbols were affected and four venue tickers were claimed twice.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_map", ROOT / "scripts" / "20260903_build_universe_ticker_map.py")
build_map_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_map_module)


def _inst(ticker, short, name):
    return {"ticker": ticker, "shortName": short, "name": name,
            "type": "STOCK", "currencyCode": "USD", "isin": "X",
            "workingScheduleId": 71}


def _members(*symbols):
    return [{"ticker": s} for s in symbols]


def test_a_renamed_ticker_does_not_capture_the_symbol_it_used_to_be():
    """Catches: pool CNX mapped to Core Natural Resources, another company."""
    instruments = [_inst("CNX_US_EQ", "CNR", "Core Natural Resources"),
                   _inst("CNX1_US_EQ", "CNX", "CNX Resources")]
    table = build_map_module.build_map(_members("CNX", "CNR"), instruments)
    assert table["CNX"]["ticker"] == "CNX1_US_EQ"
    assert table["CNX"]["venue_short_name"] == "CNX"
    assert table["CNR"]["ticker"] == "CNX_US_EQ"
    assert table["CNR"]["venue_short_name"] == "CNR"


def test_a_legacy_prefix_owned_by_another_pool_member_is_refused():
    """The rename signature, with the current owner absent from the pool."""
    instruments = [_inst("RBC_US_EQ", "RRX", "Regal Rexnord")]
    table = build_map_module.build_map(_members("RBC", "RRX"), instruments)
    assert table["RBC"]["ticker"] is None
    assert "RRX" in table["RBC"]["rejected"]
    assert table["RRX"]["ticker"] == "RBC_US_EQ"


def test_no_venue_instrument_is_claimed_by_two_pool_symbols():
    """One of the two is necessarily wrong and nothing here can say which."""
    instruments = [_inst("AAA_US_EQ", "AAA", "Alpha")]
    table = build_map_module.build_map(_members("AAA", "AAA_ALT"), instruments)
    claimed = [s for s, e in table.items() if e["ticker"] == "AAA_US_EQ"]
    assert len(claimed) <= 1


def test_a_dual_listing_resolves_to_the_us_line():
    instruments = [_inst("DAR_US_EQ", "DAR", "Darling Ingredients"),
                   _inst("DARl_EQ", "DAR", "Darling Ingredients (London)")]
    table = build_map_module.build_map(_members("DAR"), instruments)
    assert table["DAR"]["ticker"] == "DAR_US_EQ"


def test_two_us_lines_with_one_short_name_stay_undecided():
    instruments = [_inst("X_US_EQ", "X", "One"), _inst("Y_US_EQ", "X", "Two")]
    table = build_map_module.build_map(_members("X"), instruments)
    assert table["X"]["ticker"] is None
    assert table["X"]["candidates"] == ["X_US_EQ", "Y_US_EQ"]


def test_the_a0_eighteen_always_win():
    """META trades as FB_US_EQ; no derivable rule finds that."""
    instruments = [_inst("META_US_EQ", "META", "Impostor"),
                   _inst("FB_US_EQ", "META", "Meta Platforms")]
    table = build_map_module.build_map(_members("META"), instruments)
    assert table["META"]["ticker"] == "FB_US_EQ"
    assert table["META"]["matched_by"] == "a0_verified"


def test_the_shipped_map_has_no_identity_disagreement_and_no_double_claim():
    """The real file, as the live decision will read it."""
    import json
    from collections import Counter

    from common.paths import DIR_REFERENCE
    files = sorted(Path(DIR_REFERENCE).glob("t212_universe_ticker_map_*.json"))
    if not files:
        pytest.skip("no universe ticker map built in this environment")
    payload = json.loads(files[-1].read_text())
    entries = {s: e for s, e in payload["map"].items() if e.get("ticker")}
    wrong = {s: e for s, e in entries.items()
             if e.get("matched_by") == "short_name"
             and e.get("venue_short_name") not in (s, e.get("pool_ticker"))}
    assert wrong == {}
    counts = Counter(e["ticker"] for e in entries.values())
    assert [t for t, n in counts.items() if n > 1] == []
