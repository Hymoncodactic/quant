# 策略接入契约：单一副本、纯函数、按名版加载

## 1. 原则

1. 信号只有一份（`ARCHITECTURE.md` §2.0，硬性）：策略模块放
   `<venue>/strategy/`，回测经 `backtest/engine/strategy_loader.py` 加载，
   将来实盘执行层 import 同一模块。禁止出现「回测版」与「实盘版」两份信号。
2. 引擎完全独立于策略：`backtest/engine/` 零场所、零策略导入，策略以纯函数
   注入（依赖方向：backtest → `<venue>/strategy/` → common，单向）。
3. 策略身份 = 名字 + 版本（`/quant-code-standards` §4.5.1，起点 V0.0.1）。

## 2. 模块契约（t212 与 okx 两线一致）

| 项 | 要求 |
|---|---|
| 文件 | `<venue_dir>/strategy/<name>_v<M>_<m>_<p>.py`（如 `trading212/strategy/ma_cross_v0_0_1.py`） |
| 常量 | `STRATEGY_NAME = "<name>"`、`STRATEGY_VERSION = "<M>.<m>.<p>"`；加载器强制与文件名一致，不一致即拒载 |
| 入口函数 | `compute_targets(view, portfolio, params) -> dict[str, Decimal]` |
| 返回值 | 每标的的**目标持仓股数**（非订单、非权重）；引擎与当前持仓+在途差分后生成订单 |
| 纯函数约束 | 不读网络、不写文件/全局状态、不下单；随机性须由 params 传入种子 |
| 参数 | 一律来自 `<venue>/config/strategies/<name>_v<M>_<m>_<p>.yaml`，由入口层读取一次传入 params；策略体内禁止读配置（§4.8） |

入参对象（由引擎提供，只读）：

| 对象 | 可用接口 | 边界 |
|---|---|---|
| `view: MarketView` | `symbols()`、`bar(sym)`、`bars(sym, n)` | 只含 ts ≤ 当前时刻的 bar（cutoff 结构性保证） |
| `portfolio: PortfolioView` | `cash_gbp`、`available_cash_gbp`、`positions`、`pending_signed_qty` | 快照，不可变 |

## 3. 回测接入方式

```
from backtest.engine.strategy_loader import load_strategy
strategy = load_strategy("t212", name, version)   # 或 "okx"
result, metrics, paths = run_t212_backtest(config, strategy)
```

`EngineConfig.strategy_name / strategy_version` 必须与模块常量一致，
结果文件名由此生成（`framework/05_metrics_reporting.md` §4.3）。

## 4. 两线数据源接入（分场所隔离）

| 线 | 数据源加载器 | 现状 |
|---|---|---|
| 股票（t212） | `backtest/t212/data_source.py`（Yahoo 落地件，5 周期，三分组） | 可用，冒烟已验证 |
| crypto（okx） | `backtest/okx/data_source.py`（Binance 归档 spot klines，9 个 USDT 对，1d/1m，2017-08 起） | 读取层可用；**okx 撮合/成本适配器未建**（待办：OKX 费率档等 S4 取证 + 用户指定数据源组合后另任务实施） |

具体用哪些数据源（分组/标的/周期/数据集）由用户指定后写入回测配置，
不在代码里写死。

## 5. 待办

1. `backtest/okx/` 撮合与成本适配器（费率、最小下单量、精度均须 S4 现查）。
2. 引擎账本字段名带 `_gbp` 后缀，语义为「账户基准货币」；建 okx 适配器时
   做 PATCH 级中性化重命名（须证明 t212 侧输出逐字节不变）。
3. 首个真实策略模块按 `/strategy-research` 预注册流程产出，本契约不含策略内容。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-21 | 初版：策略加载器契约、两线数据源接入现状与待办 |
