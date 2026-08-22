# docs/data/okx：OKX 数据源的说明与重建凭据

## 1. 职责

存放 OKX 这一数据源的入库说明件：字段与单位口径、重建凭据、缺口登记。

本目录不装数据字节（应在 `data/okx/{raw,curated}/`），不装 OKX 接口客户端与下载
代码（应在 `crypto_trading/`），不装回测口径裁定（在 `research/decisions/`）。

当前状态：OKX 侧尚无任何落地数据。`data/okx/raw/` 与 `data/okx/curated/` 只含
`.gitkeep`，无 parquet 文件；`crypto_trading/ingest/` 下只有 `binance_archive.py`
与 `schemas.py`，不存在 OKX 下载器。加密线的回测数据当前取自 Binance 归档
（`backtest/okx/data_source.py` L1-L14），OKX 撮合与成本适配器未建，前置条件是先按
`CLAUDE.md` §1.1 的 S4 取证 OKX 现货费率档、最小下单量、精度与限频
（`research/decisions/20260821_backtest_data_sources.md` §二.5）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `MANIFEST.jsonl` | 重建凭据。当前只有一行 `_meta`：`{"bytes": 0, "files": 0, "rows": 0, "source": "okx"}`，无分区记录 | 删掉它，本目录将不再留下「OKX 侧当前零落地数据」这一入库事实；一旦 OKX 数据落地，`build_data_manifest.py` 的 `load_existing()` 无旧记录可复用，行数须对每个 parquet 重读 footer | `common/paths.py` L317 `manifest_path("okx")` 构造路径；`scripts/build_data_manifest.py` 的 `load_existing()`（L160）读、`_write_manifest()`（L233）写；`sync_to_git.command` L49 在提交前重建。无任何业务模块读取其内容 |

`MANIFEST.jsonl` 之所以只有 `_meta` 一行：`build_data_manifest.py` 的 `SCANNERS`
（L150）只登记了 `binance` 与 `t212` 两个专用扫描器，`okx` 走
`build_manifest()` 的回退分支（L191）落到 `scan_generic()`（L140）；后者遍历
`data/okx/raw/` 与 `data/okx/curated/` 下的 `*.parquet`，当前一个都没有，故记录数
为零，而 `_meta` 行仍照常写出。

缺失的说明件：

| 文件 | 状态 | 补齐条件 |
|---|---|---|
| `DATA_SPEC.md` | 不存在 | `ARCHITECTURE.md` §3.2 要求每个源都有。OKX 数据落地时同批建立，写明字段、单位、时区、bar 时间戳语义、已知陷阱 |
| `GAPS.csv` | 不存在 | 出现已知缺口时建立，列为 dataset, symbol, from, to, cause, state（`common/paths.py` L329-L334） |

## 3. 子目录索引

无。

## 4. 依赖关系

1. 本目录路径由 `common/paths.py` 的 `docs_data_dir("okx")`（L306）返回，源名合法性
   由 `_check_source()`（L343）对 `DATA_SOURCES`（L91）校验。
2. 本目录只被写入一次性刷新，不被任何模块 import，也无模块读取其内容。
3. 与 `data/okx/` 一一对应：字节在那边且不入库，说明在本目录且入库
   （`ARCHITECTURE.md` §3.2）。
4. `.claude/skills/quant-code-standards/SKILL.md` L191 与
   `.claude/skills/verified-dev/SKILL.md` L66 以本目录下的文件为示例路径。

## 5. 产出与清理

`MANIFEST.jsonl` 是运行产物，可由 `python scripts/build_data_manifest.py --source okx`
重新生成，但必须保留并入库。该文件不含生成时间戳，无数据变更时重跑逐字节相同。

本目录无可清理项。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
