# research/xsmom_wide：宽池横截面动量选股研究

## 1. 职责

装宽池（S&P 1500 ∩ T212）横截面动量选股的研究代码与结果。不装交易代码：
该策略按 2026-09-02 裁定为「探索性通过——高收益高波动变体」，未采纳为低风险配置，
`trading212/strategy/` 下无对应模块。预注册
`research/prereg/20260902_xsmom_wide_prereg.md`（含 §10 因果筛修订），
裁定 `research/decisions/20260902_xsmom_wide_ruling.md`。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `run_study.py` | 全部研究逻辑：面板装载、因果流动性掩码、A0 闸序列、16 配置家族、前视探针、300 随机基线、cutoff 断言 | 研究主入口；删掉则家族结果不可复现 | 人工执行；`scripts/20260902_xsmom_a0_headtohead.py` import 其函数 |
| `results/family_causal_20260902.csv` | 16 配置两段指标（权威口径） | 裁定 §3 的来源 | 裁定 |
| `results/curves_v_causal_20260902.csv` | 各配置 V 段日曲线 | 复核与后续组合设计 | 裁定/合并轮 |
| `results/random_calmars_causal_20260902.npy` | 300 次随机选股的 V 段 Calmar | R5 判定的来源 | 裁定 |
| `results/family_20260902.csv` 等无 causal 后缀件 | **缺陷口径**（全样本筛）的旧结果 | 仅作复审记录，禁止引用为结论 | 裁定 §5 |

## 3. 子目录索引

`results/`：运行产物，重跑 `run_study.py` 即重建（约 40 分钟，含随机基线）。
`report/`：A0 + A1 合并回测的报告工具，见 `report/README.md`。

## 4. 依赖关系

读 `data/reference/b0_universe_1500_20260823.json`、`data/t212/curated/us_equity/**`
与 `us_etf/QQQ`（闸）；import `research/b0_statarb/run_round2._load_frame`。
写 `results/`。被 `scripts/20260902_xsmom_a0_headtohead.py` import
（`load_panels`/`eligibility`/`momentum_scores`/`gate_series`）。

## 5. 产出与清理

`results/` 可重建；裁定引用的 causal 件在裁定被取代前保留；非 causal 旧件
在裁定归档满意后可删（裁定 §5 已完整记录其数字）。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-09-02 | 建目录，完成一轮：16 配置族、探针、随机基线、真实引擎对比 A0。对抗复审 blocker（非因果流动性筛）修复并全量重跑，赢家身份由 band- 翻为 band+ |
| 2026-09-02 | 新增 `report/`：A0+A1 合并（一池打满）与两池独立回测的报告数据派生、模板与拼装 |
