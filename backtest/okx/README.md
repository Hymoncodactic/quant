# crypto 线（OKX）回测思路

接入方式见 `backtest/README.md`。本线**当前只有数据读取层**
（`data_source.py`，把 Binance 归档 spot K 线转换为引擎 bar schema）；
撮合/成本适配器未建，建成前本线不产出任何回测结论。

## 1. 现状与数据（用户 2026-08-21 裁定接入的源）

| 源 | 存量 | 用途 |
|---|---|---|
| binance spot **1m K 线** | 9 个 USDT 对，2017-08 至今 | 价格主序列 |
| binance um **bookTicker L1** | BTC/ETH，320 天 81.9 亿行 | 点差/滑点校准与 L1 特征，**不是**可交易标的的价格主序列 |

裁定与限定：`research/decisions/20260821_backtest_data_sources.md`。
Binance 是数据源不是场所（`common/paths.py` DATA_SOURCES vs VENUES）；
下单场所是 OKX 现货。

## 2. 与股票线的口径差异（建适配器时逐条落实）

| 维度 | 股票（t212） | crypto（okx） |
|---|---|---|
| 年化因子 | 252 | **365**（7×24），不得混用 |
| 日历 | 由 bar 存在性表达假日 | 无休市；日线对齐用 **UTC 日期**（时区映射传 UTC，禁复用美股映射——00:00 UTC 会被 NY 时区错移一天，已有测试钉死） |
| 计价/账本 | GBP，USD 标的过 FX 费 | USDT 单币，无 FX 层；稳定币脱锚风险写入限定 |
| 成本 | 零佣金 + 点差 + 税 | **maker/taker 费率分档**（随 30 天量与持仓 OKB 变动）；点差用 bookTicker 实测校准而非推断 |
| 隔夜成本 | 不适用（现金账户） | 现货不适用；合约不可交易（FCA 禁英国零售加密衍生品），资金费率仅作数据特征 |
| 故障模型 | T212 目录 16 类 | 须另行取证：OKX 维护窗、限频、撤单语义、精度/最小下单量（lotSz/minSz/tickSz） |
| 数据陷阱 | 复权/退市 | **下架币幸存者偏差**（当前 9 对为现存对）；早期低流动性段不可用，窗口起点须逐对裁定 |

## 3. 建适配器的先决条件（S4，全部现查官方，禁凭记忆）

1. OKX 现货费率档表（maker/taker × 用户等级）与本账户实际档位。
2. `GET /api/v5/public/instruments`（SPOT）的 lotSz / minSz / tickSz 实际返回。
3. 限频表与撤单/改单语义、维护窗公告机制。
4. 由 bookTicker 生成各对点差分布的校准脚本（替代股票线的人工点差表）。

取证落 `data/reference/`，常量注明出处后方可写 `costs.py` / `broker_sim.py`
（模块集与股票线同构，实现 `backtest/engine/broker.py` 的 BrokerSim 协议，
清算估值闭包按卖侧 taker 费构造）。

## 4. 规划中的本线保守口径特化

- 权威档 = taker 全额费率 + bookTicker 校准点差的保守分位（如 P75）+ 滑点。
- 1m 主序列下延迟模型有完整区分度（秒级延迟 ≈ 1 bar）。
- 深度不可得：沿用 10% 成交量参与上限 + 冷却期，敏感性同股票线。

## 5. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 把 `backtest.okx` 声明为常规包 | 包边界；删除后 `backtest.okx.data_source` 无法导入 | `tests/backtest/test_strategy_and_okx_source.py` |
| `data_source.py` | 读 Binance 归档 spot K 线（`data/binance/curated/`，路径经 `common/paths`，可注入 `data_root`），转成引擎 bar schema | 本线目前唯一的实现件。删除后 crypto 线无任何数据入口，`test_strategy_and_okx_source.py` 失败 | `tests/backtest/test_strategy_and_okx_source.py`；将来的 okx runner |
| `README.md` | 本文件。本线现状、与股票线的口径差异、建适配器的先决条件 | 记录本线为何尚不产出结论，以及建适配器前必须现查的 S4 事实清单 | 建 okx 撮合与成本适配器时的入口文档 |

尚未建立的文件：`instruments.py`（精度、最小下单量、维护窗）、`costs.py`（maker/taker
分档、资金费率）、`broker_sim.py`（撮合模拟）、`runner.py`（组装点）。
建立条件见 §3，全部依赖现查 OKX 官方文档。

## 6. 变更记录

2026-08-22 按 `CLAUDE.md` §4.3 补 §5 文件清单与本节。原有 §1 至 §4 未改动。
