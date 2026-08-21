# docs/data/binance

## 1. 职责

装 Binance 归档数据的说明与重建凭据。字节本身在 `data/binance/`，整棵 gitignore
且预期迁往外置磁盘；本目录是这批字节在仓库里唯一的记录（`ARCHITECTURE.md` §3.2）。

不装：任何 parquet 或原始 zip；任何解析代码（在 `crypto_trading/ingest/`）；
任何回测口径（在 `research/decisions/`）。

Binance 是数据源不是交易场所。下单场所是 OKX，二者在 `common/paths.py` 中分属
`DATA_SOURCES` 与 `VENUES` 两套注册表，不得混用。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `DATA_SPEC.md` | 字段、单位、时区、复权口径、已知陷阱与已知不存在的数据。记录端点为 `data.binance.vision` 批量归档，生成脚本为 `scripts/20260819_ingest_crypto_phase1.py`，解析层为 `crypto_trading/ingest/` 下两个模块，每个 zip 附 SHA-256 旁文件并逐个校验 | 字节不入库，字段语义只在此处。删除后无人能判断 parquet 里的列是什么单位、时间戳指向 bar 的哪一端，已落地的 9343 个分区随之不可用 | `backtest/okx/data_source.py` 的字段假设以此为准；`scripts/data_inventory.py` 的输出口径在此登记 |
| `MANIFEST.jsonl` | 重建凭据。9344 行，首行 `_meta` 汇总（68,698,242,528 字节、9343 个文件、9,015,841,101 行），其余每个分区一条，含坐标、上游相对路径、字节数与行数 | 这批 68.7 GB 字节唯一的入库凭据。删除后无法判断本地数据是否完整、是否被截断，也无法从上游重建 | `scripts/build_data_manifest.py` 写；`sync_to_git.command` 每次同步前重建 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：无，本目录只存文档与凭据。
写：`scripts/build_data_manifest.py` 写 `MANIFEST.jsonl`。
被谁引用：`backtest/okx/data_source.py`、`scripts/data_inventory.py`、
`research/decisions/20260821_backtest_data_sources.md`。

## 5. 产出与清理

`MANIFEST.jsonl` 是脚本产物但必须入库，不得清理。它刻意不写生成时间戳，
使数据无变更时重跑逐字节相同。`DATA_SPEC.md` 为手写长期件。

## 6. 变更记录

2026-08-22 建立本文件，登记现有两份说明与凭据（`CLAUDE.md` §4.3）。
