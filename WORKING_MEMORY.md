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
- **bookTicker 过夜任务运行中**：可断电续传，用 `scripts/resume_bookticker.command`
  双击恢复；或 `caffeinate -i ./.venv/bin/python -u scripts/20260819_ingest_crypto_bookticker.py`。
- 已纳入 git 版本管理，remote `origin` = `https://github.com/Hymoncodactic/quant.git`，
  分支 `main`。同步入口：`python3 scripts/sync_to_git.py`（每日手动执行）。
- **该 GitHub 仓库为 PUBLIC**（用户 2026-08-19 明确裁定保持公开）。今后一切入库内容
  全网可读且被永久缓存：策略逻辑、参数、口径裁定入库前须自行判断是否可公开。
  `.gitignore` 已排除 `secrets/`、`data/`、`logs/`、`reports/`、`*.live.yaml`、`*.paper.yaml`。

- **股票方向已开工（2026-08-20）**：美股做多、科技股、日频。第一轮
  KDJ 入场 + MACD 筛选 + KDJ 死叉出场裁定**不通过**
  （`research/decisions/20260820_kdj_macd_daily_ruling.md`）。第二轮出场规则
  搜索（378 格、两段式）裁定见
  `research/decisions/20260820_kdj_exit_search_ruling.md`：K>=80 出场两格
  胜率判据样本外通过（V2 +18/+21pp，13/13 标的为正），但归因诊断证明边际主要
  来自出场的内生停时（日历入场对照即 +12~13.5pp），且**收益口径为负**
  （每笔比同持有期无条件均值低 0.53~0.65pp）、左尾深（最差 -53%，MAE -65%）。
  下一轮若继续，判据必须换收益口径。第三轮（regime_lab，状态层+低频信号）
  已完成，裁定 `research/decisions/20260820_regime_lf_ruling.md`。代码在 `research/factor_kdj_macd/`
  （用户裁定：股票研究回测代码放 `research/` 下；若因子将来被采用，信号须按
  ARCHITECTURE §2.0 下沉到 `trading212/strategy/` 作唯一副本）。

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
| 10 | KDJ+MACD 手算实际规则与预注册规则不符（预注册规则为空集） | 用户 | 需用户说明手算时的实际条件（K<30 是判在哪一根、MACD 是否同 bar 判），定位后重跑；结论 2~5 不受影响 |
| 11 | `research/factor_kdj_macd/` 与 `research/regime_lab/` 是否入库 | 用户 | 仓库为 PUBLIC，策略逻辑与裁定入库即全网可读。当前**未提交** |
| 12 | 数据申请：(a) Norgate 等含退市股的 NDX 历史成分（约 40 USD/月）解决存活者偏差；(b) Massive/Polygon 10 年级小时线（79 USD/月）做真实 RV 状态层与小时频信号确认 | 用户 | 第三轮裁定 §10；批准即走 `/market-data-pipeline` |

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
| 2026-08-20 10:0x | 股票方向开工。用户裁定四项口径（S6）：入场 K 上穿 D 且 K<30；筛选 DIF>DEA 且 DIF>0；出场 KDJ 死叉；先跑 NVDA/AAPL/MSFT/META/AMD 五只。不计滑点与手续费亦为用户明言，属偏离 `/backtest-discipline` §二.3/§二.4 的保守默认，已在预注册 §2.6 与裁定 §8 登记 |
| 2026-08-20 10:1x | 预注册 `research/prereg/20260820_kdj_macd_daily_prereg.md` 先于任何回测代码运行冻结。新建 `research/factor_kdj_macd/`：data / indicators / rules / engine / benchmark / run_backtest / acceptance / diagnose_grid / diagnose_entry 九个模块。回滚：删该目录与 `research/prereg,decisions/20260820_*` |
| 2026-08-20 10:2x | **裁定：不通过**，见 `research/decisions/20260820_kdj_macd_daily_ruling.md`。三条核心事实：(a) 预注册规则在两窗口成交笔数为零，K<30 与 DIF>0 结构互斥（W1 五只标的逐条件筛剩 236/242/233/227/228 → 40/49/36/46/46 → 4/1/1/0/3 → 全 0）；(b) 两个拆解臂胜率显著低于同持有期无条件基准，W1 边际 -14.57 与 -12.85 个百分点，W2 -17.44 与 -10.39，20 个「窗口×臂×标的」单元格无一为正；(c) 负边际来自**出场规则**，入场信号单独看的边际约为零（固定持有期事件研究 1~60 日边际在 ±2.5 个百分点内摆动） |
| 2026-08-20 10:2x | 放松网格 60 格（5 档 K 上界 × 6 种 MACD 模式 × 2 窗口）**无一格边际为正**，最好 -7.70 个百分点。故结论 2~5 不依赖「用户手算的实际规则究竟是哪一条」 |
| 2026-08-20 10:2x | 方法学留痕：前视对照臂具备判别力（指标前移一期后胜率由 0.4347 升至 0.8693），指标因果性以前缀重算逐位验证（45 个信号 bar 全等），该技术本身的判别力以居中滚动均值反证。`acceptance.py` 11 项验收全部通过。引擎两次重构（MACD 分派表、抽出 `run_spec`）后 21 个结果文件逐字节不变（`/backtest-discipline` §七.2） |
| 2026-08-20 10:2x | 关键结论供后续：若继续这条线，方向是**换出场规则**而非调入场参数。死叉出场的持有期是内生的，恰在价格转弱时结束持仓，系统性把终点选在不利位置 |
| 2026-08-20 12:2x | 用户授权出场规则与 MACD 模式的显式搜索（S6，含点名格 b_20_80×death_cross 与「金叉后最高点出场」）。预注册 `research/prereg/20260820_kdj_exit_search_prereg.md` 先于运行冻结：378 格（6 K带位×7 MACD模式×9 出场）、两段式（S=核心5标的 2000-2017 搜索；V1=同标的 2018-2026；V2=13只全新标的 2018-2026）、筛选与定级判据全部先冻结。「最高点出场」以因果代理落地（trail 回撤 / K转跌 / K>=80） |
| 2026-08-20 12:3x | 引擎扩展：rules.py 加 k_min 与 dif_rising 模式、新增 exits.py（9 种出场注册表）、engine.py 新增 extract_trades_spec（旧 extract_trades 冻结不动）。扩展后第一轮 21 个结果文件逐字节重现。acceptance 16 项全过（新增 trail/fixed/带位边界/搜索窗封闭/新旧引擎等价 5 项）。回滚：`git checkout` 删 exits.py/search.py/diagnose_exit_attribution.py 并还原 rules/engine/acceptance |
| 2026-08-20 12:3x | 修复 `_summarize` n_open 缺陷：空标的拼接后 closed 列变 object dtype，`~True` 按 Python 语义得 -2，n_open 出现负数。改为 astype(bool) 后计数；只影响 n_open 列，第一轮汇总文件逐字节不变 |
| 2026-08-20 12:4x | 修复 `_return_baseline` 窗口缺陷：基准起点硬编码 201 使 V 段收益基准混入 2000-2017。修复后策略相对收益差从 -0.28/-0.36pp 扩大到 -0.53/-0.65pp，方向不变 |
| 2026-08-20 12:4x | **第二轮裁定落笔**（20260820_kdj_exit_search_ruling.md）：k_over_80 两格胜率口径通过但归因在出场停时、收益口径为负、左尾深；fixed_5 一格不通过一格探索性通过（不跟进）；用户点名死叉格样本外 -10.4/-12.4pp 不通过。关键方法学发现：**胜率判据本身可被「只在局部强势卖出」的停时规则刷高**，与死叉出场（终点挑在不利位置）互为镜像；后续判据须换收益口径。7 名独立审计员的对抗验证工作流已启动，结果回来后补裁定 §7 |
| 2026-08-20 12:5x | 对抗验证工作流完成：7 名独立审计员 7/7 confirmed（两名按文字规格重写全流程逐笔重现、引擎前视审计、统计复算、搜索窗封闭探针、筛选器 378 格重放、基准公允性、头条数字复算）。三点措辞修正并入裁定：归因改「出场占 54~85%、入场真实增量 +4~8pp」；日历对照被单持仓步进吞掉 30~38% 排程（方向保守）；收益差点估计为负但未显著。裁定 §7 已补记，第二轮收口 |
| 2026-08-20 15:2x | 用户开启第三轮（S6）：策略复杂化，HF 划分市场状态 + LF 出信号，日内小时频以下，零成本信号层验证，目标年化>=30% 且 MaxDD<=20%，标的框死本地数据，可申请新数据。文献综述工作流启动（7 方向并行） |
| 2026-08-20 15:4x | 新建 `research/regime_lab/`（data/vol/metrics/rigor/engine/configs/run_search/validate_rv_proxy/acceptance 九模块）。**HF 代理桥验证**：Yang-Zhang(20d) 对小时线 RV 在 711 重叠日 Pearson 0.93~0.98、应激分类一致 0.87~0.97（7 标的），日线状态层合法（`regime_lab/results/rv_proxy_validation.csv`）。acceptance 7/7（SPA 检验水平 2/20@10%、DSR 幸运儿 0.43/真技能 0.999、去 shift 作弊臂 +20.1% 年化） |
| 2026-08-20 16:0x | 预注册 `research/prereg/20260820_regime_lf_prereg.md` 冻结：家族 450 配置（3 收益流×5 信号×5 波动闸×2 趋势闸×3 波动目标）、两段式 S=2000-2017 / V=2018-2026、选择=S Calmar 前5、判据=目标线+SPA+DSR；冻结前查看过 EW18 基准指标已在预注册 §7 披露并禁其入围 |
| 2026-08-20 16:1x | **第三轮裁定：目标判据不通过——差之毫厘且性质清楚**。入围第一 on·tsmom252·p80x0·qqq200：V 段 CAGR 29.32%（差 0.68pp）/ MaxDD 10.91% / Sharpe 2.01 / 2022 全年 -0.7%。S→V Calmar 秩相关 0.873。归因：收益=隔夜异象+存活者池（裸隔夜 EW18 已 27.3%），双闸只压回撤（36.6%→10.9%）；SPA p=0.53 无平均收益边际；QQQ 单资产对照 15.5%/11.3%（存活者-free 下 30% 不成立）；隔夜流成交额约 168 倍本金/年，全项目最成本脆弱的结论。未入围邻格 on·always·p80x0·qqq200 字面过线（32.0%/13.9%）但拒绝事后采纳，登记为下轮先验候选 |
| 2026-08-20 17:0x | 两工作流收口：文献综述 76 篇落 `research/notes/20260820_regime_lf_literature.md`（隔夜异象在指数级 2015 后衰减、730 天小时线仅够确认不够搜索、分钟 RV 对状态层仅 +0.02 夏普增量）；对抗验证 6 审计员 5 confirmed + 1 发现 SPA p_lower 居中缺陷（应 min(mean,0)，原实现方向保守），已修复重跑：仅 p_lower 0.5445→0.3670，其余逐位不变。数据申请两项待用户裁定（未决项 #12） |
