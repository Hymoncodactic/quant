# 工作记忆

> 用途：跨会话状态载体。**开工前必读，收工前必写**（`CLAUDE.md` §五）。
> 只记状态、裁定、未决、时间线；结论正文写进 `research/` 下各记录件，本文件只指路。
> 时间线**只增不改**；结论变了写新行注明「原值 → 现值 + 依据」，不删旧行。

## 当前状态

- 项目刚建立框架，尚无任何代码实现、无任何数据落地。
- 两条方向均处于「接入前取证」阶段：OKX 与 Trading 212 的 API 事实
  （端点、字段、单位、精度、费率、限频、复权口径）**全部未取证**。
- `secrets/trading212_api_key.txt` 已就位（权限 600），内容未读取、未验证。
- OKX 侧尚无任何密钥。
- 已纳入 git 版本管理，remote `origin` = `https://github.com/Hymoncodactic/quant.git`，
  分支 `main`。同步入口：`python3 scripts/sync_to_git.py`（每日手动执行）。
- **该 GitHub 仓库为 PUBLIC**（用户 2026-08-19 明确裁定保持公开）。今后一切入库内容
  全网可读且被永久缓存：策略逻辑、参数、口径裁定入库前须自行判断是否可公开。
  `.gitignore` 已排除 `secrets/`、`data/`、`logs/`、`reports/`、`*.live.yaml`、`*.paper.yaml`。

## 未决项

| # | 事项 | 需要谁定 | 备注 |
|:--:|---|---|---|
| 1 | OKX API Key 是否已申请、权限位如何设置 | 用户 | 建议研究期只开只读；交易权限单独一把、绑 IP、关提现 |
| 2 | Trading 212 API 的实际能力（是否支持下单、限频、历史数据深度） | 取证 | 属 S4，须查官方文档，不得凭记忆 |
| 3 | 标的池筛选标准（流动性口径、样本数量） | 用户 + 取证 | 写进 `<venue>/config/universe.yaml`，须可复现 |
| 4 | 数据起始时间与周期（要几年、要哪些 bar 周期） | 用户 | 决定下载量级 |
| 5 | 加密方向是现货还是合约 | 用户 | 影响资金费率、杠杆、风控设计 |
| 6 | ~~是否 `git init` 纳入版本管理~~ | 已决 | 2026-08-19 已 init 并首推，见时间线 |

## 时间线

| 时刻（本地） | 动作 |
|---|---|
| 2026-08-19 13:4x | 建项目框架于 `~/Desktop/data`：CLAUDE.md、目录骨架、skills 首批移植 |
| 2026-08-19 13:5x | 用户改根目录为 `~/Desktop/quant`，重排为 `common/ + crypto_trading/ + trading212/ + data/`；纪律与 skills 上移到根 |
| 2026-08-19 13:5x | 停用 OpenClaw 定时任务 `StockTracker_DailySummary`（id a602a589…，原 cron `15 21 * * 1-5` Europe/London）；该 agent 现有 3 个 job 全部 disabled，`cron status` 的 nextWakeAtMs 为 null |
| 2026-08-19 13:5x | `trading212/api_key.txt` 明文密钥移入 `secrets/trading212_api_key.txt`，权限改 600；内容全程未读取 |
| 2026-08-19 14:0x | skills 定稿 10 个；`common/` 基础模块与两侧 config 模板落地 |
| 2026-08-19 14:1x | 用户裁定：`crypto_trading/` 与 `trading212/` 只放交易代码，回测独立到顶层 `backtest/`。信号仍只有一份，放 `<venue>/strategy/`，回测与实盘共用（ARCHITECTURE.md §2.0） |
| 2026-08-19 14:1x | 用户裁定：文件名与目录名一律 ASCII，无例外。`工作记忆.md`→`WORKING_MEMORY.md`、`文件变更日志.md`→`CHANGELOG.md`、`research/{预注册,裁定,笔记}`→`{prereg,decisions,notes}`、数据说明改名 `DATA_SPEC.md` |
| 2026-08-19 14:1x | 用户裁定：不要变更日志纪律。删除 `CHANGELOG.md` 与 CLAUDE.md §三，章节重排（原四→三 资金红线、五→四 目录命名、六→五 工作记忆、七→六 工作约定），各 skill 的补记钩子改为写入本文件的时间线（含回滚要点） |
| 2026-08-19 14:2x | 用户要求：代码须模块化到「改功能能快速定位文件与具体函数」。落为 `/quant-code-standards` §四（10 条：分层、规模上限、模块 docstring 功能索引 + `__all__`、文件内固定结构、可预测命名、分派表代替 if 链、禁复制粘贴、配置外置、五步定位流程、反模式表），并接进 `/verified-dev` 阶段 1.1。`common/` 五个模块已按新规范回填功能索引与 `__all__`。回滚：删 §四 4.2~4.10 与各模块的 `__all__` 块 |
| 2026-08-19 14:4x | 用户裁定三项：(a) GitHub 仓库 `Hymoncodactic/quant` 保持 **PUBLIC**（已告知策略与口径将全网可读，用户明确选择公开）；(b) 强制覆盖远端 2025-06-22 的旧内容；(c) 提交身份用 `Hymoncodactic` / `Hymoncodactic@users.noreply.github.com`（GitHub noreply，避免真实邮箱入库）。三项均为用户本轮明言（S6） |
| 2026-08-19 14:4x | 新增 `scripts/sync_to_git.py`：每日手动执行的 git 同步器。四道闸门 —— origin 必须等于 `EXPECTED_REMOTE`（防推错仓库）、提交身份必须已配置（不代猜）、单文件 >10MB 拒绝、密钥闸（路径模式 + PEM/GitHub/AWS/Slack/Anthropic 等硬令牌 + 「密钥字段名 ∧ 取值像密钥」软规则）。判别力检验：植入 5 个假密钥（UUID 形 OKX key、混类 32 位 secret、PEM 头、`ghp_` 令牌、URL 内嵌口令）全部命中；3 个对照（`secret_name: trading212_api_key` 引用名、`${OKX_API_KEY}`、`CHANGE_ME`）全部放行。检验中发现并修复一处漏检：`SECRET_FIELD_RE` 原带 `\b` 锚，下划线是词字符，导致 `OKX_API_KEY` 这类 `PREFIX_API_KEY` 命名漏网（已去锚）。回滚：删 `scripts/sync_to_git.py` |
| 2026-08-19 14:4x | `git init -b main` + 首次强制推送 `c7e2e0e`（远端原 `c92f475` 被覆盖）。远端实际落地 51 个 blob，经 `gh api .../git/trees/main?recursive=1` 核验与本地暂存清单逐项一致，不含 `secrets/`、`data/`、`logs/`、`reports/`、`*.live.yaml`、`.DS_Store`。回滚：远端 2025-06-22 的内容已不可恢复 |
