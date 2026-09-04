# `.agents/skills/` 目录说明

## 1. 职责

存放本项目供 Codex 自动发现的 10 个技能。每个技能目录以 `SKILL.md` 为入口；不存放
项目纪律、工作状态、业务代码或运行数据。

## 2. 文件清单

| 文件名 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `README.md` | 登记技能索引与迁移边界 | 删除后无法集中核对技能集合与职责 | 修改或审查项目技能的会话 |

本目录没有其他直属文件。

## 3. 子目录索引

| 子目录 | 何时触发 | 说明文件 |
|---|---|---|
| `verified-dev/` | 新增或修改项目代码 | `verified-dev/SKILL.md` |
| `standardized-bug-fix/` | 系统诊断与修复代码缺陷 | `standardized-bug-fix/SKILL.md` |
| `quant-code-standards/` | 编写、审查、拆分或重构项目代码 | `quant-code-standards/SKILL.md` |
| `quant-error-handling/` | 外部 API、重试、限频、日志与故障定位 | `quant-error-handling/SKILL.md` |
| `market-data-pipeline/` | 行情下载、清洗、校验与缺口管理 | `market-data-pipeline/SKILL.md` |
| `strategy-research/` | 预注册驱动的策略研究 | `strategy-research/SKILL.md` |
| `backtest-discipline/` | 新建、修改、重跑或评估回测 | `backtest-discipline/SKILL.md` |
| `live-trading-architecture/` | 实盘执行进程与订单状态机 | `live-trading-architecture/SKILL.md` |
| `live-trading-risk-check/` | 实盘执行代码与风控闸终审 | `live-trading-risk-check/SKILL.md` |
| `html-report/` | 交互式单文件 HTML 报告 | `html-report/SKILL.md` |

各技能子目录由 `SKILL.md` 自身承担说明职责，不另设 `README.md`。

## 4. 依赖关系

技能由 Codex 按 frontmatter 的 `description` 自动路由。各技能引用根目录 `AGENTS.md`，
相互依赖关系保留自 `.claude/skills/` 原件。

## 5. 产出与清理

本目录不产生运行时文件。技能是长期文档；迁移验证要求除 `CLAUDE.md` 到 `AGENTS.md`
及代理专属路径替换外，内容无差异。

## 6. 变更记录

- 2026-09-04 从 `.claude/skills/` 迁移 10 个技能并建立集中索引。
