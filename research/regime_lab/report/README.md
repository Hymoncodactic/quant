# research/regime_lab/report：A0 £1,000 回测报告三件套

## 1. 职责

装 A0 策略 £1,000 本金回测的交互式 HTML 报告的全部源件：`/html-report` §一 规定的
三件套（取数管线、模板、组装脚本）与取数管线最近一次运行的产物 JSON。三件套装配出的
交付件是 `reports/a0_cap1000_20260821.html`。

本目录不产生新的实验结果。取数管线只读既有回测结果件并做派生统计与断言，一切净值、
成交、费率数字的来源是 `backtest/results/` 下由 `scripts/20260821_a0_framework_backtest.py`
一类入口脚本写出的结果件。本目录也不存交付 HTML 本身（落在 `reports/`，由 `.gitignore`
第 19 行排除，不入库）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `make_a0_report_data.py` | 取数管线，214 行。以 `RUN_STEM`（L37）锁定一次运行：`a0_v0_0_1_a0_cap1000_actual_2010-01-04_2026-08-19_fee-actual_seed20260820`，读该运行的 `.equity.parquet`、`.trades.parquet`、`.meta.json`（L84 至 L86）与 `a0_capital_scaling_20260821.csv`（L39、L123）。把逐笔 equity 按日期取末条降为日频，自 `LIVE_FROM = "2018-01-01"`（L43）截取，派生持仓市值、持仓占比、回撤、CAGR、年化波动与夏普；成交按（日期，方向）聚合成标注点；`_holding_intervals()`（L65）按 `FLAT_EPS = 1e-4`（L48）把持仓市值高于该阈值的连续日期段切成持仓区间。锚定断言（L123 至 L134）取 CSV 中 `cash == 1000` 且 `tier == "actual"` 的一行，逐项比对 cagr、max_dd、sharpe、final_equity 四个数，容差 `ANCHOR_TOLERANCE = 5e-4`（L44，取绝对与相对两者的较大值），任一项不符即 `raise SystemExit("ANCHOR MISMATCH")`。`_verify()`（L51）对八个证据件读盘，记录字节数、MD5 前八位与 mtime。产物写到同目录 `a0_report_data.json`（L35 `OUT`） | 删除后 `a0_report_data.json` 无法重新生成，报告数字与回测结果之间的锚定校验链断裂。该断言是报告不与裁定文档数字漂移的唯一机制（`/html-report` §二.1），失去它则交付 HTML 中的五张 KPI 卡无任何自动校验 | 无模块 import（全仓无 import 命中）。命令行入口，用法写在其 docstring 第 15 行：`python research/regime_lab/report/make_a0_report_data.py`。`research/README.md` 第 46、69 行登记其读写路径 |
| `a0_report_template.html` | 报告模板，329 行，纯静态可审阅。含两个注入占位符：第 7 行 `__PLOTLY_JS__`、第 162 行 `__DATA_JSON__`（后者置于 `<script id="payload" type="application/json">` 内）。CSS 在裸 `:root` 定义完整浅色令牌，并在 `@media (prefers-color-scheme: dark)` 下的 `:root:not([data-theme="light"])` 与 `:root[data-theme="dark"]` 两处各重定义一次，构成系统、浅色、深色三态。JS 从 payload 解析出 `D` 后渲染：七枚徽章、五张 KPI 卡（年化收益率、最大回撤、夏普比率、盈亏比、成本拖累）、限定说明、图 1、折叠解释卡、最深回撤表、证据清单表、口径与限定表。图 1 用 `Plotly.react` 画三格共轴：上格净值折线加买卖三角标注加持仓区间底纹，中格持仓占比，下格回撤。三个切换按钮控制 `state` 的 `markers` / `bands` / `log` 三位，另有折叠卡展开与主题切换两个监听 | `build_a0_report.py` 第 25 行直接 `read_text()` 它，删除后组装在该行抛 `FileNotFoundError`。它是报告全部呈现逻辑的唯一副本：配色令牌、三态主题、买卖用形状（三角朝上与朝下）作颜色之外的二级编码以满足色觉障碍与黑白打印、口径与限定表的九行文字，均只存在于此文件，无处可复原 | `build_a0_report.py` 第 25 行 |
| `a0_report_data.json` | 取数产物，158,501 字节。八个顶层键：`run`（运行标识、本金 1000.0、费率档 actual、策略 a0 v0.0.1、引擎窗 `2010-01-04 .. 2026-08-19`、`live_from` 2018-01-01、标的 18 只）、`kpi`（14 项，含 CAGR 0.209269、最大回撤 0.218512、夏普 1.0977、成交 962 笔、交易日 2249 天、在场 1396 天、平均持仓占比 0.542019）、`series`（`dates`、`equity`、`exposure_pct`、`drawdown_pct` 各 2,249 点）、`holding_spans`（26 段）、`markers`（326 组）、`worst_drawdown`（峰值日 2024-07-10、谷底日 2024-09-06、深度 21.85%）、`evidence`（8 条，其中 V-1 的 `found` 为 `false`）、`anchor_report`（四项的 computed 与 recorded 并列） | `build_a0_report.py` 第 26 行读它注入模板，删除后组装在该行失败。它同时是 2026-08-21 交付件所用数值与八条证据哈希的唯一留痕：重跑取数管线得到的是当下磁盘状态，证据哈希与字节数不再等同于交付时刻，已交付报告的可复核性随之丧失（`research/README.md` §5 已作此裁定） | `build_a0_report.py` 第 26 行。已入库（`git ls-files research/regime_lab/` 命中） |
| `build_a0_report.py` | 组装脚本，40 行，只做替换，无数据派生、无图表逻辑。读同目录模板与 JSON，把 JSON 文本中的 `</` 替换为 `<\/`（L26），防止任一字符串值提前闭合 script 标签；读本机 plotly 包内的 `plotly/package_data/plotly.min.js`（L27）；两处占位符替换后断言无残留，有残留即 `raise SystemExit("placeholder left unsubstituted")`（L30 至 L31）；建父目录后写出 `reports/a0_cap1000_20260821.html`（L21 `OUT_HTML`、L33 至 L34） | 删除后无法把三件合成交付 HTML。转义与占位符残留断言这两道保护也只在此文件内，改由手工拼接会同时失去两者 | 无模块 import（全仓无 import 命中）。命令行入口，用法写在其 docstring 第 8 行：`python research/regime_lab/report/build_a0_report.py` |

运行顺序固定为先 `make_a0_report_data.py` 后 `build_a0_report.py`。两者之间只经由
`a0_report_data.json` 传递数据，不共享内存状态，因此可分别单独重跑。

三处已知的文档与实现不一致，记录在此，本轮未改动代码：

| 位置 | 文档写法 | 实现 |
|---|---|---|
| `make_a0_report_data.py` docstring 第 19 行 | `main()  Write results/a0_report_data.json` | L35 的 `OUT` 是 `Path(__file__).resolve().parent / "a0_report_data.json"`，写到脚本自身所在目录，不是 `results/` |
| `make_a0_report_data.py` docstring 第 9 行 | 引「html-report section 2.1」 | `/html-report` 用中文序号分节，对应 §二.1 锚定断言，内容一致 |
| `make_a0_report_data.py` L52，`_verify()` 的 docstring | 引「html-report section 4.3.2」 | `.claude/skills/html-report/SKILL.md` 的一级标题只到 §十五 且无二级编号，无 §4.3.2。证据件读盘登记的对应条款是 §十 证据溯源页 |

## 3. 子目录索引

无。

## 4. 依赖关系

1. 读取的回测结果件（四件，均在 `backtest/results/`，该目录由 `.gitignore` 第 20 行排除）：

   | 文件 | 磁盘字节数 | 在管线中的用途 |
   |---|---|---|
   | `a0_v0_0_1_a0_cap1000_actual_2010-01-04_2026-08-19_fee-actual_seed20260820.equity.parquet` | 77,160 | 逐日净值、现金、持仓占用（列 `ts`、`equity_gbp`、`cash_gbp`） |
   | 同前缀 `.trades.parquet` | 58,831 | 逐笔成交（列 `ts`、`symbol`、`quantity`、`cash_delta_gbp`），聚合成买卖标注点 |
   | 同前缀 `.meta.json` | 426,027 | 运行配置：本金、费率档、策略名与版本、引擎窗、标的清单 |
   | `a0_capital_scaling_20260821.csv` | 431 | 锚定断言的比对基准，另供 `win_rate`、`pf`、`cost_drag_pct`、`costs_gbp` 四项 KPI |

2. 只读盘取哈希、不解析内容的证据件（`_verify()`，L180 至 L195）：上表四件，加
   `trading212/strategy/a0_v0_0_1.py`（C-1）、`trading212/config/strategies/a0_v0_0_1.yaml`（C-2）、
   `research/decisions/20260820_regime_lf_ruling.md`（V-1）、
   `research/decisions/20260821_a0_framework_comparison.md`（V-2）。V-1 在磁盘上不存在，
   `_verify()` 如实写入 `"found": false`，模板据此在证据清单表渲染「磁盘上未找到」。
   该缺件同时被 `trading212/strategy/a0_v0_0_1.py` 第 6 行与
   `research/decisions/20260821_a0_framework_comparison.md` 第 9 行引用，
   `research/README.md` §3 已登记为待补建或待改写引用的项。
3. 写出：同目录 `a0_report_data.json`；`reports/a0_cap1000_20260821.html`。
4. 被谁 import：无。两个 `.py` 无 `__init__.py` 伴随，不构成包，只作命令行入口。
5. 第三方依赖：`numpy`、`pandas`（含 parquet 引擎）与 `plotly`。`plotly.min.js` 取自本机
   安装的 plotly 包内，实测 plotly 6.9.0 下该文件为 4,851,164 字节，故交付 HTML 体积
   约 5 MB 属正常（`/html-report` §一 的自包含原则）。
6. 上级说明文档：`research/regime_lab/README.md` 与 `research/README.md`。

## 5. 产出与清理

| 文件 | 性质 | 清理规则 |
|---|---|---|
| `a0_report_data.json` | 运行产物，由 `make_a0_report_data.py` 生成 | 必须保留，且已入库。理由见 §2 该行的存在必要性：它是交付报告所用数值与证据哈希的唯一留痕，重生成不等价 |
| `make_a0_report_data.py`、`a0_report_template.html`、`build_a0_report.py` | 源件 | 永久保留。按 `/html-report` §一 的修订版本管理，日后修订时另存带版本号的新文件并保留旧版作血缘，不原地覆盖 |
| `reports/a0_cap1000_20260821.html` | 本目录写出的交付件，落在 `reports/` | 不属本目录，不入库（`.gitignore` 第 19 行）。磁盘现存 5,028,184 字节。删除后可由本目录三件重新装配，前提是 §4.1 的四个输入件仍在 |

本目录内无临时件、无备份副本、无版本后缀试验件。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
