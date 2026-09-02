# research/xsmom_wide/report：A0 + A1 合并回测报告工具

## 1. 职责

把 `scripts/20260902_a0_a1_merge_backtest.py` 写下的六个引擎运行（合并一池 / A0 独立 /
A1 独立 × 最坏 / 实测费率档）派生为报告数据，并拼装成单文件 HTML。不跑回测、不算策略。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `make_merge_report_data.py` | 读引擎结果件，算净值/回撤/月度/资金占用归因/持仓段/换手/成本/相关性与两池合计，写 `merge_report_data.json` | 报告里每个数字的唯一来源 | `build_merge_report.py`（间接） |
| `merge_report_template.html` | Plotly 页面模板：指标总表、净值、回撤、资金占用堆叠、持仓数与区间、月度、持仓时长、成本与相关性、口径与来源 | 删除则无法渲染 | `build_merge_report.py` |
| `build_merge_report.py` | 注入 plotly.min.js 与数据，写 `reports/a0_a1_merge_20260902.html` | 交付件的生成入口 | 人工执行 |
| `merge_report_data.json` | 运行产物 | 重跑 `make_merge_report_data.py` 即重建；入库以便复现页面 | 模板 |

## 3. 子目录索引

无。

## 4. 依赖关系

读 `backtest/results/*_merged_*`、`*_a0_solo_*`、`*_a1_solo_*`、`a0_a1_plan_*.json`，
读 `data/t212/curated/` 的日线用于持仓市值归因；写 `reports/`（gitignore）。

## 5. 产出与清理

`merge_report_data.json` 与 `reports/a0_a1_merge_20260902.html` 均可由脚本重建。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-09-02 | 建目录，三件工具与数据文件 |
