"""Parquet writing and reading for the market data lake.

The write configuration here is not a default; it is the outcome of benchmarks on
real BTCUSDT data, and each choice below cites the measurement that justifies it.
Changing any of them without re-measuring is how a lake silently doubles in size.

Public functions:
    write_table(table, path, sort_by=None)   Write one partition, atomically
    read_dataset(root, **filters)            Read a partitioned dataset via DuckDB
    parquet_stats(path)                      Row count, byte size, bytes per row
    is_readable_parquet(path)                Whether a file is a complete Parquet file
    clear_stale_temps(root)                  Remove temp files left by an interrupted run

Public constants:
    COMPRESSION, COMPRESSION_LEVEL, ROW_GROUP_SIZE
"""

from __future__ import annotations

__all__ = ["write_table", "read_dataset", "parquet_stats", "is_readable_parquet",
           "clear_stale_temps", "COMPRESSION", "COMPRESSION_LEVEL", "ROW_GROUP_SIZE",
           "TEMP_SUFFIX"]

import os
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

# zstd beats snappy on size and beats gzip on write speed by roughly 8x.
COMPRESSION = "zstd"

# pyarrow's zstd default is level 1, NOT level 3. Passing level=None and level=1
# produces byte-identical files; passing 3 explicitly measured 8% smaller on a day
# of BTCUSDT trades (7.15 MB -> 6.58 MB). Always state the level.
COMPRESSION_LEVEL = 3

# Size is flat above 500k rows per group, but selective-query latency degrades 16x
# from 100k to 3M because min/max statistics can no longer prune. DuckDB's own
# guidance is 100k-1M. 128k sits at the good end of both curves.
ROW_GROUP_SIZE = 131_072

# Partitions are written to this suffix and then renamed into place. A long ingest
# will be interrupted -- by a signal, a crash, or the machine losing power -- and
# a partition written directly to its final path leaves a truncated file that
# still satisfies an existence check, so a resumed run skips it and the corruption
# survives silently. Renaming makes the file either absent or complete.
TEMP_SUFFIX = ".writing"


def write_table(table: pa.Table, path: Path | str,
                sort_by: str | Iterable[str] | None = "ts") -> Path:
    """Write one Arrow table to a single Parquet partition.

    Sorting first is not cosmetic. Delta, run-length and dictionary encodings only
    compress adjacent similar values, and sorted data yields tight per-row-group
    statistics. Writing the same rows shuffled measured 5.2-5.5x larger.

    The write is atomic: data goes to a temporary sibling and is then renamed onto
    the target. os.replace is an atomic rename within one filesystem, so a reader
    or a resumed run never observes a half-written partition.

    Args:
        table: The data. Column order is preserved.
        path: Destination file. Parent directories are created.
        sort_by: Column or columns to sort by before writing, or None to keep the
            existing order. Defaults to the timestamp column.

    Returns:
        The path written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + TEMP_SUFFIX)

    if sort_by is not None:
        keys = [sort_by] if isinstance(sort_by, str) else list(sort_by)
        keys = [k for k in keys if k in table.column_names]
        if keys:
            table = table.sort_by([(k, "ascending") for k in keys])

    # Delta encoding suits monotonically increasing integer columns; dictionary
    # encoding suits the low-cardinality repeated values typical of price and size.
    #
    # pyarrow rejects a column that is named in column_encoding while dictionary
    # encoding is still enabled for it, so the two sets are made disjoint here.
    # Passing use_dictionary as a bare True with any column_encoding raises
    # ValueError: "To use 'column_encoding' set 'use_dictionary' to False".
    MONOTONIC_ID_COLUMNS = ("ts", "trade_id", "agg_id", "first_id", "last_id",
                            "update_id", "open_time", "transaction_time")
    encodings = {}
    for name, field in zip(table.column_names, table.schema):
        if pa.types.is_integer(field.type) and name in MONOTONIC_ID_COLUMNS:
            encodings[name] = "DELTA_BINARY_PACKED"
    dictionary_columns = [c for c in table.column_names if c not in encodings]

    try:
        pq.write_table(
            table,
            temp,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            column_encoding=encodings or None,
            use_dictionary=dictionary_columns if encodings else True,
            row_group_size=ROW_GROUP_SIZE,
            write_statistics=True,
        )
        os.replace(temp, path)
    except BaseException:
        # BaseException rather than Exception so that KeyboardInterrupt and
        # SystemExit also clean up rather than leaving the temp behind.
        temp.unlink(missing_ok=True)
        raise
    return path


def is_readable_parquet(path: Path | str) -> bool:
    """Whether a file is a complete, readable Parquet file.

    Reads only the footer, so this costs no data scan and is cheap enough to run
    over an entire dataset when resuming. Parquet ends with a footer and a magic
    marker, so any truncation is detected: verified against files truncated to
    99%, 50% and 1% of their length, and against a zero-byte file.
    """
    try:
        pq.read_metadata(Path(path))
        return True
    except Exception:
        return False


def clear_stale_temps(root: Path | str) -> list[Path]:
    """Delete temporary partitions left behind by an interrupted run.

    Returns the paths removed. These are always incomplete by construction, so
    removing them is safe; the corresponding work is simply redone.
    """
    removed = []
    for temp in Path(root).rglob(f"*{TEMP_SUFFIX}"):
        temp.unlink(missing_ok=True)
        removed.append(temp)
    return removed


def read_dataset(root: Path | str, columns: list[str] | None = None,
                 where: str | None = None, limit: int | None = None) -> Any:
    """Query a directory of Parquet partitions in place with DuckDB.

    DuckDB's native storage is roughly 3.2x larger than zstd Parquet because it
    applies only lightweight encodings, so Parquet stays the archive and DuckDB is
    only the query engine over it.

    Args:
        root: Directory holding the partitions. Read recursively.
        columns: Columns to project, or None for all.
        where: SQL predicate applied to the scan, e.g. "ts >= '2026-01-01'".
        limit: Maximum rows.

    Returns:
        A pandas DataFrame.
    """
    import duckdb

    projection = ", ".join(columns) if columns else "*"
    sql = f"SELECT {projection} FROM read_parquet('{Path(root)}/**/*.parquet', hive_partitioning=true)"
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {limit}"
    return duckdb.sql(sql).df()


def parquet_stats(path: Path | str) -> dict[str, Any]:
    """Return row count, byte size and bytes per row for one Parquet file."""
    path = Path(path)
    meta = pq.read_metadata(path)
    size = path.stat().st_size
    return {
        "path": str(path),
        "rows": meta.num_rows,
        "bytes": size,
        "bytes_per_row": size / meta.num_rows if meta.num_rows else 0.0,
        "row_groups": meta.num_row_groups,
    }
