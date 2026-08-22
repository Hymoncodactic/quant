# trading212/ 目录说明

## 1. 职责

装 Trading 212（场所 slug `t212`）这一条线的交易代码，按 `ARCHITECTURE.md` §2
的分层拆成 `config/`、`ingest/`、`strategy/`、`execution/` 四层。

不装：回测撮合与绩效统计（在 `backtest/`）、两条线共用的基础设施（在 `common/`）、
落地数据字节（在 `data/t212/`）、数据说明（在 `docs/data/t212/`）、
任何密钥（只在 `secrets/`）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 空文件（0 字节），把本目录声明为常规 Python 包，与 `crypto_trading/__init__.py` 同规格 | 三处绝对导入以 `trading212` 为包根：`scripts/update_data.py:44`、`scripts/20260822_a0_minute_backtest.py:42`、`trading212/strategy/a0_intraday_v0_0_1.py:56`。删除后这些导入仍能以隐式命名空间包解析，但包边界不再显式，且与 `crypto_trading/` 不一致 | 上述三处导入语句 |

本目录直属文件只有 `__init__.py` 一个，其余内容全部在四个子目录中。

`ARCHITECTURE.md` §2 登记的 `<venue>/client.py`（REST/WS 客户端，供 ingest 与
execution 共用）在本目录**尚未创建**。原因见 `WORKING_MEMORY.md` 未决项 2：
Trading 212 的 `equity/*` 系列接口返回 401（存在，需密钥），
`equity/prices`、`equity/quotes`、`market/candles` 均返回 404（不存在），
故本线的行情不走 T212，改由 `ingest/yahoo_bars.py` 取自 Yahoo。

## 3. 子目录索引

| 子目录 | 职责 | 说明文档 |
|---|---|---|
| `config/` | yaml 配置与标的池，永不含密钥 | `config/README.md` |
| `config/strategies/` | 策略参数基线，按策略名加版本一文件 | `config/strategies/README.md` |
| `execution/` | 下单、撤单、订单状态机、对账 | `execution/README.md` |
| `ingest/` | 行情下载与落地 | `ingest/README.md` |
| `strategy/` | 信号的唯一副本，纯函数 | `strategy/README.md` |

## 4. 依赖关系

读：`common/`（路径、配置、密钥、日志、网络、存储）、`data/t212/curated/`
（由 `ingest/` 写入、由 `backtest/t212/data_source.py` 读出）、
本目录 `config/` 下的 yaml。

写：`data/t212/curated/`（只有 `ingest/yahoo_bars.py` 写）。

被谁 import：

1. `scripts/update_data.py:44` 导入 `trading212.ingest.yahoo_bars`，执行日常增量更新。
2. `scripts/20260821_a0_framework_backtest.py:108` 经
   `backtest/engine/strategy_loader.py::load_strategy("t212", "a0", "0.0.1")`
   按路径加载 `strategy/a0_v0_0_1.py`。
3. `scripts/20260822_a0_minute_backtest.py:42` 直接导入
   `trading212.strategy.a0_intraday_v0_0_1`。
4. `common/paths.py:85` 把 slug `t212` 映射到本目录，`venue_dir()`、`config_dir()`
   与 `backtest/engine/strategy_loader.py::strategy_path()` 都经此解析路径。

依赖方向单行，禁止反向：本目录不得 import `backtest/`。

## 5. 产出与清理

本目录本身不产生运行产物。`ingest/` 的产物落在 `data/t212/curated/` 下，
不落在本目录内。

当前磁盘上存在下列不应保留的工具产物（`CLAUDE.md` §4.2.3 列为禁止留存，
`.gitignore` 已排除但未从磁盘删除）：

| 路径 | 类型 |
|---|---|
| `trading212/.DS_Store` | 系统产物 |
| `trading212/config/.DS_Store` | 系统产物 |
| `trading212/__pycache__/` | Python 字节码缓存 |
| `trading212/ingest/__pycache__/` | Python 字节码缓存 |
| `trading212/strategy/__pycache__/` | Python 字节码缓存 |

必须保留：`__init__.py` 与四个子目录下登记在册的源文件与配置文件。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
