"""探针：用 Binance 日线实证计算主流币两两相关性，检验「存在流动性好且与 BTC 负相关的币」这一前提。

数据源：https://data-api.binance.vision/api/v3/klines（只读镜像，UK 可达，无鉴权）
口径：日线收盘价的对数收益率；Pearson 相关；全窗口 + 分年 + 下跌日子样本。
本脚本为一次性探针，结论写入 research/notes/，不进入生产管线。

对外函数：
    main()   取数、计算、打印相关性结果
"""

from __future__ import annotations

__all__ = ["main"]

import json
import time
import urllib.request
from typing import Optional

import numpy as np
import pandas as pd

BASE = "https://data-api.binance.vision"
INTERVAL = "1d"
LIMIT = 1000                      # 单次上限，约 2.7 年日线
KLINE_WEIGHT = 2                  # 依据：exchangeInfo 权重表，klines limit<=100 为 2
SLEEP_SEC = 0.25                  # 主动限速，远低于 6000 权重/分钟

CANDIDATES = [
    # 主流高流动性
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "TRXUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
    "DOTUSDT", "TONUSDT", "SUIUSDT",
    # 隐私币 / 常被称作「避险」叙事
    "ZECUSDT", "XMRUSDT", "DASHUSDT",
    # 黄金代币（真正的跨资产分散候选）
    "PAXGUSDT", "XAUTUSDT",
    # 稳定币（相关性应约等于 0，用作方法论对照组）
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT",
]


def _fetch_klines(symbol: str) -> Optional[pd.DataFrame]:
    """取单个交易对的日线。返回 index=UTC 日期、含 close 列的 DataFrame；失败返回 None。"""
    url = f"{BASE}/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}"
    req = urllib.request.Request(url, headers={"User-Agent": "quant-research/1.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        print(f"  {symbol:<12} 取回失败: {type(exc).__name__}")
        return None
    rows = json.loads(raw)
    if not rows:
        print(f"  {symbol:<12} 无数据")
        return None
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close"] = df["close"].astype(float)
    df["quote_volume"] = df["quote_volume"].astype(float)
    out = df.set_index("dt")[["close", "quote_volume"]]
    print(f"  {symbol:<12} {len(out):>5} 根  {out.index[0].date()} ~ {out.index[-1].date()}"
          f"  日均成交额 {out['quote_volume'].mean()/1e6:>8,.1f} 百万")
    return out


def main() -> None:
    print("=" * 78)
    print("取数：Binance 日线（data-api.binance.vision 只读镜像）")
    print("=" * 78)
    closes, volumes = {}, {}
    for sym in CANDIDATES:
        df = _fetch_klines(sym)
        if df is not None:
            closes[sym] = df["close"]
            volumes[sym] = df["quote_volume"].mean()
        time.sleep(SLEEP_SEC)

    px = pd.DataFrame(closes).sort_index()
    ret = np.log(px / px.shift(1)).dropna(how="all")
    print(f"\n收益率面板：{ret.shape[0]} 个交易日 × {ret.shape[1]} 个标的")
    print(f"区间：{ret.index[0].date()} ~ {ret.index[-1].date()}")

    # ---- 1. 与 BTC 的全窗口相关性 ----
    btc = ret["BTCUSDT"]
    print("\n" + "=" * 78)
    print("与 BTCUSDT 的日收益率相关性（全窗口，按相关性升序 = 最「负」的排最前）")
    print("=" * 78)
    print(f"{'标的':<12}{'相关性':>10}{'重叠天数':>10}{'日均成交额(百万)':>18}   判定")
    print("-" * 78)
    stats = []
    for sym in ret.columns:
        if sym == "BTCUSDT":
            continue
        pair = pd.concat([btc, ret[sym]], axis=1).dropna()
        if len(pair) < 60:
            continue
        c = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        stats.append((c, sym, len(pair), volumes.get(sym, float("nan"))))
    stats.sort()
    for c, sym, n, v in stats:
        if c < -0.1:
            verdict = "负相关"
        elif c < 0.15:
            verdict = "近似无关"
        elif c < 0.5:
            verdict = "弱正相关"
        else:
            verdict = "强正相关"
        print(f"{sym:<12}{c:>10.3f}{n:>10}{v/1e6:>18,.1f}   {verdict}")

    # ---- 2. 判别力检验：只看 BTC 大跌日，相关性是否更糟 ----
    print("\n" + "=" * 78)
    print("危机相关性检验：BTC 单日跌幅进入最差 10% 的子样本")
    print("（对冲标的的价值在于「BTC 跌时它不跌」，全窗口相关性掩盖这一点）")
    print("=" * 78)
    thr = btc.quantile(0.10)
    crash = ret[btc <= thr]
    print(f"阈值：BTC 日收益 <= {thr:.2%}，样本 {len(crash)} 天\n")
    print(f"{'标的':<12}{'全窗口ρ':>10}{'危机日ρ':>10}{'危机日均收益':>14}{'BTC同期均收益':>15}")
    print("-" * 78)
    for c, sym, n, v in stats:
        sub = crash[["BTCUSDT", sym]].dropna()
        if len(sub) < 20:
            continue
        cc = sub["BTCUSDT"].corr(sub[sym])
        print(f"{sym:<12}{c:>10.3f}{cc:>10.3f}{sub[sym].mean():>14.2%}"
              f"{sub['BTCUSDT'].mean():>15.2%}")

    # ---- 3. 分年稳定性 ----
    print("\n" + "=" * 78)
    print("分年相关性（检验是否稳定，还是只在某一年偶然为负）")
    print("=" * 78)
    yearly = {}
    for sym in [s for _, s, _, _ in stats]:
        row = {}
        for year, grp in ret.groupby(ret.index.year):
            sub = grp[["BTCUSDT", sym]].dropna()
            row[year] = sub["BTCUSDT"].corr(sub[sym]) if len(sub) >= 60 else np.nan
        yearly[sym] = row
    print(pd.DataFrame(yearly).T.round(3).to_string())

    px.to_csv("/tmp/crypto_closes.csv")
    print("\n收盘价面板已存 /tmp/crypto_closes.csv")


if __name__ == "__main__":
    main()
