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
| `reports/` `logs/` `scripts/` `tests/` `secrets/` | 见下 | |

## 2. 代码分层

依赖方向**单行**，禁止反向导入：

```
backtest/  ─┐
            ├─→ <venue>/strategy/ ─→ common/
<venue>/execution/ ─┤
                    └─→ <venue>/client.py ─→ common/
<venue>/ingest/ ────────→ <venue>/client.py ─→ common/
```

| 层 | 路径 | 允许 | 禁止 |
|---|---|---|---|
| 基础 | `common/` | 路径、配置、密钥、日志、网络与限频、存储、指标 | 任何场所特有的口径 |
| 客户端 | `<venue>/client.py` | REST/WS 连接、鉴权、签名、限频；被 ingest 与 execution 共用 | 业务决策 |
| 接入 | `<venue>/ingest/` | 下载落 `data/<venue>/raw/`、清洗到 `curated/` | 交易决策 |
| 策略 | `<venue>/strategy/` | 信号计算，**纯函数**（快照+持仓 → 目标仓位） | 下单、读网络、写状态 |
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
都 import 同一份，⛔ 不得各写一份「回测版」和「实盘版」——那是回测与实盘结果
系统性背离的头号来源。为此 strategy 必须是纯函数：输入快照与持仓，输出目标仓位，
不读网络、不写状态、不下单。

### 2.2 `backtest/` 布局

| 路径 | 职责 |
|---|---|
| `backtest/engine/` | 场所无关：撮合、账本、持仓推进、绩效指标 |
| `backtest/okx/` | OKX 适配：费率档、资金费率、滑点假设、年化因子 365 |
| `backtest/t212/` | T212 适配：佣金、印花税、FX 费、交易日历、年化因子 252 |
| `backtest/results/` | 结果落地，参数进文件名，gitignore |

纪律见 `/backtest-discipline`。

### 2.1 `common/` 现有模块

本表是**定位索引**：改功能时先查这里缩到文件，再看该文件 docstring 的功能索引缩到函数
（`/quant-code-standards` §4.9）。⛔ 新增模块必须先在本表登记再写代码。

| 模块 | 职责 |
|---|---|
| `paths.py` | 项目路径常量与数据分区路径构造，全项目唯一路径来源 |
| `config.py` | 加载 `<venue>/config/*.yaml`，按 `QUANT_ENV` 选 paper/live，默认 paper |
| `secrets.py` | **唯一**的密钥读取入口（`secrets/` 或环境变量），带脱敏 |
| `logging_setup.py` | 日志初始化，UTC 时间，落 `logs/<模块>_YYYYMMDD.log` |
| `net.py` | HTTP 会话、指数退避、令牌桶限频 |
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

```
data/                       全部字节。gitignore 整棵树，预期迁往外置磁盘
└── <source>/{raw,curated}/

docs/data/<source>/         全部说明与凭据。入库，随代码一起版本化
├── DATA_SPEC.md            字段、单位、时区、已知陷阱、已知不存在的数据
├── MANIFEST.jsonl          每个分区一条：坐标、上游 URL、本地字节数与行数
└── GAPS.csv                缺口登记：dataset, symbol, from, to, cause, state
```

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

路径构造一律经 `common/paths.py`，⛔ 不得在业务代码里自行拼接。

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
