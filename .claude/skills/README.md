# `.claude/skills/` 目录说明

## 1. 职责

装本项目的 10 个 skill。每个 skill 占一个同名子目录，目录内只有一份 `SKILL.md`，
内容是某一类任务的执行流程、检查表与验收口径。

不装 always-on 的项目纪律（在根目录 `CLAUDE.md`）、不装路径与分层地图
（在 `ARCHITECTURE.md`）、不装跨会话状态（在 `WORKING_MEMORY.md`）、
不装设计裁定与建设计划（在 `fixplans/` 与 `research/decisions/`）、
不装任何可执行代码、配置或数据。

各 `SKILL.md` 的定位是流程而非纪律：纪律无条件生效不需调用，skill 在触发条件
满足时才载入。两者的分界写在 `CLAUDE.md` 开头「代码开发的完整流程另见 skill：
`/verified-dev`」一行。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `README.md` | 本文件。按 `CLAUDE.md` §4.3 的六节结构给出 10 个 skill 的触发条件索引与相互引用关系 | 删掉它，10 个 skill 的触发条件就只存在于各 `SKILL.md` 的 frontmatter 中，须逐份打开才能回答「哪类任务该用哪一个」；且本目录成为 §4.3 所禁止的「无 `README.md` 的既有目录」 | 按 `/verified-dev` 阶段 1.2 第 1 步，改动本目录下任何文件前须先读本文件 |

除本文件外，本目录下无其他直接文件。10 份 `SKILL.md` 各自位于同名子目录内，
逐份登记在 §3。

## 3. 子目录索引

`CLAUDE.md` §4.3 的豁免表把 `.claude/skills/<name>/` 列为不需要 `README.md` 的目录，
理由是 `SKILL.md` 本身即该目录的说明文件。因此本节指向各子目录的 `SKILL.md`。

触发条件取自各 `SKILL.md` frontmatter 的 `description` 字段与正文的适用范围节。

| 子目录 | 何时触发 | 说明文件 |
|---|---|---|
| `verified-dev/` | 任何新增或修改本项目代码的任务，是代码改动的主流程。触发词：写代码、改代码、改逻辑、修 bug、加功能、重构、改口径、改参数、接接口、写下载器。正文为五阶段加一个前置阶段 0 红线自检：读文档、确认关键假设、写代码、三层验证、留痕 | `verified-dev/SKILL.md` |
| `standardized-bug-fix/` | 修复代码缺陷、处理运行异常、执行系统性排查。触发词：修复流程、问题诊断、修复计划。正文为 7 阶段。明确不适用于纯格式调整、纯文档更新与新功能开发，后者走 `verified-dev` | `standardized-bug-fix/SKILL.md` |
| `quant-code-standards/` | 编写或审查本项目代码、要改某个功能、拆文件、重构、新建模块、做规范检查。触发词：代码规范、命名规范、格式检查、写模块、新建文件、代码审查、模块化、拆文件、重构、定位代码、改功能、这个功能在哪。正文含 §零 语言口径与美式拼写对照表、§一命名、§四模块化与可定位性、§七按严重度的审查触发规则 | `quant-code-standards/SKILL.md` |
| `quant-error-handling/` | 编写与外部 API 交互的代码、构建容错机制、排查运行异常、设计日志、调试长驻进程。触发词：报错、异常、重试、限频、429、超时、断线、日志、排查、定位问题。正文含异常分类表、退避与主动限频、下单幂等、日志规范、问题定位决策树、优雅退出 | `quant-error-handling/SKILL.md` |
| `market-data-pipeline/` | 下载行情、补历史、清洗数据、建数据表、排查数据问题。触发词：下载数据、拉数据、补数据、历史 K 线、数据清洗、数据校验、缺口、数据说明。正文含取证先行清单、raw 与 curated 两层、分区与命名、七条落地校验、缺口登记、`DATA_SPEC.md` 模板 | `market-data-pipeline/SKILL.md` |
| `strategy-research/` | 构思新策略、研究策略方向、找收益来源、扩充策略矩阵、验证某个策略想法是否成立、复现或评估他方策略思路。触发词：研发策略、思考策略、策略方向、新策略、策略研究、找因子、找收益来源、策略矩阵、回测验证想法、这个思路行不行。正文为预注册驱动的 8 阶段循环，含对抗性证伪清单 | `strategy-research/SKILL.md` |
| `backtest-discipline/` | 新建、修改、重跑任何回测，或评估回测结论。触发词：回测、跑回测、回测引擎、backtest、样本外、walk-forward、夏普、最大回撤、参数寻优。正文含无未来函数三件套、保守口径十条硬清单、walk-forward、预注册与诚实归因、业绩率口径、判别力验收、单一权威口径 | `backtest-discipline/SKILL.md` |
| `live-trading-architecture/` | 设计或编写常驻交易进程、接入交易所 WebSocket 与 REST、实现下单执行层、排查执行侧问题。触发词：写执行层、主循环、下单、撤单重报、状态机、对账、接实盘、上线。正文含进程五层分工、主循环骨架、订单状态机与撤单重报四态、持仓对账、五阶段上线路径 | `live-trading-architecture/SKILL.md` |
| `live-trading-risk-check/` | 审查实盘执行代码的风险，或任何改动触及 `crypto_trading/execution/`、`trading212/execution/` 与风控闸时的整表复查，接实盘前必过。触发词：检查一下风险、审一下这段代码、会不会爆仓、帮我看下风控、能不能接实盘。正文为 A 至 E 五组共 31 项检查表加输出格式，A 组任一项不过即禁止接实盘 | `live-trading-risk-check/SKILL.md` |
| `html-report/` | 产出带可视化图表的单文件 HTML 报告，含回测报告、研究报告、数据分析报告、审计报告。触发词：写报告、HTML 报告、可视化报告、交互式报告、plotly 报告、图表报告。正文含三件套架构、锚定断言、首屏用率不用总量、折叠解释卡、术语页与证据溯源页、客观性纪律、视觉 QA | `html-report/SKILL.md` |

## 4. 依赖关系

1. 读什么：各 `SKILL.md` 不读取任何文件，是被读方，由 Claude Code 在触发时载入正文。
2. 写什么：无。本目录不产生任何运行产物。
3. 被谁 import：无。目录内不含 `.py` 或其他可执行源文件。
4. 被谁引用：下表为检索结果（命令
   `grep -rn "<skill 名>" --include='*.md' .`，排除 `.venv`、`.claude/worktrees`、
   `vendor` 与该 skill 自身目录），不是印象。

| skill | 被哪些 skill 引用 | 被哪些项目文档引用 |
|---|---|---|
| `verified-dev` | `backtest-discipline` §前置 2、`market-data-pipeline` §前置 2、`live-trading-architecture` §前置 2、`standardized-bug-fix` §前置 2 与 §适用场景 与 §阶段 2 | `CLAUDE.md` 第 4 行、`ARCHITECTURE.md` §4、`fixplans/validation/02_test_plan.md` 第 3 行 |
| `quant-code-standards` | `verified-dev` 阶段 0 第 8 项、阶段 1.1、阶段 3；`quant-error-handling` §前置 2 与 §3.1；`live-trading-architecture` §前置 2 与 §4.1；`standardized-bug-fix` §前置 3 与 §阶段 3；`backtest-discipline` §前置 2；`market-data-pipeline` §前置 2 | `CLAUDE.md` §二第 5 条（第 89 行）、§2.3 第 2 条（第 161 行）、§4.4（第 354 行）、`ARCHITECTURE.md` §2.0.1 与 §2.1 与 §4、`fixplans/framework/01_architecture.md`、`fixplans/framework/05_metrics_reporting.md`、`fixplans/framework/06_strategy_plugin.md`、`fixplans/validation/02_test_plan.md`、`WORKING_MEMORY.md` 时间线多行 |
| `backtest-discipline` | `strategy-research` §十；`live-trading-architecture` §六上线路径第 1 阶段 | `CLAUDE.md` §二第 6 条（第 90 行）、`ARCHITECTURE.md` §2.2 与 §4、`backtest/README.md` §4、`fixplans/framework/02_data_layer.md`、`fixplans/framework/04_cost_model.md`、`fixplans/framework/05_metrics_reporting.md`、`fixplans/validation/01_no_lookahead.md`、`fixplans/validation/02_test_plan.md` |
| `quant-error-handling` | `market-data-pipeline` §前置 3 与 §三第 2 条；`verified-dev` 阶段 3 第 4 条 | `ARCHITECTURE.md` §4 |
| `live-trading-risk-check` | `live-trading-architecture` §前置 3；`standardized-bug-fix` 阶段 5 第 3 条 | `ARCHITECTURE.md` §4 |
| `html-report` | `backtest-discipline` §九第 4 条 | `CLAUDE.md` §4.2 第 6 条（第 286 行）、`ARCHITECTURE.md` §4、`WORKING_MEMORY.md` 时间线 2026-08-21 行 |
| `strategy-research` | `backtest-discipline` §前置 3 | `ARCHITECTURE.md` §4、`fixplans/framework/06_strategy_plugin.md` 第 56 行 |
| `live-trading-architecture` | 无 | `ARCHITECTURE.md` §4、`research/decisions/20260821_a0_framework_comparison.md` 第 113 行 |
| `standardized-bug-fix` | 无 | `ARCHITECTURE.md` §4 |
| `market-data-pipeline` | 无 | `ARCHITECTURE.md` §4 |

表中「被哪些 skill 引用」为无的三项不构成删除理由：skill 的存在必要性来自它被任务
触发条件命中后载入，不来自被其他文档引用。三者各自覆盖一类无替代流程，
即执行进程架构、缺陷修复 7 阶段、数据管线规范，删掉任一项，对应任务将失去
唯一的流程依据。

## 5. 产出与清理

无运行产物。本目录只含说明文档，运行时不产生任何文件，也不存在可清理项。

| 对象 | 性质 | 清理约定 |
|---|---|---|
| 10 份 `<name>/SKILL.md` | 长期文档，已入库（`git ls-files .claude` 逐份命中） | 必须保留 |
| `README.md`（本文件） | 长期文档，2026-08-22 新建，随下次提交入库 | 必须保留 |

## 6. 变更记录

- 2026-08-22 建立本文件，登记现有文件。
