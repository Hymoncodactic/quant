# 架构计划：事件驱动回测引擎（从零自建）

## 1. 总体裁定

从零自建单线程、确定性的事件驱动 bar 级引擎，不基于任何现成回测库。
调研了 9 个开源框架（backtesting.py、backtrader、zipline-reloaded、vectorbt、
nautilus_trader、qstrader、PyBroker、lumibot、bt），无一同时满足四项硬需求：
GBP 现金账户、碎股原生支持、非 GBP 标的的 FX 费建模、T212 平台缺陷的忠实模拟。
调研全文（含每框架的架构、前视处理、成本模型、可借鉴与须回避项）存
`data/reference/t212_research_20260820/framework_survey.json`。

### 1.1 bt 库不适用的裁定（本地源码级证据）

| # | 缺陷 | 证据（vendor/bt，commit bd5650a） |
|---|---|---|
| 1 | 无订单对象与订单生命周期，`transact()` 在**当根价格即时成交**，成交价即算权重用的价 | `bt/core.py:1633` `transact()`：直接 `self._position += q` 并按 `self._price` 计 outlay |
| 2 | 现金为单一无币种标量，无法表达 GBP 账户买 USD 标的 | `bt/core.py` `_capital = cy.declare(cy.double)`；`adjust()` 无币种 |
| 3 | 默认整数股（`integer_positions=True` 向下取整），与 T212 碎股相反 | `bt/core.py` Node docstring |
| 4 | 成本只有 `commission_fn(q, p)` 单钩子；`CostModel` 类族未接入执行流 | `bt/core.py:2127` 起 |

结论：bt 回答的是「按月再平衡持有这组权重会怎样」，不能回答「这笔订单在
T212 上会以什么价格、什么延迟、什么费用成交」。仅可借鉴其可组合研究 API 的思路。

## 2. 组件分解（QuantStart 事件驱动模式，四组件）

```
BarFeed ──(ts, bars)──> Engine 主循环
                          │ 1. broker.process_bar(): 结算既往订单的成交
                          │ 2. strategy(view, portfolio): 产出目标持仓
                          │ 3. diff -> OrderSpec -> broker.submit(): 按时间资格排队
                          │ 4. ledger.mark(): 估值与占用采样（在提交之后，
                          │    使本步新冻结的资金进入占用峰值）
                          v
                       结果落地 (trades / equity / meta)
```

撮合资格按**时间**而非合并时间轴的步数：订单的 eligible_ts = 提交键 +
整数个 bar 间隔。依据：美股 1h bar 在 :30 网格、伦敦在 :00 网格，混合时间轴
相邻两步只隔 30 分钟，按步计数会让成交用到决策 bar 尚未形成的收盘信息。
引擎另设日内成交时间断言（fill.ts ≥ submitted_ts + interval）。

| 组件 | 位置 | 职责 | 参考来源 |
|---|---|---|---|
| BarFeed / FxSeries | `backtest/engine/feed.py` | 多标的按时对齐的 bar 流，天然 cutoff | QuantStart DataHandler（drip-feed 消除前视）|
| Ledger | `backtest/engine/ledger.py` | GBP 现金（Decimal）、持仓、占用资金序列、权益曲线 | nautilus 的类型化记账思想，简化为单币现金（依据见 §4）|
| BrokerSim 协议 | `backtest/engine/broker.py` | submit/cancel/on_bar 接口 + 订单簿容器 | lumibot「回测与实盘同一接口」原则 |
| T212 撮合模拟 | `backtest/t212/broker_sim.py` | 订单生命周期、成交定价、费用、延迟、故障注入 | 本目录 03/04 号计划与 `t212_faults/` |
| 绩效 | `backtest/engine/metrics.py` | `05_metrics_reporting.md` 的全部口径 | — |
| 结果落地 | `backtest/engine/results.py` | trades/equity/meta 三件套，原子写 | `common/store.py::write_table` |

策略仍是纯函数（`ARCHITECTURE.md` §2.0：唯一副本在 `<venue>/strategy/`）：
`strategy(view: MarketView, portfolio: PortfolioView, params: dict) -> dict[symbol, Decimal 目标股数]`。
引擎对目标持仓与当前持仓做差得到订单。测试用策略以测试夹具形式放在 `tests/`，
不进 `trading212/strategy/`（避免出现未经预注册流程的「正式策略」）。

## 3. 时序纪律（硬性，引擎断言强制）

1. bar 的 ts = **bar 开始时刻**（本地实证，见 `02_data_layer.md` §3）。
   引擎在「bar t 结束后」的边界上运行：策略可见含 t 在内的全部历史。
2. t 边界产生的订单最早在 t+1 根 bar 成交（backtesting.py 与 backtrader 的
   共识默认；两者文档均已核）。禁止同 bar 成交；不提供 cheat 开关。
3. 限价/止损单的 bar 内触发顺序按 O-H-L-C 判定（nautilus 撮合引擎惯例），
   歧义 bar（同根内两个方向都可触发）取对策略不利的一侧。
4. 市价单闭市提交 → 排队到下一开盘（T212 官方语义，见 `03_order_lifecycle.md`）。

## 4. 现金记账为单一 GBP 的依据

官方文档：API 下单只能以主账户货币执行（docs.trading212.com/api.md
「Orders can be executed only in the primary account currency」），且社区实证
API 卖出所得立即折回主币种（community 87988，laqula 2025-10）。
故模拟器不保留外币现金余额：每笔非 GBP 成交即时换汇，0.15% FX 费嵌入汇率
（机制见 `04_cost_model.md` §2）。多币现金的 nautilus 模式不引入。

## 5. 可借鉴项引用表（来自调研，均已核对到源）

| 借鉴 | 来源 |
|---|---|
| 下一根开盘成交为默认，不设便捷绕过 | backtesting.py 文档「market orders are filled on next bar's open」 |
| 成交量参与上限 + 跨 bar 部分成交 | zipline-reloaded `src/zipline/finance/slippage.py`（本地 vendor 有全文）：`FixedBasisPointsSlippage` 默认 5bp、volume_limit 10%，`LiquidityExceeded` 结转 |
| FeeModel 接口形态 `calc_total_cost(asset, qty, consideration)` | qstrader `broker/fee_model/`（本地 vendor） |
| 买按卖价、卖按买价 + 开市门控 | qstrader `simulated_broker.py` |
| 现金不足硬拒单（反例：qstrader 允许负现金只警告） | qstrader `simulated_broker.py` 中 WARNING 字样为反面教材 |
| Decimal 碎股为原生表示 | bt 整数股与 backtesting.py 需另挂 FractionalBacktest 均为反面教材 |
| walk-forward 与自助法置信区间放引擎外的评估层 | PyBroker；AFML 的 purged CV / deflated Sharpe 同理 |

## 6. 模块规模与规范约束

全部模块遵守 `/quant-code-standards`：文件 ≤400 行、函数 ≤50 行、
模块 docstring 带功能索引与 `__all__`、`# [n]` 分节、注释英文美式拼写、
金额 Decimal、时间 UTC、变体走分派表。故障模型注册表见
`t212_faults/01_fault_catalog.md` §3。

## 7. 依赖边界

1. `backtest/` 不 import `<venue>/execution/`、不发任何网络请求。
2. 读数据只经 `backtest/engine/feed.py`，路径构造经 `common/paths.py`
   （允许注入 `data_root` 以便测试与跨工作树读主仓数据）。
3. 引擎（`backtest/engine/`）零场所口径：年化因子、费率、日历语义全部由
   `backtest/t212/` 注入。OKX 侧将来实现 `backtest/okx/` 同协议适配器。

## 8. 参考仓库清单（vendor/，浅克隆，不入库；本表为重建凭据）

克隆日期 2026-08-20，`git clone --depth 1`。

| 仓库 | origin | commit | 参考点 |
|---|---|---|---|
| backtesting.py | github.com/kernc/backtesting.py | ca2e261 | 下一根开盘成交、_Broker/Order/Trade 生命周期 |
| qstrader | github.com/mhallsmoore/qstrader | 4c59e15 | FeeModel 接口、买卖价不对称、负现金反面教材 |
| zipline-reloaded | github.com/stefan-jansen/zipline-reloaded | 943010b | slippage.py 成交量参与上限与跨 bar 部分成交 |
| nautilus_trader | github.com/nautechsystems/nautilus_trader | e8daa04 | 多币种记账、O-H-L-C bar 内撮合序 |
| bt | github.com/pmorissette/bt | bd5650a | 不适用裁定的证据（§1.1） |
| pybroker | github.com/edtechre/pybroker | db53c67 | walk-forward 与自助法置信区间（评估层） |
| Enhanced-Event-Driven-Backtester | github.com/DavidCico/... | aa26d51 | QuantStart 事件队列骨架的完整小实现 |
| Trading212API (pytrading212) | github.com/HellAmbro/Trading212API | 2ec60ee | 网页端内部接口的订单类型证据；issue 目录 |
| agent-skills | github.com/trading212-labs/agent-skills | aaed5cc | T212 官方 labs：状态表、错误码、限频速查 |

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-20 | 初版 |
| 2026-08-20 | 对抗性审查后修订：主循环 mark 移至提交之后（占用采样含本步冻结）；撮合资格由步数改为时间（修复混交易所 1h 时间轴 30 分钟前视泄漏）；新增 `engine/broker.py`（BrokerSim Protocol）与 `t212/admission.py`（准入检查拆分，守 400 行上限） |
| 2026-08-21 | 策略接入与双线数据源：新增 `engine/strategy_loader.py`（按名版加载 `<venue>/strategy/` 模块，契约见 `06_strategy_plugin.md`）、`okx/data_source.py`（Binance spot klines 读取层）；feed 计价币白名单参数化并纳入 USDT。okx 撮合/成本适配器与账本币种中性化重命名列为待办 |
