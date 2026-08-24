#!/usr/bin/env python3
"""Build the committed rebuild manifest for every stored data source.

Responsibility: walk each data source's curated tree, emit one JSONL record per
stored partition holding its coordinates, the upstream URL that produced it, its
local size and its row count, and write the result to the versioned manifest.
The data tree is excluded from version control in full and is expected to move
to an external disk; the manifest is what keeps it accountable. With it, 64 GiB
of parquet is a cache. Without it, the same bytes are an undocumented blob that
nobody can verify or reconstruct.

The manifest deliberately carries no generation timestamp. A run that finds
nothing changed must produce a byte-identical file, otherwise every daily sync
would commit a diff that says nothing.

Cost is controlled in three ways. Row counts come from the parquet footer and
never from reading the data. A record whose file size is unchanged reuses the
previous row count, so a second run over an unchanged tree does no per-file work
beyond a stat call; size is treated as the change signal because these loaders
write partitions whole from immutable upstream files, so a rewrite landing on an
identical byte count with different contents is not a failure mode they have.
Upstream checksums cost one request each and the archive is immutable, so they
are fetched only on request, only for records that lack one, and never twice.

A source with no dedicated scanner falls back to scan_generic(), which emits
minimal records rather than skipping the tree silently. A partition whose footer
cannot be read has its row count recorded as null and raises the process exit
code, so damage is reported rather than recorded as an empty file.

Out of scope: downloading or writing any data, which belongs to the ingest
scripts and to crypto_trading/ingest/ and trading212/ingest/; path construction,
which belongs to common/paths.py; describing fields, units and time zones, which
belongs to docs/data/<source>/DATA_SPEC.md; the on-disk inventory read straight
from the parquet statistics, which belongs to scripts/data_inventory.py and is
the independent cross-check against this manifest.

Public functions:
    main(argv=None)                   Command-line entry point; returns the exit
                                      code, 1 when any partition was unreadable.
    build_manifest(source, fetch_checksums=0, with_local_hash=False)
                                      Scan one source, write its manifest, and
                                      return the summary counts.
    scan_binance()                    Yield one record per stored Binance
                                      partition, reconstructing the upstream URL
                                      from the stored path so it cannot drift.
    scan_t212()                       Yield one record per stored equity
                                      partition. No upstream URL is recorded:
                                      the equity source is a query API whose
                                      adjusted prices are retroactive, so there
                                      is no immutable object to point at and the
                                      row count is the only integrity signal
                                      available.
    load_existing(path)               Return the previous manifest's records
                                      indexed by relative path.

Constants:
    HASH_BLOCK_BYTES         int  Bytes per read when hashing a local file,
                                  1 MiB. Files are read in blocks because the
                                  largest partition is about 300 MB and this
                                  host has 8 GB of memory shared with the ingest
                                  workers.
    CHECKSUM_BATCH_DEFAULT   int  Upstream sidecars fetched when
                                  --fetch-checksums is given without a number,
                                  200. The ceiling exists so an accidental full
                                  run cannot issue thirteen thousand requests in
                                  a burst against a free public service.
    TIMEOUT_CHECKSUM_SEC     int  Timeout for one sidecar request, 30 seconds.
    USER_AGENT               str  User-Agent sent with sidecar requests.
    SCANNERS                 dict Data-source slug mapped to its scanner;
                                  sources absent here fall back to
                                  scan_generic().

Inputs:
    data/<source>/curated/**/*.parquet   Footers only, plus a stat call per file.
    docs/data/<source>/MANIFEST.jsonl    The previous manifest, for reuse.
    <upstream>.CHECKSUM                  Only when --fetch-checksums is given.
                                         The sidecar holds the digest of the
                                         upstream zip, not of the parquet
                                         derived from it, so it proves the
                                         source is unchanged rather than that
                                         the local copy is intact; absence is
                                         normal for older objects.
Outputs:
    docs/data/<source>/MANIFEST.jsonl    One _meta line followed by one record
                                         per partition, sorted by relative path.
    stdout carries one summary line per source. Exit code 1 when any partition
        was unreadable, 0 otherwise.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main", "build_manifest", "scan_binance", "scan_t212", "load_existing"]

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq

from common.paths import (DATA_SOURCES, DIR_DATA, ROOT, data_dir, manifest_path,
                          stamp_freq)
from crypto_trading.ingest import binance_archive as archive

# ============================================================================
# [1] Constants
# ============================================================================

# Read in blocks rather than whole files: the largest partition is about 300 MB
# and this host has 8 GB of memory shared with the ingest workers.
HASH_BLOCK_BYTES = 1024 * 1024

# One request per sidecar against a free public service. The ceiling exists so
# an accidental full run cannot issue thirteen thousand requests in a burst.
CHECKSUM_BATCH_DEFAULT = 200
TIMEOUT_CHECKSUM_SEC = 30

USER_AGENT = "quant-research/1.0"

# The one field a compacted record always keeps; see _compact().
ROWS_KEY = "rows"

# scripts/sync_to_git.py refuses to stage any file above this, so a manifest
# that reaches it stops the daily sync outright. Warn well before that, because
# the equity manifest grew twelvefold in four days as intraday intervals landed
# and the ceiling is a real horizon rather than a theoretical one.
SYNC_BLOB_CEILING_BYTES = 10 * 1024 * 1024
SIZE_WARN_FRACTION = 0.7


# ============================================================================
# [2] Scanners, one per source layout
# ============================================================================

def scan_binance() -> Iterator[dict]:
    """Yield one record per stored Binance partition.

    Layout: binance/curated/<market>/<dataset>/<symbol>/<leaf>/year=YYYY/<stamp>.parquet
    The leaf is the bar interval for the kline family and the dataset name
    otherwise, which is exactly the distinction build_url() needs, so the
    upstream URL is reconstructed from the stored path rather than recorded
    separately and risking drift.
    """
    root = data_dir("binance", "curated")
    for path in sorted(root.rglob("*.parquet")):
        parts = path.relative_to(root).parts
        if len(parts) != 6:
            yield _unstructured(path, note="unexpected depth under binance/curated")
            continue
        market, dataset, symbol, leaf, _year, filename = parts
        stem = Path(filename).stem
        period = leaf if leaf != dataset else None
        try:
            freq = stamp_freq(stem)
        except ValueError:
            yield _unstructured(path, note=f"unrecognised stamp {stem!r}")
            continue
        stamp = _archive_stamp(stem)
        yield {
            "market": market,
            "dataset": dataset,
            "symbol": symbol,
            "period": period,
            "freq": freq,
            "stamp": stamp,
            "rel": _rel(path),
            "bytes": path.stat().st_size,
            "rows": None,
            "url": archive.build_url(market, freq, dataset, symbol, stamp, period),
            "sha256_upstream": None,
        }


def scan_t212() -> Iterator[dict]:
    """Yield one record per stored equity partition.

    Layout: t212/curated/<group>/<symbol>/<interval>/<file>.parquet

    No upstream URL is recorded. The equity source is a query API whose adjusted
    prices are retroactive, so there is no immutable object to point at: a split
    rewrites the whole history and the same request returns different numbers
    afterwards. Rebuilding means re-running the loader, not refetching an
    address, and the row count is therefore the only integrity signal available.
    """
    root = data_dir("t212", "curated")
    for path in sorted(root.rglob("*.parquet")):
        parts = path.relative_to(root).parts
        if len(parts) != 4:
            yield _unstructured(path, note="unexpected depth under t212/curated")
            continue
        group, symbol, interval, _filename = parts
        yield {
            "group": group,
            "symbol": symbol,
            "interval": interval,
            "rel": _rel(path),
            "bytes": path.stat().st_size,
            "rows": None,
            "url": None,
            "sha256_upstream": None,
        }


def scan_generic(source: str) -> Iterator[dict]:
    """Yield minimal records for a source with no dedicated scanner yet."""
    for layer in ("raw", "curated"):
        root = data_dir(source, layer)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.parquet")):
            yield _unstructured(path, note=f"no scanner for source {source!r}")


SCANNERS: dict[str, Callable[[], Iterator[dict]]] = {
    "binance": scan_binance,
    "t212": scan_t212,
}


# ============================================================================
# [3] Manifest assembly
# ============================================================================

def load_existing(path: Path) -> dict[str, dict]:
    """Return the previous manifest's records indexed by relative path."""
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "_meta" in record:
            continue
        out[record["rel"]] = record
    return out


def build_manifest(source: str, fetch_checksums: int = 0,
                   with_local_hash: bool = False) -> dict:
    """Scan one source, write its manifest, and return a summary.

    Args:
        source: Data-source slug, see DATA_SOURCES.
        fetch_checksums: Upper bound on upstream .CHECKSUM sidecars to fetch for
            records that lack one. Zero disables fetching entirely.
        with_local_hash: Also record a SHA-256 of each local file. Reads every
            byte, so a full pass over the Binance tree moves 64 GiB.

    Returns:
        Summary counts: files, bytes, rows, reused, hashed, checksums.
    """
    out_path = manifest_path(source)
    previous = load_existing(out_path)
    scanner = SCANNERS.get(source, lambda: scan_generic(source))

    records: list[dict] = []
    summary = {"files": 0, "bytes": 0, "rows": 0, "reused": 0,
               "hashed": 0, "checksums": 0, "unreadable": 0}

    for record in scanner():
        old = previous.get(record["rel"])
        unchanged = old is not None and old.get("bytes") == record["bytes"]

        # Size is the change signal. A rewrite that lands on the identical byte
        # count with different contents is not a failure mode these loaders
        # have: partitions are written whole, from immutable upstream files.
        if unchanged and old.get("rows") is not None:
            record["rows"] = old["rows"]
            summary["reused"] += 1
        else:
            record["rows"] = _row_count(Path(DIR_DATA / record["rel"]))
            if record["rows"] is None:
                summary["unreadable"] += 1

        if unchanged and old.get("sha256_upstream"):
            record["sha256_upstream"] = old["sha256_upstream"]
        if unchanged and old.get("sha256_local"):
            record["sha256_local"] = old["sha256_local"]

        if with_local_hash and not record.get("sha256_local"):
            record["sha256_local"] = _file_sha256(Path(DIR_DATA / record["rel"]))
            summary["hashed"] += 1

        records.append(record)
        summary["files"] += 1
        summary["bytes"] += record["bytes"]
        summary["rows"] += record["rows"] or 0

    if fetch_checksums:
        summary["checksums"] = _fill_upstream_checksums(records, fetch_checksums)

    _write_manifest(out_path, source, records, summary)
    return summary


def _write_manifest(path: Path, source: str, records: list[dict],
                    summary: dict) -> None:
    """Write the manifest deterministically: sorted records, no timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"_meta": {"source": source, "files": summary["files"],
                      "bytes": summary["bytes"], "rows": summary["rows"]}}
    lines = [json.dumps(meta, sort_keys=True, ensure_ascii=False)]
    for record in sorted(records, key=lambda r: r["rel"]):
        lines.append(json.dumps(_compact(record), sort_keys=True,
                                ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _compact(record: dict) -> dict:
    """Drop keys that carry no information, so the file stays small.

    The equity source has no immutable upstream object, so every one of its
    records would otherwise spell out `"url": null, "sha256_upstream": null`:
    thirty-nine bytes of nothing on each of tens of thousands of lines. A
    missing key and an explicit null are read identically here, because every
    consumer reaches for these through dict.get().

    ROWS_KEY is never dropped. A null row count is the signal that a partition
    could not be read, and silence would turn a damaged file into an absent
    field.
    """
    return {k: v for k, v in record.items()
            if v is not None or k == ROWS_KEY}


def _fill_upstream_checksums(records: list[dict], limit: int) -> int:
    """Fetch missing .CHECKSUM sidecars, up to limit, and record the digest."""
    filled = 0
    for record in records:
        if filled >= limit:
            break
        if record.get("sha256_upstream") or not record.get("url"):
            continue
        digest = _fetch_checksum(record["url"])
        if digest:
            record["sha256_upstream"] = digest
            filled += 1
    return filled


# ============================================================================
# [4] Internals
# ============================================================================

def _rel(path: Path) -> str:
    """Path relative to the data root, as a POSIX string."""
    return path.relative_to(DIR_DATA).as_posix()


def _unstructured(path: Path, note: str) -> dict:
    """Record for a file whose path does not match the expected layout."""
    return {"rel": _rel(path), "bytes": path.stat().st_size, "rows": None,
            "url": None, "sha256_upstream": None, "note": note}


def _archive_stamp(stem: str) -> str:
    """Normalise a partition stem to the label the archive uses in file names.

    The bookTicker partitions written on 2026-08-19 use a dash-free YYYYMMDD
    stem while every later loader stores the archive's own YYYY-MM-DD. Both
    describe the same upstream object, so the dash-free form is expanded rather
    than treated as a separate dataset.
    """
    if len(stem) == 8 and stem.isdigit():
        return f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
    return stem


def _row_count(path: Path) -> int | None:
    """Return a parquet file's row count, read from its footer.

    Returns None when the footer cannot be read, which marks the partition as
    damaged rather than silently recording it as empty.
    """
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None


def _file_sha256(path: Path) -> str | None:
    """Return a file's SHA-256, read in blocks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _fetch_checksum(url: str) -> str | None:
    """Return the SHA-256 an archive object's .CHECKSUM sidecar declares.

    The sidecar holds the digest of the upstream zip, not of the parquet derived
    from it, so it proves the source is unchanged rather than that the local
    copy is intact. Absence is normal for older objects and is not an error.
    """
    request = urllib.request.Request(url + ".CHECKSUM",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_CHECKSUM_SEC) as response:
            return response.read().decode().split()[0].strip()
    except (urllib.error.URLError, OSError, IndexError, UnicodeDecodeError):
        return None


# ============================================================================
# [5] Command-line entry point
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the committed rebuild manifest for the excluded data tree.")
    parser.add_argument("--source", action="append", choices=DATA_SOURCES,
                        help="Limit to one source; repeatable. Default: all present.")
    parser.add_argument("--fetch-checksums", nargs="?", type=int,
                        const=CHECKSUM_BATCH_DEFAULT, default=0,
                        metavar="N",
                        help=f"Fetch up to N missing upstream .CHECKSUM sidecars "
                             f"(default {CHECKSUM_BATCH_DEFAULT} when given without a "
                             f"number). Already recorded digests are never refetched.")
    parser.add_argument("--hash-local", action="store_true",
                        help="Also record a SHA-256 of every local file. Reads the "
                             "whole tree; expect a long run on the Binance data.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    sources = args.source or [s for s in DATA_SOURCES
                              if data_dir(s, "curated").is_dir()]
    if not sources:
        print("No data source directories present; nothing to do.")
        return 0

    exit_code = 0
    for source in sources:
        summary = build_manifest(source, args.fetch_checksums, args.hash_local)
        notes = [f"reused {summary['reused']:,}"]
        if args.hash_local:
            notes.append(f"hashed {summary['hashed']:,}")
        if args.fetch_checksums:
            notes.append(f"checksums +{summary['checksums']:,}")
        if summary["unreadable"]:
            notes.append(f"UNREADABLE {summary['unreadable']:,}")
            exit_code = 1
        print(f"{source:<10} {summary['files']:>7,} files  "
              f"{summary['bytes'] / 1024 ** 3:>7.1f} GiB  "
              f"{summary['rows']:>15,} rows  ({', '.join(notes)})")

        written = manifest_path(source)
        size = written.stat().st_size
        print(f"{'':<10} -> {written.relative_to(ROOT)}  "
              f"({size / 1024 ** 2:.2f} MiB)")
        if size > SYNC_BLOB_CEILING_BYTES * SIZE_WARN_FRACTION:
            print(f"{'':<10}    WARNING: past {SIZE_WARN_FRACTION:.0%} of the "
                  f"{SYNC_BLOB_CEILING_BYTES // 1024 ** 2} MiB ceiling that "
                  f"sync_to_git.py enforces.")
            print(f"{'':<10}    At the ceiling the daily sync stops. Shard this "
                  f"manifest before then.")

    if exit_code:
        print("\nSome partitions could not be read. Their manifest records carry "
              "a null row count; treat them as damaged until re-fetched.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
