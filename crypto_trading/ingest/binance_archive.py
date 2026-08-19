"""Client for the Binance public bulk archive at data.binance.vision.

The archive is a plain public S3 bucket behind CloudFront. It needs no
credentials and, unlike api.binance.com which answers HTTP 451 from the UK, it is
not geo-restricted. Verified from this host on 2026-08-19.

Three facts about the bucket that a naive crawler gets wrong:

    1. Enumeration only works against the S3 hostname. The CDN hostname serves a
       JavaScript single-page app, not ListObjects XML.
    2. There are three roots, not one. `data/` holds everything current, `data3/`
       holds USD-M liquidation snapshots which are absent from the futures tree,
       and `data2/` is a legacy duplicate of early spot history that also ships
       uncompressed CSVs. Crawling the bucket root double-ingests 2017-2021.
    3. Kline files are named by interval, not by dataset, so `BTCUSDT-1m-....zip`
       is the filename for klines, markPriceKlines, indexPriceKlines and
       premiumIndexKlines alike. Only the directory distinguishes them.

Public functions:
    build_url(market, freq, dataset, symbol, period, date_str)  Compose an object URL
    list_prefix(prefix)                     Enumerate one prefix, following pagination
    head_size(url)                          Object size in bytes, or None if absent
    fetch_to_frame(...)                     Download, verify checksum, parse to a frame
    available_dates(market, freq, dataset, symbol, period)      Dates actually present

Public constants:
    CDN_BASE, S3_LIST_BASE
"""

from __future__ import annotations

__all__ = ["build_url", "list_prefix", "head_size", "fetch_to_frame",
           "available_dates", "CDN_BASE", "S3_LIST_BASE"]

import hashlib
import io
import re
import urllib.error
import urllib.request
import zipfile
from typing import Optional

import pandas as pd

from crypto_trading.ingest.schemas import spec_for, timestamp_unit

CDN_BASE = "https://data.binance.vision"
S3_LIST_BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
USER_AGENT = "quant-research/1.0"
TIMEOUT_SEC = 120

# No documented rate limit exists for the archive and none was observed under 40
# concurrent requests, but a modest ceiling costs nothing and keeps us a good
# citizen on a free service.
MAX_CONCURRENCY = 8


def _prefix(market: str, freq: str, dataset: str, symbol: str,
            period: str | None = None) -> str:
    """Compose the bucket prefix for one dataset directory."""
    if market == "spot":
        base = f"data/spot/{freq}/{dataset}/{symbol}"
    else:
        base = f"data/futures/{market}/{freq}/{dataset}/{symbol}"
    return f"{base}/{period}/" if period else f"{base}/"


def build_url(market: str, freq: str, dataset: str, symbol: str,
              date_str: str, period: str | None = None) -> str:
    """Compose the full object URL for one archive file.

    Args:
        market: "spot", "um" or "cm".
        freq: "daily" or "monthly".
        dataset: e.g. "klines", "aggTrades", "metrics", "bookDepth".
        symbol: Venue-native symbol, e.g. "BTCUSDT".
        date_str: "YYYY-MM-DD" for daily, "YYYY-MM" for monthly.
        period: Bar interval such as "1m", required for kline datasets.

    Returns:
        A URL under CDN_BASE.
    """
    # Kline-family files are named by interval; everything else by dataset name.
    tag = period if period else dataset
    return f"{CDN_BASE}/{_prefix(market, freq, dataset, symbol, period)}{symbol}-{tag}-{date_str}.zip"


def _get(url: str, timeout: int = TIMEOUT_SEC) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout).read()


def head_size(url: str) -> Optional[int]:
    """Return an object's size in bytes without downloading it.

    Returns None when the object does not exist, which is the normal way to
    discover a dataset's coverage boundaries.
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.headers.get("Content-Length", 0))
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def list_prefix(prefix: str, max_pages: int = 50) -> list[tuple[str, int]]:
    """Enumerate every object under a prefix, following pagination.

    The legacy ListObjects API returns at most 1000 keys per page, and because
    each data file is interleaved with its .CHECKSUM sidecar a full page is only
    about 500 actual files.

    Args:
        prefix: Bucket prefix, e.g. "data/spot/monthly/klines/BTCUSDT/1m/".
        max_pages: Safety bound on pagination.

    Returns:
        List of (key, size_bytes), excluding .CHECKSUM sidecars.
    """
    out: list[tuple[str, int]] = []
    marker = ""
    for _ in range(max_pages):
        url = f"{S3_LIST_BASE}?delimiter=/&prefix={prefix}"
        if marker:
            url += f"&marker={urllib.parse.quote(marker)}"
        body = _get(url, timeout=60).decode("utf-8", "replace")
        page = re.findall(r"<Key>(.*?)</Key>.*?<Size>(\d+)</Size>", body, re.S)
        for key, size in page:
            if key.endswith(".zip"):
                out.append((key, int(size)))
        if "<IsTruncated>true</IsTruncated>" not in body or not page:
            break
        marker = page[-1][0]
    return out


def available_dates(market: str, freq: str, dataset: str, symbol: str,
                    period: str | None = None) -> list[str]:
    """Return the dates for which a dataset actually has files.

    Coverage differs per dataset and per symbol, and several datasets were
    silently discontinued, so coverage is discovered rather than assumed.
    """
    keys = list_prefix(_prefix(market, freq, dataset, symbol, period))
    dates = []
    for key, _size in keys:
        m = re.search(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$", key)
        if m:
            dates.append(m.group(1))
    return sorted(dates)


def _verify_checksum(url: str, payload: bytes) -> Optional[bool]:
    """Compare a downloaded object against its .CHECKSUM sidecar.

    Returns True when they match, False when they differ, None when no sidecar
    exists. The sidecar holds a SHA-256 digest; the S3 ETag is not the digest.
    """
    try:
        sidecar = _get(url + ".CHECKSUM", timeout=30).decode().split()[0].strip()
    except Exception:
        return None
    return hashlib.sha256(payload).hexdigest() == sidecar


def fetch_to_frame(market: str, freq: str, dataset: str, symbol: str,
                   date_str: str, period: str | None = None,
                   verify: bool = True) -> Optional[pd.DataFrame]:
    """Download one archive file and parse it into a typed frame.

    Applies the dataset's schema: supplies column names for the header-less spot
    files, drops exactly-derivable columns, and converts the timestamp using the
    correct unit for the market and date.

    Args:
        verify: Check the SHA-256 sidecar. A mismatch raises rather than returning
            partial data, because a silently corrupt day is worse than a gap.

    Returns:
        The parsed frame, or None when the file does not exist.

    Raises:
        ValueError: The checksum sidecar exists and does not match.
    """
    url = build_url(market, freq, dataset, symbol, date_str, period)
    try:
        payload = _get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    if verify and _verify_checksum(url, payload) is False:
        raise ValueError(f"checksum mismatch for {url}")

    spec = spec_for(market, dataset)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as handle:
            frame = pd.read_csv(
                handle,
                header=0 if spec.has_header else None,
                names=None if spec.has_header else spec.columns,
            )

    if spec.has_header:
        frame.columns = [c.strip() for c in frame.columns]
        rename = dict(zip(frame.columns, spec.columns))
        frame = frame.rename(columns=rename)

    frame = frame.drop(columns=[c for c in spec.drop if c in frame.columns])

    if spec.ts_column and spec.ts_column in frame.columns:
        col = frame[spec.ts_column]
        if pd.api.types.is_numeric_dtype(col):
            frame[spec.ts_column] = pd.to_datetime(
                col, unit=timestamp_unit(market, date_str), utc=True)
        else:
            frame[spec.ts_column] = pd.to_datetime(col, utc=True)

    return frame
