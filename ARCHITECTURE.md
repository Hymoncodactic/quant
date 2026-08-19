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
| `data/` | 全部落地数据 | gitignore |
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

后续按需新增（先更新本表再写代码）：`store.py`（parquet 读写与校验）、
`metrics.py`（业绩率与风险比率）、`risk.py`（风控闸）。回测引擎不在这里，在 `backtest/`。

## 3. 数据布局

```
data/<venue_slug>/
├── raw/        原始返回，只增不改（CLAUDE.md §3.4）；元信息见 _manifest.jsonl
└── curated/    清洗后，每目录配 DATA_SPEC.md；缺口登记 _gaps.csv
data/reference/ 合约规格、费率、交易日历（须注明取自哪个接口、取回时间）
```

分区：`data/<venue>/<layer>/<instrument>/<period>/year=YYYY/YYYYMMDD.parquet`

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
