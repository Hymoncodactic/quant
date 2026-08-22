"""Report what the data lake actually contains: instrument, frequency, span, location.

Responsibility: walk every parquet file under the data root, aggregate file
count, row count, byte size and time span per (data source, group, dataset,
symbol, frequency), and print one row per group followed by a total. The report
reads the partitions themselves rather than any manifest, so it reflects what is
on disk rather than what an ingest run believed it wrote, which makes it the
independent cross-check on docs/data/<source>/MANIFEST.jsonl.

Two choices keep the scan cheap and the numbers honest. Row counts and column
statistics come from the parquet metadata, which is written per row group, so no
data is scanned. The time span is read from the timestamp column's own
statistics rather than from the file name, because equity partitions are named
by symbol while crypto partitions are named by date, and only the data itself is
authoritative for either.

Out of scope: downloading or writing any data, which belongs to the ingest
scripts and to scripts/update_data.py; the committed rebuild manifest, which
belongs to scripts/build_data_manifest.py; field, unit and time-zone
definitions, which belong to docs/data/<source>/DATA_SPEC.md.

Public functions:
    main()   Walk the lake and print one row per (source, group, dataset,
             symbol, frequency), then the total.

Constants:
    None. The data root is DIR_DATA, imported from common/paths.py.

Inputs:
    data/**/*.parquet   Metadata only: row counts, column statistics and a stat
                        call per file.
Outputs:
    stdout only. No file is written.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main"]

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq

from common.paths import DIR_DATA


def _ts_range(meta) -> tuple[str | None, str | None]:
    """Return the min and max of the timestamp column from Parquet statistics.

    Statistics are written per row group, so this costs no data scan.
    """
    names = meta.schema.names
    col = next((c for c in ("ts", "calc_time", "open_time") if c in names), None)
    if col is None:
        return None, None
    idx = names.index(col)
    lo = hi = None
    for g in range(meta.num_row_groups):
        st = meta.row_group(g).column(idx).statistics
        if st is None or st.min is None:
            continue
        lo = st.min if lo is None else min(lo, st.min)
        hi = st.max if hi is None else max(hi, st.max)
    fmt = lambda v: str(v)[:10] if v is not None else None
    return fmt(lo), fmt(hi)


def _describe(path: Path) -> tuple[str, str, str, str, str]:
    """Derive (venue, group, symbol, frequency, dataset) from a partition path."""
    rel = path.relative_to(DIR_DATA).parts
    venue = rel[0]
    parts = [p for p in rel[1:] if not p.startswith("year=") and not p.endswith(".parquet")]
    parts = [p for p in parts if p != "curated" and p != "raw"]
    if venue == "binance":
        market, dataset, symbol, leaf = (parts + ["", "", "", ""])[:4]
        return venue, market, symbol, leaf, dataset
    group, symbol, leaf = (parts + ["", "", ""])[:3]
    return venue, group, symbol, leaf, "ohlcv"


def main() -> None:
    """Walk the lake and print one row per (venue, dataset, symbol, frequency)."""
    files = sorted(DIR_DATA.rglob("*.parquet"))
    if not files:
        print("Data lake is empty.")
        return

    agg: dict[tuple, dict] = defaultdict(
        lambda: {"files": 0, "rows": 0, "bytes": 0, "first": None, "last": None})

    for path in files:
        venue, group, symbol, freq, dataset = _describe(path)
        key = (venue, group, dataset, symbol, freq)
        meta = pq.read_metadata(path)
        entry = agg[key]
        entry["files"] += 1
        entry["rows"] += meta.num_rows
        entry["bytes"] += path.stat().st_size
        entry["dir"] = str(path.parent.parent if path.parent.name.startswith("year=")
                           else path.parent)
        # Read the span from the timestamp column's own statistics rather than the
        # file name: equity partitions are named by symbol, crypto by date, and only
        # the data itself is authoritative for either.
        lo, hi = _ts_range(meta)
        if lo is not None:
            entry["first"] = lo if entry["first"] is None else min(entry["first"], lo)
            entry["last"] = hi if entry["last"] is None else max(entry["last"], hi)

    header = (f"{'venue':<9}{'group':<13}{'dataset':<13}{'symbol':<11}{'freq':<7}"
              f"{'files':>7}{'rows':>14}{'size':>11}   span")
    print("=" * len(header))
    print(header)
    print("=" * len(header))
    tot_rows = tot_bytes = tot_files = 0
    for key in sorted(agg):
        venue, group, dataset, symbol, freq = key
        e = agg[key]
        size = (f"{e['bytes']/1e6:,.1f} MB" if e["bytes"] >= 1e6
                else f"{e['bytes']/1e3:,.0f} KB")
        print(f"{venue:<9}{group:<13}{dataset:<13}{symbol:<11}{freq:<7}"
              f"{e['files']:>7,}{e['rows']:>14,}{size:>11}   {e['first']} .. {e['last']}")
        tot_rows += e["rows"]; tot_bytes += e["bytes"]; tot_files += e["files"]

    print("=" * len(header))
    print(f"TOTAL  {tot_files:,} files   {tot_rows:,} rows   {tot_bytes/1e9:.3f} GB")
    print(f"Root: {DIR_DATA}")


if __name__ == "__main__":
    main()
