# 目录与模块职责地图

本文件是路径与分层的唯一权威。新增模块前先读本文件；新增目录后同步更新本文件。
纪律见 `CLAUDE.md`，流程见 `.claude/skills/`。

## 1. 顶层

| 路径 | 职责 | 备注 |
|---|---|---|
| `CLAUDE.md` | 项目纪律，always-on | 资金红线 §3 优先级最高 |
| `ARCHITECTURE.md` | 本文件 | |
| `WORKING_MEMORY.md` | 跨会话状态：当前状态 / 未决项 / 时间线 | 开工必读，收工必写 |
| `.claude/skills/` | 本项目 skills，共 10 个 | 见 §4 |
| `common/` | 两条线共用的基础库 | 不得含场所特有口径 |
| `crypto_trading/` | OKX 交易代码 | 只装与交易所打交道的代码 |
| `trading212/` | Trading 212 交易代码 | 同上 |
| `backtest/` | 回测，独立于交易代码 | 不碰交易所接口 |
| `data/` | 全部落地数据 | gitignore，见 §3 |
| `docs/data/<source>/` | 数据的**说明与重建凭据**：`DATA_SPEC.md`、`MANIFEST.jsonl`、`GAPS.csv` | 入库。见 §3 |
| `research/` | `prereg/` 预注册、`decisions/` 裁定、`notes/` 笔记 | |
| `fixplans/` | **交易代码规格**，只有 `t212/` 与 `crypto/` 两个顶层目录，其下按策略分目录 | 每份说明都指向具体交易代码文件；`trading212/` 与 `crypto_trading/` 读它来更新策略与执行 |
| `docs/backtest/` | 回测框架建设计划（framework / validation） | 代码实现以计划为准，先改计划再改代码 |
| `vendor/` | 第三方参考代码的浅克隆，只读参考 | gitignore（`/vendor/`），不入库 |
| `scripts/` | 一次性脚本与常驻工具，一次性件带日期前缀 | 见 `scripts/README.md` |
| `dashboard.command` | 双击启动本地看板（`trading212/dashboard/`，实盘环境，端口 8787） | 只启动看板，不碰策略进程 |
| `dashboard_demo.command` | 双击启动模拟盘看板（paper 环境，端口 8788，可与实盘看板并跑） | 同上；模拟盘状态物理隔离于 `data/t212/execution_state_paper/` 与 `trading212/records/paper/` |
| `trading212/records/` | 账户记账归档（成交、流水、分红、快照、信号、资产曲线） | **不入库**，带自身 `.gitignore`；仓库公开，内含持仓与账户号 |
| `tests/` | 引擎与适配层测试 | 见 `tests/README.md` |
| `reports/` `logs/` `backtest/results/` | 运行时产物落地位置 | gitignore，允许为空，不放 `README.md`（`CLAUDE.md` §4.3） |
| `secrets/` | 唯一的密钥落地位置 | gitignore，永不展示内容 |

每个目录必须配 `README.md`，登记其中每个文件的作用与存在必要性，
豁免范围见 `CLAUDE.md` §4.3。每个源文件必须有六节模块头，见 `CLAUDE.md` §4.4。

## 2. 代码分层

依赖方向单行，禁止反向导入。允许的 import 关系：

| 模块 | 允许 import |
|---|---|
| `backtest/` | `<venue>/strategy/`、`common/` |
| `<venue>/execution/` | `<venue>/strategy/`、`<venue>/client.py`、`common/` |
| `<venue>/ingest/` | `<venue>/client.py`、`common/` |
| `<venue>/strategy/` | `common/` |
| `<venue>/client.py` | `common/` |
| `common/` | 只依赖标准库与第三方库 |

| 层 | 路径 | 允许 | 禁止 |
|---|---|---|---|
| 基础 | `common/` | 路径、配置、密钥、日志、网络与限频、存储、指标 | 任何场所特有的口径 |
| 客户端 | `<venue>/client.py` | REST/WS 连接、鉴权、签名、限频；被 ingest 与 execution 共用 | 业务决策 |
| 接入 | `<venue>/ingest/` | 下载落 `data/<venue>/raw/`、清洗到 `curated/` | 交易决策 |
| 策略 | `<venue>/strategy/` | 信号计算，纯函数：输入快照与持仓，输出目标仓位 | 下单、读网络、写状态 |
| 执行 | `<venue>/execution/` | 下单、撤单、订单状态机、对账 | 信号计算 |
| 回测 | `backtest/` | 撮合模拟、成本建模、绩效统计 | **调用任何交易所接口** |
| 配置 | `<venue>/config/` | yaml 配置、标的池 | **任何密钥** |

`<venue>` = `crypto_trading`（slug `okx`）或 `trading212`（slug `t212`）。

### 2.0.1 策略带版本号

策略身份 = 名字 + 版本，起点 **V0.0.1**。文件 `<name>_v0_0_1.py`，
常量 `STRATEGY_VERSION = "0.0.1"`，回测结果与预注册/裁定/报告的文件名都带同一版本串。
MAJOR=信号逻辑变、MINOR=参数变、PATCH=重构且须证明输出逐字节不变。
细则见 `/quant-code-standards` §4.5.1。

### 2.0 信号只有一份（硬性）

`<venue>/strategy/` 是信号的**唯一副本**。`backtest/` 与 `<venue>/execution/`
都 import 同一份，不得各写一份「回测版」与「实盘版」。各写一份是回测与实盘结果
系统性背离的头号来源。为此 strategy 必须是纯函数：输入快照与持仓，输出目标仓位，
不读网络、不写状态、不下单。

### 2.2 `backtest/` 布局

| 路径 | 职责 |
|---|---|
| `backtest/engine/` | 场所无关：撮合、账本、持仓推进、绩效指标 |
| `backtest/okx/` | OKX 适配：费率档、资金费率、滑点假设、年化因子 365 |
| `backtest/t212/` | T212 适配：佣金、印花税、FX 费、交易日历、年化因子 252 |
| `backtest/results/` | 结果落地，参数进文件名，gitignore |

模块登记（定位索引，设计依据见 `fixplans/`）：

| 模块 | 职责 |
|---|---|
| `engine/types.py` | 枚举与数据结构：Bar、Order、Fill、EngineConfig、INTERVAL_SECONDS |
| `engine/broker.py` | BrokerSim 协议（typing.Protocol）：回测模拟器与将来实盘适配器的同一接口 |
| `engine/feed.py` | 多标的 bar 流对齐、质量闸、FX 序列、MarketView（cutoff 视图） |
| `engine/matching.py` | 纯撮合规则：各订单类型的触发判定与原始成交价（O-H-L-C 序） |
| `engine/ledger.py` | GBP 现金（Decimal）、持仓、占用资金序列、权益估值 |
| `engine/engine.py` | 主循环，每根 bar 固定四步：结算成交、估值、调策略、差分下单 |
| `engine/metrics.py` | 业绩率与风险比率（口径见 `docs/backtest/framework/05_metrics_reporting.md`） |
| `engine/results.py` | trades / equity / meta 三件套落地 |
| `engine/report.py` | 每轮图表（净值 mid/清算双线 + 在场底色 + 逐标的开仓区间横道） |
| `engine/strategy_loader.py` | 按 (venue, name, version) 加载 `<venue>/strategy/` 模块并校验契约（契约见 `docs/backtest/framework/06_strategy_plugin.md`） |
| `okx/data_source.py` | 读 Binance 归档 spot klines（`data/binance/curated/`，经 `common/paths`，可注入 data_root）；okx 撮合/成本适配器待建 |
| `t212/data_source.py` | 读 `data/t212/curated/` parquet（经 `common/paths`，可注入 data_root） |
| `t212/instruments.py` | 交易所时区映射、半点差表、印花税适用性、年化因子 252 |
| `t212/costs.py` | FX 费、SDRT、PTM、FINRA、SEC 费与折算函数（依据均注明出处） |
| `t212/faults.py` | 平台故障注入（目录见 `fixplans/t212/platform/01_fault_catalog.md`） |
| `t212/admission.py` | 订单准入检查（固定顺序）与最坏成本预估，供 broker_sim 调用 |
| `t212/broker_sim.py` | T212 撮合模拟器：订单生命周期、撤单/过期/资格判定、提交 |
| `t212/fills.py` | 成交记账：点差/滑点、成交量预算、费用栈、账本与冻结额 |
| `t212/same_close.py` | same_close 成交时序：决策 bar 收盘成交的前置条件与执行 |
| `t212/runner.py` | 组装点，一次调用串起六步：读数据、建 feed、建 broker、跑 engine、算 metrics、落地 |

纪律见 `/backtest-discipline`。

### 2.3 `trading212/` 实盘执行模块（2026-08-22 改为小时频）

设计遵循 `/live-trading-architecture`；规格为 `fixplans/t212/a0/02_execution.md`。
T212 无行情接口也无推送通道，故「主循环」体现为**每个交易场次两相位的批处理**：
`decide` 在场次内 15:30（纽约）决策、收盘前 60 秒提交市价单，成交落在当日收盘价；
`settle` 在收盘后从账单收割成交。入口 `python -m trading212.execution.run_a0 <phase>`。

策略模块（`trading212/strategy/`，回测与实盘共用同一份）：

| 模块 | 职责 |
|---|---|
| `a0_v0_0_1.py` | A0：18 只固定名单、TSMOM-252 信号、QQQ 双闸、等槽定量、免加仓带 |
| `a0_intraday_v0_0_1.py` | A0 的时序适配层：15:30 决策、信息止于前一根 bar、合成日线视图（`daily_view()` 对外开放供 B0 复用），信号仍委托 `a0_v0_0_1` |
| `a1_v0_0_1.py` | A1：约 1,500 只宽池、五条因果流动性准入、12-1 动量排名、前 20/前 40 缓冲带、等权定量。`rank_table` 是准入与分数的唯一实现 |
| `b0_v0_0_1.py` | B0：A0 与 A1 共用一个账户的资金分配。读 A0 信号集（合成现金视图）、按 `priority` 划归属、A1 吸收剩余资金并套 10% 免动带；`signal_diagnostics` 是看板整棵诊断树的来源 |

执行与传输模块：

| 模块 | 职责 |
|---|---|
| `client.py` | REST 传输：legacy 单钥鉴权、逐端点令牌桶限频、GET 重试；下单 POST 永不重试（venue 无幂等键），200 但不可解析同样抛 `OrderSubmitAmbiguousError`。`follow_page` 修补 transactions 端点只回查询串的分页缺陷 |
| `archive.py` | 记账归档：把券商的历史订单/流水/分红原样落 `trading212/records/`，按 venue 自身 id 去重，增量走到已知记录即停 |
| `execution/instruments.py` | 标的映射（`META→FB_US_EQ`，S4 已验证）与场次日历：`Session` 记录、半日市判定、15:30 决策键（US 表无 `CLOSE` 事件，常规收盘由 `AFTER_HOURS_OPEN` 标记） |
| `execution/market_data.py` | 1h/1d 刷新与读取、日内截止视图（`LiveMarketView`，与引擎 `MarketView` 同鸭型）、日内新鲜度闸（含 FX 必须落在决策键前 90 分钟）；B0 的三个接缝：`us_sessions`（场次真值 = 本地 SPY 日线，半日市计入）、`load_b0_injection`（只读注入包，看板可调）、`refresh_for_decision`（决策前刷新，含 A1 短窗、120 秒时间盒与 thin 语义） |
| `execution/strategy_loader.py` | 执行侧按路径加载策略模块并校验身份；支持日内壳的 `make_strategy()` 工厂注入日线历史 |
| `execution/shadow_ledger.py` | 事件溯源影子账本：event_id 幂等（复发事件带尝试计数）、写前意向、歧义冻结、组合视图 |
| `execution/ledger_store.py` | 账本持久化子层：JSONL fsync 追加、原子快照替换、装载完整性规则 |
| `execution/risk_gate.py` | 只收紧风控闸：限额缺失或为零即整体失效关闭；必须处于提交窗口内；卖量钳到持仓；数量步进逐标的（`qty_steps.json`，从拒单中学）；单笔名义上限只约束买单；卖出残量低于最小值即放大为全平；拒单理由与回测同词表 |
| `execution/order_router.py` | 唯一下单出口：意向先落账 → POST → 回执落账；DRY_RUN 短路；未带 `--allow-orders` 降级演练并 CRITICAL；`extendedHours` 恒为 false |
| `execution/order_monitor.py` | 挂单轮询至离场 + 从 `history/orders` 账单收割成交（含逐笔税费），对齐量后退休订单 |
| `execution/reconciler.py` | 账本与账户单向对账；歧义只凭正证据解除（ticker+方向+数量+建单时刻），不自动修账 |
| `execution/session_cycle.py` | 相位编排与全部闸门顺序；按场次防重；决策后等到收盘前提交瞬间；`_diff_to_intents` 镜像引擎差分语义与提交顺序；`assemble_params` 是决策与看板共用的唯一参数装配处；`adopt_book` 把 A0 账本移交 B0 |
| `execution/run_a0.py` | CLI：decide / settle / status / init-ledger / adopt-book / halt / daemon，带 fcntl 单实例锁。模块名保留 `run_a0` 不改：看板以子串 `run_a0` 识别进程 |
| `ingest/a1_rank.py` | 盘前第四 pass：已收盘场次上把 1,500 只候选池排一次名，落 `data/t212/curated/a1/rank/<date>.parquet`。准入与分数全部委托 `strategy/a1_v0_0_1.py::rank_table`；覆盖率不足 95% 的场次拒绝出表 |

状态落地：`common/paths.execution_state_dir("t212")` → `data/t212/execution_state/`
（账本日志与快照、场次状态、halt 旗标、日历缓存；机器本地，不入库）。

回测一致性由 `tests/execution/test_backtest_equivalence.py` 以真实数据守卫：
同一决策键下，实盘数据路径与引擎数据路径喂给同一策略模块的目标必须逐标的相等。

### 2.4 `trading212/dashboard/` 本地看板（2026-08-22）

本机浏览器界面，只读也只画：展示策略账本与账户、延迟行情、资产曲线，集中管理
上交易前必须填的配置，并提供一个独立的手动下单页。**不启动也不停止策略**——策略
是另一个按计划运行的进程，关掉看板对它无影响。启动件为仓库根目录 `dashboard.command`。

| 模块 | 职责 |
|---|---|
| `context.py` | 进程内共享：配置、券商客户端（快速失败档：6 秒超时、不重试）、账本读取、关注标的 |
| `collector.py` | 秒更采集：账户与行情各自独立轮询，采样线程只读缓存，故数据源变慢不影响刷新节奏 |
| `snapshots.py` | 最新快照原子替换 + 逐日采样追加 + **按日汇总**（长跨度曲线的来源）；读取时降采样，且始终保留停机断点标记 |
| `quotes.py` | 延迟行情（与策略同一数据源，1 分钟粒度），逐标的报告新鲜度 |
| `settings.py` | 上交易前配置的读、校验、写回；只产出问题码，措辞留给界面 |
| `manual_orders.py` | 手动下单：三重条件缺一不可（配置 live、界面确认、非演练），独立留痕、不进策略账本 |
| `api.py` / `server.py` | 路由（返回纯数据）与本地服务；只绑 127.0.0.1，写操作需本次运行令牌。含紧急停止（落旗随时可做、解除须过账本与对账检查）、策略资金调整（写成账本事件）、场次跨度查询（供图表标注美股交易时段） |
| `assets/` | 页面与脚本；**全部中文文案集中在 `labels.json`**，源码保持纯 ASCII |

界面语言为中文属用户当轮指定（`CLAUDE.md` §2.3 表格「用户指定的输出」一行）。
`plotly.min.js` 不入库，由服务端从已安装的 plotly 包下发并长缓存。

### 2.1 `common/` 现有模块

本表是**定位索引**：改功能时先查这里缩到文件，再看该文件 docstring 的功能索引缩到函数
（`/quant-code-standards` §4.9）。新增模块必须先在本表登记再写代码。

| 模块 | 职责 |
|---|---|
| `paths.py` | 项目路径常量与数据分区路径构造，全项目唯一路径来源 |
| `config.py` | 加载 `<venue>/config/*.yaml`，按 `QUANT_ENV` 选 paper/live，默认 paper |
| `secrets.py` | **唯一**的密钥读取入口（`secrets/` 或环境变量），带脱敏 |
| `logging_setup.py` | 日志初始化，UTC 时间，落 `logs/<模块>_YYYYMMDD.log` |
| `alerts.py` | CRITICAL 交易事件的本机系统通知（尽力而为，永不抛错） |
| `net.py` | 指数退避、令牌桶限频、可重试与不可重试异常分类。不含 HTTP 会话对象，会话由各调用方自建 |
| `store.py` | parquet 原子写入、临时件清理 |

后续按需新增（先更新本表再写代码）：`metrics.py`（业绩率与风险比率）、
`risk.py`（风控闸）。回测引擎不在这里，在 `backtest/`。

## 3. 数据布局

### 3.1 数据源 ≠ 交易场所

`data/` 下的一级目录是**数据源 slug**（`common/paths.py` 的 `DATA_SOURCES`），
不是交易场所 slug（`VENUES`）。二者是包含关系：

| slug | 是数据源 | 是交易场所 | 说明 |
|---|:--:|:--:|---|
| `binance` | 是 | 否 | 只取数据。FCA 自 2021-01-06 禁止英国零售交易加密衍生品；`api.binance.com` 从本机返回 HTTP 451 |
| `okx` | 是 | 是 | |
| `t212` | 是 | 是 | 行情实取自 Yahoo，T212 无行情接口 |

数据源不得因此获得空的 `execution/` 目录；场所代码也不得假定每个数据源都能下单。

### 3.2 字节与说明分离（硬性）

| 路径 | 内容 | 入库 |
|---|---|---|
| `data/<source>/{raw,curated}/` | 全部字节 | 否。gitignore 整棵树，预期迁往外置磁盘 |
| `docs/data/<source>/DATA_SPEC.md` | 字段、单位、时区、已知陷阱、已知不存在的数据 | 是 |
| `docs/data/<source>/MANIFEST.jsonl` | 每个分区一条：坐标、上游 URL、本地字节数与行数 | 是 |
| `docs/data/<source>/GAPS.csv` | 缺口登记：dataset, symbol, from, to, cause, state | 是 |

说明件**不得**放在 `data/` 内。整棵 `data/` 不入库且预期迁往外置磁盘，
放在字节旁边的说明会跟着磁盘走，仓库里将不剩任何关于这批数据的记录。

`MANIFEST.jsonl` 由 `scripts/build_data_manifest.py` 生成，
`sync_to_git.command` 在提交前自动重建。该文件**不含生成时间戳**：
数据无变更时重跑须逐字节相同，否则每天同步都会提交一个无信息量的 diff。

### 3.3 实际分区

Binance（`market` = spot / um / cm；`leaf` = kline 族取 bar 周期，其余取 dataset 名）：

```
data/binance/curated/<market>/<dataset>/<symbol>/<leaf>/year=YYYY/<stamp>.parquet
```

`stamp` 三种形式：`YYYY-MM`（月度归档件）、`YYYY-MM-DD`（日度）、
`YYYYMMDD`（2026-08-19 那版 bookTicker 加载器留下的旧式日度，读侧兼容）。
由 `common/paths.py` 的 `stamp_freq()` 判别，它决定重建时该取 daily 还是 monthly 的 URL。

股票（Yahoo 取数，落 `t212` 源）：

```
data/t212/curated/<group>/<symbol>/1d/<symbol>_<year>.parquet
data/t212/curated/<group>/<symbol>/<interval>/<symbol>_<start>_<end>_<interval>.parquet
```

路径构造一律经 `common/paths.py`，不得在业务代码里自行拼接。

`data/reference/` 合约规格、费率、交易日历（须注明取自哪个接口、取回时间）。

## 4. Skills 索引

| Skill | 何时用 |
|---|---|
| `verified-dev` | 任何新增/修改代码（**主流程**） |
| `standardized-bug-fix` | 修 bug、排查运行异常 |
| `quant-code-standards` | 写代码、代码审查、新建模块 |
| `quant-error-handling` | 接外部 API、重试限频、日志、故障定位 |
| `market-data-pipeline` | 下载/清洗/校验行情数据 |
| `strategy-research` | 构思与验证策略想法（预注册驱动） |
| `backtest-discipline` | 任何回测的新建/修改/重跑/评估 |
| `live-trading-architecture` | 设计与编写常驻交易进程 |
| `live-trading-risk-check` | 改动执行层或风控闸后的整表复查（**接实盘前必过**） |
| `html-report` | 产出带图表的报告 |

## 5. 环境

- Python 3.11+，UTF-8，依赖见 `requirements.txt`
- `QUANT_ENV`：`paper`（默认）/ `live`
- 密钥只放 `secrets/`（权限 600，gitignore）或环境变量
- 文件名与目录名一律 ASCII，无例外（`CLAUDE.md` §4.1）
