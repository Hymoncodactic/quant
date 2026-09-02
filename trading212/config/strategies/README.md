# trading212/config/strategies/ 目录说明

## 1. 职责

装 Trading 212 一侧各策略的**参数基线**，一个策略的一个版本对应一个 yaml 文件，
文件名与 `trading212/strategy/` 下的模块同名同版本。

不装：策略逻辑（在 `trading212/strategy/`）、环境与账户配置（在上一级 `config/`）、
消融臂的参数覆盖（由入口脚本显式指定并记入运行元数据）。

策略体内禁止读配置（`docs/backtest/framework/06_strategy_plugin.md` §参数）：
参数一律由入口层读取一次后经 `params` 传入。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `a0_v0_0_1.yaml` | A0 v0.0.1 的真值参数基线（14 个键）：`trade_symbols`（18 只美股大盘科技股）、`state_symbol: QQQ`、`fx_symbol: GBPUSD=X`、`signal_mode: tsmom252`、`tsmom_lookback: 252`、`trend_ma: 200`、`vol_window: 20`、`vol_pct_threshold: 0.80`、`vol_min_history: 756`、`use_vol_gate`、`use_trend_gate`、`warmup_bars: 260`、`live_from: 2018-01-01`、`slot_headroom: 0.99` | 两个入口脚本把它当作 A0 的唯一参数真值，消融臂由它派生并显式覆盖；删除后两个回测入口在 `yaml.safe_load` 处即失败，且 A0 的定义失去可引用的机器可读副本 | `scripts/20260821_a0_framework_backtest.py:106`（读为 `base_params`，再按 `ARM_OVERRIDES` 派生 a0 / tsmom / ma200 / bh 四臂）；`scripts/20260822_a0_minute_backtest.py:118`（读为 `base`，供 m1 与 d1 两臂共用）；`research/regime_lab/report/make_a0_report_data.py:191`（作为证据件 C-2 计算大小与 md5 前 8 位） |
| `a1_v0_0_1.yaml` | A1 v0.0.1 的真值参数基线（16 个键）：候选池文件、`n_hold: 20`、`band_multiple: 2`、`rebalance_every: 21`、`mom_long/mom_skip: 252/21`、五条准入的阈值（`liq_window`、`min_dollar_volume_usd`、`max_zero_volume_share`、`min_history_bars`、`order_usd_for_participation`、`require_verified_ticker`）、`slot_headroom: 0.99`、`fx_symbol`、`rebalance_anchor` 与 `live_from` | A1 的准入阈值一旦丢失，排名表与实盘名单都无从复现；`require_verified_ticker` 是实盘专属的第五条准入，研究臂显式置 false，两者的差别只在这份文件里可见 | `trading212/ingest/a1_rank.py::_params()`（盘前排名 pass）；`session_cycle.assemble_params()`（B0 的 `a1_params`）；`scripts/20260903_a1_module_backtest.py` 与 `scripts/20260903_b0_module_backtest.py` |
| `b0_v0_0_1.yaml` | B0 v0.0.1 的真值参数基线（7 个键）：`priority: a1`、`a1_band: 0.10`、`slot_headroom: 0.99`、`signal_view_cash_gbp: 1000000`、`sells_first: true`、`fx_symbol`、`live_from`。**不含** `a0_params` 与 `a1_params`：那两层由 `session_cycle.assemble_params()` 从同目录另两份文件拼入，写在这里会成为第二份副本并失同步 | B0 的资金分配规则全在这七个键上；`sells_first` 直接决定提交顺序，是实盘与参考实现口径差异的唯一开关 | `session_cycle.assemble_params()`；`scripts/20260903_b0_module_backtest.py` |

`a0_v0_0_1.yaml` 头部注释指向 `research/decisions/20260820_regime_lf_ruling.md` §3
作为 A0 定义来源。该裁定文件当前**在磁盘上不存在**（`research/decisions/` 下只有
`20260821_a0_framework_comparison.md`、`20260821_backtest_data_sources.md`、
`20260821_paid_data_sources.md`）。此指向为悬空引用，待确认。

## 3. 子目录索引

无。

## 4. 依赖关系

读：无。本目录是被读方。

写：无。

被谁读取：三处，全部以 `ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml"`
的形式硬编码路径（未经 `common/paths.py`）：

1. `scripts/20260821_a0_framework_backtest.py:106`，日线四臂对比回测的入口。
2. `scripts/20260822_a0_minute_backtest.py:118`，分钟线 m1 与日线 d1 对照回测的入口。
3. `research/regime_lab/report/make_a0_report_data.py:191`，报告的证据清单登记项。

命名规则：`<name>_v<M>_<m>_<p>.yaml`，与 `trading212/strategy/<name>_v<M>_<m>_<p>.py`
一一对应（`ARCHITECTURE.md` §2.0.1）。

## 5. 产出与清理

无运行产物。

必须保留：`a0_v0_0_1.yaml`。回测结果文件名带策略名与版本串，
参数基线一旦丢失，既有结果无法复现也无法归因。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
2026-09-03 新增 `a1_v0_0_1.yaml` 与 `b0_v0_0_1.yaml`；参数不再只由脚本硬编码路径读取，
执行层经 `session_cycle.assemble_params()`（接缝 S1）统一装配三层参数。
