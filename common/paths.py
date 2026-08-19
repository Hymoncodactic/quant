"""项目路径常量与数据分区路径构造。

全项目**唯一**的路径来源：其他模块一律从这里取路径，不自行拼接。
布局见 ARCHITECTURE.md §3。

对外函数：
    venue_dir(venue)                                场所代码目录
    config_dir(venue)                               场所配置目录
    data_dir(venue, layer)                          场所数据层目录（raw / curated）
    bar_path(venue, layer, inst, period, date_str)  单日 bar 文件路径
    manifest_path(venue)                            raw 层取回元信息清单
    gaps_path(venue)                                curated 层缺口登记表

对外常量：
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
# [1] 根路径
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

# 场所 slug -> 代码目录。slug 是代码中唯一合法的场所标识（quant-code-standards §1.3）。
VENUE_DIRS: dict[str, Path] = {
    "okx": ROOT / "crypto_trading",
    "t212": ROOT / "trading212",
}
VENUES = tuple(VENUE_DIRS)

LAYERS = ("raw", "curated")

# ============================================================================
# [2] 路径构造
# ============================================================================

def venue_dir(venue: str) -> Path:
    """场所代码目录。"""
    _check_venue(venue)
    return VENUE_DIRS[venue]

def config_dir(venue: str) -> Path:
    """场所配置目录（不含密钥）。"""
    return venue_dir(venue) / "config"

def data_dir(venue: str, layer: str) -> Path:
    """场所数据层目录。

    Args:
        venue: 场所 slug，见 VENUES。
        layer: "raw" 或 "curated"。
    """
    _check_venue(venue)
    if layer not in LAYERS:
        raise ValueError(f"未知数据层 {layer!r}，可选 {LAYERS}")
    return DIR_DATA / venue / layer

def bar_path(venue: str, layer: str, instrument: str, period: str, date_str: str) -> Path:
    """单日 bar 文件路径。

    分区规则：<venue>/<layer>/<instrument>/<period>/year=YYYY/YYYYMMDD.parquet
    日期为该文件**所含数据**的 UTC 日期，不是下载日期。

    Args:
        instrument: 交易所原始标识，如 "BTC-USDT" / "AAPL"。不自造映射。
        period: 小写周期，如 "1m" "1h" "1d"。
        date_str: "YYYYMMDD"。
    """
    if len(date_str) != 8 or not date_str.isdigit():
        raise ValueError(f"date_str 须为 YYYYMMDD，收到 {date_str!r}")
    return (data_dir(venue, layer) / instrument / period
            / f"year={date_str[:4]}" / f"{date_str}.parquet")

def manifest_path(venue: str) -> Path:
    """raw 层取回元信息清单（JSONL：请求 URL、参数、取回时刻、条数）。"""
    return data_dir(venue, "raw") / "_manifest.jsonl"

def gaps_path(venue: str) -> Path:
    """curated 层缺口登记表（CSV：标的、周期、起、止、原因、状态）。"""
    return data_dir(venue, "curated") / "_gaps.csv"

def _check_venue(venue: str) -> None:
    if venue not in VENUE_DIRS:
        raise ValueError(f"未知场所 {venue!r}，可选 {VENUES}")
