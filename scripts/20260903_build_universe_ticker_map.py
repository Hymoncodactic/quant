"""Build the verified symbol -> Trading 212 ticker map for the B0 universe.

Responsibility: turn the frozen 1,500-name candidate pool into a mapping the
execution layer can order against, and say plainly which names it could not
decide. A1 admits a candidate only when it has a verified ticker
(fixplans/t212/b0/00_coordination.md decision A5), so an undecided name simply
never enters the book; that is why "leave it null" is a safe answer here and
"guess a ticker" is not. Several US symbols have same-named foreign listings,
and routing an order to one of those is not a recoverable mistake.

Matching, in this fixed order:

    1. Prefix. The venue's ticker for a US equity is "<SYMBOL>_US_EQ", so the
       candidate's own symbol is tried directly.
    2. shortName. Some listings carry a venue ticker that does not embed the
       symbol; the venue's own shortName field is then the tie.

Both require type == STOCK and currencyCode == USD. A symbol matching more
than one instrument is left undecided rather than resolved by any rule: the
whole point of the table is that identity was PROVEN, and two candidates mean
it was not. A0's eighteen names are written from
trading212/execution/instruments.py A0_ORDER_TICKERS, which were verified by
hand and include META trading as FB_US_EQ, a mapping neither rule finds.

Out of scope: reading the map at decision time, which is
instruments.ticker_map_for (seam S5); admission, which is
trading212/strategy/a1_v0_0_1.py; refreshing the instrument metadata, which is
trading212/client.py.

Public functions:
    build_map(members, instruments_payload)   symbol -> entry or None.
    main()                                    Write the dated map file.

Constants:
    OUT_STEM  str  "t212_universe_ticker_map", the name
                   instruments.TICKER_MAP_GLOB looks for.

Inputs:
    data/reference/b0_universe_1500_20260823.json
    data/reference/t212_instruments_20260821.json  (or the live metadata
        endpoint with --live, which is a read-only call)
Outputs:
    data/reference/t212_universe_ticker_map_<YYYYMMDD>.json

Change log:
    2026-09-03  Created for fixplans/t212/b0/03_data_pipeline.md section 6.
"""

from __future__ import annotations

__all__ = ["build_map", "main"]

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.paths import DIR_REFERENCE                             # noqa: E402
from trading212.execution.instruments import A0_ORDER_TICKERS      # noqa: E402

OUT_STEM = "t212_universe_ticker_map"
UNIVERSE_JSON = DIR_REFERENCE / "b0_universe_1500_20260823.json"
INSTRUMENTS_JSON = DIR_REFERENCE / "t212_instruments_20260821.json"


def _disk_symbol(pool_ticker: str) -> str:
    """The spelling the price lake uses: a share class writes a hyphen."""
    return pool_ticker.replace(".", "-")


def build_map(members: list[dict], instruments: list[dict]) -> dict[str, dict]:
    """symbol -> {ticker, isin, workingScheduleId, matched_by} or nulls.

    Every candidate appears in the result, decided or not, so the file is a
    complete record of what was considered rather than only of what worked.
    """
    usable = [i for i in instruments
              if i.get("type") == "STOCK" and i.get("currencyCode") == "USD"]
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    by_short: dict[str, list[dict]] = defaultdict(list)
    for item in usable:
        by_ticker[str(item.get("ticker"))].append(item)
        by_short[str(item.get("shortName"))].append(item)

    out: dict[str, dict] = {}
    for member in members:
        pool_ticker = str(member["ticker"])
        symbol = _disk_symbol(pool_ticker)
        entry = {"ticker": None, "isin": None, "workingScheduleId": None,
                 "matched_by": None, "pool_ticker": pool_ticker,
                 "candidates": []}
        for rule, index, key in (("prefix", by_ticker, f"{symbol}_US_EQ"),
                                 ("prefix", by_ticker, f"{pool_ticker}_US_EQ"),
                                 ("short_name", by_short, symbol),
                                 ("short_name", by_short, pool_ticker)):
            found = index.get(key) or []
            if len(found) == 1:
                meta = found[0]
                entry.update({"ticker": meta.get("ticker"),
                              "isin": meta.get("isin"),
                              "workingScheduleId": meta.get(
                                  "workingScheduleId"),
                              "matched_by": rule})
                break
            if len(found) > 1:
                entry["candidates"] = sorted(str(m.get("ticker"))
                                             for m in found)
                break
        out[symbol] = entry

    for symbol, ticker in A0_ORDER_TICKERS.items():
        meta = (by_ticker.get(ticker) or [{}])[0]
        out[symbol] = {"ticker": ticker, "isin": meta.get("isin"),
                       "workingScheduleId": meta.get("workingScheduleId"),
                       "matched_by": "a0_verified", "pool_ticker": symbol,
                       "candidates": []}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="fetch instrument metadata from the venue "
                             "(read-only) instead of the stored snapshot")
    parser.add_argument("--env", default="paper")
    args = parser.parse_args()

    members = json.loads(UNIVERSE_JSON.read_text())["members"]
    if args.live:
        from common.config import load_config
        from trading212.client import T212Client
        cfg = load_config("t212", args.env)
        instruments = T212Client(args.env, cfg=cfg).instruments()
    else:
        instruments = json.loads(INSTRUMENTS_JSON.read_text())

    table = build_map(members, instruments)
    matched = {s: e for s, e in table.items() if e["ticker"]}
    ambiguous = {s: e["candidates"] for s, e in table.items()
                 if not e["ticker"] and e["candidates"]}
    unmatched = sorted(s for s, e in table.items()
                       if not e["ticker"] and not e["candidates"])
    by_rule: dict[str, int] = defaultdict(int)
    for entry in matched.values():
        by_rule[entry["matched_by"]] += 1

    stamp = date.today().strftime("%Y%m%d")
    out_path = DIR_REFERENCE / f"{OUT_STEM}_{stamp}.json"
    out_path.write_text(json.dumps(
        {"built_utc": stamp, "source": "live" if args.live
         else INSTRUMENTS_JSON.name, "universe": UNIVERSE_JSON.name,
         "counts": {"total": len(table), "matched": len(matched),
                    "ambiguous": len(ambiguous), "unmatched": len(unmatched),
                    "by_rule": dict(by_rule)},
         "ambiguous": ambiguous, "unmatched": unmatched,
         "map": table}, indent=1), encoding="utf-8")

    print(f"candidates      {len(table)}")
    for rule, count in sorted(by_rule.items()):
        print(f"  matched by {rule:<12} {count}")
    print(f"ambiguous       {len(ambiguous)}  {sorted(ambiguous)[:10]}")
    print(f"unmatched       {len(unmatched)}  {unmatched[:10]}")
    print(f"written         {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
