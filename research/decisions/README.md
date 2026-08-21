# research/decisions

## 1. 职责

装口径裁定：实验跑完之后，对「取哪个口径、采纳还是否决、依据是什么」的书面裁定。
每份裁定与 `research/prereg/` 下同日期前缀的预注册文档成对存在，前者是结果出来后的
判定，后者是结果出来之前冻结的标准。

不装：预注册标准（在 `research/prereg/`）、探索性笔记与文献摘录（在 `research/notes/`）、
框架建设计划（在 `fixplans/`）、回测结果文件（在 `backtest/results/`）。

`CLAUDE.md` §六规定，回测口径以本目录下最新一份裁定为准，不以脚本内的默认值为准。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `20260821_a0_framework_comparison.md` | A0 策略经 T212 回测框架的对比回测裁定，锚定策略模块 `trading212/strategy/a0_v0_0_1.py` 与参数基线 `trading212/config/strategies/a0_v0_0_1.yaml` | A0 的权威口径记录。删除后该策略的回测结论失去判定依据，报告中的数字无法追溯到裁定 | `WORKING_MEMORY.md` 时间线；A0 相关报告的 Reference 清单 |
| `20260821_backtest_data_sources.md` | 双线接入的数据源范围裁定，性质为用户明言（S6），约束 `backtest/` 各 runner 的数据接入配置 | 界定哪些数据源可用于回测。删除后 Binance 作为数据源而非交易场所这一区分失去权威记录 | `backtest/okx/README.md` 引用；`common/paths.py` 的 DATA_SOURCES 与 VENUES 之分以此为据 |
| `20260821_paid_data_sources.md` | 付费数据源裁定：退市股覆盖与深度日内历史的可得性与成本，价格为当日实取页面 | 记录已调查过的数据缺口与报价，避免重复调研。删除后同一问题会被再查一遍 | 后续数据采购决策 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：`research/prereg/` 下的同前缀预注册文档，`backtest/results/` 下的结果文件。
写：无，本目录只存文档。
被谁引用：`WORKING_MEMORY.md` 的时间线、各报告的 Reference 清单、
`backtest/` 下各 README 的口径说明。

## 5. 产出与清理

无运行产物。全部文件为长期留痕件，一律保留。
负面裁定（否决、不通过）与正面裁定同等保留，不得删除
（`/strategy-research` 零、P2 负面结果同样入档）。

## 6. 变更记录

2026-08-22 建立本文件，登记现有三份裁定。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
