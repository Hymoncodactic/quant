# 01 A1 信号层实施步骤（对话 T）

前置：先读 `00_coordination.md` §1（数据流）、§2（接缝）、§6（共享决定）。
本文件不定义接缝与口径，只写 A1 模块怎么实现。

## 0. 指向的交易代码

| 文件 | 状态 | 本文件约束的部分 |
|---|---|---|
| `trading212/strategy/a1_v0_0_1.py` | 新建 | 全文 |
| `trading212/config/strategies/a1_v0_0_1.yaml` | 新建 | §4 |
| `trading212/strategy/a1_spec.md` | 规格真值，按 `06` §6 修订 | 规则以它为准 |
| `tests/strategy/test_a1_v0_0_1.py` | 新建（目录连同 `__init__.py`、`README.md` 一并建） | §6 |
| `scripts/2026XXXX_a1_module_backtest.py` | 新建 | §5 |

契约：`docs/backtest/framework/06_strategy_plugin.md` §2。模块纯函数，不读盘、不写盘、无跨调用状态。

## 1. 现状事实（逐条已核；实施前须再核一遍，见 `06` §1）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 执行侧工厂只接受一个位置参数 | `trading212/execution/strategy_loader.py` L105-117 |
| 2 | 回测侧 `load_strategy` 不支持工厂，只取 `compute_targets` | `backtest/engine/strategy_loader.py` |
| 3 | SPY 日线 2020-01-02 至 2026-08-28 为 1,673 个场次，与回测口径一致 | 本回合实测 |
| 4 | 候选池 1,500 只中 17 只与 A0 的 18 只重叠（仅 TSM 不在池内） | 本回合实测 |
| 5 | A1 参考臂的回测引擎起点为 2019-06-03，非 2020-01-02 | `scripts/20260902_xsmom_a0_headtohead.py` L215 |
| 6 | 参考臂期末权益 111,347.561961，`a1_spec.md` 正文只记四舍五入的 111,348 | `backtest/results/xsmom_a0_headtohead_causal_20260902.csv` |

## 2. 设计决定

| # | 决定 | 理由 |
|---|---|---|
| D1 | 唯一注入形态：`make_strategy(injection)`，`injection` 为 `00` §2.3 的 dict。取消 `params["panel"]` 与多参工厂 | 事实 1；三份计划共用一种形态 |
| D2 | 实盘消费 `injection["a1_rank"]`（盘前算好）；回测消费 `injection["panel"]`（closes、volumes 两张表）并由模块自行调 `rank_table`。二者只能有一个存在，模块断言之 | 决策窗装不下全池计算 |
| D3 | 排名滞后由 `injection["rank_as_of"]` 表达，模块断言 `rank_as_of <= as_of`。研究口径 `rank_as_of == as_of`，实盘口径 `rank_as_of` 为前一场次 | `00` A3 |
| D4 | 场次序列由 `injection["sessions"]` 注入，**模块不判定何为场次、不区分半日市**；`session_index = sessions.index(as_of)`，重排日判据 `session_index % rebalance_every == 0` | `00` A4；避免模块内嵌日历 |
| D5 | 缓冲带的上期名单只取 `injection["a1_book"]`，不从持仓推导 | `00` A12 |
| D6 | 非重排日返回空字典；重排日返回完整目标（含对退出持仓的 0） | `a1_spec.md` §5 |
| D7 | 被选中但当日无有效价格的名字：不返回目标，其权重**不重分配**（当日闲置），名字仍留在名单内 | 参考实现与 `a1_spec.md` §11.6；重分配会改变 pick 与下期 keep 集，使复现不可达 |
| D8 | 返回字典的插入顺序：先按 `portfolio.positions` 顺序写全部当前持仓的 0，再按 rank 顺序写 pick | `00` A6；引擎按插入顺序提交 |
| D9 | 权益计算中无有效价格的持仓按 0 计（与参考实现一致） | 复现一致性 |

## 3. 模块结构

公开面（`__all__`）：

```
STRATEGY_NAME = "a1";  STRATEGY_VERSION = "0.0.1"
rank_table(closes, volumes, as_of, params) -> DataFrame[symbol, score, eligible, elig_reason, rank]
select(rank_df, book, params) -> list[str]
size(pick, equity_gbp, fx, prices, params) -> dict[str, Decimal]
make_strategy(injection) -> callable(view, portfolio, params) -> dict[str, Decimal]
compute_targets(view, portfolio, params) -> dict[str, Decimal]     # 要求 params["injection"]，仅回测插件路径用
signal_diagnostics(view, portfolio, params, injection) -> dict      # 供 B0 组装其 "a1" 子树
```

私有：`_eligible_mask`、`_score`、`_rebalance_today`、`_equity`。

`rank_table` 是准入与分数的**唯一实现**，盘前 pass（`03` §4）、回测脚本、单测都调它，
不得另写一份。`elig_reason` 枚举：`ok`、`dollar_volume`、`zero_volume`、`history`、
`participation`、`no_ticker`、`no_score`。

`select` 实现 `a1_spec.md` §6 的缓冲带：保留仍在前 `2N` 的 `book` 成员（保持 book 顺序），
空缺按 rank 顺序从未保留者依次补足到 `N`。

## 4. 参数（`a1_v0_0_1.yaml`）

| 键 | 值 | 来源 |
|---|---|---|
| `universe_file` | `data/reference/b0_universe_1500_20260823.json` | `a1_spec.md` §3.1 |
| `n_hold` / `band_multiple` / `rebalance_every` | 20 / 2 / 21 | §5、§6 |
| `mom_long` / `mom_skip` | 252 / 21 | §4 |
| `liq_window` / `min_dollar_volume_usd` / `max_zero_volume_share` / `min_history_bars` / `order_usd_for_participation` | 252 / 1000000 / 0.01 / 300 / 640 | §3.2 |
| `require_verified_ticker` | true | `00` A5（E5），须同步写入 `a1_spec.md` §3.2 |
| `slot_headroom` | 0.99 | §7.2 |
| `fx_symbol` | `GBPUSD=X` | §7.2 |
| `rebalance_anchor` / `live_from` | 由 `00` §2.1 的 S1 覆盖为 `execution.b0_live_from` | |

## 5. 实施步骤

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | 建 `a1_v0_0_1.py`（六节模块头），实现 §3 全部公开面与私有件 | `load_module("a1","0.0.1")` 通过；无中文字符；美式拼写 |
| 2 | 建 yaml 并在 `config/strategies/README.md` 登记 | 键集合与 §4 一致 |
| 3 | 写 `tests/strategy/test_a1_v0_0_1.py`（§6）与目录 `README.md` | 全过 |
| 4 | 建 `scripts/2026XXXX_a1_module_backtest.py`：装载面板与 SPY 场次，构造 `injection`（研究口径：`panel` 形态、`rank_as_of == as_of`、`sessions` 为 SPY 全部场次、`a1_book` 由脚本按重排结果滚动维护），经 `backtest/` 引擎跑 **`EngineConfig(start="2019-06-03", end="2026-08-28")`**、1d、`same_close`、worst、£10,000 | 期末权益 **111,347.561961**（`backtest/results/xsmom_a0_headtohead_causal_20260902.csv` 的 `xsmom N20\|EW\|band+\|gate-` 行）逐位一致 |
| 5 | 把 `archive.STREAMS` 的两行（`a1_plan`、`b0_allocation`）一并加入并合入 main | `00` §5.1、§8 M1 |
| 6 | 登记：`trading212/strategy/README.md` 文件清单与变更记录；`ARCHITECTURE.md` §2.3 表增 A1 行 | §2.0 是原则段，无策略清单，不在那里加 |

## 6. 测试清单（每条注明捕捉的缺陷）

| # | 测试 | 捕捉的缺陷 |
|---|---|---|
| 1 | 因果：把 `as_of` 之后的价格乘 3 加 11，`rank_table` 与目标逐位不变 | 面板未切断 |
| 2 | 分数：构造 253 行价格，手算 `C[t-21]/C[t-252]-1` 与模块一致 | 偏移错位 |
| 3 | 准入五条各自边界（成交额、零成交、历史 299/300、参与率、无 ticker） | 阈值方向、E5 缺失 |
| 4 | 缓冲带：持有的第 30 名保留、第 41 名卖出、空缺从第 21 名补；保留者顺序不变 | §6 语义 |
| 5 | 重排日历：注入 SPY 场次序列，anchor 起第 0、21、42 个场次为重排日，第 22 个不是 | 差一 |
| 6 | 无价名字：不返回目标、权重不重分配、名字仍在名单内 | D7 |
| 7 | 插入顺序：全部 0 目标在任何正数目标之前 | D8 |
| 8 | 非重排日返回空字典；重排日对退出持仓返回 0 | 漏卖 |
| 9 | `a1_book` 为空时首次重排取 rank 前 20；`a1_book` 不受持仓影响（构造持仓与 book 不一致的用例） | D5、A12 |
| 10 | `rank_as_of > as_of` 时断言失败 | D3 |
| 11 | 身份校验：篡改 `STRATEGY_NAME` 后加载器拒载 | 契约 |
| 12 | `signal_diagnostics` 输出含 `00` §2.6 的 `a1` 子树全部键与枚举 | 契约 |
