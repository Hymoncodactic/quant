# fixplans：回测框架建设计划

## 1. 地位与用法

本目录存放回测框架的建设计划（fixplans）。约束关系：

1. 代码实现以本目录计划为准。实现过程中发现计划不可行或需变更，先改计划文件、
   在文末「变更记录」追加一行，再改代码。禁止代码先行偏离。
2. 本目录与 `research/decisions/`（回测口径裁定）分工：fixplans 管**框架怎么建**，
   decisions 管**回测口径取什么值**。口径冲突时以 decisions 最新裁定为准
   （`CLAUDE.md` §六）。
3. 文档撰写遵守 `CLAUDE.md` §2.1.2（面向机器读者）与 §2.2（客观性）。

## 2. 分类

| 子目录 | 内容 |
|---|---|
| `framework/` | 引擎架构、数据层、订单生命周期、成本模型、绩效与结果落地 |
| `t212_faults/` | Trading 212 平台缺陷目录与故障注入模型（延迟、拒单、限频、卡单等） |
| `validation/` | 无未来函数三件套落地、判别力测试清单、复现纪律 |

## 3. 文档清单与状态

| 文件 | 主题 | 状态 |
|---|---|---|
| `framework/01_architecture.md` | 分层、模块清单、事件循环、开源参考取舍 | 见文件 |
| `framework/02_data_layer.md` | 数据馈送、bar 语义、时区陷阱、多币种 | 见文件 |
| `framework/03_order_lifecycle.md` | T212 订单类型、状态机、模拟器契约 | 见文件 |
| `framework/04_cost_model.md` | 佣金、FX 费、印花税、点差、滑点 | 见文件 |
| `framework/05_metrics_reporting.md` | 业绩率口径、结果文件、报告义务 | 见文件 |
| `framework/06_strategy_plugin.md` | 策略接入契约（单一副本、纯函数、按名版加载）、两线数据源接入 | 见文件 |
| `t212_faults/01_fault_catalog.md` | 平台缺陷实例目录（带来源）与模拟方式 | 见文件 |
| `t212_faults/02_latency_model.md` | 下单与成交延迟模型 | 见文件 |
| `validation/01_no_lookahead.md` | cutoff 断言、前视对照、可测性论证 | 见文件 |
| `validation/02_test_plan.md` | 测试清单（每条含判别力设计）与复现纪律 | 见文件 |

## 4. 命名与流程约定

1. 文件名 ASCII 小写下划线，两位序号前缀表达阅读顺序。
2. 每份计划文件末尾设「变更记录」节，只增不改。
3. 计划内的每个外部事实须带来源（文件:行号 / 接口路径 / 文档 URL 与章节）；
   未证实项显式标注「未证实」并给出证实路径。
