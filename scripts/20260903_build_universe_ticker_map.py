"""Build the verified symbol -> Trading 212 ticker map for the B0 universe.

Responsibility: turn the frozen 1,500-name candidate pool into a mapping the
execution layer can order against, and say plainly which names it could not
decide. A1 admits a candidate only when it has a verified ticker
(fixplans/t212/b0/00_coordination.md decision A5), so an undecided name simply
never enters the book; that is why "leave it null" is a safe answer here and
"guess a ticker" is not. Several US symbols have same-named foreign listings,
and routing an order to one of those is not a recoverable mistake.

Matching, in this fixed order:

    1. shortName. The venue's shortName is the instrument's CURRENT market
       symbol, and it is the only field that tracks a rename. When a dual
       listing puts the same shortName on a US line and a USD-quoted London
       line, the single "_US_EQ" among them is taken.
    2. Ticker prefix, "<SYMBOL>_US_EQ", and only when no other pool member
       already owns that instrument by shortName.

Both require type == STOCK and currencyCode == USD.

Why shortName comes first, and why getting this backwards is not a cosmetic
error. Trading 212 keeps the ORIGINAL ticker id after a company renames, and
gives the plain symbol id to whoever holds that symbol now. Measured against
data/reference/t212_instruments_20260821.json on 2026-09-03:

    CNX_US_EQ  shortName CNR   Core Natural Resources
    CNX1_US_EQ shortName CNX   CNX Resources
    CR_US_EQ   shortName CXT   Crane NXT
    CR1_US_EQ  shortName CR    Crane Company
    RBC_US_EQ  shortName RRX   Regal Rexnord
    UA_US_EQ   shortName UAA   Under Armour Class A
    GEN_US_EQ  shortName GENNQ Genesis Healthcare

A prefix-first rule mapped pool CNX to CNX_US_EQ -- Core Natural Resources,
a different company -- and the same rule pointed pool CNR at the same
instrument, so one venue instrument was claimed by two pool symbols while one
of them was plain wrong. Five symbols were affected (CNX, CR, GEN, RBC, UA);
all five were eligible and ranked in the 2026-08-31 table, so a later rotation
would have bought the wrong company with real money. shortName-first resolves
every one of them correctly.

Three guards on top, each refusing rather than guessing:

    - A prefix match whose instrument's shortName is a DIFFERENT pool symbol is
      rejected: that is the rename signature above.
    - A symbol matching more than one instrument is left undecided. The whole
      point of the table is that identity was PROVEN, and two candidates mean
      it was not.
    - A venue ticker claimed by two pool symbols is dropped from BOTH.

A1 admits a candidate only when it has a verified ticker (decision A5), so an
undecided name simply never enters the book; that is why "leave it null" is a
safe answer here and "guess a ticker" is not.

A0's eighteen names are written from
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
    2026-09-03  Rule order inverted to shortName-first, plus the rename and
                double-claim guards, after review found five pool symbols
                mapped to another company's instrument and four venue tickers
                claimed twice.
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

    pool_symbols = {_disk_symbol(str(m["ticker"])) for m in members}
    pool_symbols |= {str(m["ticker"]) for m in members}

    out: dict[str, dict] = {}
    for member in members:
        pool_ticker = str(member["ticker"])
        symbol = _disk_symbol(pool_ticker)
        entry = {"ticker": None, "isin": None, "workingScheduleId": None,
                 "matched_by": None, "pool_ticker": pool_ticker,
                 "candidates": [], "rejected": None}
        for rule, index, key in (("short_name", by_short, symbol),
                                 ("short_name", by_short, pool_ticker),
                                 ("prefix", by_ticker, f"{symbol}_US_EQ"),
                                 ("prefix", by_ticker, f"{pool_ticker}_US_EQ")):
            found = index.get(key) or []
            if len(found) > 1:
                # A dual listing: the same shortName on the US line and on a
                # USD-quoted London line (DARl_EQ, FIVEl_EQ). Exactly one
                # "_US_EQ" among them IS the US listing, which is the one the
                # strategy prices from Yahoo and means to trade. More than one
                # is a genuine ambiguity and stays undecided.
                us_lines = [m for m in found
                            if str(m.get("ticker", "")).endswith("_US_EQ")]
                if len(us_lines) == 1:
                    found = us_lines
                else:
                    entry["candidates"] = sorted(str(m.get("ticker"))
                                                 for m in found)
                    break
            if len(found) != 1:
                continue
            meta = found[0]
            short = str(meta.get("shortName"))
            if rule == "prefix" and short != symbol and short in pool_symbols:
                # The rename signature: this legacy ticker id now belongs to
                # another pool member, which owns it by shortName.
                entry["rejected"] = (f"{meta.get('ticker')} is {short} "
                                     f"({meta.get('name')}), a different pool "
                                     f"member")
                break
            entry.update({"ticker": meta.get("ticker"),
                          "isin": meta.get("isin"),
                          "workingScheduleId": meta.get("workingScheduleId"),
                          "matched_by": rule, "venue_short_name": short,
                          "venue_name": meta.get("name")})
            break
        out[symbol] = entry

    # No venue instrument may be claimed by two pool symbols. One of the two
    # is necessarily wrong and nothing here can say which.
    claims: dict[str, list[str]] = defaultdict(list)
    for symbol, entry in out.items():
        if entry["ticker"]:
            claims[entry["ticker"]].append(symbol)
    for ticker, owners in claims.items():
        if len(owners) > 1:
            for symbol in owners:
                out[symbol].update({"ticker": None, "matched_by": None,
                                    "rejected": f"{ticker} is also claimed by "
                                                f"{sorted(set(owners) - {symbol})}"})

    for symbol, ticker in A0_ORDER_TICKERS.items():
        meta = (by_ticker.get(ticker) or [{}])[0]
        out[symbol] = {"ticker": ticker, "isin": meta.get("isin"),
                       "workingScheduleId": meta.get("workingScheduleId"),
                       "matched_by": "a0_verified", "pool_ticker": symbol,
                       "candidates": [], "rejected": None,
                       "venue_short_name": meta.get("shortName"),
                       "venue_name": meta.get("name")}
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
    rejected = {s: e["rejected"] for s, e in table.items()
                if not e["ticker"] and e.get("rejected")}
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
         "rejected": rejected,
         "map": table}, indent=1), encoding="utf-8")

    print(f"candidates      {len(table)}")
    for rule, count in sorted(by_rule.items()):
        print(f"  matched by {rule:<12} {count}")
    print(f"ambiguous       {len(ambiguous)}  {sorted(ambiguous)[:10]}")
    print(f"unmatched       {len(unmatched)}  {unmatched[:10]}")
    print(f"rejected        {len(rejected)}")
    for symbol, why in sorted(rejected.items())[:12]:
        print(f"    {symbol}: {why}")
    print(f"written         {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
