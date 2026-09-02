# 03 数据自动化实施步骤（对话 T）

前置：先读 `00_coordination.md` §1.1（盘前段）、§5.2（产物契约）、§6（A3、A5）。

## 0. 指向的交易代码

| 文件 | 状态 | 本文件约束的部分 |
|---|---|---|
| `scripts/update_data.py` | 改 | §3（半截 bar 守卫）、§4（第四 pass）、§5（调度与锁） |
| `trading212/ingest/yahoo_bars.py` | 改 | §3 |
| `trading212/ingest/a1_rank.py` | 新建 | §4 |
| `common/paths.py` | 改 | §4.1（`a1_rank_path`） |
| `update_data.command` | 改 | §5.3 |
| `scripts/2026XXXX_build_universe_ticker_map.py` | 新建 | §6 |
| `docs/data/t212/DATA_SPEC.md`、`GAPS.csv` | 改 / 新建 | §7 |

## 1. 现状事实（逐条已核）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 1,500 只候选池日线已在 `data/t212/curated/us_equity/`，由 `_update_b0_universe` 维护 | `scripts/update_data.py` L403-457 |
| 2 | 全量刷新约 3 小时 09 分；静日校验约 40 分钟 | 年文件 mtime 实测 |
| 3 | 盘中运行时 Yahoo 返回当日未收盘 bar，更新程序原样写入且此后按时间戳跳过 | 2026-09-02 探针 |
| 4 | `update_data.py` 不取 `.refresh.lock`；实盘刷新持有该锁且为**阻塞式** | `market_data.py` L165-168 |
| 5 | `decide` 在 `now > submit_at` 时 abort | `session_cycle.py` L239-244 |
| 6 | 本机时区 CST(+0800)。夏令时收盘 16:00 EDT = 04:00 CST；冬令时 16:00 EST = 05:00 CST，而 15:30 EST（决策键）= 04:30 CST | 时区推算 |
| 7 | `write_intraday` 写入前按 `*_{start}_*_{interval}.parquet` 通配删除同月兄弟文件 | `yahoo_bars.py` L308-310 |
| 8 | `rolling(252, min_periods=252)` 使单个缺失日让该名字随后 252 个场次不可准入 | `research/xsmom_wide/run_study.py` L106-112 |
| 9 | 三套拼写并存：候选池 JSON `BRK.B`、磁盘 `BRK-B`、场所 `BRK_B_US_EQ` | `update_data.py` L389-400 |
| 10 | `common/paths.py` 已有 `execution_state_dir`、`records_dir`、`equity_interval_dir`、`gaps_path` | 本回合实测 |

## 2. 盘前段的两个不变量

1. 排名表只用**已收盘场次**的数据。任何未收盘的当日行都不得进入面板。
2. 排名表的生成不得与实盘决策争锁或争时段。

§3、§5 分别保证这两条。

## 3. 半截 bar 守卫

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `yahoo_bars._tidy` 增参数 `drop_from: date \| None`，丢弃交易所本地日期不早于该值的行 | 单测：盘中时间戳的当日行被丢弃 |
| 2 | `update_data.py` 的 `_update_equity` 与 `_update_b0_universe` 在美股场次未收盘时把「今日」传入守卫；跳过规则改为比较**最后一根完整 bar 的日期** | 单测：半截 bar 不落盘，次日正常补 |
| 3 | 一次性修复：收盘后跑一次全量，替换 2026-08-31 落下的 1,475 个半截 bar | 抽查 A、ZTS 等已替换为终值 |

实盘 15:30 的 `refresh_bars(…, "1d")` 仍会写当日半截日线：A0 的壳自行丢弃当日行，A1 只读排名表，
两者都不受影响，**该路径不改**。

## 4. 第四 pass：A1 排名表

### 4.1 产物（冻结契约，`00` §5.2）

`common/paths.a1_rank_path(session_date) -> data/t212/curated/a1/rank/<YYYY-MM-DD>.parquet`

| 列 | 类型 | 含义 |
|---|---|---|
| `symbol` | str | 磁盘拼写（连字符） |
| `ticker` | str \| null | 场所 ticker，取自 §6 的映射文件 |
| `close` | float | 该场次收盘 |
| `score` | float \| null | 12-1 动量 |
| `eligible` | bool | 五条准入全过 |
| `elig_reason` | str | `ok`、`dollar_volume`、`zero_volume`、`history`、`participation`、`no_ticker`、`no_score` |
| `rank` | int \| null | 仅对 `eligible` 且有分数者，1 起 |
| `panel_as_of` | date | 面板最后场次，等于文件名日期 |
| `generated_at_utc` | str | |
| `code_version` | str | `a1_v0_0_1` 的 `STRATEGY_VERSION` |

同日重跑原子覆盖。只对已收盘场次生成。

### 4.2 算法

计算全部委托给 `a1_v0_0_1.rank_table`（`01` §3），本层不重写任何准入或分数逻辑。

| 步 | 动作 |
|---|---|
| 1 | 读候选池 JSON，按 `_b0_tickers` 的连字符规范化得到磁盘拼写 |
| 2 | 用 `backtest/t212/data_source.load_bars` 的路径构造（经 `common/paths`）装载 closes 与 volumes，日期按纽约本地日 |
| 3 | 截断到目标场次 T（含），丢弃任何晚于 T 的行 |
| 4 | 读 §6 的映射文件，得到有已验证 ticker 的名字集合，作为 `require_verified_ticker` 的输入 |
| 5 | 调 `a1_v0_0_1.rank_table(closes, volumes, as_of=T, params)` |
| 6 | 原子写 parquet |

参数从 `a1_v0_0_1.yaml` 读一次传入。

### 4.3 陈旧与缺口

1. 若目标场次的排名表未产出，执行层回退到最近一份并记 `rank_stale_sessions`（`04` §5）。
2. 超过 3 个场次陈旧时，`injection["a1_frozen"] = True`，A1 腿按 `02` D7 冻结。
3. 上游缺口（Yahoo 后补的日子）由全量重取自然恢复；未恢复者登记到 `GAPS.csv`。
4. 事实 8 的一年失格是研究口径的既有行为，**本轮不改口径**，只在 `DATA_SPEC.md` 写明。

## 5. 调度与锁

### 5.1 调度（修正冬令时）

固定本地时刻的调度在冬令时会落在决策时刻（事实 6）。因此：

| 步 | 做什么 |
|---|---|
| 1 | 触发时刻**相对场所收盘**：由 `daemon` 在当日 `settle` 成功结束后触发盘前更新（新增 `execution.post_settle_update: true`），或由 `launchd` 在 **06:00 CST** 触发（冬令时为收盘后 60 分钟，夏令时为收盘后 120 分钟，两季都在收盘之后） |
| 2 | `update_data.py` 的股票 pass 启动前断言当前无美股常规场次在开（用 `instruments.load_calendar` 与 `current_session`）；有则退出并提示 |
| 3 | 单测：给定冬令时与夏令时各一个日期，断言触发时刻晚于当日收盘 |

### 5.2 锁

`.refresh.lock` **逐写持有**：`update_data.py` 在每个标的的写入前后取放锁，不得整 pass 持锁。
整 pass 持锁三小时会让实盘 `refresh_bars` 阻塞到 `submit_at` 之后（事实 4、5），当日决策被 abort。

### 5.3 命令行

`update_data.command` 当前不转发参数。增加 `"$@"` 转发，并在 `update_data.py` 增 `--no-a1`
（跳过第四 pass）。`--no-b0` 保留，注释改为「跳过候选池校验 pass」。

## 6. 标的映射文件（`00` A5）

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | 建 `scripts/2026XXXX_build_universe_ticker_map.py`，规则固定为：**先按 `ticker` 前缀 `<SYMBOL>_US_EQ` 匹配，未命中再按 `shortName` 匹配**，两者都要求 `type == STOCK` 且 `currencyCode == USD`；A0 的 18 条以 `instruments.A0_ORDER_TICKERS` 为准并优先 | 输出 `data/reference/t212_universe_ticker_map_<date>.json`（symbol 至 {ticker, isin, workingScheduleId, matched_by}） |
| 2 | 脚本输出三类计数：前缀直配、shortName 兜底、未匹配或多候选。**验收数字以脚本实际输出为准并写入 `WORKING_MEMORY.md` 未决项**，不在本计划预设 | 早前预设的 1,373 / 127 与 DAR、FIVE 歧义样例来自另一套规则，已删除 |
| 3 | 多候选与未匹配名单提请用户裁定；未裁定者 `ticker` 为 null，经 E5 自然不准入 | |
| 4 | `instruments.ticker_map_for(symbols)`（接缝 S5）读该文件并以 `A0_ORDER_TICKERS` 覆盖；**实现放在 `04` §7，本文件只产出数据** | 避免两处实现 |

## 7. 文档

1. `DATA_SPEC.md`：新增 `curated/a1/rank/` 数据集（§4.1 列表、口径 T、生成脚本）；更正 us_equity 名字数、volume 类型、「无缺口」表述。
2. `GAPS.csv`：新建，登记 2026-08-28 缺口（AMAT、JNJ、KO、LRCX、NEM、PG、XOM）与 LEG、WBS 尾部。
3. `MANIFEST.jsonl`：第四 pass 产物纳入 `build_data_manifest`。
4. 登记：`trading212/ingest/README.md`、`scripts/README.md`、`common/README.md`（`a1_rank_path`）。

## 8. 测试

| # | 测试 | 捕捉的缺陷 |
|---|---|---|
| 1 | `_tidy` 守卫丢弃当日行 | 半截 bar 入库 |
| 2 | 排名表 pass 在合成面板上：截断到 T、E5 无 ticker 失格、rank 自 1 起且只覆盖 eligible | 前视、排名错 |
| 3 | pass 调用的是 `a1_v0_0_1.rank_table`（spy 断言） | 信号双副本 |
| 4 | 映射脚本：前缀规则命中、shortName 兜底命中、多候选不写入 | 映射错 |
| 5 | 锁：逐写持有，长 pass 期间实盘 `refresh_bars` 不被阻塞超过单个标的的写入时间 | 阻塞决策 |
| 6 | 调度：冬令时与夏令时的触发时刻均晚于当日收盘 | 冬令时撞决策 |
