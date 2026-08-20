# 数据层计划：回测数据馈送

## 1. 范围

回测引擎的数据输入层：从本地 `data/t212/curated/` 读取 bar 数据，构造严格按时间
推进、天然满足 cutoff 的多标的 bar 流。本层不碰任何交易所或数据源接口
（`ARCHITECTURE.md` §2「回测」行：禁止调用任何交易所接口）。

## 2. 数据源事实（全部已本地证实）

| # | 事实 | 依据 |
|---|---|---|
| 1 | 落地周期共 5 档：1m / 2m / 5m / 1h / 1d；15m/30m/90m 不落地（可由 5m 与 1h 精确聚合） | `docs/data/t212/DATA_SPEC.md` §5 |
| 2 | 列结构：`ts, open, high, low, close, volume, quote_ccy` | `docs/data/t212/DATA_SPEC.md` §3；`trading212/ingest/yahoo_bars.py::_tidy()` |
| 3 | 价格已按拆股与分红复权（`auto_adjust=True`） | `docs/data/t212/DATA_SPEC.md` §1 |
| 4 | 计价币三种：USD / GBP / GBp，GBp 为便士（英镑的 1/100） | `docs/data/t212/DATA_SPEC.md` §3 |
| 5 | 历史深度：1m 30 天、2m/5m 60 天、1h 730 天、1d 全部上市历史 | `docs/data/t212/DATA_SPEC.md` §5 |
| 6 | 文件布局：日线按年 `<sym>_<year>.parquet`，日内按月 `<sym>_<start>_<end>_<interval>.parquet` | `common/paths.py::equity_daily_path / equity_intraday_path` |
| 7 | FX 序列：`uk_tradable/GBPUSD=X/`，列结构同 bar | 本地实测 2026-08-20：`GBPUSD_X_2026.parquet` 列 `[ts,open,high,low,close,volume,quote_ccy]` |

## 3. bar 时间戳语义（2026-08-20 本地实证，判别力样本）

| 周期 | ts 语义 | 判别样本 |
|---|---|---|
| 日内（1h 等） | **bar 开始时刻**，UTC | AAPL 1h：2026-07（EDT）首根 13:30 UTC；2026-01（EST）首根 14:30 UTC。恰等于两制下的开盘时刻；若 ts 为结束时刻则应为 14:30 / 15:30 |
| 日线 | **交易所本地零点转 UTC** | AAPL 2026-01 为 05:00 UTC（EST 零点）、2026-07 为 04:00 UTC（EDT 零点）；SGLN.L 2026-01 为 00:00 UTC、2026-07 为 **前一 UTC 日 23:00**（BST 零点） |

### 3.1 夏令时日线错位陷阱（硬性处理规定）

伦敦标的与 `GBPUSD=X` 在 BST 期间的日线 ts 落在**前一 UTC 日 23:00**：
交易日 2026-06-29 的 bar，ts = `2026-06-28 23:00 UTC`。直接取 UTC 日期对齐会把
伦敦标的整体错一天。规定：

1. 日线对齐键 = **ts 换算到该标的交易所时区后的本地日期**（即交易日）。
2. 交易所时区映射：`.L` 后缀与 `GBPUSD=X` → `Europe/London`；其余（us_equity /
   us_etf）→ `America/New_York`。映射作为常量落在数据层模块，注明本节为依据。
3. 判别力测试样本必须横跨 BST 边界（如 2026-06-28 至 2026-07-06），并包含
   2026-07-03（美国假日休市、伦敦开市）这类日历不对称日。

## 4. 时间轴与对齐

1. 主时间轴 = 所选标的集合的 bar 时刻并集（日线用交易日、日内用 UTC 时刻），
   升序推进。某标的在某时刻无 bar 属正常（假日不对称、停牌、上市前），
   不得因此丢弃该时刻。
2. 停牌/缺 bar 显式处理（`/backtest-discipline` §二.8）：缺 bar 的标的当期
   持仓照旧、挂单不成交；缺口计入结果元数据 `data_gaps`，禁止静默跳过。
3. 引擎按游标暴露数据：时刻 t 的策略调用只能看到 ts ≤ t 的 bar
   （日内）或交易日 ≤ t 的 bar（日线）。游标即天然 cutoff，另有运行时断言
   （见 `validation/01_no_lookahead.md`）。

## 5. 多币种与 FX

1. 账本基准货币 GBP（用户明言账户存放英镑，S6，2026-08-20）。
2. 折算规则：`GBp → /100`；`USD → /GBPUSD`。GBP 与 GBp 计价标的不涉及换汇，
   不收 FX 费（依据 `research/notes/20260819_t212_execution_and_liquidity.md` §三：
   换汇费仅 USD 计价线）。
3. FX 取值口径（无未来函数）：时刻 t 使用**最近一个 ts ≤ t 的 GBPUSD bar**。
   日线回测中估值用当交易日的 GBPUSD close 之前不可得，故 t 日估值用 t-1 日
   close，t+1 日成交换汇用 t+1 交易日可得的最近值。粒度粗于真实换汇是已知
   建模误差，方向不定，在结论限定节披露（`/backtest-discipline` §二.10）。
4. GBPUSD 语义：Yahoo `GBPUSD=X` 报价为 1 GBP 兑多少 USD。USD→GBP 折算为
   `usd_amount / rate`。实现处写明该方向，测试用非 1 的样本验证方向不可反。

## 6. 复权口径的已知偏差

价格为复权价的后果，在结果与报告中作为固定限定披露：

1. 历史成交价非当时真实市价，股数、名义额、按名义额计的税费与真实历史有偏差。
2. 现金分红不再单独入账（已折入复权价），股息预扣税不建模。
3. 结论：本数据适合**收益率口径**的策略比较，不适合重演真实账户现金流。
   若需未复权口径，另行下载并在预注册中声明。

## 7. 数据质量闸（进引擎前断言，违反即停）

| # | 断言 |
|---|---|
| 1 | ts 严格单调递增、无重复 |
| 2 | `high >= max(open, close)` 且 `low <= min(open, close)` 全量成立 |
| 3 | `volume >= 0`；价格全部 > 0 |
| 4 | `quote_ccy` 非空且属 {USD, GBP, GBp} |
| 5 | 所选窗口内 FX 序列对每个需折算时刻均有 ts ≤ t 的可用值 |

## 8. 接口草案

```
BarFeed(symbols, interval, start, end, data_root=None)
    data_root 默认经 common/paths 解析；可注入以便测试与跨仓库读取。
    迭代产出 (ts, {symbol: Bar})；Bar = (ts, open, high, low, close, volume, quote_ccy)。
FxSeries(interval, data_root=None)
    rate_at(ts) 返回最近 ts' <= ts 的 GBPUSD 收盘价；ts' 缺失时抛错不外推。
```

价格在 feed 层保持源单位（GBp 不预折算），折算集中在账本层做，避免
「部分折算」的中间态。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-20 | 初版 |
| 2026-08-20 | 澄清 §4.3：策略视图的 cutoff 仍为「ts ≤ t 的 bar 可见」，但**撮合资格**按信息可得性时间执行（bar 的收盘信息在 ts + interval 才存在，订单最早在提交键 + 1 个完整 interval 后的 bar 成交）。FX 序列自始即按此规则（§5.3），bar 撮合与之对齐 |
