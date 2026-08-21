# research：研究留痕与研究期实验代码

## 1. 职责

存放研究过程的留痕件与研究期的实验代码，分三类留痕：`prereg/` 存跑结果之前冻结的
判定标准，`decisions/` 存裁定，`notes/` 存调研笔记。

本目录不装可执行策略的信号实现（唯一副本在 `<venue>/strategy/`，`ARCHITECTURE.md`
§2.0）、不装回测引擎（在 `backtest/`）、不装框架建设计划（在 `fixplans/`）、不装
回测结果件（在 `backtest/results/`，不入库）与报告成品（在 `reports/`，不入库）。

分工边界：`fixplans/` 管框架怎么建，`research/decisions/` 管回测口径取什么值；口径
冲突时以 `research/decisions/` 下最新一份裁定为准（`CLAUDE.md` §六，
`fixplans/README.md` §1 第 2 条）。

`CLAUDE.md` §2.2 的文档客观性要求对本目录部分豁免：`notes/` 下的个人研究随笔不受
第二人称与过程记述的限制，`prereg/` 与 `decisions/` 不豁免。

## 2. 文件清单

除本 `README.md` 外，本目录顶层无文件。所有留痕件落在子目录内。

## 3. 子目录索引

| 子目录 | 内容 | 现有件数 | 说明文档 |
|---|---|---|---|
| `prereg/` | 跑结果之前冻结的判定标准 | 0（仅 `.gitkeep`） | `research/prereg/README.md` |
| `decisions/` | 裁定：口径、数据源、策略对比结论 | 3 份 md | 本轮未建立 `README.md` |
| `notes/` | 调研笔记与实证结论 | 2 份 md | 本轮未建立 `README.md` |
| `regime_lab/` | 制度切换研究的实验代码与报告三件套 | 1 个子目录 `report/`，4 个文件 | 本轮未建立 `README.md` |

`decisions/`、`notes/`、`regime_lab/` 及 `regime_lab/report/` 的 `README.md` 属
`CLAUDE.md` §4.3 要求但尚未补齐的项。`regime_lab/` 亦未在 `ARCHITECTURE.md` §1
登记，按 `CLAUDE.md` §4.2.5，未登记目录一律视为过程性产物；该目录是否升格为正式件
待裁定。

两处指向本目录的引用在磁盘上无对应文件，须补建或改写引用：

| 引用点 | 指向 | 磁盘状态 |
|---|---|---|
| `research/regime_lab/report/make_a0_report_data.py` L193、`research/decisions/20260821_a0_framework_comparison.md` L9、`trading212/strategy/a0_v0_0_1.py` L6 | `research/decisions/20260820_regime_lf_ruling.md` | 不存在。该脚本的证据校验函数已如实记录此事：`a0_report_data.json` 中证据 V-1 的字段为 `"found": false` |
| `trading212/strategy/a0_v0_0_1.py` L76 | `research/regime_lab/vol.py` | 不存在。`regime_lab/` 下只有 `report/` 一个子目录。该处仅为注释性溯源，策略模块自带估计量实现，不 import 研究代码，因此不影响运行 |

各子目录现有文件，逐个已读并核对：

| 路径 | 主题 | 谁在引用 |
|---|---|---|
| `decisions/20260821_backtest_data_sources.md` | 双线接入范围裁定：股票三分组 1h、crypto 取 Binance spot 1m 与 um bookTicker，附五条限定 | `backtest/okx/README.md` L14、`WORKING_MEMORY.md` L125 |
| `decisions/20260821_a0_framework_comparison.md` | A0 v0.0.1 经 T212 框架的四臂消融对比，两费率档，净值口径与框架口径并列 | `research/regime_lab/report/make_a0_report_data.py` L194-L195（作为证据 V-2 读取并记哈希）、`WORKING_MEMORY.md` L127 |
| `decisions/20260821_paid_data_sources.md` | 付费数据源裁定：退市股覆盖与深度日内历史的可得性与月成本 | `research/decisions/20260821_backtest_data_sources.md` L23、`backtest/t212/README.md` L54 |
| `notes/20260819_negative_correlation_findings.md` | 实证否定「存在流动性好的负相关标的」这一前提，两侧证据 | `docs/data/t212/DATA_SPEC.md` L27、`research/notes/20260819_t212_execution_and_liquidity.md` L73、`WORKING_MEMORY.md` L65/L97 |
| `notes/20260819_t212_execution_and_liquidity.md` | LSE 标的在 T212 的盘口价差与执行成本实测，做市商义务报价规模 | `backtest/t212/instruments.py` L51/L91、`fixplans/framework/02_data_layer.md` L55、`fixplans/framework/04_cost_model.md` L37/L45/L57、`docs/data/t212/DATA_SPEC.md` L64 |
| `regime_lab/report/make_a0_report_data.py` | 读回测结果件，派生序列与统计量，做 KPI 锚定断言，写出 `a0_report_data.json` | 无模块 import；由命令行按 `python research/regime_lab/report/make_a0_report_data.py` 运行 |
| `regime_lab/report/a0_report_template.html` | 报告模板，含 `__PLOTLY_JS__` 与 `__DATA_JSON__` 两个占位符 | `regime_lab/report/build_a0_report.py` L25 |
| `regime_lab/report/a0_report_data.json` | 上一次运行的数据载荷：`run` / `kpi` / `series`（2,249 日）/ `holding_spans`（26 段）/ `markers`（326 组）/ `worst_drawdown` / `evidence`（8 条）/ `anchor_report` | `regime_lab/report/build_a0_report.py` L26 |
| `regime_lab/report/build_a0_report.py` | 模板加数据加 plotly.min.js 装配为自包含单文件，写到 `reports/a0_cap1000_20260821.html` | 无模块 import；由命令行运行 |

## 4. 依赖关系

1. `common/paths.py` L72 定义 `DIR_RESEARCH = ROOT / "research"` 并在 `__all__`
   （L54）导出。全仓检索显示该常量除 `paths.py` 自身的定义与导出外无调用点，
   业务代码不经它访问本目录。
2. 读取本目录的代码：`research/regime_lab/report/make_a0_report_data.py` 读
   `research/decisions/` 下两份裁定并记录其字节数与 MD5 前八位；
   `research/regime_lab/report/build_a0_report.py` 读同目录的模板与 JSON。
3. 引用本目录结论的代码注释：`backtest/t212/instruments.py` L51/L91、
   `trading212/strategy/a0_v0_0_1.py` L6/L76。
4. 本目录读取的外部数据：`backtest/results/` 下的 equity/trades/meta 结果件与
   `a0_capital_scaling_20260821.csv`（`make_a0_report_data.py` L34 `RESULTS`、L39 `SCALING_CSV`）。
5. 本目录写出的外部产物：`reports/a0_cap1000_20260821.html`
   （`build_a0_report.py` L21 `OUT_HTML`）。
6. 流程侧引用：`.claude/skills/backtest-discipline/SKILL.md` §四.1、
   `.claude/skills/strategy-research/SKILL.md` 前置约束第 3 条（L12-L13）、§七.1 与 §九、
   `fixplans/validation/01_no_lookahead.md` §3、`CLAUDE.md` §六、
   `ARCHITECTURE.md` §1。

## 5. 产出与清理

| 文件 | 性质 | 清理规则 |
|---|---|---|
| `prereg/`、`decisions/`、`notes/` 下的 md | 留痕件，手写 | 永久保留。负面结果同样入档（`/backtest-discipline` §四.3），不得因结论被推翻而删除；结论变更写新件并注明取代关系 |
| `regime_lab/report/a0_report_data.json` | 运行产物，由 `make_a0_report_data.py` 重新生成 | 保留。它记录了该次报告实际使用的数值与八条证据的哈希，删除后已交付报告的可复核性丧失 |
| `regime_lab/report/*.py`、`*.html` | 报告三件套源码与模板 | 保留 |
| `reports/a0_cap1000_20260821.html` | 本目录脚本写出的成品，落在 `reports/`（`.gitignore` L19） | 不入库，可由三件套重新装配 |

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
