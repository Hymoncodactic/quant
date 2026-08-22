# research/b0_statarb：B0 统计套利配对策略研究

## 1. 职责

装 B0（配对交易 / 价差回归）的研究代码与结果。**不装**交易代码：B0 未通过采纳规则，
`trading212/strategy/` 下没有也不应有 b0 模块；若将来通过，规格才进 `fixplans/t212/b0/`。

预注册 `research/prereg/20260823_b0_statarb_prereg.md`，
裁定 `research/decisions/20260823_b0_statarb_ruling.md`，两者同前缀成对。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `data.py` | 把逐标的日线分区读成对齐的收盘价面板，并施加流动性下限 | 删掉则无输入面板 | `run_study.py`、`diagnose.py` |
| `pairs.py` | 配对选择：距离法（Gatev 2006）与 Engle-Granger 协整；半衰期过滤 | 删掉则无法选对 | `run_study.py` |
| `engine.py` | 价差 z 化、三变体状态机、成本，产出单对日收益 | 删掉则无信号与撮合 | `run_study.py` |
| `run_study.py` | 滚动形成期/交易期驱动，聚合成组合并出汇总表 | 研究主入口 | 人工执行 |
| `diagnose.py` | 回答预注册怀疑点：市场回归、与 A0 相关性、分年、分半 | 主结论「长仓变体只是 beta」由它给出 | 人工执行 |
| `results/summary.csv` | 18 个配置的年化、夏普、回撤、NW t | 裁定 §5.1 的来源 | 裁定 |
| `results/market_regression.csv` | 每个配置对 SPY 的 alpha/beta 与 t | 裁定 §5.2 的来源 | 裁定 |
| `results/sensitivity.csv` | 入场阈值 × 形成期的 6 格扫描 | 裁定 §5.5 的来源 | 裁定 |
| `results/daily_returns.csv` | 每个配置的日收益序列 | `diagnose.py` 的输入 | diagnose |
| `results/windows_*.json` | 每个交易窗选中的配对数与样例 | 复核选对过程 | 人工 |

## 3. 子目录索引

| 子目录 | 说明 |
|---|---|
| `results/` | 运行产物，见上表。可重跑重建，不必保留历史版本 |

## 4. 依赖关系

读 `data/t212/curated/us_equity/**` 与 `data/reference/b0_universe_20260823.json`；
读 `data/t212/curated/us_etf/SPY/1d/`（市场回归）与
`backtest/results/a0_intraday_v0_0_1_1h_same_close_actual_*`（与 A0 的相关性）。
写 `results/`。不被任何交易代码 import。

数据落地由 `scripts/20260823_ingest_b0_universe.py` 完成，日常更新并入
`scripts/update_data.py` 的 `_update_b0_universe()`。

## 5. 产出与清理

`results/` 全部为运行产物，`run_study.py` 与 `diagnose.py` 重跑即重建。
裁定引用的数字全部来自这些文件，因此在裁定被取代之前不要删除。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-08-23 | 建目录。完成一轮完整研究：502 只标的池、18 配置、6 格敏感性；结论为否决（alpha 显著为正者 0/18）。修复止损未锁定导致的换手虚增缺陷 |
