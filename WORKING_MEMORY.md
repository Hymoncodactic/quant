# 工作记忆

> 用途：跨会话状态载体。**开工前必读，收工前必写**（`CLAUDE.md` §五）。
> 只记状态、裁定、未决、时间线；结论正文写进 `research/` 下各记录件，本文件只指路。
> 时间线**只增不改**；结论变了写新行注明「原值 → 现值 + 依据」，不删旧行。

## 当前状态

- 框架完成。Python 3.11.15 venv 于 `.venv/`，依赖已装。
- **数据管线已跑通并落地**（2026-08-19）：加密走 Binance 归档，股票走日线。
  清单随时用 `./.venv/bin/python scripts/data_inventory.py` 生成。
- **已证实的关键环境事实**（本机实测，非推断）：
  - `api.binance.com` / `fapi` / `dapi` 从本机返回 **HTTP 451**（英国地理封锁，
    响应体明示 "restricted location"）。
  - `data.binance.vision`（批量归档）、`data-api.binance.vision`（只读行情 REST）、
    `wss://data-stream.binance.vision:9443`（行情 WS）**三者均可达**，无鉴权。
  - **L2 订单簿实时重建已验证可行**：25 秒录制零序列缺口，5000 档快照 + 100ms
    增量流。历史 L2 在 Binance 免费归档中**完全不存在**（现货连 bookTicker 都没有）。
  - 现货归档只有 trades / aggTrades / klines 三类；bookDepth / bookTicker / metrics /
    fundingRate 等**只存在于期货**。
- 两条方向的交易接口仍未接：OKX 无密钥；T212 API 已确认**只有交易接口、零行情接口**。
- **带宽是真正的约束，不是磁盘**：到 data.binance.vision 实测约 1.6 MB/s，
  且并发不提升（单连接 1.66、4 连接 1.54 MB/s）。灌满 500 GB 需约 90 小时。
  选数据集应按「每小时下载能换到多少研究价值」排序，不是按 GB。
- 本机内存仅 8 GB；bookTicker 单日 2665 万行，解析峰值约 1 GB/worker，故 workers=4。
- **bookTicker 已 100% 完成**：640/640 文件，BTC+ETH 各 320 天（2023-05-16 ~ 2024-03-30），
  81.9 亿行，66.7 GB，零缺口零交叉盘口。该数据集已停更，不再需要更新。
- **日常更新入口**：双击根目录 `update_data.command`，或
  `./.venv/bin/python -u scripts/update_data.py`。可反复运行、可随时中断。
- 已纳入 git 版本管理，remote `origin` = `https://github.com/Hymoncodactic/quant.git`，
  分支 `main`。同步入口：`python3 scripts/sync_to_git.py`（每日手动执行）。
- **该 GitHub 仓库为 PUBLIC**（用户 2026-08-19 明确裁定保持公开）。今后一切入库内容
  全网可读且被永久缓存：策略逻辑、参数、口径裁定入库前须自行判断是否可公开。
  `.gitignore` 已排除 `secrets/`、`/data/`、`/logs/`、`/reports/`、`*.live.yaml`、`*.paper.yaml`。
- **字节与说明分离已落地**（2026-08-20 裁定）：`data/` 整棵不入库、预期迁往外置盘；
  说明与重建凭据一律在仓库内 `docs/data/<source>/`（`DATA_SPEC.md` + `MANIFEST.jsonl`）。
  两份 DATA_SPEC 已从 `data/*/curated/` 移出。布局见 `ARCHITECTURE.md` §3。
- **manifest 已生成**：binance 9,317 分区 / 64.0 GiB / 90.16 亿行，t212 3,784 分区 / 167.7 万行。
  重建入口 `scripts/build_data_manifest.py`，全量扫描约 4 秒（只读 parquet footer），
  `sync_to_git.command` 提交前自动重建。manifest 不含时间戳，数据无变更时重跑逐字节相同。
- **数据源 ≠ 交易场所**：`common/paths.py` 的 `DATA_SOURCES`（binance/okx/t212）
  与 `VENUES`（okx/t212）分开。binance 只供数据，不下单。
- **T212 回测框架 v0 已建成**（2026-08-20，工作树分支
  `claude/trading212-backtest-framework-b729c3`，未提交）：事件驱动 bar 级引擎
  `backtest/engine/`（8 模块）+ T212 适配 `backtest/t212/`（7 模块，含 16 类
  平台故障注入），71 项单测 + 真实数据冒烟 6 项判别力断言全过。设计计划与
  裁定在 `fixplans/`（9 份，含变更记录）；权威凭据在 `data/reference/`
  （T212 OpenAPI v0 规范 + api.md 镜像 + 5 份调研 JSON，均 2026-08-20 取回）；
  9 个参考仓库浅克隆于 `vendor/`（已 gitignore，清单与 commit 见
  `fixplans/framework/01_architecture.md` §8）。
- **已实证的 t212 bar 语义**（S5，判别力样本）：日内 ts = bar 开始时刻；
  日线 ts = 交易所本地零点转 UTC，伦敦标的与 GBPUSD=X 在 BST 期间落在
  **前一 UTC 日 23:00**——跨标的日线对齐必须按交易所本地日期。已补记
  `docs/data/t212/DATA_SPEC.md` §3。

- **T212 实盘执行层已建成（2026-08-21，本工作树，DRY_RUN 态）**：`trading212/client.py`
  + `trading212/execution/` 9 模块 + 策略注册表，模块登记见 `ARCHITECTURE.md` §2.3，
  操作规程见 `trading212/execution/README.md`。A0 信号模块从主树复制（字节一致），
  执行与回测共用同一份。74 项执行层测试全过；live 只读+dry-run 冒烟全链路通过
  （决策日 2026-08-20 算出 15 条意向；风控全零时失效关闭且不占用决策日；同日防重
  与并发锁均实测生效）。**真实下单四重闸全部未开**：dry_run=true、risk 全零、
  无 --allow-orders、账户 API 可见资金为 0。
- **T212 关键 S4 事实已落 `data/reference/`（2026-08-21 实调）**：密钥为 live 环境
  legacy 单钥（demo 401）；账户 currency=GBP 但 totalValue=0（历史已实现盈亏
  £3,661.16）；`t212_instruments_20260821.json`（17,398 个标的，TECH18 全部命中
  `_US_EQ`，**META 的美股 ticker 是 FB_US_EQ**）；`t212_exchanges_20260821.json`
  （US 时段表无 CLOSE 事件，常规收盘=当日 AFTER_HOURS_OPEN 20:00Z，周末界=
  AFTER_HOURS_CLOSE，日历窗仅约 6 周滚动）。

## 未决项

| # | 事项 | 需要谁定 | 备注 |
|:--:|---|---|---|
| 1 | OKX API Key 是否已申请、权限位如何设置 | 用户 | 建议研究期只开只读；交易权限单独一把、绑 IP、关提现 |
| 2 | ~~T212 API 能力~~ **已证实**：`equity/*` 系列 401（存在，需密钥），`equity/prices`/`quotes`/`market/candles` 均 **404 不存在** | 已закрыт | 行情须另找源，T212 只做执行与对账 |
| 3 | 标的池筛选标准（流动性口径、样本数量） | 用户 + 取证 | 写进 `<venue>/config/universe.yaml`，须可复现 |
| 4 | 数据起始时间与周期（要几年、要哪些 bar 周期） | 用户 | 决定下载量级 |
| 5 | 加密方向是现货还是合约 | 用户 | ⚠️ **FCA 自 2021-01-06 禁止英国零售交易加密衍生品，至今有效**。永续合约不可交易，只能取其数据。等于只剩现货 |
| 7 | 是否购买 L2 历史（Crypto Lake $64-80/月）或自行录制（180-360 GB/年/标的） | 用户 | 见 `research/notes/20260819_negative_correlation_findings.md` 与本轮报告 |
| 8 | LSE 上的 UCITS 替代品（SGLN/IB01/IDTL/XSPS）在 T212 是否真的可买 | 用户在 App 内确认 | `trading212.com` 对自动化请求一律 403，无法程序化验证 |
| 9 | 磁盘：当前可用 146 GB，完整计划需 250 GB+，录制 L2 需 1 TB+ | 用户 | 是否加外置盘 |
| 6 | ~~是否 `git init` 纳入版本管理~~ | 已决 | 2026-08-19 已 init 并首推，见时间线 |
| 10 | ~~`data/binance/` 无 `raw/` 层~~ | 已决 | 2026-08-20 用户选 (a)：权威 raw 就在 `data.binance.vision`，本地只留 curated，重建凭据落 `docs/data/<source>/MANIFEST.jsonl`。已实现并验证 |
| 11 | `data/` 的备份（≠ 版本管理，git 已排除） | 用户 | **挂起：等外置磁盘就位**（用户 2026-08-20 明言）。重下载代价实测约 11.9 小时（66.7 GB ÷ 1.6 MB/s，并发不提升）。磁盘就位后应跑一次 `build_data_manifest.py --hash-local` 建立本地基线，此后可检出位腐 |
| 12 | manifest 的 `sha256_upstream` 字段目前全为 null | 可选 | 填满需 9,317 次 `.CHECKSUM` 请求（免费公共服务）。取回函数已单测通过。价值有限：下载时 `fetch_to_frame(verify=True)` 已逐个校验过。真正该做的是 `--hash-local`（见未决项 11） |
| 13 | T212 费用细节的经验校验：FX 费与税费的舍入规则、PTM 对 ETF 是否实收、最小订单值现行值（帮助页已下线，仅 Wayback + 员工帖）、下单数量精度现行上限（官方无文档，实测 4 位） | 用户 + demo 账户实测 | 待将来对 demo 环境下单后用 `GET /equity/history/orders` 的 walletImpact.taxes 逐项对账；回测成本列已按同一枚举命名以便对账 |
| 14 | T212 API 的 POST→FILLED 实测延迟无公开数据（实盘 API 下单 2025-10-01 才开放）；故障注入的 p/q/r/k 概率参数全为推断值 | 用户裁定是否实测 + 敏感性 | 延迟档位与证据见 `fixplans/t212_faults/02_latency_model.md`；报告结论前须跑参数敏感性 |
| 15 | 回测框架待办：F3 outage 抽样发生器（v0 仅显式窗口）；`avoid_first_bar` 由策略层承担；对账层故障目录（fault_catalog §4）待实盘执行层设计时启用 | 后续任务 | 见各 fixplan 变更记录 |
| 16 | **账户 API 可见资金为 0**（totalValue=0、availableToTrade=0，2026-08-21 实调）——与「T212 存放英镑」的既有认知矛盾。资金是否在另一账户类型（API 仅覆盖 Invest 与 Stocks ISA）或需入金 | 用户 | 不阻塞 dry-run；阻塞模拟盘与实盘 |
| 17 | A0 实盘参数三项：init-ledger 资金分配额度、`t212.live.yaml` risk 区块全部限额、执行排期（建议本地 08:30 settle→decide） | 用户 | 全零=失效关闭；见 `trading212/execution/README.md` §2 |
| 18 | demo 账户 practice API key（现有 key 仅 live 有效）——上线路径第 3 步模拟盘的前提；顺带实测三件事：`walletImpact.netValue` 符号约定与税费包含关系（併入 13 号）、API 下单的 `initiatedFrom` 是否确为字面 `API`（归因机制依赖它，现仅有 schema 枚举证据）、POST 到成交的实际延迟 | 用户 | `QUANT_ENV=paper` 已预留 demo 主机 |
| 19 | 执行层两项弱化设计需用户明示接受或另建：B4 日内亏损熔断（A0 日频单批次，未实现）；E5 CRITICAL 告警仅落日志，无外部触达通道 | 用户 | 见风险审查报告 |

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
| 2026-08-19 14:5x | 新增仓库根 `sync_to_git.command`（已 chmod +x，无 quarantine 属性）：Finder 双击即在 Terminal 跑日常同步。职责只有四件——补 PATH（git 是 Homebrew 构建，在 `/opt/homebrew/bin/git`，Finder 的最小环境找不到）、以自身位置而非 cwd 定位仓库（双击时 cwd 是家目录）、按退出码 0/1/2 输出结论、`read -n 1` 挂住窗口使闸门命中不会一闪而过。参数透传给 `scripts/sync_to_git.py`。**`--overwrite-remote` 刻意不可由双击触发**，强制覆盖只能显式命令行执行。验证：`env -i PATH=/usr/bin:/bin` + cwd=`$HOME` 下运行 `--dry-run`，仓库定位与 git 调用均正常。回滚：删该文件 |
| 2026-08-19 14:5x | 待办（未做）：`scripts/sync_to_git.py` 的注释与 docstring 仍为中文，违反 `/quant-code-standards` §3.0（代码注释一律英文 + 英式拼写）。同批次 `common/` 五模块与探针脚本已完成英文化。**【已完成 — 见 2026-08-19 15:0x 行】** |
| 2026-08-19 15:0x | `scripts/sync_to_git.py` 英文化完成（`/quant-code-standards` §3.0）：模块与函数 docstring、全部注释、argparse help、print 与 RuntimeError 消息改为英文 + 英式拼写；输出前缀由中文标签改为 `[repo] [remote] [staged] [secret-gate] [abort] [dry-run] [commit] [done] [warning] [result] [init]`。等价性证明：剥离 docstring 并把全部字符串常量置空后，新旧文件 AST 完全一致；`EXPECTED_REMOTE`、`DEFAULT_BRANCH`、`MAX_BLOB_BYTES`、`SECRET_PATH_PATTERNS`、`__all__` 与全部 6 个正则（`HARD_CONTENT_PATTERNS` 的 10 条 pattern+flags、`SECRET_FIELD_RE`、`ASSIGN_VALUE_RE`、`PLACEHOLDER_RE`、`UUID_RE`、`HEX_RE`）逐项相等；退出码 0/1/2 未动。判别力复检（5 假密钥 + 3 对照，`tests/secret_gate_probe.txt` 用后即删）：5 处全中、3 个对照全放行、退出码 2。改写中发现并修复一处自伤：URL 内嵌口令规则的英文标签若写作 `credentials embedded in a URL`，其中的 `credential` 会被本模块自己的 `SECRET_FIELD_RE` 命中，而该行含 `://`，`ASSIGN_VALUE_RE` 随即取到取值，导致脚本每次运行都自堵密钥闸（首轮 dry-run 实测命中 `scripts/sync_to_git.py:102`）；标签改为 `login details inline in a URL` 后自伤消失。该约束已写进该行上方注释。回滚：`git checkout f360ebc -- scripts/sync_to_git.py` |
| 2026-08-19 15:0x | 用户裁定：注释语言先英式后改**美式英语**；代码全部回填，规范见 `/quant-code-standards` §3.0 |
| 2026-08-19 15:2x | 多智能体工作流完成数据源调研（13 agent / 652 工具调用 / 137 万 token），结论与本机实测互相印证，报告存 `/tmp/synthesis.md` |
| 2026-08-19 15:3x | 实证否定「存在流动性好的负相关标的」这一前提，两侧证据写入 `research/notes/20260819_negative_correlation_findings.md`。最重要发现：**股债负相关制度已于 2022 年反转且连续五年未回归** |
| 2026-08-19 15:5x | Phase 1 数据落地：新增 `common/store.py`（调优 parquet 写入）、`crypto_trading/ingest/{schemas,binance_archive}.py`、三个 ingest 脚本、`scripts/data_inventory.py`。回滚：删这些文件与 `data/` 下对应目录 |
| 2026-08-19 18:0x | 股票日线改为 `period="max"` 全历史：us_equity 由 130,560 增至 228,214 个交易日（最早 1962-01-02），us_etf 97,706（SPY 回到 1993），uk_tradable 40,301。回滚：把 `PERIOD_MAX` 改回固定 `start` 日期并重跑 |
| 2026-08-19 18:1x | 发现并修复 yfinance 批量取数缺陷：`period=max` 批量请求被解析成统一 1927 起点并触发限流（24 标的只回 3 个，21 个误报 delisted）。`_fetch_one()` 改为逐标的请求 + 4 次指数退避。回滚：改回 `yf.download(tickers, period=...)` 批量调用（会重现该缺陷） |
| 2026-08-19 18:3x | `common/store.py::write_table()` 改为**原子写入**：先写 `<name>.parquet.writing` 再 `os.replace()`。新增 `is_readable_parquet()`（读 footer 检测截断，已对 99%/50%/1%/零字节四种截断验证均可检出）与 `clear_stale_temps()`。回滚：删这两个函数并把 `pq.write_table` 的目标改回 `path`——但那样崩溃会留下截断文件且被 `exists()` 误判为已完成 |
| 2026-08-19 18:3x | 修复 `write_table` 潜伏缺陷：`column_encoding` 与 `use_dictionary=True` 冲突（pyarrow 报 ValueError）。Phase 1 未触发是因其 `ts` 为 timestamp 类型不满足 `is_integer`；bookTicker 的 `update_id` 是 int64，会在每个文件上崩溃。现改为两集合互斥。回滚：删 `dictionary_columns` 与 `use_dictionary=` 参数 |
| 2026-08-19 18:4x | bookTicker 脚本加断点续传：`_prune_damaged()` 恢复时逐个读 footer，损坏件删除并重排；SIGINT/SIGTERM 设停止标志，在途文件写完后退出。三项测试均以故障注入验证通过 |
| 2026-08-19 18:5x | **修复高错误率**：`binance_archive._get()` 原本无任何重试，实测错误率 55%（30/55）。改为 5 次指数退避重试，404 立即抛出不重试；`TIMEOUT_SEC` 120→180。故障注入三例验证通过。回滚：把 `_get` 改回单次 `urlopen`——会重现 55% 丢失率 |
| 2026-08-20 09:xx | 用户双击续传时昨晚任务未停，产生两个实例抢带宽，错误率由 0 升至 13/560。杀掉重复实例后错误停止增长。教训：`resume_bookticker.command` 未做单实例保护，后续应加锁文件 |
| 2026-08-20 10:xx | 股票改为五档周期（1m/2m/5m/1h/1d）。实测确认 Yahoo 合法周期枚举中**无任何亚分钟粒度**，秒级美股在此源不可得；1m 硬上限为「单次 8 天、总计 30 天」。15m/30m/90m 不存（可由 5m 与 1h 精确聚合） |
| 2026-08-20 10:xx | 用户裁定命名规则：日内文件起始**固定为当月 1 号**（原用实际首根 K 线日期，遇假期会漂成 02）。新增 `common/paths.month_bounds()`；1,489 个既有文件已重命名。回滚：删 `month_bounds` 并改回用 `frame.ts.min()` |
| 2026-08-20 10:xx | 取数与写入逻辑抽到 `trading212/ingest/yahoo_bars.py`，ingest 与 updater 共用一份，避免 §4.7 的复制粘贴 |
| 2026-08-20 11:xx | 新增 `scripts/update_data.py` + `update_data.command` 手动更新入口。股票走「全窗口重取」（复权是追溯性的，追加会在拆股日造成序列断裂），加密走真增量。回滚：删这两个文件 |
| 2026-08-20 11:xx | 更新器试跑暴露真实缺口：Binance 月度文件月末才发布，故当月数据完全未被覆盖（现货 K 线停在 08-01）。已改为「当月用日度文件补齐，月度文件到货后删除被取代的日度件」，三项逻辑测试通过 |
| 2026-08-20 11:xx | 用户裁定：策略命名须带版本号，起点 V0.0.1。写入 `/quant-code-standards` §4.5.1（MAJOR=信号逻辑变 / MINOR=参数变 / PATCH=重构且须证明输出逐字节不变），`ARCHITECTURE.md` §2.0.1 同步 |
| 2026-08-20 12:5x | 裁定：`data/`（实测 64 GiB / 13126 文件）**不入 git，维持 .gitignore 排除**。依据非偏好而是硬限制：229 个文件超过 GitHub 的 100 MiB 硬性拒绝线（最大 300.9 MB，`data/binance/curated/um/bookTicker/ETHUSDT/.../20240229.parquet`），604 个超 50 MiB 警告线；仓库 64 GiB 对 GitHub「理想 <1 GB，强烈建议 <5 GB」；Git LFS 在 Free/Pro 仅含 10 GiB 存储 + 10 GiB 流量，且本仓库 PUBLIC 会使数据一并公开。另经实测，`data.binance.vision` 至今仍供给同一批归档（`HEAD .../BTCUSDT-bookTicker-2024-02-29.zip` → 200，474,096,996 字节，last-modified 2024-03-01），故该数据集可重建，应入库的是重建器与 manifest 而非字节。现状已正确：`git ls-files data/` 为 0，`.gitignore:12` 生效，同步器 10 MiB 单文件闸兜底 |
| 2026-08-20 13:0x | 用户裁定三项：(a) 两份 `DATA_SPEC.md` 移出 `data/`，建 `docs/data/<source>/`；(b) manifest 现在就做，落在仓库内；(c) binance 认定为**数据源**而非交易场所。原因：`data/` 整棵不入库且将迁往外置盘，说明件放在字节旁边会跟磁盘一起走，仓库将不剩任何关于这批数据的记录 |
| 2026-08-20 13:0x | **修复 `.gitignore` 的未锚定缺陷**：第 12 行 `data/` 未加前导斜杠，匹配任意层级同名目录，把新建的 `docs/data/` 一并排除（`git check-ignore -v docs/data/binance/MANIFEST.jsonl` 命中 `.gitignore:12:data/`）。改为 `/data/` `/logs/` `/reports/` `/backtest/results/`。验证：`data/` 下真实 parquet 仍被忽略、`secrets/` 仍被忽略、`docs/data/` 三份文件均已可入库。回滚：去掉前导斜杠即回到旧行为，但 `docs/data/` 会再次消失 |
| 2026-08-20 13:0x | 新增 `scripts/build_data_manifest.py`：扫 curated 树生成重建凭据。每分区一条 JSONL，含坐标、上游 URL、本地字节数与行数。行数取自 parquet footer 不读数据；按字节数缓存，无变更时复用；上游 `.CHECKSUM` 与本地 sha256 均为按需开关（`--fetch-checksums` / `--hash-local`）。**不含时间戳**，保证无变更时重跑逐字节相同，否则每天同步都会提交无信息量 diff。回滚：删该脚本 + 删 `docs/data/*/MANIFEST.jsonl` |
| 2026-08-20 13:0x | `common/paths.py` 扩展：新增 `DATA_SOURCES`（binance/okx/t212，区别于只含 okx/t212 的 `VENUES`）、`DIR_DOCS`、`docs_data_dir()` / `data_spec_path()` / `manifest_path()` / `gaps_path()`（后两者**从 `data/` 下改指 `docs/data/<source>/`**，原位置在 gitignore 内故永不入库）、`binance_partition_path()` / `binance_partition_dir()` / `stamp_freq()`。`data_dir()` 的校验由 `_check_venue` 改为 `_check_source`。改动前确认这些函数**零外部调用点**（`grep -rn` 仅命中 paths.py 自身）。回滚：还原 paths.py 并把 update_data.py 的路径构造改回内联 |
| 2026-08-20 13:0x | `scripts/update_data.py` 去重：`_crypto_out` / `_existing_stamps` / `_drop_superseded_days` 三处内联路径拼接改为调用 `paths.binance_partition_*`（`/quant-code-standards` §4.7 一处定义）。回归验证：305 个样本覆盖全部 5 种分支组合，新旧构造 0 处不一致、构造出的路径 100% 存在于磁盘；负例（period 由 None 改 1h）既不等于旧实现也不存在于磁盘，证明该检验有判别力 |
| 2026-08-20 13:1x | `sync_to_git.command` 改为两步：先用 `.venv/bin/python` 重建 manifest（需 pyarrow，系统 python3 没有），再用系统 python3 跑 `sync_to_git.py`（纯标准库）。manifest 失败只警告不阻断同步，理由：分区损坏是数据问题，不该连带阻止当天源码入库。新增 `--no-manifest`。⛔ `--overwrite-remote` 仍刻意不可由双击触发。回滚：还原该文件 |
| 2026-08-20 13:1x | 验证（C 类官方源 + B 类本地数据）：manifest 反推的归档 URL 抽样 6/6 命中 200，覆盖 spot/um × daily/monthly × tag取period/取dataset × stem带横杠/不带；负例 4/4 按预期 404（freq 改错、market 改错、stamp 未展开横杠、klines 用 dataset 当 tag），证明四条推导分支每条都有判别力。行数 3/3 与全量读取一致且取值有 836 种（非常数）。`_meta` 汇总等于逐行求和。确定性：重跑三份 manifest 全部 sha256 不变。密钥闸在 .jsonl 内植入假密钥能命中（第 2 行），证明 3.19 MiB 的 manifest 是真扫了而非被跳过 |
| 2026-08-20 15:3x | T212 回测框架开工（工作树 `claude/trading212-backtest-framework-b729c3`）。多智能体调研 5 研究员取回：T212 公开 API v0 完整契约（OpenAPI 规范落 `data/reference/t212_openapi_v0_20260820.yaml` 并本地抽验关键 schema）、费用与税费全表、延迟证据链（无官方 SLA；常规秒级~20s、拥堵 1–26 分钟、SETSqx 仅日内 5 次竞价、IB 中介宕机 2 例、GME 只减仓窗口）、30 条带来源的平台 bug 实例、9 框架源码级调研。bt 判不适用：`vendor/bt/bt/core.py:1633` 当根价即时成交、现金为单一无币种标量、默认整数股。9 仓库浅克隆 `vendor/`，`.gitignore` 增根锚定 `/vendor/` |
| 2026-08-20 16:xx | `fixplans/` 建立（README + framework 5 份 + t212_faults 2 份 + validation 2 份）。本地实证 bar 语义两条（判别力样本）：日内 ts=bar 开始（AAPL 1h 首根夏 13:30/冬 14:30 UTC，随 DST 切换）；日线 ts=交易所本地零点转 UTC，伦敦标的 BST 期间落前一 UTC 日 23:00（SGLN.L 交易日 06-29 → ts 06-28 23:00Z）。后者补记入 `docs/data/t212/DATA_SPEC.md` §3。回滚：删 fixplans/ 与 DATA_SPEC 增补段 |
| 2026-08-20 17:xx | 引擎代码落地：`backtest/engine/` 8 模块（types/feed/matching/ledger/engine/metrics/results/broker 协议）+ `backtest/t212/` 7 模块（data_source/instruments/costs/faults/admission/broker_sim/runner），16 类故障开关，`tests/backtest/` 判别力测试，`scripts/20260820_t212_backtest_smoke.py` 真实数据冒烟（BST 边界 + 2026-07-03 美假日窗口，6 项断言含 FX 前视判别）。`common/paths.py` 增 `equity_curated_root`/`equity_interval_dir`。ARCHITECTURE §1/§2.2 同步登记。回滚：整棵工作树未提交，`git checkout -- .` + 删除未跟踪件即回 |
| 2026-08-20 23:2x | 对抗性审查工作流（4 查错员 ×39 发现，每条 2 反驳者验证；21 个验证员因会话限额中断，由本方逐条裁定）→ 修复 24 项。关键修复：混交易所 1h 时间轴 30 分钟前视泄漏（撮合资格由「合并时间轴步数」改为「时间」eligible_ts，引擎加日内成交时间断言；美股 1h 在 :30 网格、伦敦在 :00 网格，步数制会用到未形成的收盘信息）；止损限价可即成腿曾按 bar 未交易过的价格成交；卖侧止损限价曾采信触发前的 high；STOP 部分成交后丢失触发态；PTM 曾按笔而非按订单收；夏普与年化收益曾用不同日基底；重叠点差窗口未随 DST；F13 上限改按标的×bar 聚合；执行时资金闸曾可挪用他单冻结。三项按 fixplans 规则改计划留痕（F12 拒单+重提语义、F3 抽样发生器降待办、avoid_first_bar 归策略层）。broker_sim 超 400 行拆出 admission.py。终态：71 测试 + 冒烟 6/6 全过；全部 9 份 fixplan 补变更记录。回滚：同上一行 |
| 2026-08-21 11:3x | **对抗性审查修复 9 项**（4 查错员×13 发现，逐条 2 轮反驳验证，10 项确证全修，86 项测试过）：(1) `order_monitor` 账单方向缺省成买入→改为 side 必须为 BUY/SELL 且与账本意向符号交叉核对，矛盾即拒收；(2) 账单 quantity/netValue 为 null 曾静默记零→改为拒收待重试；(3) `_history_items_for` 早停假设同单成交在列表中连续→改全量扫描（同 ticker 手工成交夹层会截断收割并假性冻结）；(4) 账本 SUBMIT/REJECT/AMBIG/RESOLVE 事件 id 加尝试计数 `#n`（QMT J1 教训：无生命周期因子的幂等键跨周期撞车，二次歧义曾不冻结）；(5) `load()` 增日志领先快照检测（崩溃于 journal fsync 与快照替换之间不再静默丢事件）；(6) 解歧证据收紧：ticker+方向+|数量|+createdAt≥歧义时刻-10min+排除本方已知订单（`knows_order`），旧日同量反向单不再冒充丢失单；(7) `client` 200 响应解析失败改抛歧义而非拒单（200=已受理，解析失败仍是活单）；(8) 路由器只认 `PermanentError` 为已证实拒单，POST 后一切意外（含回执落账失败）走歧义冻结；(9) decide 增开盘前安全边距（默认 10 分钟，防临开盘提交穿入盘中成交）。另补 `run_a0` fcntl 单实例锁（并发第二实例实测被拒）。回滚：本批全部在前一行新增文件内，随前一行回滚一并消失 |
| 2026-08-21 11:xx | **T212 实盘执行层 v0 建成（本工作树，全程 DRY_RUN）**：`common/paths.py` 增 `execution_state_dir()`；新增 `trading212/client.py`（REST 传输：legacy 单钥鉴权、逐端点令牌桶、GET 重试、**下单 POST 单次尝试**，歧义抛 `OrderSubmitAmbiguousError`）与 `trading212/execution/` 9 模块（instruments 映射与日历 / market_data 截止视图与新鲜度闸 / shadow_ledger 事件溯源账本 / risk_gate 失效关闭闸 / order_router 写前意向 / order_monitor 账单收割 / reconciler 单向对账 / daily_cycle 编排 / run_a0 CLI 带 fcntl 单实例锁）；`trading212/strategy/__init__.py` 建版本注册表；A0 信号模块与参数从主树复制（cmp 字节一致）。账本设计借鉴 QMT 参考脚本（event_id 幂等、原子快照、拒空基底重建、歧义冻结、保锁优先），T212 以 fill id 为天然幂等键。S4 取证：`data/reference/t212_{instruments,exchanges}_20260821.json`（META→FB_US_EQ；US 时段表常规收盘=AFTER_HOURS_OPEN，无 CLOSE 事件——该发现修正了首版日历判定）。验证：74 项测试（判别力样本含 4dp 余数、越日 bar 泄漏、双验证员歧义冻结）+ live 只读/dry-run 冒烟（15 意向、失效关闭、同日防重、并发锁）。配置 `t212.live.yaml`/`t212.paper.yaml`（gitignore）risk 全零。回滚：删 `trading212/client.py`、`trading212/execution/`（保留 `__init__.py` 与 `.gitkeep`）、`tests/execution/`、`trading212/strategy/{__init__.py 恢复为空, a0_v0_0_1.py}`、config 三份还原、`common/paths.py` 去掉 `execution_state_dir`、`ARCHITECTURE.md` 删 §2.3；`data/t212/execution_state/` 整目录删除即清状态 |
| 2026-08-21 00:xx | 策略接入与双线数据源读取层：新增 `backtest/engine/strategy_loader.py`（按 (venue, name, version) 加载 `<venue>/strategy/` 模块，校验 STRATEGY_NAME/STRATEGY_VERSION 与 `compute_targets` 契约，契约文档 `fixplans/framework/06_strategy_plugin.md`）、`backtest/okx/data_source.py`（Binance 归档 spot klines → 引擎 bar schema，UTC 日对齐、USDT 计价；本地存量 9 个 USDT 对 × 1d/1m × 2017-08 起）；feed 计价币白名单参数化并纳入 USDT；`common/paths.py` 的 `binance_partition_dir` 增 data_root 注入。80 项测试全过；okx 读取层真实数据抽验（2024-02-25~03-05 跨闰日 10 行/标的）。待办：okx 撮合/成本适配器（OKX 费率等 S4 现查）+ 账本 `_gbp` 字段中性化（PATCH 级，须字节级等价证明）。回滚：删三个新文件与 06 号计划，还原 feed/paths 的新参数 |
