# 回测框架：通用接入与回测逻辑说明

本文件是 `backtest/` 的接入入口文档。市场特有的回测思路分别见
`backtest/t212/README.md`（股票）与 `backtest/okx/README.md`（crypto）。
设计裁定与变更历史在 `fixplans/`；本文件只说明**怎么用**与**引擎保证什么**。

## 1. 定位与边界

- `backtest/engine/` 是**场所无关**的事件驱动 bar 级引擎：不 import 任何场所
  或策略代码，不发任何网络请求。场所差异（费用、日历、故障、清算价值）全部
  由 `backtest/<venue>/` 适配层注入。
- 策略是**纯函数**，唯一副本放 `<venue>/strategy/`，回测与将来的实盘执行层
  import 同一份（`ARCHITECTURE.md` §2.0）。
- 数据只读本地 `data/` 落地件（构造经 `common/paths.py`，可注入 `data_root`）。

## 2. 最小接入示例（股票线）

```python
from decimal import Decimal
from backtest.engine.strategy_loader import load_strategy
from backtest.engine.types import EngineConfig
from backtest.t212.runner import run_t212_backtest

strategy = load_strategy("t212", "ma_cross", "0.0.1")   # <venue>/strategy/ma_cross_v0_0_1.py
config = EngineConfig(
    symbols=["AAPL", "VUSA.L"], interval="1h",
    start="2026-01-05", end="2026-08-14",
    initial_cash_gbp=Decimal("10000"),
    arm="baseline", fee_tier="worst", seed=1,
    strategy_name="ma_cross", strategy_version="0.0.1",
)
result, metrics, paths = run_t212_backtest(config, strategy)
```

策略模块契约（常量 + `compute_targets(view, portfolio, params) -> {标的: 目标股数}`）
见 `fixplans/framework/06_strategy_plugin.md`；加载器对名字/版本不符直接拒载。

## 3. 引擎时序（每根 bar 固定四步）

1. `broker.process_bar()`：结算既往订单（过期 → 撤单裁决 → 撮合）。
2. `strategy(view, portfolio)`：视图只含 ts ≤ 当前时刻的 bar（结构性 cutoff）。
3. 目标持仓与（持仓 + 在途）差分 → 市价单提交，数量向下取整到场所精度。
4. `ledger.mark()`：估值与占用采样（在提交之后，冻结资金进占用峰值）。

**无未来函数保证**：t 的决策最早在 t+1 个完整 bar 间隔后成交；撮合资格按
**时间**（非时间轴步数）判定，混交易所交错网格不会提前半个间隔成交；引擎内
置两条运行时断言（同步成交、日内成交时间）+ 两条结构性保证（视图游标、FX
可得性），违反即抛异常终止。前视探针臂（`lookahead_probe=True`）仅供检验，
结果文件强制带 `_PROBE` 标记。

## 4. 保守口径符合性（/backtest-discipline §二 十条）

| # | 项 | 状态 |
|:--:|---|---|
| 1 | 信号 ≤t、成交 ≥t+1 | 已开（时间制资格 + 双断言） |
| 2 | 成交价对手价/下一根开盘 | 已开（开盘 ± 半点差；挂单须**严格穿透**限价才成交，触及不成交） |
| 3 | 滑点固定 bps 可配 | 已开（worst 档 5bp） |
| 4 | 费率双档、主口径最坏档 | 已开（`fee_tier=worst/actual`，档名进文件名） |
| 5 | 容量上限不外推 | 已开（单 bar 成交量 10%，按标的×bar 聚合，余量跨 bar 结转） |
| 6 | 最小量/步进取整 | 已开（向下取整；取整后价值不足即废单） |
| 7 | 冷却期 | 已开（`cooldown_bars`，worst 档默认 2 个 bar 间隔；关小若收益大升即容量幻觉，须降级结论） |
| 8 | 停牌/退市显式处理 | 已开（缺 bar 不静默跳过 + 持仓中断供超过 `max_stale_days_with_position`（默认 5 天）硬报错） |
| 9 | 融资/隔夜成本 | 不适用（现金账户、只多、无杠杆） |
| 10 | 未建模成本列示 | 见 §7 与各市场 README |

## 5. 输出

每轮落 `backtest/results/<stem>.{trades,equity,meta,chart}`，`<stem>` 含策略
名版、arm、窗口、费率档、种子。trades/equity/meta 三件**逐字节可复现**
（同配置重跑 sha256 相同）；chart.html 为派生可视件，不在字节保证内。

- `trades.parquet`：逐笔成交（提交/成交时刻、方向、价格、各成本项分列、订单终态）。
- `equity.parquet`：逐步现金、冻结、占用、`equity_gbp`（mid 诊断列）与
  **`equity_liq_gbp`（清算价值列：按卖侧点差 + 滑点 + FX 费 + 卖侧税逐份估值）**。
- `meta.json`：完整配置、故障开关（含「开而空配」诚实标注）、指标、订单审计、
  git commit。
- `chart.html`：净值曲线（mid 与清算两条）+ 占用曲线 + 在场区间底色 +
  **逐标的开仓持续区间**（下幅横道，虚线 = 窗口结束仍持仓）。

## 6. 指标全集（`compute_metrics`，均为样本内区间统计量）

- 本金口径：`capital_peak_occupied_gbp`（峰值同时占用，含冻结）、
  `single_outlay_max/mean_gbp`、`positions_never_overlapped`
- 收益：`total_return_gbp`、`total_return_rate_on_capital`、
  `annualized_return_rate`（总收益 ÷ 在线日 × 年化因子 ÷ 本金，不复利）；
  **权威净值口径为清算列**：`final_equity_liquidation_gbp`、
  `total_return_liquidation_gbp(/rate)`、`exit_costs_at_end_gbp`
- 风险比率：`sharpe_rf0`、`sortino_rf0`、`annualized_volatility_on_capital`
  （在线日基底，与年化收益同基底）、`max_drawdown_gbp(/on_capital)` 与
  清算版 `max_drawdown_liq_*`、`calmar`、`longest_drawdown_days`
- 交易统计：`closed_trades`、`win_rate`、`profit_factor`、
  `avg_win_over_avg_loss`、`expectancy_gbp`、`largest_win/loss_gbp`、
  `max_consecutive_wins/losses`、`pnl_median_signed_gbp` 与
  `pnl_median_abs_deviation_gbp`（分列，禁混）
- 换手与在场：`turnover_both_legs_on_capital`（双边、含费用）、
  `exposure_time_fraction`、`online_trading_days`
- 持仓时间：`holding_episodes`、`avg_holding_days`、`median_holding_days`、
  `max/min_holding_days`、`holding_episodes_open_at_end`（删失标记）
- 成本分列：`costs_gbp_total`（与真实账户 `walletImpact.taxes` 同枚举，可对账）

## 7. 全框架级已知乐观偏差（结论限定必须携带）

1. 价格为复权价：股息按**毛额**即时再投资——无预扣税（美股约损益 +20~25bp/年
   量级）、无 FX 费、无到账时滞；方向均偏乐观。消除须未复权数据 + 现金股息事件。
2. 挂单成交概率按「严格穿透即全成」，仍无排队模型；bar 数据无 L2 深度。
3. mid 列（`equity_gbp`）不含退出成本，仅诊断用；头条一律用清算列。
4. 故障概率参数（拒单率等）为推断值，敏感性必跑。

## 8. 新市场适配清单

实现四件即可接入引擎：`data_source.py`（落地件 → bar schema）、
`instruments.py`（时区/点差/税类/年化因子）、`costs.py`（费用栈）、
`broker_sim.py`（实现 `backtest/engine/broker.py` 的 BrokerSim 协议）+
一个 runner 组装点。年化因子、日历语义、清算估值全部由适配层注入，
引擎零默认值。

## 9. 文件清单

本节按 `CLAUDE.md` §4.3 登记本目录直属文件。子目录的文件清单在各自 `README.md` 中。

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 把 `backtest` 声明为常规包，模块头说明本包分层 | 全项目 `from backtest.X import ...` 以它为包根；删除后包导入失效 | `tests/backtest/` 全部测试、`scripts/` 下三个回测入口 |
| `README.md` | 本文件。引擎接入方式与引擎对外保证 | 唯一说明「怎么用引擎」与「引擎保证什么」的文档；口径裁定在 `fixplans/`，二者不重复 | 新增市场适配层时的入口文档 |

## 10. 子目录索引

| 子目录 | 内容 | 说明文档 |
|---|---|---|
| `engine/` | 场所无关的事件驱动引擎，11 个模块 | `backtest/engine/README.md` |
| `okx/` | crypto 线适配层，当前只有数据读取 | `backtest/okx/README.md` |
| `t212/` | 股票线适配层，数据、成本、故障、撮合、组装 | `backtest/t212/README.md` |
| `results/` | 每轮结果落地位置 | gitignore，不放 `README.md`（`CLAUDE.md` §4.3），命名与内容见 §5 |

## 11. 变更记录

2026-08-22 按 `CLAUDE.md` §4.3 补 §9 文件清单、§10 子目录索引与本节。原有 §1 至 §8 未改动。
