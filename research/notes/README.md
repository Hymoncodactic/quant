# research/notes

## 1. 职责

装研究过程的笔记：文献摘录、探索性实证的结论、调研发现。本目录是三类研究留痕中
约束最松的一类，`CLAUDE.md` §2.2 的文档客观性条款对本目录不适用，可自由记述。

不装：预注册标准（在 `research/prereg/`）、口径裁定（在 `research/decisions/`）、
可复用代码（在 `scripts/` 或对应模块目录）。

笔记不是裁定。笔记里的结论要进入回测或实盘口径，必须另行写成 `research/decisions/`
下的裁定文件。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `20260819_negative_correlation_findings.md` | 负相关标的的实证结论。方法为本机实测加多智能体调研并对抗性验证，对应脚本 `scripts/20260819_crypto_correlation_probe.py` 与 `scripts/20260819_equity_hedge_probe.py` | 记录「是否存在流动性好且与 BTC 负相关的币」这一前提的检验结果。删除后两个探针脚本的产出失去解释，脚本本身也失去保留理由 | `scripts/README.md` 中两个探针脚本的结论出口 |
| `20260819_t212_execution_and_liquidity.md` | Trading 212 上 LSE 标的的流动性与执行成本，数据取自伦交所自有接口与 T212 官方文件 | 点差与执行成本的取证记录，是 `backtest/t212/costs.py` 与 `instruments.py` 中若干取值的上游依据 | `backtest/t212/` 的成本口径 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：探针脚本的运行输出。
写：无，本目录只存文档。
被谁引用：`scripts/README.md`、`backtest/t212/` 的成本依据、各报告的 Reference 清单。

## 5. 产出与清理

无运行产物。笔记为长期留痕件，一律保留。
被后续证据推翻的笔记不删除，在原文追加更正段落并注明依据。

## 6. 变更记录

2026-08-22 建立本文件，登记现有两份笔记。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
