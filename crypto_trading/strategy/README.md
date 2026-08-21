# crypto_trading/strategy

## 1. 职责

装 OKX 侧信号的唯一副本。策略必须是纯函数：输入行情快照与持仓，输出目标仓位，不读网络、
不写状态、不下单（`ARCHITECTURE.md` §2.0）。不装下单执行（在
`crypto_trading/execution/`），不装参数取值（在 `crypto_trading/config/strategies/`）。

当前为骨架，无任何策略文件。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 0 字节空文件，把本目录声明为常规 Python 包 | 本目录尚无策略模块，全仓检索无任何导入点。保留理由是包边界与 `CLAUDE.md` §4.4 的模块头 docstring 落点；当前文件为空，该 docstring 尚未写 | 无调用点 |

骨架待实现，将来放什么（契约见 `backtest/engine/strategy_loader.py` 第 11 至 18 行与
`fixplans/framework/06_strategy_plugin.md`）：

1. 文件名 `<name>_v<M>_<m>_<p>.py`，版本号中的点在文件名里写成下划线。
2. 模块内两个常量 `STRATEGY_NAME` 与 `STRATEGY_VERSION`，取值须与文件名一致。不一致时
   `load_strategy()` 抛 `ValueError`，因为文件名谎报身份会把回测结果归因到错误的逻辑版本。
3. 入口函数 `compute_targets(view, portfolio, params) -> dict[str, Decimal]`。
4. 版本语义（`ARCHITECTURE.md` §2.0.1）：MAJOR 为信号逻辑变，MINOR 为参数变，
   PATCH 为重构且须证明输出逐字节不变。起点为 V0.0.1。
5. 已落地的同构实例是 `trading212/strategy/a0_v0_0_1.py`，可作格式参照。

## 3. 子目录索引

无。

## 4. 依赖关系

本目录当前不 import 任何模块，也不被任何模块 import。按 `ARCHITECTURE.md` §2 的单行依赖，
将来只允许 import `common/`。`backtest/` 与 `crypto_trading/execution/` 都从这里 import
同一份信号，禁止各写一份「回测版」与「实盘版」。加载方是
`backtest/engine/strategy_loader.py` 的 `load_strategy("okx", name, version)`，它按
`common/paths.py` 的 `venue_dir("okx")` 加子目录名 `strategy` 定位本目录。

## 5. 产出与清理

无运行产物。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
