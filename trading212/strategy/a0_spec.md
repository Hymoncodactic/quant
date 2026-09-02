# A0 策略规格：18 只美国科技股的时序动量 + 双市场闸

本文件是 A0 策略的完整、自足的说明。读者预设为没有任何项目背景的程序化读者。
读完本文件应能：不看其他任何文件，精确复现策略的每一个决策；知道每个数字从哪里来；
知道策略已验证到什么程度、它在什么行情下会失效。

| 项 | 值 |
|---|---|
| 策略名 | A0 |
| 命名规则 | 规格文档不带版本号；后续改版由用户指定名称（例如 A0.1）。代码模块按项目代码规范带版本（现为 `a0_v0_0_1.py`），两者不冲突 |
| 交易场所 | Trading 212 Invest（英镑账户，只能做多，支持 0.0001 股碎股） |
| 标的类型 | 美股（USD 计价） |
| 形态 | 每日决策、长期持有的多头组合，带两道市场级清仓开关 |
| 状态 | **已实现并接入实盘执行层**（`trading212/execution/`，常驻调度器，默认 dry-run，实弹提交须用户逐次授权）。现行权威口径为小时频 `same_close`（§7） |
| 信号代码（唯一副本） | `trading212/strategy/a0_v0_0_1.py`；时序变体 `a0_intraday_v0_0_1.py`（只做管道，不改信号） |
| 参数文件 | `trading212/config/strategies/a0_v0_0_1.yaml` |
| 交易代码规格 | `fixplans/t212/a0/01_strategy.md`、`02_execution.md` |

## 1. 一段话说清策略

固定持有名单为 18 只美国大型科技股。每个交易日收盘前对每只股票问一个问题：
「今天的收盘价是否高于 252 个交易日前的收盘价」，是则持有、否则清掉这只。
在此之上有两道针对整个组合的总闸，看的都是纳斯达克 100 ETF（QQQ）：
QQQ 收盘价低于其 200 日均线，或 QQQ 的 20 日 Yang-Zhang 波动率在自身全部历史中
排到最高的 20%，任一成立就全部清仓持现金。资金平均分成「有资格的股票数」份，
一只一份；持有期间不加仓不减仓，信号关闭时整份卖出。

## 2. 术语与记号

| 记号 | 含义 |
|---|---|
| 交易日 / bar | 美股一个常规交易日的日线 bar（开、高、低、收）；小时频变体见 §7.2 |
| `C[s, t]` | 股票 `s` 在交易日 `t` 的收盘价（Yahoo 日线，分红与拆股回溯复权） |
| `t − k` | 交易日 `t` 往前数第 `k` 根 bar（按 bar 计数，不是日历日） |
| 状态标的 | QQQ。只提供数据，永不买卖 |
| 活跃集 `active` | 18 只中当日拥有至少 253 根日线的股票 |
| 槽位 slot | 一只股票获配的资金份额 |
| 免加仓 | 已持有且信号仍开的股票不重定量 |

## 3. 标的池

### 3.1 交易名单（固定，不由策略选择）

AAPL、AMAT、AMD、AMZN、AVGO、DELL、GOOGL、INTC、LRCX、META、MRVL、MSFT、MU、NVDA、
ORCL、PLTR、TSLA、TSM（`a0_v0_0_1.yaml` 的 `trade_symbols`）。

这是一份 2026 年回头看的赢家名单，因此**含存活者偏差**：光拿着不动的回测收益
已经很高，策略的贡献主要在回撤而不在收益（§10）。

### 3.2 辅助数据

| 符号 | 用途 | 频率 | 历史起点 |
|---|---|---|---|
| QQQ | 两道市场闸 | 日线 OHLC | **2010-01-04（硬性，§5.3）** |
| GBPUSD=X | 英镑权益折算与定量 | 与 bar 同频 | 覆盖运行窗口 |
| 18 只标的 | 信号与成交 | 日线；小时频变体另需 1h bar | 至少 253 根 |

## 4. 逐标的信号：TSMOM-252

对活跃集中每只股票，在决策 bar `t`：

```
signal_on[s] = C[s, t] > C[s, t − 252]
```

严格大于。活跃集的准入：`view.bars(s, 253)` 返回的 bar 数 ≥ 253
（代码 L273–275；参数 `warmup_bars = 260` 在基线下不起决定作用，因为视图只索取
253 根，判定由 253 根分支完成）。不足 253 根的股票不在活跃集，既不持有也不分槽。

信号只有开/关两态。价格高出多少不影响权重（§6）。

参数 `signal_mode` 另有 `ma200`（收盘 ≥ 200 日均线）与 `always`（恒开）两档，
仅供消融研究，基线为 `tsmom252`。

## 5. 两道市场闸（针对整个组合，任一关闭即全清仓）

两道闸都只看状态标的 QQQ，与 18 只个股无关。代码 `_gates_open()`（L167–182）。

### 5.1 趋势闸

```
blocking_trend = C[QQQ, t] < SMA200(QQQ, t)      # SMA 为最近 200 根收盘的算术均值
```

QQQ 历史不足 200 根时闸不可评估、视为放行。

### 5.2 波动闸

对 QQQ 日线 OHLC 计算 Yang-Zhang (2000) 年化波动率（代码 `_yang_zhang()` L114–132）：

```
o_t = ln(Open_t / Close_{t−1})            # 隔夜跳空
c_t = ln(Close_t / Open_t)                # 日内开到收
u_t = ln(High_t / Open_t),  d_t = ln(Low_t / Open_t)
rs_t = u_t (u_t − c_t) + d_t (d_t − c_t)  # Rogers-Satchell 项
k = 0.34 / (1.34 + (w + 1)/(w − 1)),  w = 20
var_t = Var_20(o) + k · Var_20(c) + (1 − k) · Mean_20(rs)      # 20 根滚动窗，ddof = 1，负值截为 0
vol_t = sqrt(var_t × 252)
```

然后在 QQQ **自身从历史起点到 `t` 的全部有效 `vol`** 中取扩展分位：

```
pct_t = mean(vol_{τ} ≤ vol_t, τ ≤ t)
blocking_vol = pct_t ≥ 0.80
```

有效观测数 < 756 时闸不可评估、视为放行（`vol_min_history`）。

### 5.3 历史起点是策略参数的一部分（硬性）

分位的分母是「序列起点到今天」的全部观测，因此**历史起点改变会改变闸的判定**。
回测与实盘统一使用 **2010-01-04** 起的完整 QQQ 日线（`fixplans/t212/a0/01_strategy.md` §3）。
代码按 `view.bars(state_symbol, 8000)` 索取，当前约 4,200 根。
已知实测：把历史截断到最近 2,000 根，2026-07-27 的闸判定由「关」翻为「开」
（`research/decisions/20260822_a0_intraday_frequency_ruling.md` §6.3）。

### 5.4 闸的行为特征

闸是全有或全无：关闭当日 18 只全部卖出，重开当日按 §6 重建仓。
它压回撤的代价是错过反弹：闸在急跌后关闭、在波动回落后重开，
若反弹发生在关闭期间则完全错过。实测事件：波动闸分位于 2026-07-30 越过 0.80
（0.8199），在 07-30 至 08-11 的 **9 个决策日**持续关闭（分位 0.8025–0.8214），
08-12 回落到 0.7962 重开；策略于 07-30 决策日全部卖出、08-12 全部买回，
期间 QQQ 收盘由 683.55 升至 723.70（+5.87%）。证据：
`backtest/results/a0_recent1m_1h_actual.trades.parquet`（主副本）的成交日期，
以及用本模块 `_vol_gate_values` 对 QQQ 日线（自 2010-01-04）重算的分位序列。

## 6. 定量

### 6.1 权益与槽位

```
fx        = GBPUSD 最新中间价（USD per GBP）
equity    = 现金(GBP) + Σ_{s ∈ 18 只且在 active} 持仓股数 × C[s, t] / fx
slot      = equity / |active| × 0.99
```

注意：权益只计**本策略名单内**的持仓（代码 L292–296 只对 `price_usd` 中有价格的
符号累加）。在多策略共用账户时，其他策略的持仓对 A0 不可见，外层须自行定量
（`b0_spec.md` §3 的处理方式）。

### 6.2 目标股数（对 active 中每只）

```
want = gates_open AND signal_on[s]
held = 当前持仓股数 + 未成交挂单的带符号股数
if not want:        target[s] = 0                        # 全部卖出
elif held > 0:      target[s] = held                     # 免加仓：不重定量
else:               target[s] = floor_0.0001( slot × fx / C[s, t] )
```

| 常数 | 值 | 依据 |
|---|---|---|
| 余量系数 | 0.99 | `a0_v0_0_1.yaml` `slot_headroom`，为费用与滑移留 1% |
| 股数步进 | 0.0001 股，向下取整 | 代码 `_SHARE_STEP` L100，为本模块自设常数。Trading 212 未公布碎股精度且**按标的不同**：首个实盘场次 INTC 以 4 位小数被拒（精度为 3），执行层 `trading212/execution/risk_gate.py` 以 `QTY_STEP_OVERRIDES = {"INTC": 0.001}` 覆盖。实现者不得把 0.0001 当作场所事实 |
| `live_from` | 基线 "2018-01-01"；各回测按窗口另设 | 该日期之前策略返回空目标（不持仓） |

含义：现金槽位（信号关闭的份额）**不会**挪去加仓其他股票；账户上涨后既有仓位
也不扩大；只有新开仓按当时的 `slot` 定量。因此策略常态下不是满仓：2020-01-02 至 2026-08-28
最坏档独立臂的平均资金占用约 55%（成本口径，1 − 现金/权益），有持仓的交易日占 62%
（`research/xsmom_wide/report/merge_report_data.json` 的 `a0/worst`）。

FX 行情缺失时返回当前持仓（不动）。

## 7. 决策与执行时序

### 7.1 日线版（回测基线）

每个交易日一根 bar。权威成交口径为 `same_close`：在 `t` 日 bar 上决策、按 `t` 日
收盘价成交，并计入收盘临近滑移（最坏档 11 bp，实测档 5 bp）。
这对应实盘「收盘前约一分钟提交市价单」的做法
（`research/decisions/20260822_close_execution_timing.md`）。

### 7.2 小时频版（现行权威，`a0_intraday_v0_0_1.py`）

| 项 | 规则 |
|---|---|
| 决策时刻 | 每个常规场次的 **15:30 bar**（纽约时间）触发一次 |
| 信息集 | 严格早于 15:30 bar 的 bar，即截至 14:30 bar 收盘；15:30 bar 本身尚未完成、不用 |
| 当日价格 | 由当日 09:30–14:30 的 1h bar 合成当日日线（开=首根开，高/低=区间极值，收=14:30 bar 收） |
| 复权拼接 | 日线分区已复权、1h 分区为原始价；用因子 `f = 前一场次日线收盘 / 前一场次 1h 末根收盘` 把当日合成价拉到日线尺度 |
| 半日市 | 无 15:30 bar，不决策；日历取交易所公布，不靠 bar 缺失倒推 |
| 触发守卫 | 状态标的 QQQ 在该时刻确有 bar 才触发（FX 全天候有 bar，会在美股休市日给出同一时钟键） |
| 信号 | 合成好当日日线后**原样调用** `a0_v0_0_1.compute_targets`，信号不重写 |

来源：`fixplans/t212/a0/02_execution.md` §2；`trading212/execution/instruments.py` `DECISION_TIME_NY = "15:30"`。

### 7.3 实盘执行意图

常驻调度器（`trading212/execution/daemon.py`）在 15:30 决策、收盘前约一分钟提交
市价单；默认 `dry_run`，实弹提交须用户当轮授权。Trading 212 无行情接口，
行情来自与回测同源的 Yahoo 日线与 1h bar（换源会破坏 §7.2 的复权拼接）。

## 8. 完整决策伪代码（每个决策时刻执行一次）

```
输入：
  bars[s]    : 18 只各自的日线序列（截至决策时刻，≥253 根者进入 active）
  bars[QQQ]  : QQQ 日线 OHLC，自 2010-01-04 起
  fx         : GBPUSD 最新中间价
  portfolio  : 现金(GBP)、各股票持仓股数、未成交挂单股数
  params     : 见 §12

if today < live_from: return {}
if fx 缺失: return {s: 当前持仓}                     # 不动

# 市场闸（§5）
gates_open = not (Close_QQQ < SMA200_QQQ) and not (pct_YZ20_QQQ >= 0.80 with >= 756 obs)

# 活跃集与信号（§4）
active = [s for s in 18 只 if len(bars[s]) >= 253]
signal_on[s] = bars[s][-1].close > bars[s][-253].close

# 定量（§6）
equity = cash + Σ_{s in active} positions[s] * close[s] / fx     # 只计已成交持仓，不含挂单
slot   = equity / len(active) * 0.99
for s in active:
    want = gates_open and signal_on[s]
    held = positions[s] + pending[s]
    target[s] = 0 if not want else (held if held > 0 else floor_0.0001(slot * fx / close[s]))
return target          # 执行层与现持仓做差下单；不在 target 中的符号不动
```

## 9. 参数表（`a0_v0_0_1.yaml`）

| 键 | 值 | 作用 | 出处 |
|---|---|---|---|
| `trade_symbols` | 18 只 | 固定名单 | §3.1 |
| `state_symbol` | QQQ | 两道闸的数据源 | §5 |
| `fx_symbol` | GBPUSD=X | 定量折算 | §6 |
| `signal_mode` | tsmom252 | 逐标的信号 | §4 |
| `tsmom_lookback` | 252 | 动量比较基准 | Moskowitz-Ooi-Pedersen (2012) 的 12 个月惯例 |
| `trend_ma` | 200 | 趋势闸均线 | Faber (2007) 的 10 月均线惯例（约 200 日） |
| `vol_window` | 20 | YZ 波动窗 | §5.2 |
| `vol_pct_threshold` | 0.80 | 波动闸阈值 | §5.2 |
| `vol_min_history` | 756 | 波动闸生效所需观测 | §5.2 |
| `use_vol_gate` / `use_trend_gate` | true / true | 开关 | 消融用 |
| `warmup_bars` | 260 | 名义准入门槛（基线下由 253 根分支决定） | §4 |
| `live_from` | 2018-01-01（基线） | 起用日 | §6.2 |
| `slot_headroom` | 0.99 | 余量 | §6.1 |

来源分两类：`tsmom_lookback = 252` 与 `trend_ma = 200` 为文献惯例；`vol_window = 20`、
`vol_pct_threshold = 0.80`、`vol_min_history = 756`、`warmup_bars = 260`、`slot_headroom = 0.99`
为**研究阶段的选定值**——yaml 与代码头注均引用 `research/decisions/20260820_regime_lf_ruling.md` §3
作为依据，但该文件已不在磁盘，代码头注自述「依据未验证」；基线配置
`on|tsmom252|p80x0|qqq200|off` 是一族候选配置中的一员。策略没有用优化器拟合的参数，
但上述五个值不能声称与数据无关。

## 10. 已验证的表现（全部为回测，含存活者偏差）

### 10.1 日线版，2018-01-02 至 2026-08-19，本金 £10,000，实测费率档，**next_open 成交口径**

注意：本表来自 2026-08-21 的运行，成交为**次根 bar 开盘价**（`next_open`），无收盘临近滑移；
早于 2026-08-22 改为 `same_close` 的裁定。按 §7.1 的 `same_close` 复现日线版不会得到这些数字，
它们只作为长窗口量级参考。

| 指标 | 值 |
|---|---|
| 期末 | £54,657 |
| CAGR | 20.97% |
| 最大回撤 | 21.85% |
| 夏普 | 1.10 |
| 年化波动 | 19.0% |
| 2022 年 | −10.9% |

来源：`research/decisions/20260821_a0_framework_comparison.md` §2。

### 10.2 小时频 same_close（现行权威），2023-11-07 至 2026-08-21，本金 £10,000，实测档

| 指标 | 值 |
|---|---|
| 期末（清算） | £26,588.65（+165.89%） |
| 年化收益（占用口径） | 29.26% |
| 最大回撤（清算 / 占用） | 17.90%；净值口径 23.23% |
| 夏普（rf=0） | 1.43（曲线口径 1.57） |
| 年化波动 | 20.50% |
| 双边换手 | 5.08 倍/年 |
| 平均 / 中位持仓 | 78.8 天 / 23 天 |
| 最长回撤时长 | 253 天（来源值 252.875） |
| 订单拒绝率 | 39.8%（满仓状态下每日重定量的买单被买入力校验拒绝，次日重试；属口径特征） |

来源：`backtest/results/a0_1h_full_stats_20260822.csv`；`research/decisions/20260822_a0_intraday_frequency_ruling.md` §11.2。
该窗口不是样本外。同窗的日频对照臂在 `same_close` 口径下期末 £26,751.00（+167.51%，
占用口径年化 32.54%），各项均不低于小时频臂，说明高收益是窗口效应而非小时频的贡献
（`next_open` 口径下的日频对照臂 CAGR 42.94% 见同裁定 §3，口径不同不可与本表混用）。

### 10.3 与 A1 的同口径对比，2020-01-02 至 2026-08-28，最坏费率档

| 指标 | A0（£10,000） | A0（£1,000） |
|---|---:|---:|
| 期末 | £48,736 | £4,873 |
| CAGR | 26.94% | 26.94% |
| 最大回撤 | 24.17% | 24.17% |
| 夏普 | 1.28 | 1.28 |
| 月胜率 | 48.1% | 48.1% |

来源：`backtest/results/xsmom_a0_headtohead_causal_20260902.csv`；`a0_a1_merge_summary_a1_20260902.csv`。
两种本金在四位有效数字内成比例（£1,000 期末 ×10 = £48,734.35 对 £48,736.29，差 £1.93），
说明 £1,000 量级无规模约束。

## 11. 风险与限定（必读）

1. **收益的大头来自名单**。同一名单买入持有的收益与 A0 相近；A0 的可证实贡献是回撤
   （2022 年 −10.9% 对科技股大跌）。不要把 A0 的 CAGR 当作策略「本身」的能力。
2. **闸会错过反弹**（§5.4）。高波动的上涨行情里 A0 会站在场外。
3. **扩展分位对历史起点敏感**（§5.3），实盘数据必须自 2010-01-04 起完整。
4. **月胜率只有 48%**：大量月份持币或小幅波动，收益集中在少数月份。
5. **权益只看自己的名单**（§6.1），共用账户时需外层定量。
6. 拒单率高是「每日重定量 + 满仓」的口径特征，实盘表现为同一订单次日重试。
7. 全部回测窗口都不是样本外；策略常数为文献惯例而非拟合。

## 12. 实现对照（现有模块）

| 项 | 现状 |
|---|---|
| 模块 | `trading212/strategy/a0_v0_0_1.py`，`STRATEGY_NAME = "a0"`，`STRATEGY_VERSION = "0.0.1"` |
| 公开面 | `compute_targets(view, portfolio, params) -> dict[str, Decimal]`（纯函数）；`signal_diagnostics(view, params)`（看板只读） |
| 加载 | `backtest/engine/strategy_loader.py::load_strategy("t212", "a0", "0.0.1")` |
| 时序变体 | `a0_intraday_v0_0_1.py::make_strategy(daily_history)`，把 1h 视图适配成日线视图后委托上式 |
| 复现判据 | 1h same_close 实测档 2023-11-07~2026-08-21 期末 £26,588.65；1d same_close 最坏档 2020-01-02~2026-08-28 £10,000 → £48,736.29 |

## 13. 与 A1、B0 的关系

| 维度 | A0 | A1 | B0 |
|---|---|---|---|
| 名单 | 固定 18 只科技股 | 约 1,500 只宽池每月选 20 | A0 与 A1 同时运行于一个账户 |
| 信号 | 时序动量开关 | 横截面动量排名 | 沿用两者 |
| 市场闸 | 有两道 | 无 | 仅 A0 部分受闸 |
| 资金使用 | 常态非满仓 | 满仓 | A1 吸收 A0 未用资金，满仓 |
| 规格 | 本文件 | `a1_spec.md` | `b0_spec.md` |

## 14. 来源

| 内容 | 来源 |
|---|---|
| 信号、闸、定量的实现 | `trading212/strategy/a0_v0_0_1.py`（行号见各节） |
| 参数值 | `trading212/config/strategies/a0_v0_0_1.yaml` |
| 历史起点硬性条款、执行时序 | `fixplans/t212/a0/01_strategy.md` §3；`02_execution.md` §2 |
| 日线版表现 | `research/decisions/20260821_a0_framework_comparison.md` §2 |
| 小时频表现与截断敏感性 | `research/decisions/20260822_a0_intraday_frequency_ruling.md` §6.3、§11.2；`backtest/results/a0_1h_full_stats_20260822.csv` |
| same_close 口径 | `research/decisions/20260822_close_execution_timing.md` |
| 2020–2026 对比 | `backtest/results/xsmom_a0_headtohead_causal_20260902.csv`、`a0_a1_merge_summary_a1_20260902.csv` |
| 文献 | Moskowitz, Ooi & Pedersen (2012) *JFE*；Faber (2007) *JWM*；Yang & Zhang (2000) *J. Business* |
