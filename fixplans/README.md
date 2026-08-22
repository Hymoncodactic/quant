# fixplans：交易代码规格

## 1. 地位与用法

1. 本目录**只放交易代码的规格**。每份说明都指向具体的交易代码文件（含策略层）：
   `trading212/` 与 `crypto_trading/` 会读取本目录来更新策略与执行代码，
   因此规格中的每一条都要能对应到一个可改的文件与函数。
2. 规格与口径分工：fixplans 管**代码怎么写**，`research/decisions/` 管
   **口径取什么值**。冲突时以 decisions 最新裁定为准（`CLAUDE.md` §六）。
3. 回测框架自身的建设计划**不属交易代码**，在 `docs/backtest/`。
4. 撰写遵守 `CLAUDE.md` §2.1.2（面向机器读者）与 §2.2（客观性）。

## 2. 目录

顶层只有两个，其下按策略或主题分目录。⛔ 不得新开其他顶层目录。

| 目录 | 对应交易代码 |
|---|---|
| `t212/` | `trading212/`（Trading 212 股票线） |
| `crypto/` | `crypto_trading/`（OKX 加密线） |

## 3. 文件清单

| 文件 | 主题 | 指向的交易代码 |
|---|---|---|
| `t212/a0/01_strategy.md` | A0 信号层规格：标的池、动量、双闸、等槽定量 | `trading212/strategy/a0_v0_0_1.py`、`a0_intraday_v0_0_1.py`、`trading212/config/strategies/a0_v0_0_1.yaml` |
| `t212/a0/02_execution.md` | A0 执行层规格：决策时刻、成交时序、数据装配、对账 | `trading212/execution/`（待建）、`trading212/client.py`（待建） |
| `t212/platform/01_fault_catalog.md` | T212 平台缺陷目录 | `trading212/execution/`、`trading212/client.py` |
| `t212/platform/02_latency_model.md` | T212 延迟证据与模型 | 同上 |
| `crypto/README.md` | OKX 线占位 | `crypto_trading/` |

## 4. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-22 | 重构：目录收敛为 `t212/` 与 `crypto/`；`framework/` 与 `validation/` 移至 `docs/backtest/`；`t212_faults/` 移至 `t212/platform/`；A0 规格进 `t212/a0/`。依据 `CLAUDE.md` §六新增条款。 |
