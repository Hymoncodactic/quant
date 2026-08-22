# crypto_trading/config/strategies

## 1. 职责

装 OKX 侧策略的参数基线 yaml，一个策略版本一个文件。不装策略代码（在
`crypto_trading/strategy/`），不装连接与风控等运行配置（在上级 `crypto_trading/config/`）。

当前为骨架，除占位文件外无内容。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|

骨架待实现，将来放什么：

1. 文件名 `<name>_v<M>_<m>_<p>.yaml`，与 `crypto_trading/strategy/<name>_v<M>_<m>_<p>.py`
   同名同版本成对（`docs/backtest/framework/06_strategy_plugin.md:21`）。
2. 内容为该策略版本的真实参数基线。消融臂由入口脚本在此基线上做显式且留痕的覆写，
   不另建文件。
3. 参数由入口层读取一次后作为 `params` 传入策略函数，策略体内禁止读配置。
4. 已落地的同构实例是 `trading212/config/strategies/a0_v0_0_1.yaml`，可作格式参照。

## 3. 子目录索引

无。

## 4. 依赖关系

本目录不 import 任何模块，当前也无任何模块读取本目录。
`backtest/engine/strategy_loader.py:9` 明确参数加载不由加载器负责，而由入口层从
`<venue>/config/strategies/` 读取，因此将来的读取方是回测与执行的入口脚本。

## 5. 产出与清理

无运行产物。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
