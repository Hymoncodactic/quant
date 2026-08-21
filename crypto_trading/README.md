# crypto_trading

## 1. 职责

OKX 侧（加密货币方向）的交易代码，按 `ARCHITECTURE.md` §2 划分为客户端、接入、策略、
执行、配置五层。只装与交易所打交道以及直接为其服务的代码。不装回测（在 `backtest/`），
不装两条线共用的基础设施（在 `common/`），不装密钥（在 `secrets/`）。

当前实现程度：只有 `ingest/` 有实现代码，且实现的是 Binance 公共归档的取数与解析层，
不是 OKX 接口；`strategy/` 与 `execution/` 为空骨架；`ARCHITECTURE.md` §2 规定的
`client.py`（REST/WS 客户端，由 ingest 与 execution 共用）尚未创建。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 0 字节空文件，把本目录声明为常规 Python 包 | 删除后本目录不再是常规包，包边界与 `CLAUDE.md` §4.4 要求的模块头 docstring 落点一并消失。当前文件为空，§4.4 要求的 docstring 尚未写 | `scripts/update_data.py:43`、`scripts/20260819_ingest_crypto_phase1.py:33`、`scripts/20260819_ingest_crypto_bookticker.py:50`、`scripts/build_data_manifest.py:49` 均以 `from crypto_trading.ingest import binance_archive` 绝对导入 |

## 3. 子目录索引

| 子目录 | 说明文档 | 一句话职责 |
|---|---|---|
| `config/` | `config/README.md` | 非密钥运行配置与标的池模板 |
| `execution/` | `execution/README.md` | 下单、撤单、状态机与对账，骨架待实现 |
| `ingest/` | `ingest/README.md` | 行情数据下载与解析，已实现 Binance 归档层 |
| `strategy/` | `strategy/README.md` | 信号的唯一副本，骨架待实现 |

## 4. 依赖关系

1. 读代码：`common/net.py` 的 `backoff_seconds`（`crypto_trading/ingest/binance_archive.py:46`），
   以及第三方 `pandas`。
2. 读外部：`https://data.binance.vision` 与
   `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision`，均无鉴权。
3. 写：本目录代码不写任何数据路径。`fetch_to_frame()` 返回内存表，落盘由 `scripts/`
   下的入口脚本经 `common/store.py` 完成。
4. 被谁 import：`scripts/` 下四个入口脚本，见 §2。`common/paths.py:84` 的
   `VENUE_DIRS["okx"]` 指向本目录，`config_dir("okx")` 与 `venue_dir("okx")` 由此解析。
5. 依赖方向单行（`ARCHITECTURE.md` §2）：本目录不得反向 import `backtest/`。

## 5. 产出与清理

`__pycache__/` 是解释器运行产物，已被 `.gitignore` 排除，可随时删除。除此之外本目录
无运行产物。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
