# `backtest/engine/` 目录说明

## 1. 职责

本目录是**场所无关**的事件驱动 bar 级回测引擎：只提供时序推进、撮合规则、
账本记账、绩效统计与结果落地这五类通用能力。

不装的内容：任何交易所特有的口径（费率、印花税、交易日历、故障概率、
年化因子）、任何策略信号、任何网络调用、任何直接读 `data/` 的代码。
这些分别由 `backtest/<venue>/`、`<venue>/strategy/`、`<venue>/data_source.py`
承担。引擎与场所之间的耦合面只有三处：`BrokerSim` 协议、
`to_base_ccy` 折算函数、`to_liquidation` 清算估值函数，全部由场所侧注入。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 包声明，把 20 个名字（`BrokerSim`、`BacktestEngine`、`PortfolioView`、`RunResult`、`BarFeed`、`FxSeries`、`MarketView`、`Ledger`、`Position`、`compute_metrics`、`run_name`、`write_run`、`Bar`、`EngineConfig`、`Fill`、`Order`、`OrderSpec`、`OrderStatus`、`OrderType`、`TimeInForce`）提到包级并写进 `__all__` | 声明本包的对外 API 集合。检索显示**当前无任何 `from backtest.engine import ...` 的调用点**，全部消费方都直接 import 子模块；删除后子模块 import 仍能按命名空间包解析，损失的是这份 API 清单本身 | 无代码调用点；`ARCHITECTURE.md` §2.2 的模块登记与其对应 |
| `types.py` | 全引擎共用的枚举与记录：`OrderType`/`OrderStatus`/`TimeInForce`、`Bar`、`OrderSpec`、`Order`、`Fill`、`EngineConfig`，以及常量 `INTERVAL_SECONDS`（每种 bar 周期的秒数） | 删掉则引擎与全部适配层、脚本、测试同时失去数据契约。订单语义（卖出为负数量、DAY/GTC 有效期）对齐 T212 OpenAPI v0，`Fill.costs_gbp` 的键名刻意与场所 `Tax.name` 枚举同名以便与真实账户逐项对账 | `engine.py`、`ledger.py`、`matching.py`、`results.py`、`broker.py`、`feed.py`；`backtest/t212/{broker_sim,runner,faults,admission}.py`；`scripts/20260820_t212_backtest_smoke.py`、`scripts/20260821_a0_framework_backtest.py`、`scripts/20260822_a0_minute_backtest.py`；`tests/backtest/` 五个测试文件 |
| `broker.py` | `BrokerSim`（`typing.Protocol`，带 `@runtime_checkable`）：声明引擎对撮合器的全部要求，共 6 个方法与 1 个属性（`orders`、`submit`、`cancel`、`process_bar`、`open_orders`、`pending_signed_qty`、`order_quantity_step`），只有类型契约、无任何函数体 | 用途待确认的边界情况：**除 `__init__.py` 的再导出外无代码调用点**，`BacktestEngine.__init__` 的 `broker` 参数未加类型标注，`T212BrokerSim` 也未继承该 Protocol，故删除它不会使任何现有代码报错。其价值是把「回测模拟器与将来实盘适配器实现同一接口」这条设计裁定固化成可被类型检查器使用的代码 | `backtest/engine/__init__.py`；文档引用见 `ARCHITECTURE.md` L77、`backtest/README.md` L113、`backtest/okx/README.md` L38、`fixplans/framework/01_architecture.md` L46 |
| `feed.py` | 多标的 bar 流：`validate_frame` 数据质量闸、`trading_key` 对齐键、`BarFeed` 按时间推进的合并流、`FxSeries` 无前视的 GBPUSD 汇率查询、`MarketView` 只含 ts ≤ 当前时刻的策略视图。常量 `VALID_QUOTE_CCYS` 覆盖 USD/GBP/GBp/USDT | 删掉则失去两条结构性无前视保证（视图游标、FX 可得性）与日线跨时区对齐。日线按**交易所本地日期**对齐是硬要求：BST 期间伦敦日线的原始 UTC 时间戳落在前一 UTC 日 23:00，按 UTC 日期对齐会把伦敦整体错位一天 | `engine.py`、`__init__.py`；`backtest/t212/broker_sim.py`（`FxSeries`）、`backtest/t212/runner.py`（`BarFeed`/`FxSeries`/`trading_key`）；`tests/backtest/{conftest,test_feed,test_engine,test_conservatism_and_metrics,test_strategy_and_okx_source,test_review_regressions}.py` |
| `matching.py` | 纯撮合规则四函数：`match_market`、`match_limit`、`match_stop`、`match_stop_limit`。输入一根 bar 与订单参数，输出是否触发与**未叠加任何成本**的原始成交价，按 O-H-L-C 的 bar 内顺序判定，歧义一律判给策略的不利侧 | 删掉则撮合判定要在场所侧各写一份，两条线的成交口径会分叉。保守口径的两条关键规则在此：挂单必须**严格穿透**限价才成交（触及不成交），止损单遇跳空一律按开盘价成交 | `backtest/t212/broker_sim.py`；`tests/backtest/test_conservatism_and_metrics.py`、`tests/backtest/test_review_regressions.py`。未列入 `__init__.py` 的再导出 |
| `ledger.py` | `Position`（数量 + GBP 成本基础）与 `Ledger`（现金、冻结、成交入账、占用资金与权益采样）。全部金额用 `Decimal`；卖出按平均成本释放基础；`mark()` 每步追加一行含 `equity_gbp`（mid 诊断列）与 `equity_liq_gbp`（清算价值列） | 删掉则没有账本，成交无处入账、占用资金序列消失，而占用峰值正是全部收益率指标的分母。三处防御式断言（现金转负、超卖、持仓缺估值价）在此，任何一处静默通过都会污染净值曲线 | `broker.py`、`engine.py`、`__init__.py`；`backtest/t212/broker_sim.py`、`backtest/t212/admission.py`；`tests/backtest/conftest.py`、`tests/backtest/test_ledger_metrics.py` |
| `engine.py` | 主循环 `BacktestEngine.run()` 与两个记录类型 `PortfolioView`、`RunResult`。每根 bar 固定四步：结算既往订单、调策略、目标与（持仓 + 在途）差分下单、估值与占用采样。另含两条在线断言（`_assert_fill_timing` 的同步成交与日内成交时间守卫）与陈旧持仓守卫 `_guard_stale_positions` | 删掉则没有回测。四步的**顺序**本身是口径：`mark()` 必须在提交之后，否则本步冻结的资金逃出占用峰值；成交资格按时间而非时间轴步数判定，否则混交易所 1h 网格会提前半个间隔成交 | `results.py`、`strategy_loader.py`、`report.py`、`__init__.py`；`backtest/t212/runner.py`；`tests/backtest/{test_engine,test_conservatism_and_metrics,test_review_regressions}.py` |
| `metrics.py` | `compute_metrics` 出全部业绩与风险统计量，辅以 `realized_pnl_per_sell`（平均成本法逐笔重放）、`holding_episodes`（逐标的持仓区间，窗口末仍持仓者标 `open_at_end`）、`naive_utc`（时区归一） | 删掉则无绩效结论。三条口径固化于此：资本 = 占用**峰值**而非计划额度；年化 = 总收益 / 在场天数 × 因子 / 资本，单利不复利；年化因子由调用方传入（252 与 365 混用是已命名的失效模式）。带符号中位与绝对偏离中位分列两个键，对应 `CLAUDE.md` §2.3 | `engine.py`（`naive_utc`）、`report.py`、`__init__.py`；`backtest/t212/runner.py`；`tests/backtest/test_ledger_metrics.py`、`tests/backtest/test_conservatism_and_metrics.py`、`tests/backtest/test_review_regressions.py` |
| `results.py` | `run_name` 生成把策略名版、arm、窗口、费率档、种子全写进去的文件名主干（前视探针运行强制加 `_PROBE` 后缀）；`write_run` 落 `<stem>.trades.parquet` / `<stem>.equity.parquet` / `<stem>.meta.json` 三件，meta 内含完整配置、指标、订单审计与 git commit | 删掉则结果无法留痕，逐字节可复现的基线比对纪律也随之失效——本文件刻意不写入任何墙钟时间戳，同配置重跑三件产物 sha256 相同 | `__init__.py`；`backtest/t212/runner.py`；`tests/backtest/test_review_regressions.py::test_write_run_byte_identical` |
| `report.py` | `write_chart` 出单文件 HTML 快视图（净值 mid 与清算双线、占用曲线、在场区间底色、逐标的开仓横道），`in_market_spans` 计算在场区间 | 删掉则失去每轮的目视检查件。该文件**不在**逐字节复现保证内（内嵌 plotly 库），正式报告层是 `/html-report` skill 且读结果文件而非本图 | `backtest/t212/runner.py`；`tests/backtest/test_conservatism_and_metrics.py::test_chart_written_with_traces`。未列入 `__init__.py` 的再导出 |
| `strategy_loader.py` | `strategy_path` 把 (venue, name, version) 映射到 `<venue>/strategy/<name>_v<M>_<m>_<p>.py`；`load_strategy` 导入该模块，校验其 `STRATEGY_NAME` / `STRATEGY_VERSION` 与请求一致后返回 `compute_targets` | 删掉则策略只能被硬编码 import，版本身份不再被校验，回测结果可能被归因到错误的逻辑版本。名字或版本不符时直接拒载，不做兼容 | `scripts/20260821_a0_framework_backtest.py`、`scripts/20260822_a0_minute_backtest.py`；`tests/backtest/test_strategy_and_okx_source.py`；`trading212/strategy/a0_v0_0_1.py` 的模块头引用此加载路径。未列入 `__init__.py` 的再导出 |

## 3. 子目录索引

无。

## 4. 依赖关系

读入：

| 来源 | 用途 |
|---|---|
| `common/paths.py` | `results.py` 取 `DIR_BACKTEST_RESULTS`、`ROOT`；`strategy_loader.py` 取 `venue_dir` |
| `common/store.py` | `results.py` 取 `write_table` 写 parquet |
| 第三方库 | pandas、numpy（`metrics.py`）、pyarrow（`results.py`）、plotly（`report.py`，在 `write_chart` 内部延迟 import） |
| 外部进程 | `results.py` 的 `_code_version()` 在 `ROOT` 下调 `git rev-parse HEAD` 与 `git status --porcelain`，失败时返回 `None` 不中断 |

本目录**不读** `data/` 下任何文件：bar 数据以已构造好的 DataFrame 形式由场所侧
`data_source.py` 传入 `BarFeed`。本目录**不发**任何网络请求。

写出：见第 5 节。

被谁 import：

| 消费方 | 依赖的模块 |
|---|---|
| `backtest/t212/runner.py` | `engine`、`feed`、`report`、`results`、`metrics`、`types` |
| `backtest/t212/broker_sim.py` | `feed`、`ledger`、`matching`、`types` |
| `backtest/t212/admission.py` | `ledger`、`types` |
| `backtest/t212/faults.py` | `types` |
| `scripts/20260820_t212_backtest_smoke.py` | `types` |
| `scripts/20260821_a0_framework_backtest.py` | `types`、`strategy_loader` |
| `scripts/20260822_a0_minute_backtest.py` | `types`、`strategy_loader` |
| `tests/backtest/` | 见第 2 节各行 |

`backtest/okx/data_source.py` 目前只在模块头注释中引用 `engine/feed.py` 的职责边界，
尚无 import；okx 侧的撮合与成本适配器待建。

依赖方向单行：本目录只向下依赖 `common/`，不 import 任何 `backtest/<venue>/`、
`crypto_trading/`、`trading212/` 下的模块。

## 5. 产出与清理

| 产物 | 产生者 | 落点 | 处置 |
|---|---|---|---|
| `<stem>.trades.parquet`、`<stem>.equity.parquet`、`<stem>.meta.json` | `results.write_run` | 默认 `backtest/results/`，可由 `out_dir` 覆盖 | 已 gitignore。三件为一组，缺一则该轮结果不可用；作为基线比对对象的那几轮必须保留 |
| `<stem>.chart.html` | `report.write_chart`（路径由调用方给定） | 通常与上述三件同目录 | 派生可视件，可随时删除后重新生成，不在逐字节保证内 |
| `__pycache__/` | Python 解释器 | 本目录下 | 工具产物，按 `CLAUDE.md` §4.2 不得留在项目目录，可随时删除 |

本目录源文件自身无运行产物，全部 11 个 `.py` 均须保留（`broker.py` 的调用点情况见第 2 节）。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
