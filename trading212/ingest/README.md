# trading212/ingest/ 目录说明

## 1. 职责

装股票线的行情**取数与落地**逻辑：从 Yahoo 拉 bar，整形为项目 schema，
按分区规则写进 `data/t212/curated/`。

不装：交易决策（在 `strategy/`）、下单（在 `execution/`）、
数据的字段与时区说明（在 `docs/data/t212/DATA_SPEC.md`）、
调度与报告（在 `scripts/update_data.py` 与 `scripts/20260819_ingest_equity.py`）。

行情不取自 Trading 212：`WORKING_MEMORY.md` 未决项 2 记载，T212 的
`equity/prices`、`equity/quotes`、`market/candles` 均返回 404（接口不存在），
故本线的行情源是 Yahoo，落地目录仍用 `t212` 这个 slug。
数据源与交易场所的区别见 `ARCHITECTURE.md` §3.1。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `yahoo_bars.py` | 取数与落地的唯一实现。公开函数五个：`fetch_interval(ticker, interval, lookback, chunk)` 取一个标的一个周期（1m 按 7 天一段拼接）、`write_daily(group, ticker, frame)` 日线一年一文件、`write_intraday(group, ticker, interval, frame)` 日内一月一文件（同月旧名文件先删再写）、`latest_stored(group, ticker, interval)` 只读 parquet footer 统计量取最新时刻、`quote_currency(ticker)` 查交易所计价币并缓存。公开常量：`INTERVALS`（1d/1h/5m/2m/1m 五档及各自历史上限）、`UNIVERSE`（三组共 52 个标的：`us_equity` 24、`us_etf` 17、`uk_tradable` 11）、`RETRY_BASE_SEC`、`RETRY_ATTEMPTS`、`PACE_SEC` | 初次 ingest 与增量更新共用这一份，删除后 `scripts/update_data.py` 在导入处即失败，日常数据更新入口失效；且分区布局与命名规则会在两个调用方之间分叉（模块 docstring 明写这是设立本模块的理由） | `scripts/update_data.py:44` 导入为 `yb`，在 220 至 245 行使用 `UNIVERSE`、`latest_stored`、`fetch_interval`、`PACE_SEC`、`INTERVALS`、`write_daily`、`write_intraday`。`backtest/t212/instruments.py:79-80` 的注释引用本模块的 `UNIVERSE` 作为「当前池内全为 ETF/ETC，故印花税结构性为零」的依据。`docs/backtest/framework/02_data_layer.md:14` 引用 `_tidy()` 作为列结构凭据 |
| `a1_rank.py` | 盘前第四 pass：把 1,500 只候选池在**已收盘**场次上排一次名，落 `data/t212/curated/a1/rank/<YYYY-MM-DD>.parquet`。公开面：`load_panels`、`verified_tickers`、`latest_complete_session`、`build_table`、`write_table`、`run`。准入与分数**全部委托** `trading212/strategy/a1_v0_0_1.py::rank_table`，本层不复制任何阈值。常量 `PANEL_START = "2010-01-04"`（口径：准入 E3 从面板起点数起）、`MIN_SESSION_COVERAGE = 0.95`（覆盖率闸） | 决策窗只有约半小时，装不下 1,500 只面板，排名必须盘前算好；覆盖率闸是硬防线——2026-09-03 实测半更新的湖只有 17/1498 只有当日 bar，若照排会产出一份只含 15 只可选的表，实盘会把整池轮动进那 15 只 | `scripts/update_data.py::_update_a1_rank()`；`trading212/execution/market_data.py::load_b0_injection()` 读它的产物 |
| `__init__.py` | 空文件（0 字节），把本目录声明为常规 Python 包 | `scripts/update_data.py:44` 的 `from trading212.ingest import yahoo_bars as yb` 以此为包锚点 | `scripts/update_data.py:44` |

`quote_currency()` 只对 `.L` 结尾的伦敦标的做慢速元数据查询，其余一律记 USD。
伦敦同时以 GBp（便士）、GBP、USD 挂牌，认错计价币是一百倍的误差。

`write_intraday()` 的注释中出现英式拼写 `labelled`（第 230 行），
与 `CLAUDE.md` §2.3 第 2 条要求的美式拼写不符，登记待处理。

## 3. 子目录索引

无。

## 4. 依赖关系

读：Yahoo 的 `yfinance` 接口（在 `quote_currency()` 与 `_history()` 内部延迟导入，
模块顶层不导入）；`data/t212/curated/<group>/<ticker>/<interval>/` 下已有 parquet
的 footer 统计量（`latest_stored()`，不读数据体）。

写：`data/t212/curated/` 下的 parquet 分区。路径一律经
`common/paths.py` 的 `equity_daily_path()`、`equity_intraday_path()`、
`month_bounds()` 构造，落盘经 `common/store.py::write_table()` 原子写入。
`latest_stored()` 是唯一一处直接拼路径的地方（`DIR_DATA / "t212" / "curated" / ...`）。

import 的项目模块：`common.paths`（`equity_daily_path`、`equity_intraday_path`、
`month_bounds`、`DIR_DATA`）、`common.store`（`write_table`）。
第三方：`pandas`、`pyarrow`、`pyarrow.parquet`、`yfinance`（延迟导入）。
不 import 任何 `backtest/` 或 `strategy/` 代码。

被谁 import：`scripts/update_data.py:44`。全仓检索无其他导入点；
`scripts/20260819_ingest_equity.py` 是本模块抽出之前的旧入口，
自行 import `common.paths` 与 `common.store`，不经过本模块。

## 5. 产出与清理

运行产物全部落在 `data/t212/curated/` 下（整棵 `data/` 已 gitignore），
不落在本目录内。产物是数据资产，不清理；`data/` 下删除或覆盖须先问用户
（`CLAUDE.md` §3.4）。

`trading212/ingest/__pycache__/` 是 Python 字节码缓存，
`CLAUDE.md` §4.2.3 列为禁止留存，`.gitignore` 已排除但文件仍在磁盘上。

必须保留：`yahoo_bars.py`、`a1_rank.py`、`__init__.py`。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
2026-09-03 新增 `a1_rank.py`（A1 盘前排名 pass）。`yahoo_bars._tidy` 与
`fetch_interval` 增 `drop_from` 参数（半截 bar 守卫），新增公开函数
`exchange_tz(ticker)`。必须保留清单增 `a1_rank.py`。
