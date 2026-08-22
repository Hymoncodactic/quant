# `tests/backtest/` 目录说明

## 1. 职责

本目录装 `backtest/engine/`（场所无关引擎）、`backtest/t212/` 与 `backtest/okx/`
（场所适配层）、以及 `<venue>/strategy/` 下策略模块的单元测试与回归测试，
共 10 个 Python 文件，2026-08-22 pytest 收集到 100 项用例。

不装的内容：真实数据检验（在 `scripts/` 下的日期前缀脚本里）、任何会联网或下单的
用例、任何被生产代码 import 的辅助模块。每个用例的数据都在进程内合成，
不触碰 `data/` 下任何文件。

设计要求是**判别力**而非覆盖率：每个用例都要能在实现写错时给出不同的数值或异常，
判别点在各文件的行内注释中写明（例如 `test_broker.py` 的下一根开盘价成交用例，
提交那根 bar 收于 110、下一根开于 120，同步收盘成交的错误实现会得到 110、
同步开盘成交的会得到 100，只有 120 通过）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 空文件（0 字节），把 `tests.backtest` 声明为常规包 | 5 个测试文件用 `from tests.backtest.conftest import ...` 的绝对路径取夹具函数，该 import 要求 `tests.backtest` 可作为包解析；本文件把它声明为常规包，使这条路径在任何 import 模式下都成立。当前内容为空，未按 `CLAUDE.md` §4.4 写模块头 docstring | pytest 收集；`test_broker.py`、`test_engine.py`、`test_conservatism_and_metrics.py`、`test_feed.py`、`test_review_regressions.py` 的 import 路径 |
| `conftest.py` | 共享夹具与构造函数：`daily_ts`（交易所本地零点转 UTC，复刻真实日线的时间戳形态）、`bar_frame` / `daily_rows` / `flat_bar` / `fx_frame`（合成 bar 与汇率帧）、`cost_cfg_clean`（零滑点、零时段加宽、冷却期取结构下界，使预期成交价与时刻可精确断言）、`faults_off`（全部故障开关关闭）、`mk_broker` / `mk_ledger`（预接线的撮合器与账本）、pytest 夹具 `zero_spread`（把测试标的的半点差改为 0） | 删掉则 5 个测试文件无法 import，其余文件失去统一的数据构造口径，各自复制一份合成逻辑后测试之间的时间戳与费用假设会分叉 | `test_broker.py`、`test_engine.py`、`test_conservatism_and_metrics.py`、`test_feed.py`、`test_review_regressions.py`；`zero_spread` 夹具被前述 4 个文件使用（`test_feed.py` 只用其中的构造函数） |
| `test_a0_intraday.py` | 11 项：`trading212/strategy/a0_intraday_v0_0_1.py` 的分钟级择时变体——决策时刻闸（非决策 bar 不出目标）、时间戳未带时区时一律不交易、决策不使用当根 bar 自身的收盘而使用前一根、日线历史切片严格因果、当日分钟价按拼接比例缩放、首个交易时段回退到比例 1.0、状态标的与交易标的的回看窗口都不被截断、信号计算委托给日线版而非重写一份、入口契约要求传入历史。全部数据由 `FakeBar` / `FakeView` 在进程内合成 | 删掉则该策略的决策时刻闸与因果切片无回归保护，二者出错会直接产生前视收益。每个用例的 docstring 都写明它捕捉的具体缺陷 | pytest 收集。本文件不 import `conftest.py`，自带 `FakeBar` / `FakeView` 替身与 `params` / `build_history` / `make_case` 构造函数 |
| `test_broker.py` | 16 项：`backtest/t212/broker_sim.py` 的成交时机（下一根开盘成交、提交那根不成交、休市时排队到复市）、准入拒单（数量精度、最小订单额、未知标的、超卖、买力缓冲）、订单生命周期（卖出冻结、DAY 到期与 GTC 存活、部分成交跨 bar 结转、重复活动订单拒单）、故障开关（撤单竞态、停机窗口阻断提交、只减仓窗口）、限价买入定价 | 删掉则撮合器的成交时机与准入拒单规则失去回归保护，这些行为直接决定回测成交是否含前视。`backtest/t212/faults.py` 的 `FAULT_SWITCH_DEFAULTS` 共 15 个开关，其中 F3/F4/F6/F7/F8/F9/F10/F13/F15 由本文件覆盖，F11/F12/F14 由 `test_review_regressions.py` 覆盖 | pytest 收集 |
| `test_conservatism_and_metrics.py` | 9 项：钉住 2026-08-21 保守性复审的修订——限价单触及不成交而严格穿透才成交（相等是判别点）、跨订单冷却期与同一订单跨 bar 续成交的豁免、陈旧持仓守卫触发 `RuntimeError`、USD 持仓的清算价值列低于 mid 列、持仓时长的均值与中位分列、窗口末仍持仓的区间标 `open_at_end`、图表写出且含预期轨迹 | 删掉则复审修订可被后续改动无声撤销。该文件是唯一同时覆盖 `engine/matching.py`、`engine/metrics.py`、`engine/report.py` 与 `t212/runner.py` 清算估值的用例集 | pytest 收集 |
| `test_same_close.py` | 5 项：`fill_timing=same_close` 下市价单按决策 bar 收盘价 × (1+gap) 当根成交、延迟 120s 超 60s 窗口落到下一开盘且不加 gap、决策键闭市则排队、引擎集成（bar 0 成交且文件名带 `fill-same_close`）、引擎守卫在非 same_close 模式拒绝 at_close 成交 | 收盘价策略口径（用户裁定 2026-08-22）的唯一保护；删掉则同根收盘成交与保守默认之间的边界无测试 | pytest 收集；用 `conftest.py` 的 `daily_ts`/`bar_frame`/`fx_frame`/`faults_off`/`mk_ledger` |
| `test_costs.py` | 13 项：`backtest/t212/costs.py` 的折算与费用——USD 按汇率相除、GBp 除以 100、GBP 直通、未知币种拒绝；FX 费只在 USD 双边收取；印花税三分（伦交所个股买入收 0.5%、同一标的卖出不收、伦交所 ETF 买入不收）；PTM 征费的门槛（本金超过 10,000 英镑才收）与 ETF 开关（`ptm_levy_on_etf` 默认为收，取值未经证实故取保守侧），且按订单只收一次（跨部分成交按累计本金判定）；美股卖出侧费用；worst 档严格劣于 actual 档；点差符号；法国金融交易税只在符合条件的买入侧 | 删掉则费用口径无回归保护。费用键名与真实账户 `walletImpact.taxes` 的枚举同名，是将来与实盘逐项对账的前提 | pytest 收集。本文件不 import `conftest.py`，自带 `CFG` 常量 |
| `test_engine.py` | 6 项：合成数据端到端跑通、同配置两次运行结果一致（确定性）、同步成交触发 `AssertionError` 且信息可读、前视探针臂在锯齿行情上确实赚到钱而普通策略赚不到（探针的判别力本身被检验）、探针关闭时 `next_bar()` 返回 `None`、拒单出现在订单审计里 | 删掉则主循环的四步顺序与无前视断言失去保护。锯齿帧构造成「下一根开盘价恒等于上一根收盘价」，唯一可赚的钱就是提前知道下一根的 bar 内方向，前视与非前视因此可判别 | pytest 收集 |
| `test_feed.py` | 11 项：质量闸接受干净帧、逐项拒绝被污染的行（OHLC 次序、非正价格、负成交量、异常币种）、拒绝重复时间戳；日线跨 BST 边界的对齐（伦敦标的的原始 UTC 时间戳落在前一日 23:00，按本地日期对齐才与美股同日）；交易日历不对称时时间轴保留缺半边的那一天；FX 收盘价只在该 bar 收盘后才可得；查询早于首个可得时刻时拒绝外推而非回填 | 删掉则数据对齐与 FX 无前视两条结构性保证失去回归保护。日线对齐一旦错位一天，全部跨标的日线结论作废 | pytest 收集 |
| `test_ledger_metrics.py` | 9 项：`Decimal` 现金精确（0.1 + 0.2 用 float 会留下尾数）、超卖被拒、现金转负被拒、平均成本释放正确；资本取占用**峰值**而非累计投入、在场天数只数占用为正的日子；平均成本法的逐笔已实现盈亏；带符号中位与绝对偏离中位是两个不同的数；冻结影响可用现金而不影响现金余额 | 删掉则账本精度与业绩率分母口径无保护。占用峰值与累计投入的区分决定全部收益率数字的量级 | pytest 收集。本文件不 import `conftest.py` |
| `test_review_regressions.py` | 16 项：钉住 2026-08-20 对抗性复审的每条发现——混交易所 1h 网格交错 30 分钟时不得提前成交、止损触发状态在部分成交后保持、止损限价单三种情形（可立即执行腿在触碰止损价成交、卖出腿需要触发后的证据、卖出可执行腿在止损价成交）、美股重叠时段随夏令时变化、引擎按场所精度向下取整目标数量、行情陈旧与鉴权中断两个故障开关、1 分钟 bar 上的提交限速、`write_run` 三件产物逐字节相同（用 sha256 比对） | 删掉则每条复审发现都可能被重新引入。逐字节比对用例是基线复现纪律的执行点 | pytest 收集 |
| `test_strategy_and_okx_source.py` | 9 项：策略加载器的正常路径、身份不符拒载、文件缺失、缺 `compute_targets` 入口、`strategy_path` 走 `common/paths.venue_dir`；加密侧 `backtest/okx/data_source.py` 的 `load_klines` 结构与窗口、缺标的报错、日线按 UTC 对齐（与股票的本地日期对齐相反）、`quote_ccy` 白名单可由调用方收紧 | 删掉则引擎与策略、引擎与加密数据布局这两个接缝无保护。加密日线按 UTC 对齐、股票日线按交易所本地日期对齐是两条相反的规则，必须各有用例 | pytest 收集。本文件不 import `conftest.py`，在 `tmp_path` 下自建策略源文件与 parquet 数据湖 |

## 3. 子目录索引

无。

## 4. 依赖关系

读入：无文件读取。全部输入在进程内合成——bar 与汇率帧由 `conftest.py` 的构造函数
生成，`test_strategy_and_okx_source.py` 在 `tmp_path` 下临时写出策略源文件与
parquet 分区再读回。

被测代码：

| 被测模块 | 覆盖它的文件 |
|---|---|
| `backtest/engine/types.py` | `test_broker`、`test_engine`、`test_ledger_metrics`、`test_conservatism_and_metrics`、`test_review_regressions` |
| `backtest/engine/feed.py` | `test_feed`、`test_engine`、`test_conservatism_and_metrics`、`test_strategy_and_okx_source`、`test_review_regressions`、`conftest` |
| `backtest/engine/matching.py` | `test_conservatism_and_metrics`、`test_review_regressions` |
| `backtest/engine/ledger.py` | `test_ledger_metrics`、`conftest` |
| `backtest/engine/engine.py` | `test_engine`、`test_conservatism_and_metrics`、`test_review_regressions` |
| `backtest/engine/metrics.py` | `test_ledger_metrics`、`test_conservatism_and_metrics`、`test_review_regressions` |
| `backtest/engine/results.py` | `test_review_regressions` |
| `backtest/engine/report.py` | `test_conservatism_and_metrics` |
| `backtest/engine/strategy_loader.py` | `test_strategy_and_okx_source` |
| `backtest/engine/broker.py` | 无用例。该文件只有 `typing.Protocol` 声明、无行为，测试无可断言的对象 |
| `backtest/t212/{broker_sim,costs,faults,instruments,runner}.py` | `test_broker`、`test_costs`、`test_conservatism_and_metrics`、`test_review_regressions`、`conftest` |
| `backtest/okx/data_source.py` | `test_strategy_and_okx_source` |
| `trading212/strategy/a0_intraday_v0_0_1.py` | `test_a0_intraday` |
| `common/paths.py`、`common/store.py` | `test_strategy_and_okx_source`（间接） |

写出：见第 5 节。

被谁 import：无生产代码 import 本目录。`conftest.py` 被同目录 5 个测试文件 import，
是本目录内唯一的被 import 项。

## 5. 产出与清理

| 产物 | 落点 | 处置 |
|---|---|---|
| 策略源文件、parquet 分区、结果三件套、图表 HTML | pytest 的 `tmp_path` | 由 pytest 自行回收，项目目录内不落地 |
| `__pycache__/` | 本目录 | 工具产物，按 `CLAUDE.md` §4.2 不得留在项目目录，可随时删除 |

10 个源文件全部必须保留，无过程性文件。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。

2026-08-22 补登 `test_a0_intraday.py`（该文件由并行工作新增于建档过程中，内容以建档时读到的版本为准）。
2026-08-22 新增 `test_same_close.py`（5 项，合计 106 项）。
