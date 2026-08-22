# research/regime_lab：A0 策略的研究期实验代码与报告产出

## 1. 职责

存放 A0 策略（闸控等槽位多头组合，信号唯一副本在 `trading212/strategy/a0_v0_0_1.py`）
研究期的实验代码与报告产出件。磁盘现状是本目录顶层无任何文件，全部内容位于唯一子目录
`report/`，即 `/html-report` 三件套与其取数产物。

本目录不装策略信号的可执行实现（唯一副本在 `<venue>/strategy/`，`ARCHITECTURE.md`
§2.0）、不装回测引擎（在 `backtest/`）、不装裁定与调研笔记（在 `research/decisions/`
与 `research/notes/`）、不装回测结果件（在 `backtest/results/`，不入库）与报告成品
（在 `reports/`，不入库）。

登记状态：本目录未在 `ARCHITECTURE.md` §1 登记（该文件第 20 行的 `research/` 行只列
`prereg/`、`decisions/`、`notes/` 三项）。按 `CLAUDE.md` §4.2.5，未登记目录视为过程性
产物，本目录是否升格为正式件待裁定；`research/README.md` §3 已记录同一未决项。

## 2. 文件清单

本目录顶层无文件（`find research/regime_lab -type f` 只返回 `report/` 下的四个文件）。
本节按 `CLAUDE.md` §4.3 的固定结构保留，内容为无。

补充记录一处指向本目录顶层、但磁盘上无对应文件的引用：

| 引用点 | 指向 | 磁盘状态 | 后果 |
|---|---|---|---|
| `trading212/strategy/a0_v0_0_1.py` 第 76 行，`_yang_zhang()` 的 docstring 写「Same estimator as the research derivation (research/regime_lab/vol.py)」 | `research/regime_lab/vol.py` | 不存在 | 仅为注释性溯源。同一 docstring 第 77 行说明策略模块不 import 研究代码、估计量在本模块内重述，因此不影响运行，但该溯源链目前指向空处 |

## 3. 子目录索引

| 子目录 | 内容 | 说明文档 |
|---|---|---|
| `report/` | A0 £1,000 本金回测报告的三件套（取数脚本、模板、组装脚本）与取数产物 JSON，共 4 个文件 | `research/regime_lab/report/README.md` |

## 4. 依赖关系

1. 本目录顶层不读取任何文件、不写出任何文件、不含可导入模块。全部读写发生在
   `report/` 内，逐项列在 `research/regime_lab/report/README.md` §4。
2. 被谁 import：无。`report/` 下的两个 `.py` 没有 `__init__.py` 伴随，不构成 Python 包；
   全仓检索（`grep -rn "regime_lab" --include='*.py'`，排除 `.venv/` 与 `.git/`）只命中
   两个脚本自身 docstring 中的运行命令，以及 `trading212/strategy/a0_v0_0_1.py` 第 76 行
   的注释，无任何 import 语句。
3. 引用本目录路径的文档：`research/README.md` 第 30、32、33、42、46 至 49、56、58、75、76 行；
   `trading212/strategy/README.md` 第 23、34 行；`trading212/config/strategies/README.md`
   第 18、41 行；`WORKING_MEMORY.md` 第 130 行。
4. 上级说明文档：`research/README.md`。按 `CLAUDE.md` §4.3 的第 3 个时机要求，改动本目录
   下任何文件前须先读该文件与本文件。

## 5. 产出与清理

本目录顶层无运行产物，无可清理项。`report/` 内的产物与保留理由见
`research/regime_lab/report/README.md` §5。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
