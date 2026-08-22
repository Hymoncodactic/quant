# docs/backtest：回测框架建设计划

## 1. 地位与用法

1. 本目录存放**回测框架自身**的建设计划。约束 `backtest/` 下的代码。
2. 代码实现以本目录计划为准。实现过程中发现计划不可行或需变更，先改计划文件、
   在文末「变更记录」追加一行，再改代码。禁止代码先行偏离。
3. 与相邻目录的分工：

| 目录 | 管什么 | 约束的代码 |
|---|---|---|
| `docs/backtest/` | 回测框架怎么建 | `backtest/` |
| `fixplans/` | 交易代码怎么写 | `trading212/`、`crypto_trading/` |
| `research/decisions/` | 口径取什么值 | 两者的参数与判定 |

口径冲突时以 `research/decisions/` 最新裁定为准（`CLAUDE.md` §六）。

## 2. 文件清单

| 文件 | 主题 |
|---|---|
| `framework/01_architecture.md` | 分层、模块清单、事件循环、开源参考取舍 |
| `framework/02_data_layer.md` | 数据馈送、bar 语义、时区陷阱、多币种 |
| `framework/03_order_lifecycle.md` | T212 订单类型、状态机、模拟器契约 |
| `framework/04_cost_model.md` | 佣金、FX 费、印花税、点差、滑点 |
| `framework/05_metrics_reporting.md` | 业绩率口径、结果文件、报告义务 |
| `framework/06_strategy_plugin.md` | 策略接入契约：单一副本、纯函数、按名版加载 |
| `validation/01_no_lookahead.md` | 无未来函数三件套落地 |
| `validation/02_test_plan.md` | 判别力测试清单、复现纪律 |

## 3. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-22 | 自 `fixplans/framework/` 与 `fixplans/validation/` 迁入。依据 `CLAUDE.md` §六：fixplans 只放交易代码规格。 |
