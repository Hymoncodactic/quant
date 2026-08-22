# docs：数据说明与重建凭据

## 1. 职责

存放描述数据的文件，与存放数据字节的 `data/` 一一对应。`data/` 整棵被排除于版本
控制之外（`.gitignore` L17 `/data/`）且预期迁往外置磁盘；说明件若放在字节旁边，会
随磁盘一起离开仓库，仓库里将不剩任何关于那批数据的记录。因此说明与重建凭据一律
落在本目录并随代码入库（`ARCHITECTURE.md` §3.2）。

本目录不装数据字节，不装研究留痕（在 `research/`）、框架建设计划（在 `fixplans/`）
与报告成品（在 `reports/`，不入库）。

## 2. 文件清单

除本 `README.md` 外，本目录顶层无文件。

## 3. 子目录索引

| 子目录 | 内容 | 说明文档 |
|---|---|---|
| `data/` | 按数据源分目录的字段口径、重建凭据与缺口登记 | `docs/data/README.md` |

## 4. 依赖关系

1. 路径在 `common/paths.py` 定义：`DIR_DOCS = ROOT / "docs"`（L78）、
   `DIR_DOCS_DATA = DIR_DOCS / "data"`（L79），二者均在该模块的 `__all__`（L52）导出。
2. 本目录不含任何 `.py` 文件，不被任何模块 import。
3. 写入方只有 `scripts/build_data_manifest.py`，且只写到子目录
   `docs/data/<source>/MANIFEST.jsonl`，不在本目录顶层落文件。
4. 读取方为人与 AI 读者。代码与计划文件通过注释引用本目录下的口径，引用点清单见
   `docs/data/README.md` §4。

## 5. 产出与清理

本目录顶层无运行产物。子目录内的产出与保留规则见 `docs/data/README.md` §5。

磁盘上存在 macOS Finder 生成的 `.DS_Store`。该文件已被 `.gitignore`（L33）阻止入库，
但仍占据磁盘位置；按 `CLAUDE.md` §4.2.3 与 §4.2.4，属应从磁盘删除的工具产物。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
