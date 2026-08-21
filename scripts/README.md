# `scripts/` 目录说明

## 1. 职责

本目录装可直接执行的入口，分两类：带日期前缀 `YYYYMMDD_` 的**一次性脚本**
（探针、首轮 ingest、单次回测运行），以及不带前缀的**常驻工具**（可反复执行的
数据更新、盘点、manifest 重建、git 同步）。命名规则见 `CLAUDE.md` §4.1，一次性
脚本的去留判定见 §4.2 第 2 条。

不装的内容：任何被其他模块 import 的库代码——可复用逻辑一律下沉到 `common/`、
`<venue>/ingest/` 或 `backtest/`；不装 pytest 测试（在 `tests/`）；不装策略信号
（唯一副本在 `<venue>/strategy/`）。用真实落地数据做的冒烟检验按
`fixplans/validation/02_test_plan.md` §2 的裁定放在本目录，不放 `tests/`
（`tests/README.md` §1）。

## 2. 文件清单

「作用」一栏首词标明类型：一次性探针、一次性 ingest、一次性运行入口、常驻工具、
双击入口。

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `20260819_crypto_correlation_probe.py` | 一次性探针。从 `data-api.binance.vision` 的 `/api/v3/klines` 取 23 个候选币对的日线（单次上限 1000 根），算对 BTCUSDT 的对数收益 Pearson 相关，输出三视图：全窗口、BTC 最差十分位子样本、逐年。稳定币列入候选作对照组，提供判别力 | 检验「存在与 BTC 负相关的高流动性币种」这一前提。`research/notes/20260819_negative_correlation_findings.md:4` 指名本脚本为结论来源，删除后该笔记的结论失去可复现凭据 | `research/notes/20260819_negative_correlation_findings.md:4`。无代码调用点 |
| `20260819_equity_hedge_probe.py` | 一次性探针。同一问题问美股：经 yfinance 取 20 个标的自 2015-01-01 的日线复权收盘，输出三视图（全窗口对 SPY、SPY 最差十分位、逐年），并对每个候选给出「是否算对冲」的判定 | 同上，检验负相关对冲标的是否存在；脚本内注明检查的是「回撤期是否真的上涨」而非仅看系数 | `research/notes/20260819_negative_correlation_findings.md:4`。无代码调用点 |
| `20260819_ingest_crypto_bookticker.py` | 一次性 ingest，窗口固定且已完成。取 um 市场 BTCUSDT/ETHUSDT 的 bookTicker 日档，窗口 2023-05-16 至 2024-03-30（Binance 已停止发布，两端均经列举核实）。4 个 worker，装 SIGINT/SIGTERM 停机标志使中断落在文件边界；启动时清理临时件并逐个读 footer，不可读的分区删除后重新排队 | 该数据集是 Binance 免费归档中唯一的 tick 级盘口，已 640/640 完成（`WORKING_MEMORY.md`「当前状态」）。本脚本是该窗口的重建凭据，也是 `resume_bookticker.command` 的被调对象，删除后二者同时失效 | `scripts/resume_bookticker.command` 末行 |
| `20260819_ingest_crypto_phase1.py` | 一次性 ingest，首轮落地。按 5 个 job（spot klines 1m 与 1d、um fundingRate、um metrics、um bookDepth）逐符号发现归档覆盖范围后并行下载并写 curated parquet，8 个 worker。标的选取依据是实测 10bp 内盘口深度，不是 24 小时名义成交额 | `docs/data/binance/DATA_SPEC.md:9` 登记它为该数据集的生成脚本。日常增量已改由 `update_data.py` 承担，本脚本保留为首轮落地的口径留痕 | `docs/data/binance/DATA_SPEC.md:9`。无代码调用点 |
| `20260819_ingest_equity.py` | 一次性 ingest，首轮落地。为三组共 52 个标的下载 5 个区间（1d、1h、5m、2m、1m）的 Yahoo 行情，日线按自然年、日内按自然月分区落地；1m 因单请求上限 8 天而由连续窗口拼接 | `docs/data/t212/DATA_SPEC.md:8` 登记它为该数据集的生成脚本。**注意重复**：其取数与写盘逻辑此后已提炼为 `trading212/ingest/yahoo_bars.py`（该模块 docstring 自述为「单一实现，首轮与增量都调它」），本脚本内仍保有一份独立实现（`_fetch_interval`/`_write_daily`/`_write_intraday`/`_quote_currency` 与 `INTERVALS`/`UNIVERSE` 常量），两份已构成重复定义，取值是否仍一致未经核对 | `docs/data/t212/DATA_SPEC.md:8`。无代码调用点 |
| `20260820_t212_backtest_smoke.py` | 一次性运行入口。用真实 curated 数据跑 T212 回测框架，窗口与标的按判别力挑选（跨 BST 时段、含 2026-07-03 美股假日、混 USD/GBP/GBp 三种计价）。6 项断言：两档费率均全标的成交、worst 档成本严格高于 actual、美假日无 AAPL 成交、伦敦日线成交带 23:00 UTC 戳、USD 成交的 `fx_mid` 等于前一交易日 GBPUSD 收盘（并检查该断言本身是否有判别力）、同配置重跑逐字节一致。任一项失败以退出码 1 返回 | 这 6 条是框架的对外保证，落为可执行脚本后可随时复跑；删除后这些保证只剩文档陈述，无复验手段 | `tests/README.md:10` 指名为两例真实数据冒烟之一；`WORKING_MEMORY.md:122` 记录其落地。无代码调用点 |
| `20260821_a0_framework_backtest.py` | 一次性运行入口。加载 `trading212/strategy/a0_v0_0_1.py` 与基线参数 `trading212/config/strategies/a0_v0_0_1.yaml`，以显式覆盖派生 4 个消融臂（a0、tsmom、ma200、bh），每臂跑 2 档费率，跑 2010-01-04 至 2026-08-19（2018-01-01 起实盘化），并从成交记录后处理持仓时长与年化换手，写 `backtest/results/a0_comparison_20260821.csv` | `research/decisions/20260821_a0_framework_comparison.md:5` 指名它为该裁定的入口脚本，裁定中的全部数字由它产出；删除后裁定失去可复现凭据 | `research/decisions/20260821_a0_framework_comparison.md:5`；`fixplans/framework/02_data_layer.md:103` 引用其首跑的全池违规扫描；`tests/README.md:10` |
| `20260822_a0_minute_backtest.py` | 一次性运行入口，尚未入库（`git status` 显示为未跟踪）。跑分钟频 A0 与日频对照：m1 臂用 `trading212/strategy/a0_intraday_v0_0_1.py`，每个交易日 15:59 决策（信息截至 15:58）、次日 09:30 开盘成交；d1 臂同信号走日频。两臂共用同一段日线历史，净值统一重采样到每交易日一点后再比，避免分钟采样的回撤与日线回撤直接对照 | **用途待确认**。当前无任何文档或代码引用它，`ARCHITECTURE.md` 与 `WORKING_MEMORY.md` 均未登记。按 `CLAUDE.md` §4.2 第 2 条，一次性脚本须在任务结束时二选一（登记保留或删除），本文件的判定尚未做出 | 无。其依赖的策略模块 `trading212/strategy/a0_intraday_v0_0_1.py` 同为未跟踪件，但已有测试 `tests/backtest/test_a0_intraday.py:16` 引用该策略模块 |
| `build_data_manifest.py` | 常驻工具。扫描 `data/<source>/curated/` 下全部 parquet，每个分区生成一条 JSONL 记录（坐标、上游 URL、字节数、行数），写 `docs/data/<source>/MANIFEST.jsonl`。行数取自 parquet footer 不读数据；字节数未变即复用旧记录；刻意不写生成时间戳，保证数据无变更时重跑逐字节相同。上游 `.CHECKSUM` 与本地 sha256 分别由 `--fetch-checksums`、`--hash-local` 按需开启。分区不可读时行数记为 null 并以退出码 1 返回 | `data/` 整棵不入库，manifest 是这批字节唯一的入库重建凭据（`ARCHITECTURE.md:144`）。`sync_to_git.command:49` 在每次同步前调它，删除即断该链路，入库仓库将不再记录数据是什么 | `sync_to_git.command:49`；`ARCHITECTURE.md:144`；`WORKING_MEMORY.md:39` |
| `data_inventory.py` | 常驻工具。遍历 `data/` 全部 parquet，按（数据源、组、数据集、标的、频率）聚合文件数、行数、字节数与时间跨度并打印。跨度取自时间列的 parquet 统计信息而非文件名，理由是股票分区按标的命名、加密按日期命名，只有数据本身对两者都权威 | `docs/data/binance/DATA_SPEC.md:58` 把「各标的实际起始日」的口径指向本脚本输出。它读盘上实况而非 manifest，是核对 manifest 是否失真的独立一路，删除后失去这一路交叉验证 | `docs/data/binance/DATA_SPEC.md:58`；`WORKING_MEMORY.md`「当前状态」记它为清单生成入口 |
| `resume_bookticker.command` | 双击入口（已置可执行位）。`cd` 到仓库根后用 `caffeinate -i` 抑制闲置休眠，以 `.venv/bin/python -u` 跑 `20260819_ingest_crypto_bookticker.py` | bookTicker 数据集已 100% 完成且不再更新，本入口当前无待办工作；保留仅为该数据集需重下时的现成入口。已知缺陷：无单实例保护，`WORKING_MEMORY.md:105` 记录过双开抢带宽使错误数由 0 升至 13/560 的实例 | 用户手工双击。无代码调用点 |
| `sync_to_git.py` | 常驻工具，每日手动执行的 git 同步器。四道闸门：origin 必须等于 `EXPECTED_REMOTE`、提交身份必须已配置、单文件超过 `MAX_BLOB_BYTES`（10 MB）拒绝、密钥闸（路径模式 + PEM/GitHub/AWS/Slack/Anthropic 等硬令牌正则 + 「字段名像密钥且取值也像密钥」软规则，占位符与引用名放行）。闸门命中以退出码 2 中止，不提交不推送，索引保持已暂存状态；`--overwrite-remote` 为强制覆盖，只能显式命令行触发 | 仓库为 public（`WORKING_MEMORY.md`「当前状态」），密钥闸是 `.gitignore` 之外的第二道防线。删除后入库内容无门禁，且推错仓库无拦截 | 根目录 `sync_to_git.command:48`；`WORKING_MEMORY.md:31` 记它为每日同步入口。`__all__` 导出 4 个符号，但检索无任何外部 import 点 |
| `update_data.py` | 常驻工具，日常数据更新的主入口。股票走「全窗口重取」（复权是追溯性的，追加会在拆股日造成序列断裂），实取逻辑调 `trading212/ingest/yahoo_bars.py`，并先用一次 5 日探针跳过无变动的标的；加密走真增量（归档一经发布即不可变），月档到位后删除被其覆盖的日档；bookTicker 不更新（已停止发布）。开跑前先清理中断残留的临时分区。可反复运行、可随时中断 | 根目录 `update_data.command:9` 双击入口直接调它，是数据保鲜的唯一路径。删除后日常更新无入口 | `update_data.command:9`；`WORKING_MEMORY.md:28` |

## 3. 子目录索引

无子目录。

## 4. 依赖关系

读：

- `data/` 下的 parquet：`data_inventory.py`、`build_data_manifest.py`、
  `update_data.py`，以及三个回测入口经 `backtest/t212/data_source.py` 间接读。
- `trading212/config/strategies/a0_v0_0_1.yaml`：两个 A0 回测入口。
- git 索引与 `git cat-file` 输出：`sync_to_git.py`，只读，不触碰 `data/`。
- 外部源：`data.binance.vision` 归档与 `data-api.binance.vision` 只读行情
  （加密探针与加密 ingest）、Yahoo（股票探针与股票 ingest，经 yfinance）。

写：

- `data/<source>/curated/` 下的 parquet：三个 ingest 脚本与 `update_data.py`。
- `docs/data/<source>/MANIFEST.jsonl`：`build_data_manifest.py`。
- `backtest/results/` 下的 csv、parquet 与框架自身的结果件：三个回测入口。
- git 提交与 origin 推送：`sync_to_git.py`。
- 两个探针脚本无文件产出，只写 stdout。

import 本仓库：`common.paths`、`common.store`、`crypto_trading.ingest.binance_archive`、
`trading212.ingest.yahoo_bars`、`trading212.strategy.a0_intraday_v0_0_1`、
`backtest.engine.*`、`backtest.t212.*`。各脚本以
`sys.path.insert(0, 仓库根)` 定位包，因此可从任意工作目录执行。

被谁 import：无。本目录的文件一律是入口，不被任何模块 import。被外部执行的方式
只有三处 `.command` 启动器：根目录 `sync_to_git.command`、根目录
`update_data.command`、本目录 `resume_bookticker.command`。

## 5. 产出与清理

- `backtest/results/` 下的 csv 与 parquet：三个回测入口的运行产物，在 `.gitignore`
  内，可随时删除。框架保证同配置重跑的 trades/equity/meta 逐字节一致
  （`backtest/README.md` §5），故删除后可复现。
- `docs/data/<source>/MANIFEST.jsonl`：`build_data_manifest.py` 的产物，**必须保留
  并入库**，它是 `data/` 的唯一重建凭据。
- `data/` 下的 parquet：ingest 与 `update_data.py` 的产物，必须保留；
  `data/*/raw/` 只增不改，删除前须问用户（`CLAUDE.md` §3.4）。
- 本目录内无临时件、无中间产物、无运行产物。
- 一次性脚本的去向判定（`CLAUDE.md` §4.2 第 2 条）：带日期前缀的 8 个文件中，
  7 个已被文档指名引用故保留（见 §2「谁在用」栏），`20260822_a0_minute_backtest.py`
  的判定尚未做出。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
