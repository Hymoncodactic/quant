# trading212/strategy/ 目录说明

## 1. 职责

装 Trading 212 一侧信号的**唯一副本**（`ARCHITECTURE.md` §2.0）：回测与将来的
实盘执行层 import 同一份文件，不允许各写一份「回测版」与「实盘版」。

模块必须是纯函数：输入市场视图与持仓，输出目标股数，不读网络、不写状态、不下单。
契约全文见 `docs/backtest/framework/06_strategy_plugin.md`，加载与校验在
`backtest/engine/strategy_loader.py`。

不装：参数取值（在 `trading212/config/strategies/`，由入口层读一次经 `params` 传入）、
撮合与成本（在 `backtest/t212/`）、下单（在 `trading212/execution/`）。

命名规则（`ARCHITECTURE.md` §2.0.1）：文件 `<name>_v<M>_<m>_<p>.py`，
模块内 `STRATEGY_NAME` 与 `STRATEGY_VERSION` 必须与文件名一致，
不一致时 `load_strategy()` 直接拒载。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `a0_v0_0_1.py` | A0 信号的唯一副本。`STRATEGY_NAME = "a0"`、`STRATEGY_VERSION = "0.0.1"`，公开面只有 `compute_targets(view, portfolio, params)`。逻辑三段：逐标的信号（`tsmom252` 为默认，可切 `ma200` / `always` 供消融）、两道市场级闸（状态标的收盘价低于 SMA200 则全仓离场；Yang-Zhang 20 日年化波动在自身扩张窗分位达到 0.80 则全仓离场）、等现金槽位定仓（分数股，步进 0.0001 股，持仓期间不再调仓的无换手带）。私有件 `_now_date()`、`_yang_zhang()`、`_gates_open()`、`_SHARE_STEP` | 删除后 `load_strategy("t212", "a0", "0.0.1")` 抛 `FileNotFoundError`，两个回测入口全部失效，且 `a0_intraday_v0_0_1.py` 在导入处即失败。它同时是 A0 逻辑的唯一权威表述，已发布裁定与报告的数字都归因到这一份代码 | `backtest/engine/strategy_loader.py::load_strategy("t212", "a0", "0.0.1")`，调用点在 `scripts/20260821_a0_framework_backtest.py:108` 与 `scripts/20260822_a0_minute_backtest.py:161`；`trading212/strategy/a0_intraday_v0_0_1.py:56` 直接 import 并把全部决策委托给它；`research/regime_lab/report/make_a0_report_data.py:189` 作为证据件 C-1 计算大小与 md5 前 8 位；`research/decisions/20260821_a0_framework_comparison.md` 第 3 行按路径引用 |
| `a0_intraday_v0_0_1.py` | A0 的**时序变体**，分钟 bar 用。`STRATEGY_NAME = "a0_intraday"`、`STRATEGY_VERSION = "0.0.1"`。模块自述「不重新实现信号，只做管道」：把分钟视图适配成日线视图后调用未经修改的 `a0_v0_0_1.compute_targets`，因而 A0 的信号仍只有一份。它自己只决定三件事——在交易所本地 15:59 那根 bar 做决策；信息集截到 15:58（丢弃当前 bar）；当日剩余时段不成交，市价单落到次日开盘。今日日线由本场次分钟 bar 合成，历史日线由入口层经 `make_strategy()` 注入并按交易所本地日期严格切在今日之前。复权拼接因子 `f = 前一场次日线收盘 / 前一场次分钟末收盘`（1d 分区已复权、1m 分区未复权，二者不同尺度）。公开面：`make_strategy(daily_history)` 与 `compute_targets(view, portfolio, params)` | 删除后 `scripts/20260822_a0_minute_backtest.py:42` 在导入处失败，分钟频与日频的对照回测无法运行。它承载的是「决策时刻提前到收盘前一分钟」这一时序假设的可检验实现，该假设无法在日线 bar 上表达 | `scripts/20260822_a0_minute_backtest.py:42`（导入为 `a0m`），第 152 行调用 `a0m.make_strategy(history)`。全仓检索无其他调用点 |
| `__init__.py` | 空文件（0 字节），把本目录声明为常规 Python 包 | `scripts/20260822_a0_minute_backtest.py:42` 与 `a0_intraday_v0_0_1.py:56` 的 `from trading212.strategy import ...` 以此为包锚点。注意 `a0_v0_0_1.py` 走的是 `strategy_loader` 的按路径加载（`importlib.util.spec_from_file_location`），不经包机制 | 上述两处 import |

待处理事项两项：

1. `a0_intraday_v0_0_1.py` 当前**未纳入 git**（`git status` 显示 `??`），
   与它的唯一调用方 `scripts/20260822_a0_minute_backtest.py` 同为未跟踪状态。
2. `a0_v0_0_1.py` 的 docstring 引用两个磁盘上不存在的文件：
   第 6 行的 `research/decisions/20260820_regime_lf_ruling.md`
   与第 76 行的 `research/regime_lab/vol.py`（`research/regime_lab/` 下只有 `report/`）。
   两处均为悬空引用，来源待确认。

## 3. 子目录索引

无。

## 4. 依赖关系

读：无文件读取，无网络请求。两个模块的全部输入都来自调用方传入的
`view`、`portfolio`、`params` 三个参数。`a0_intraday_v0_0_1.py` 的历史日线
由入口层经 `make_strategy()` 注入闭包（`compute_targets` 分支也接受
`params["daily_history"]`，缺失时抛 `ValueError`）。

写：无。两个模块都不落盘、不改全局状态、不在调用之间保留状态。

import：

| 模块 | 项目内 import | 第三方 import |
|---|---|---|
| `a0_v0_0_1.py` | 无 | `numpy`、`pandas`、`decimal` |
| `a0_intraday_v0_0_1.py` | `trading212.strategy.a0_v0_0_1`（第 56 行） | `pandas`、`sys`、`decimal`、`pathlib` |

`a0_intraday_v0_0_1.py` 在第 52 至 54 行把仓库根插入 `sys.path`，
以便在被脚本直接执行时也能解析 `trading212.` 前缀的绝对导入。

被谁 import：见 §2 的「谁在用」一栏。依赖方向单行，本目录不得 import
`backtest/`、`trading212/execution/` 或 `trading212/ingest/`。

## 5. 产出与清理

无运行产物。回测结果落 `backtest/results/`（gitignore），
文件名带策略名与版本串。

`trading212/strategy/__pycache__/` 是 Python 字节码缓存，
`CLAUDE.md` §4.2.3 列为禁止留存，`.gitignore` 已排除但文件仍在磁盘上；
其中还留有 `a0_intraday_v0_0_1.cpython-311.pyc` 与 `a0_v0_0_1.cpython-311.pyc`。

必须保留：`a0_v0_0_1.py`、`a0_intraday_v0_0_1.py`、`__init__.py`。
策略文件按版本只增不改：逻辑变更须新开版本号，
既有版本删除即等于既有回测结果无法复现。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
2026-08-31 a0_v0_0_1 新增只读 `signal_diagnostics`（阈值距离诊断，供看板），双闸公式抽为 `_trend_gate_values`/`_vol_gate_values` 两个取值助手，`_gates_open` 改为调用它们；`compute_targets` 行为未变（等价性测试守卫）。
