# crypto_trading/ingest

## 1. 职责

装加密方向的行情数据接入代码：向数据源取数、校验完整性、按数据集布局解析成内存表。
当前实现只覆盖 Binance 公共批量归档 `data.binance.vision`，不含任何 OKX 接口。
不负责落盘（写 parquet 由 `scripts/` 下的入口脚本经 `common/store.py` 完成），
不负责交易决策。

数据源与交易场所是两个概念：Binance 只供数据，下单场所是 OKX。二者在
`common/paths.py` 的 `DATA_SOURCES` 与 `VENUES` 中分列（第 83 至 91 行）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `binance_archive.py` | Binance 归档客户端。对外五个函数：`build_url`（组装对象 URL；K 线族文件按 bar interval 命名而非按 dataset 命名，只有目录能区分是哪一种 K 线）、`list_prefix`（S3 ListObjects 枚举并跟随分页，剔除 `.CHECKSUM` 旁文件）、`head_size`（HEAD 取字节数，返回 None 即对象不存在）、`available_dates`（实际存在的日期，覆盖范围靠发现而非假设）、`fetch_to_frame`（下载、校验 SHA-256、解压、按 schema 解析成 DataFrame）。对外常量 `CDN_BASE`、`S3_LIST_BASE`；模块级常量 `TIMEOUT_SEC = 180`（单次套接字操作超时，不是整体传输预算）、`GET_ATTEMPTS = 5`、`MAX_CONCURRENCY = 8` | 删除后四个入口脚本全部无法取数。内部 `_get()` 的五次指数退避重试是实测必需：`WORKING_MEMORY.md:104` 记录改造前实测错误率 55%（30/55）。404 单独处理、立即抛出不重试，因为它是覆盖范围的事实而非故障 | `scripts/update_data.py:43`、`scripts/20260819_ingest_crypto_phase1.py:33`、`scripts/20260819_ingest_crypto_bookticker.py:50`、`scripts/build_data_manifest.py:49`（后者只用 `build_url`，见其第 105 行） |
| `schemas.py` | 15 个 `(market, dataset)` 组合的列布局表 `SPECS`；数据类 `DatasetSpec`（列名与顺序、是否带表头、时间戳列、需丢弃的列、路径是否含 interval 段）；函数 `spec_for()` 与 `timestamp_unit()`；常量 `MICROSECOND_SWITCH = "2025-01-01"` | 删除后 `binance_archive.py:47` 的 import 断。它还是两处静默数据损坏的唯一拦截点：其一，现货 CSV 不带表头，按表头推断读取会吞掉每天第一笔成交；其二，现货时间戳自 2025-01-01 起为微秒、此前为毫秒（期货始终毫秒），按错误单位解析会把数据放到公元 55000 年 | `binance_archive.py:47` 导入 `spec_for` 与 `timestamp_unit`；`docs/data/binance/DATA_SPEC.md:10` 与 `:33` 把本文件登记为该数据集的解析层依据 |
| `__init__.py` | 0 字节空文件，把本目录声明为常规 Python 包 | 删除后包边界与 `CLAUDE.md` §4.4 的模块头 docstring 落点消失。当前文件为空，该 docstring 尚未写 | 四个入口脚本以 `from crypto_trading.ingest import binance_archive` 导入 |

## 3. 子目录索引

无。目录下的 `__pycache__/` 按 `CLAUDE.md` §4.3 的豁免表不需要说明文档。

## 4. 依赖关系

1. 读代码：`common/net.py` 的 `backoff_seconds`（`binance_archive.py:46`）；本目录内
   `schemas.py` 的 `spec_for` 与 `timestamp_unit`（`binance_archive.py:47`）；第三方 `pandas`。
2. 读外部：`https://data.binance.vision` 取对象，
   `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision` 做枚举，二者均无鉴权。
   枚举只能对 S3 主机名发起，CDN 主机名返回的是单页应用而非 ListObjects XML
   （`binance_archive.py` 第 9 至 11 行）。
3. 写：无。`fetch_to_frame()` 返回 DataFrame，不触碰文件系统。落盘路径由
   `common/paths.py` 的 `binance_partition_path()` 决定，写入动作由 `common/store.py` 执行。
4. 被谁 import：`scripts/` 下四个入口脚本，见 §2。

## 5. 产出与清理

本目录代码本身不产出文件。`__pycache__/` 是解释器运行产物，当前含 `__init__`、
`binance_archive`、`schemas` 三个 `.pyc`，已被 `.gitignore` 排除，可随时删除。
下载得到的数据落 `data/binance/curated/`，其字段说明与重建凭据在 `docs/data/binance/`
（`DATA_SPEC.md` 与 `MANIFEST.jsonl`），两者都不在本目录。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
