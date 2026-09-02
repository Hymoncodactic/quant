# 02 B0 信号层实施步骤（对话 T）

前置：先读 `00_coordination.md` §1、§2、§6，再读 `01_strategy_a1.md`。
B0 不引入新信号，只组合 A0 与 A1 并分配资金。

## 0. 指向的交易代码

| 文件 | 状态 | 本文件约束的部分 |
|---|---|---|
| `trading212/strategy/b0_v0_0_1.py` | 新建 | 全文 |
| `trading212/config/strategies/b0_v0_0_1.yaml` | 新建 | §4 |
| `trading212/strategy/b0_spec.md` | 规格真值，按 `06` §6 修订 | 规则以它为准 |
| `trading212/strategy/{a0_v0_0_1,a0_intraday_v0_0_1,a1_v0_0_1}.py` | 只读 import | 不改动 |
| `tests/strategy/test_b0_v0_0_1.py` | 新建 | §6 |

## 1. 现状事实（逐条已核）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 执行侧被调用的模块由 `intraday_name` 决定，故 B0 自身即 `intraday_name`，`decide` 的调用行不需要改 | `session_cycle.py` L305；`00` A1 |
| 2 | A0 模块无 `make_strategy`，只有 `compute_targets` | `a0_v0_0_1.py` L90 |
| 3 | A0 的日内壳按 `params["trade_symbols"]` 迭代并在 15:30 且状态标的有 bar 时触发 | `a0_intraday_v0_0_1.py` L217-264 |
| 4 | A0 的权益只计现金与它自己 18 只的持仓 | `a0_v0_0_1.py` L293-296 |
| 5 | `LedgerPortfolioView` 为 frozen dataclass，字段 `cash_gbp`、`available_cash_gbp`、`positions`、`pending_signed_qty` | `shadow_ledger.py` L101-107 |
| 6 | 参考实现按 `set` 迭代写入目标，插入顺序随进程哈希种子变化 | `scripts/20260902_a0_a1_merge_backtest.py` L166-190 |
| 7 | 参考实现的引擎起点为 2010-01-04，`live_from` 覆盖为 2020-01-02 | 同上 L257、L81 |

## 2. 设计决定

| # | 决定 | 理由 |
|---|---|---|
| D1 | `make_strategy(injection)` 单参数；内部构造 A0 腿与 A1 腿：A0 腿 = `a0_intraday_v0_0_1.make_strategy(injection["a0_rows"])`（实盘）或直接用 `a0_v0_0_1.compute_targets`（回测，由 `injection["a0_mode"]` 选择）；A1 腿 = `a1_v0_0_1.make_strategy(injection)` | 事实 1、2；`00` §2 接缝 S7 |
| D2 | A0 信号集经合成视图读取：`dataclasses.replace(portfolio, cash_gbp=Decimal(signal_view_cash_gbp), available_cash_gbp=同值)`，持仓与挂单原样传递；只取返回值中 `q > 0` 的键 | 事实 4、5；`b0_spec.md` §3.1 |
| D3 | A0 的活跃集与价格在实盘取自 `injection["a0_rows"]` 的日线（严格早于今日），在回测取自 1d `view`；由 `injection["a0_mode"]` 分派 | 实盘 `view` 是 1h，按 1h 计 253 根只需约 36 个场次，活跃集会算错 |
| D4 | A1 当期名单 B1 = `injection["a1_book"]`；重排日以 A1 腿返回值中 `q > 0` 的键为新 B1 并写入记录流（写盘由执行层做，模块只在 `signal_diagnostics` 中报告） | `00` A12；模块保持纯函数 |
| D5 | 目标插入顺序按 `00` A6：先全部减仓与清零，再 A0 买入，最后 A1 买入。参数 `sells_first` 默认 true；复现参考实现时置 false 并按 `sorted()` 迭代（消除事实 6 的哈希不确定性） | `00` A6；`b0_spec.md` §9.6 |
| D6 | `priority` 参数化，默认 `"a1"`，两种归属都实现 | `b0_spec.md` §3.2 |
| D7 | 冻结名字规则（实盘专属，须同步写入 `b0_spec.md` §3.4）：对 `injection["thin"]` 内的名字与 `injection["a1_frozen"]` 为真时的全部 A1 名下名字，目标 = 当前持仓（冻结，不买不卖）；其市值计入「已占用」后再算 `C1`，且不计入 `\|a1_sized\|` 分母 | 无当日价格时无法定量；`b0_spec.md` §3.4 原文的「卖出」适用于价格缺失而非行情延迟，二者须在规格中分开 |
| D8 | 免动带守卫：`per > 0`、`close > 0`、`tgt > 0` 三者皆真才比较 10% 带；否则按 D7 冻结或按规格清零 | `b0_spec.md` §3.4 |

## 3. 模块结构

```
STRATEGY_NAME = "b0";  STRATEGY_VERSION = "0.0.1"
_synthetic_view(portfolio, params) -> LedgerPortfolioView
_a0_signal_set(a0_leg, view, portfolio, params) -> set[str]
_a0_active_and_prices(view, injection, params) -> tuple[list[str], dict[str, Decimal]]
_equity(view, portfolio, fx, prices) -> Decimal
_split(s0, book, priority) -> tuple[set, set]
_size_a0(...) / _size_a1(...)
_ordered(targets, held, params) -> dict          # D5
make_strategy(injection) -> callable
compute_targets(view, portfolio, params)          # 回测插件路径，要求 params["injection"]
signal_diagnostics(view, portfolio, params, injection) -> dict    # 接缝 S6，结构见 00 §2.6
```

`compute_targets` 的判定顺序：`as_of` 不在 `injection["sessions"]` 内则返回空字典；FX 缺失返回空字典；
其余情况按 `00` A7 必返回非空目标（18 只 A0 与 A1 名单总有显式目标）。

## 4. 参数（`b0_v0_0_1.yaml`）

| 键 | 值 |
|---|---|
| `priority` | `"a1"` |
| `a1_band` | 0.10 |
| `slot_headroom` | 0.99 |
| `signal_view_cash_gbp` | 1000000 |
| `sells_first` | true |
| `fx_symbol` | `GBPUSD=X` |
| `live_from` | 由 S1 覆盖为 `execution.b0_live_from` |

`a0_params` 与 `a1_params` 不写在本文件里，由接缝 S1 拼入（`00` §2.1）。

## 5. 实施步骤

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | 建 `b0_v0_0_1.py`（六节头），实现 §3 | 加载器身份校验通过 |
| 2 | 建 yaml 与 README 登记 | 键与 §4 一致 |
| 3 | 写 `tests/strategy/test_b0_v0_0_1.py`（§6） | 全过 |
| 4 | **参考实现的确定性前置**：用两个不同的 `PYTHONHASHSEED` 各跑一次 `scripts/20260902_a0_a1_merge_backtest.py`。若两次结果不同，先把该脚本的两处集合迭代改为 `sorted()` 并重录 `b0_spec.md` §9.4 的判据，再进行步 5 | 两次结果一致，或新判据已入档 |
| 5 | 回测复现：`EngineConfig(start="2010-01-04", end="2026-08-28")`、1d、`same_close`、worst、£1,000，`a0_params.live_from` 与 `a1_params.rebalance_anchor` 均为 2020-01-02，`sells_first=false`，A1 腿为研究口径（`rank_as_of == as_of`） | 期末 **17,469.4818**（actual 档 **18,793.7032**）逐位一致 |
| 6 | 打开 `sells_first=true` 与实盘口径重跑，新数字按 `06` §3 写入 `b0_spec.md` §9.4 的「实盘口径」行 | 拒单构成同步记录 |
| 7 | 实现 `signal_diagnostics`，用 `00` §2.6 样例做结构测试 | 键与枚举完整 |
| 8 | 登记 README 与 `ARCHITECTURE.md` §2.3 | |

## 6. 测试清单

| # | 测试 | 捕捉的缺陷 |
|---|---|---|
| 1 | 合成视图：账户现金为 0、A1 持有全部资金时，A0 想买的名字仍进入 S0 | 死锁 |
| 2 | 合成视图不改真实持仓与挂单（frozen dataclass 未被就地修改） | 污染 |
| 3 | `priority=a1` 时重叠名字按 A1 定量；`priority=a0` 时按 A0 定量；两种下 `attribution` 与实际定量一致 | 归属与归因错配 |
| 4 | `C1 = 0.99E − A0 名下价值`；A1 每只 `C1/len(a1_sized)`，手算对照 | 公式 |
| 5 | 免动带：偏离 9% 不动、11% 重定量；`per <= 0` 或无价时按 D7 冻结 | 守卫、除零 |
| 6 | 闸关：S0 为空时 99% 权益进 A1 | 替代语义 |
| 7 | `sells_first=true` 时返回字典的键序中全部减仓在任何买入之前；`false` 时复现参考顺序 | D5 |
| 8 | 清零：持有但不在两边名单的名字目标为 0；18 只未赋值者为 0 | 漏卖 |
| 9 | 非场次日与 FX 缺失返回空字典；正常场次永不返回空字典 | `00` A7 |
| 10 | `_a0_active_and_prices` 在 1h view + `a0_rows` 下得到的活跃集与 1d view 下一致 | D3 |
| 11 | 与参考实现在 30 个构造日上目标逐位一致（`sells_first=false`、`sorted()` 迭代） | 复现 |
