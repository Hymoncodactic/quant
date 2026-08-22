# docs/data：按数据源分目录的数据说明

## 1. 职责

每个数据源一个子目录，装该源的三类入库件：字段与单位口径（`DATA_SPEC.md`）、
重建凭据（`MANIFEST.jsonl`）、缺口登记（`GAPS.csv`）。合法源名由
`common/paths.py` 的 `DATA_SOURCES`（L91）限定，取值为 `binance`、`okx`、`t212`。

本目录不装数据字节（在 `data/<source>/{raw,curated}/`，整棵不入库），不装下载与
清洗代码（在 `<venue>/ingest/` 与 `scripts/`），不装回测结论（在
`research/decisions/`）。

## 2. 文件清单

除本 `README.md` 外，本目录顶层无文件。三类说明件一律落在各数据源子目录内。

## 3. 子目录索引

| 子目录 | 数据源 | 现有说明件 | 说明文档 |
|---|---|---|---|
| `binance/` | Binance 公开归档，仅研究数据源，非交易场所 | `DATA_SPEC.md`、`MANIFEST.jsonl`（9,344 行） | 本轮未建立 `README.md` |
| `okx/` | OKX，加密线交易场所 | 仅 `MANIFEST.jsonl`（1 行，计数全零） | `docs/data/okx/README.md` |
| `t212/` | Trading 212 股票线，行情实取自 Yahoo | `DATA_SPEC.md`、`MANIFEST.jsonl`（3,785 行） | 本轮未建立 `README.md` |

`binance/` 与 `t212/` 的 `README.md` 属 `CLAUDE.md` §4.3 要求但尚未补齐的项。

## 4. 依赖关系

读取本目录内容的一方：

| 引用点 | 引用对象 | 用途 |
|---|---|---|
| `common/paths.py` L306 `docs_data_dir()` | 本目录下的源子目录 | 构造路径，业务代码不得自行拼接 |
| `common/paths.py` L312 `data_spec_path()` | `DATA_SPEC.md` | 同上 |
| `common/paths.py` L317 `manifest_path()` | `MANIFEST.jsonl` | 同上 |
| `common/paths.py` L329 `gaps_path()` | `GAPS.csv` | 同上 |
| `backtest/okx/data_source.py` L9 | `docs/data/binance/DATA_SPEC.md` | 记录该 loader 依据的字段与时间戳口径 |
| `backtest/t212/data_source.py` L8 | `docs/data/t212/DATA_SPEC.md` §5.1 | 目录与文件命名口径 |
| `backtest/engine/types.py` L32 | `docs/data/t212/DATA_SPEC.md` | 各周期每根 bar 的秒数 |
| `backtest/t212/README.md` L13 | `docs/data/t212/DATA_SPEC.md` §3 | 日线时间戳落在交易所本地零点的实证 |
| `docs/backtest/framework/02_data_layer.md` L13-L17 | `docs/data/t212/DATA_SPEC.md` §1/§3/§5 | 数据层计划的五条事实依据 |
| `research/decisions/20260821_backtest_data_sources.md` L18 | `docs/data/t212/DATA_SPEC.md` §5 | 1h 历史深度约 730 天的限定 |
| `.claude/skills/verified-dev/SKILL.md` L57 | `docs/data/<source>/DATA_SPEC.md` | 开发流程规定：涉及数据先读该文件 |

写入本目录的一方只有 `scripts/build_data_manifest.py`。该脚本按 `--source` 参数
选定数据源；不给该参数时，取 `DATA_SOURCES` 中所有 `data/<source>/curated/` 目录
已存在的源（L351-L352），逐个重写其 `MANIFEST.jsonl`。`sync_to_git.command` L49
在提交前调用该脚本。`DATA_SPEC.md` 与 `GAPS.csv` 由人与 AI 手写维护，无生成脚本。

## 5. 产出与清理

| 文件类型 | 性质 | 保留规则 |
|---|---|---|
| `MANIFEST.jsonl` | 运行产物，可由 `build_data_manifest.py` 重新生成 | 必须保留并入库。它是 `data/` 整棵不入库仍能被核对与重建的唯一依据；删除后仓库对那批字节不再有任何记录 |
| `DATA_SPEC.md` | 手写 | 必须保留 |
| `GAPS.csv` | 手写登记 | 有缺口时必须保留；无缺口时可不存在 |

`MANIFEST.jsonl` 不含生成时间戳（`build_data_manifest.py` L233 `_write_manifest`
的设计约束），数据无变更时重跑逐字节相同，因此不会产生无信息量的提交差异。

磁盘上存在 macOS Finder 生成的 `.DS_Store`，已被 `.gitignore`（L33）阻止入库，
按 `CLAUDE.md` §4.2.3 属应从磁盘删除的工具产物。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
