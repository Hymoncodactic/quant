# docs/data/t212

## 1. 职责

装股票行情数据的说明与重建凭据。字节本身在 `data/t212/`，整棵 gitignore；
本目录是这批字节在仓库里唯一的记录（`ARCHITECTURE.md` §3.2）。

不装：任何 parquet；任何取数代码（在 `trading212/ingest/yahoo_bars.py`）；
任何成本或执行口径（在 `backtest/t212/` 与 `research/notes/`）。

数据源是 Yahoo Finance，交易场所是 Trading 212，二者不是一回事：
可得的历史行情与实际可交易标的须分别核对。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `DATA_SPEC.md` | 字段、单位、时区、复权口径与取数方式。记录经 yfinance 1.6.0 取 Yahoo 数据，`auto_adjust=True` 已按拆股与分红复权（以 AAPL 2020 年 4:1 拆股无跳空验证），`period="max"` 不设人为起点，并写明必须逐标的单独请求的实测理由：批量请求会把 max 解析成统一 1927 起点并触发限流，24 个标的只返回 3 个 | 复权口径直接决定回测结论。删除后无人知道价格是否已复权，也无从判断「已复权价按毛额即时再投资」这一乐观偏差从何而来（`backtest/README.md` §7 第 1 条） | `backtest/t212/data_source.py` 的字段假设；`trading212/ingest/yahoo_bars.py` 的取数策略 |
| `MANIFEST.jsonl` | 重建凭据。3785 行，首行 `_meta` 汇总（56,250,381 字节、3784 个文件、1,688,391 行），其余每个分区一条，含分组、频率、相对路径、行数与校验值 | 字节不入库，manifest 是唯一的完整性凭据。删除后无法判断某标的某年是否缺失，也无法核对重下结果 | `scripts/build_data_manifest.py` 写；`sync_to_git.command` 每次同步前重建 |
| `GAPS.csv` | 缺口登记。每行一个标的的一段缺口：来源、分组、代码、周期、首末缺失场次、场次数、成因、状态、复核日期。2026-09-03 首版登记 11 行：7 只 2026-08-28 单日空洞、2 只尾部截断（LEG、WBS）、2 只候选池拼写不匹配（BRK-B、BF-B，设计使然） | A1 的准入用 `rolling(252, min_periods=252)`，一天空洞让该标的其后 252 个场次全部不可准入。缺口不登记就会被误读为「这只票不够流动」，而不是「这天没取到」 | 人工复核；`docs/data/t212/DATA_SPEC.md` §7 引用 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：无，本目录只存文档与凭据。
写：`scripts/build_data_manifest.py` 写 `MANIFEST.jsonl`。
被谁引用：`backtest/t212/data_source.py`、`trading212/ingest/yahoo_bars.py`、
`scripts/update_data.py` 的全窗口重取策略以本文件的复权说明为依据。

## 5. 产出与清理

`MANIFEST.jsonl` 是脚本产物但必须入库，不得清理。`DATA_SPEC.md` 与 `GAPS.csv` 为手写长期件。

## 6. 变更记录

2026-08-22 建立本文件，登记现有两份说明与凭据（`CLAUDE.md` §4.3）。
