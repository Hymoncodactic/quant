# A0 信号层规格

## 0. 指向的交易代码

| 文件 | 角色 | 本规格约束的部分 |
|---|---|---|
| `trading212/strategy/a0_v0_0_1.py` | **信号唯一副本** | 全文。§1–§4 逐条对应其中的函数 |
| `trading212/strategy/a0_intraday_v0_0_1.py` | 日内时序适配层，不含信号 | §5 |
| `trading212/config/strategies/a0_v0_0_1.yaml` | 参数基线 | §6 |
| `trading212/execution/`（待建） | 实盘执行层 | 见 `02_execution.md` |

改动 A0 的信号必须改 `a0_v0_0_1.py` 并按 `/quant-code-standards` §4.5.1 升版本
（MAJOR = 信号逻辑变，MINOR = 参数变，PATCH = 重构且须证明输出逐字节不变）。
⛔ 禁止在执行层复制一份信号：`backtest/` 与 `trading212/execution/` 必须 import
同一个模块（`ARCHITECTURE.md` §2.0）。

## 1. 标的池与可用性

`compute_targets` 第 165、183–188 行。

标的池由 `params["trade_symbols"]` 注入，基线为 18 只美股科技股：
AAPL、AMAT、AMD、AMZN、AVGO、DELL、GOOGL、INTC、LRCX、META、MRVL、MSFT、MU、
NVDA、ORCL、PLTR、TSLA、TSM。

可用判据（`active` 列表）：`view.bars(symbol, max(lookback, trend_ma) + 1)` 的长度
达到 `lookback + 1 = 253`。

实现注记：`warmup_bars: 260` 在当前实现下不生效。判据写作
`if len(bars) < warmup and len(bars) < lookback + 1: continue`，而 `view.bars`
最多返回 253 根，故第一个条件恒真，实际判据是第二个。修改 warmup 语义前须先
改这行，否则参数不产生任何效果。

## 2. 个股信号

`compute_targets` 第 190–197 行，由 `params["signal_mode"]` 分派：

| 模式 | 判据 | 用途 |
|---|---|---|
| `tsmom252` | `closes[-1] > closes[-lookback-1]` | **A0 基线** |
| `ma200` | `closes[-1] >= mean(closes[-trend_ma:])` | 消融臂 |
| `always` | 恒真 | 买入持有对照臂 |

## 3. 市场闸

`_gates_open(view, params)`，第 107–129 行。作用于 `params["state_symbol"]`（QQQ），
任一触发即整个组合清零。

| 闸 | 判据 | 开关 |
|---|---|---|
| 趋势闸 | `closes[-1] < mean(closes[-trend_ma:])` | `use_trend_gate` |
| 波动闸 | Yang-Zhang(`vol_window`) 年化波动率在其**扩展历史**中的经验分位 `(vol <= vol[-1]).mean() >= vol_pct_threshold` | `use_vol_gate` |

波动闸在有效观测数 `< vol_min_history`（756）之前一律放行。

Yang-Zhang 实现在 `_yang_zhang`，第 84–104 行：
`sigma^2 = var(o) + k*var(c) + (1-k)*mean(RS)`，`k = 0.34/(1.34+(n+1)/(n-1))`，
年化乘 252。该函数按向量化 rolling 实现，因为策略每步重算（纯函数、无跨调用状态），
python 循环会使整个运行对历史长度呈二次复杂度。

**扩展分位对历史起点敏感（硬性）**：分位分母是「序列起点到今天」的全部观测。
回测与实盘必须使用同一日线历史起点（现为 **2010-01-04**），否则同一天可能得到
相反的闸判定。实测：状态符号历史截断到最近 2000 根时，2026-07-27 的闸由关变开。
`compute_targets` 向视图索取 `bars(state_symbol, 8000)`，执行层须保证能提供
2010-01-04 起的完整 QQQ 日线。

## 4. 组合构造与定量

`compute_targets` 第 199–225 行。

```
equity_gbp = portfolio.cash_gbp + Σ_i qty_i × price_usd_i / fx     # L204-207
slot_gbp   = equity_gbp / len(active) × slot_headroom              # L209-210
shares     = (slot_gbp × fx / price_usd[sym]).quantize(1e-4, ROUND_DOWN)  # L222-223
```

四条必须一并实现的性质：

1. **分母是可用标的数**（18），不是有信号的只数。无信号的槽位即现金，不做集中。
2. **槽位随权益浮动**：`slot_gbp` 每次决策都用当前权益重算，权益涨则新建仓的槽位涨。
3. **免churn带**（L218–220）：已持仓且信号仍为真时目标 = 当前持仓（含在途），
   **不重算股数**。因此已有仓位不会随权益增长被加仓。
4. **整仓退出**（L216–217）：信号转假或闸关闭时目标置 0，一次性清仓，不分批。

性质 2 与 3 组合的后果：复利只体现在**新建仓**上，已有仓位的规模停在建仓当时。
若要让已有仓位随权益同步放大（全额再平衡），属信号逻辑变更，须升 MAJOR 版本并
单独回测——当前 A0 **不做**这件事。

`fx` 取自 `view.bar(params["fx_symbol"]).close`，语义为 USD per GBP。
FX bar 缺失时（L172–174）返回「维持当前持仓」而不是清仓，避免因行情缺口误平。

## 5. 日内时序适配层

`trading212/strategy/a0_intraday_v0_0_1.py` **不含任何信号逻辑**：它把日内
MarketView 适配成日频视图后，直接调用未改动的 `a0_v0_0_1.compute_targets`。
`tests/backtest/test_a0_intraday.py::test_signal_is_delegated_not_reimplemented`
以打桩断言该委派确实发生。时序规格见 `02_execution.md`。

## 6. 参数

`trading212/config/strategies/a0_v0_0_1.yaml`。策略体内禁止读配置，
一律由入口层读取一次后传入 `params`（`docs/backtest/framework/06_strategy_plugin.md` §2）。

| 键 | 基线值 | 约束 |
|---|---|---|
| `trade_symbols` | 18 只 | 改动即改标的池，须重跑全部回测 |
| `state_symbol` | `QQQ` | 只读数据，不交易 |
| `fx_symbol` | `GBPUSD=X` | 只读数据，不交易 |
| `signal_mode` | `tsmom252` | 见 §2 |
| `tsmom_lookback` | 252 | |
| `trend_ma` | 200 | |
| `vol_window` | 20 | |
| `vol_pct_threshold` | 0.80 | |
| `vol_min_history` | 756 | |
| `use_vol_gate` / `use_trend_gate` | true / true | |
| `warmup_bars` | 260 | 当前不生效，见 §1 |
| `live_from` | `2018-01-01` | 此前空仓 |
| `slot_headroom` | 0.99 | 成本缓冲 |

日内运行额外注入（由入口层给出，见 `02_execution.md`）：
`decision_time_local`、`exchange_tz`、`bars_per_session`。

## 7. 已知限定

1. 标的池是 2026 年仍存续的赢家名单，**带存活者偏差**。无该偏差的下界估计是同一
   结构套在 QQQ 单资产上：年化 15.5% / 回撤 11.3%
   （`research/decisions/20260820_regime_lf_ruling.md` §1 结论 4）。
2. 收益基座是隔夜异象与标的池本身，不是闸层：裸隔夜 EW18 无信号无闸已达 27.3%；
   SPA 检验一致 p=0.53，家族内无配置的平均收益显著高于池的买入持有（同上 §1 结论 3）。
3. 闸层贡献的是回撤压缩，不是收益（同上 §4 归因表）。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-22 | 建档。从 `research/decisions/20260820_regime_lf_ruling.md` 与 `a0_v0_0_1.py` 实现逐行核对写成。 |
