# research/prereg：预注册（跑结果之前冻结的判定标准）

## 1. 职责

存放在结果产出之前写定并冻结的判定标准：先验立场、判据、实验清单、采纳规则、
特征可测性论证。

本目录不存结果、不存结论、不存事后解释。结论落 `research/decisions/`，调研随笔落
`research/notes/`，结果件落 `backtest/results/`（不入库）。预注册一经写定不得因结果
不合预期而修改；结果出来后追加的臂须在预注册文档中补记追加缘由与先于运行写定的
判据，并降级为探索性（`/backtest-discipline` §四.2）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|

本目录当前无预注册件：除 `.gitkeep` 与本 `README.md` 外无其他文件。

## 3. 子目录索引

无。

## 4. 依赖关系

1. 无代码读写本目录。`common/paths.py` 未为本目录提供路径构造函数，全仓检索未见
   任何模块引用 `research/prereg`。
2. 与 `research/decisions/` 成对：同一日期前缀，预注册在前、裁定在后
   （`CLAUDE.md` §4 目录约束表、`/backtest-discipline` §四.1、
   `/strategy-research` 前置约束第 3 条与 §九收尾清单「每份预注册文档都有配对的
   裁定文档」）。
3. 当前配对状态：`research/decisions/` 下有三份裁定
   （`20260821_backtest_data_sources.md`、`20260821_a0_framework_comparison.md`、
   `20260821_paid_data_sources.md`），本目录无任何同前缀预注册件，三份裁定均无配对
   预注册。该缺口须在下一轮研究开工时按 `/strategy-research` §九补记或说明。
4. 内容要求的来源：`/strategy-research` §七.1 规定四块内容（先验立场、判定标准、
   实验清单、采纳规则）；`fixplans/validation/01_no_lookahead.md` §3 规定策略接入
   回测前须在本目录的预注册文件中逐特征填写可测性论证表（特征、T 时刻取值用到的
   数据、最晚数据时间戳、可测性论证）。
5. 命名：ASCII 小写，日期前缀 `YYYYMMDD_`，与配对裁定同前缀（`CLAUDE.md` §4.1）。
   对抗性证伪轮次的预注册按 `/strategy-research` §七.1 另有
   `round<N>_prereg.md` 形式，用于研究工作区内部分节，不改变本目录的同前缀成对约定。

## 5. 产出与清理

无运行产物。本目录所有内容均为手写留痕件，写定后永久保留，只增不改；被否定的假设
与不通过的臂照常存档（`/backtest-discipline` §四.3）。

`.gitkeep` 为可清理项，理由见 §2。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
