# 股票 curated 数据说明

## 1. 来源

| 项 | 值 |
|---|---|
| 端点 | Yahoo Finance，经 yfinance 1.6.0 |
| 生成脚本 | `scripts/20260819_ingest_equity.py` |
| 取回时间 | 2026-08-19 |
| 复权 | `auto_adjust=True`，已按拆股与分红复权。验证：AAPL 2020 年 4:1 拆股无跳空 |
| 历史深度 | `period="max"`，取该标的全部可得历史，不设人为起点 |
| 取数方式 | **逐标的单独请求**。批量请求会把 `period=max` 解析成统一的 1927 年起点并施加于所有标的，Yahoo 对此限流：24 标的批量请求只返回 3 个，另 21 个报 "possibly delisted"（实际均未退市）。逐个请求各自的上市区间则成功，失败重试 4 次指数退避 |

注意：Yahoo 是页面接口的非授权抓取，无可用性承诺。适合做日线便利层，
**不适合作为管线基础**。付费替代见 §6。

## 2. 覆盖深度（2026-08-19 实测）

| 分组 | 标的数 | 交易日合计 | 最早 | 最长单一标的 |
|---|---:|---:|---|---|
| us_equity | 24 | 228,214 | 1962-01-02 | JNJ / KO / PG / XOM 各 64.6 年 |
| us_etf | 17 | 97,706 | 1993-01-29 | SPY 33.5 年 |
| uk_tradable | 11 | 40,301 | 2003-12-01 | GBPUSD 22.7 年 |

覆盖多个利率与通胀周期（1970 年代滞胀、1987、2000、2008、2022 通胀冲击），
这是做制度切换分析所必需的深度，见
`research/notes/20260819_negative_correlation_findings.md` §三。

## 3. 字段与单位

| 列 | 类型 | 单位 | 含义 |
|---|---|---|---|
| ts | timestamp UTC | — | 交易日 |
| open/high/low/close | float64 | **见 quote_ccy** | 复权后价格 |
| volume | float64 | 股 | 成交量 |
| quote_ccy | string | — | **交易所计价币**：USD / GBP / GBp |

注意，**计价币陷阱（本项目实际踩过）**：伦敦上市标的混用三种计价币。
**`GBp` 是便士，数值是英镑的 100 倍。** 不看 `quote_ccy` 直接算成交额或跨标的
比较，会有 100 倍误差。实测分布：

- `GBp`（便士）：SGLN.L、IBTL.L、XSPS.L、EQQQ.L
- `USD`：IGLN.L、IB01.L、IDTL.L、CSPX.L、IUCS.L，及全部美股与美国 ETF
- `GBP`：VUSA.L

折算为英镑：`GBp` 计价的价格除以 100，`USD` 计价的价格除以 GBPUSD 汇率
（`uk_tradable/GBPUSD=X/` 已下载）。

注意，**时间戳语义（2026-08-20 实证补记）**：

- 日线 `ts` 为**交易所本地零点转 UTC**，不是 UTC 零点。美股冬令时 05:00 UTC、
  夏令时 04:00 UTC；伦敦标的与 `GBPUSD=X` 在 BST 期间落在**前一 UTC 日 23:00**
  （实测 SGLN.L 交易日 2026-06-29 的 ts = `2026-06-28 23:00 UTC`）。
  跨标的按日对齐必须先转交易所时区取本地日期；按 UTC 日期对齐会把伦敦标的
  错位一天。对齐实现见 `backtest/engine/feed.py::trading_key()`。
- 日内 bar 的 `ts` 为 **bar 开始时刻**（实测 AAPL 1h 首根恰为开盘时刻，
  夏令时 13:30 / 冬令时 14:30 UTC，DST 差异构成判别力）。

## 4. 分组

| 分组 | 含义 | 可交易性 |
|---|---|---|
| `us_equity` | 美股普通股：首轮 24 只，自 2026-08-23 起并入 B0 候选池，磁盘上 1,501 个目录、A1 有效池 1,498 只（见 §7） | Trading 212 可买 |
| `us_etf` | 美国注册 ETF 17 只 | **英国零售不可买**，仅供研究对照。两道法律障碍：PRIIPs 无 KID + FSMA 2000 s238 |
| `uk_tradable` | 伦敦上市 UCITS / ETC 10 只 + GBPUSD 汇率 | 可买（App 已确认有 Buy 按钮）。价差与执行成本见 `research/notes/20260819_t212_execution_and_liquidity.md` |

## 5. 粒度上限（2026-08-19 逐项请求实测，非推断）

Yahoo 在拒绝非法周期时会枚举全部合法值：
`[1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 4h, 1d, 5d, 1wk, 1mo, 3mo]`。
**不存在任何亚分钟周期**，秒级美股数据在此源上无法获得，与付费与否无关。

各周期的历史深度（均为实测，附 API 原话）：

| 周期 | 单次请求上限 | 历史深度 | 依据 |
|---|---|---|---|
| 1m | **8 天** | **30 天** | API 原话：`Only 8 days worth of 1m granularity data are allowed to be fetched per request`；`The requested range must be within the last 30 days` |
| 2m | 无 | 60 天 | 实测返回 6,435 根，起 2026-07-06 |
| 5m | 无 | 60 天 | 实测返回 4,680 根，起 2026-05-26 |
| 15m / 30m / 90m | 无 | 60 天 / 730 天 | **不存储**，可由 5m 与 1h 精确聚合得到 |
| 1h（= 60m） | 无 | 730 天 | 实测返回 5,073 根，起 2023-09-21 |
| 1d | 无 | 全部上市历史 | 最早 1962-01-02（JNJ/KO/PG/XOM） |

**本项目存储的周期**：`1m` `2m` `5m` `1h` `1d`。
15m/30m/90m 是 5m 与 1h 的精确聚合，按「不存可导出量」原则不落地。
2m 与 5m 并存的理由：两者互不整除，在 30–60 天窗口内彼此无法重建。

1m 通过连续 8 天窗口拼接覆盖满 30 天上限（实测 AAPL 得 8,189 根，
覆盖 2026-07-22 至 2026-08-19）。

## 5.1 目录与文件命名

```
data/t212/curated/<分组>/<代码>/<周期>/
```

| 周期 | 切分粒度 | 文件名格式 | 示例 |
|---|---|---|---|
| `1d` | **按年** | `<代码>_<年>.parquet` | `AAPL_2026.parquet` |
| 日内 | **按月** | `<代码>_<起始YYYYMMDD>_<结束YYYYMMDD>_<周期>.parquet` | `AAPL_20260801_20260819_1m.parquet` |

日内文件名中的起止日期是该文件**实际包含**的首末日期，不是名义月界，
因此当月的部分数据自带说明。

切分理由：单标的日频合并成一个文件时，每次更新都要整份重写（AAPL 45 年、
11,513 根），既慢又有整份损坏的风险。按年/按月切分后，刷新只重写当前年或当前月。

## 6. 若需日内数据的付费门槛

- Massive（原 Polygon.io）Developer $79/月：SIP 逐笔成交，10 年历史
- Massive Advanced $199/月：加报价，回溯至 2003-09-10
- Databento Standard $199/月：**仅含 1 个月**的 L2/L3 历史
- 免费但不连续：NYSE Integrated Feed 样本（真 L3，已是 CSV，仅 2 天）、
  Nasdaq ITCH（真 L3 二进制，约 15 天）、IEX DEEP（连续到昨日，但仅占全市场 2.5%）

## 7. 缺口

首轮 52 个标的全部成功（ORCL 首轮失败，单独重试后补齐）。

自 2026-08-23 起 `us_equity` 另含 B0 候选池的 1,498 只（见 §4），其缺口登记在
本目录 `GAPS.csv`，2026-09-03 复核：

| 类型 | 标的 | 说明 |
|---|---|---|
| 上游单日空洞 | AMAT、JNJ、KO、LRCX、NEM、PG、XOM | 2026-08-28 缺日线，上游后补则全量重取自然恢复 |
| 尾部截断 | LEG、WBS | 自 2026-08-28 起无新 bar |
| 拼写不匹配（设计使然） | BRK-B、BF-B | 候选池 JSON 写 `BRK.B` / `BF.B`，磁盘写连字符。研究面板按池内拼写查目录，两者都查不到而被跳过，故 A1 的有效池是 **1,498** 而非 1,500。全部已记录的 A1 与 B0 结果都建立在这 1,498 只上；补回这两只属口径变更，须先回测 |

缺口对 A1 准入的影响是**放大的**：准入用 `rolling(252, min_periods=252)`，
一天空洞会让该标的其后 252 个场次全部不可准入。这是研究口径的既有行为，
本轮不改，只在此写明。

## 8. `curated/a1/rank/` —— A1 排名表（2026-09-03 新增）

一个已收盘美股场次一份 parquet：`data/t212/curated/a1/rank/<YYYY-MM-DD>.parquet`。
生成脚本 `trading212/ingest/a1_rank.py`（由 `scripts/update_data.py` 的第四 pass 调用），
准入与分数全部委托 `trading212/strategy/a1_v0_0_1.py::rank_table`。

| 列 | 类型 | 含义 |
|---|---|---|
| `symbol` | str | 磁盘拼写（连字符） |
| `ticker` | str \| null | 场所下单 ticker，取自 `data/reference/t212_universe_ticker_map_<date>.json` |
| `close` | float | 该场次收盘（已复权） |
| `score` | float \| null | 12-1 动量 `C[t-21]/C[t-252] - 1` |
| `eligible` | bool | 五条准入全过 |
| `elig_reason` | str | `ok`、`dollar_volume`、`zero_volume`、`history`、`participation`、`no_ticker`、`no_score` |
| `rank` | int \| null | 仅对 `eligible` 且有分数者，1 起，按分数降序 |
| `panel_as_of` | str | 面板最后场次，等于文件名日期 |
| `generated_at_utc` | str | 生成时刻 |
| `code_version` | str | 生成时的策略模块版本，如 `a1_v0_0_1` |

口径：只对**已收盘**场次生成；面板起点固定 2010-01-04（准入 E3 从此起算）；
同日重跑原子覆盖。当日覆盖率低于 95% 时**拒绝出表**（`MIN_SESSION_COVERAGE`），
2026-09-03 实测未完成的日线 pass 只让 17/1498 只有当日 bar，照排会产出一份
只含 15 只可选的表。

2026-09-03 首份产出：`2026-08-31.parquet`，1,498 行，1,475 只准入并排名。
