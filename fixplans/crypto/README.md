# crypto：OKX 加密线交易代码规格

## 1. 状态

尚无已定稿的策略。`crypto_trading/strategy/` 为空，`crypto_trading/execution/` 为空。

## 2. 已知约束（进入策略设计前必须满足）

| # | 约束 | 依据 |
|---|---|---|
| 1 | FCA 自 2021-01-06 起禁止英国零售交易加密衍生品，永续合约不可交易，只能取其数据；可交易范围仅现货 | `WORKING_MEMORY.md` 未决项 5 |
| 2 | OKX 撮合与成本适配器尚未建成，`backtest/okx/` 只有 `data_source.py` | `research/decisions/20260821_backtest_data_sources.md` |
| 3 | 年化因子取 365，不得与股票线的 252 混用 | `.claude/skills/backtest-discipline/SKILL.md` §十 |
| 4 | 费率、最小下单量、精度须按 `CLAUDE.md` §1.1 的 S4 现查 OKX 官方文档，不得凭记忆 | `CLAUDE.md` §1.1 |

## 3. 待建文件

本目录在第一个 OKX 策略定稿后按 `t212/a0/` 的形式分目录建档：
`crypto/<strategy>/01_strategy.md` 指向 `crypto_trading/strategy/`，
`crypto/<strategy>/02_execution.md` 指向 `crypto_trading/execution/`。
