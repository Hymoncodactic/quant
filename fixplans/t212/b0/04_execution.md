# 04 执行层实施步骤（对话 T）

前置：先读 `00_coordination.md` §1.2（决策段）、§2（接缝 S1 至 S5）、§3（既有签名冻结）、§6。

## 0. 指向的交易代码

| 文件 | 状态 | 本文件约束的部分 |
|---|---|---|
| `trading212/execution/session_cycle.py` | 改 | §3（S1）、§5（S3）、§6（意向）、§8（记录流）、§9（重排状态） |
| `trading212/execution/market_data.py` | 改 | §4（S4）、§5（S2、S3） |
| `trading212/execution/instruments.py` | 改 | §7（S5） |
| `trading212/execution/risk_gate.py` | 改 | §6.3（卖单豁免）、§7.3（精度） |
| `trading212/execution/order_router.py` | 改 | §7.3 |
| `trading212/execution/{shadow_ledger,ledger_store}.py` | 改 | §10（迁移与回滚） |
| `trading212/execution/reconciler.py` | 改 | §7.2（ticker 表来源） |
| `trading212/execution/daemon.py` | 改（最小） | §8.3、§3.3（文案） |
| `trading212/archive.py` | 改 | §8.1（两条流，已于 M1 合入） |
| `trading212/config/{t212.example.yaml}` | 改 | §3.1 |
| `docs/backtest/framework/06_strategy_plugin.md` | 改 | §3.2（注入对象契约的变更记录） |

`run_a0.py` 的模块名、`run_a0.lock` 与 `daemon.lock` **不改**：看板以子串 `run_a0` 识别进程。

## 1. 现状事实（逐条已核，行号为本分支实测）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 被调用的策略对象由 `intraday_name` 选择：`load_intraday_strategy(cycle.shim_name, cycle.shim_version, history)` | `session_cycle.py` L305-306 |
| 2 | 工厂只接受一个位置参数；模块无 `make_strategy` 时回退到裸 `compute_targets` | `strategy_loader.py` L105-117 |
| 3 | `trade_symbols` 驱动映射校验、日历分歧、对账 ticker 表、刷新、冻结视图与日线历史 | `session_cycle.py` L250-301、L396-409、L528-529 |
| 4 | `assert_intraday_ready`：交易标的缺**决策键 bar** 记入 `thin` 不 raise；仅状态标的缺决策键 bar、任一标的缺**前一根信息 bar**、FX 缺 bar 才 raise | `market_data.py` L317-331 |
| 5 | 空目标被判为 abort | `session_cycle.py` L307-309 |
| 6 | 意向按目标字典插入顺序提交；风控现金预算随先提交的卖单滚动 | `session_cycle.py` L631-669；`risk_gate.py` L177-215 |
| 7 | `max_order_notional_gbp` 按 `ref_notional_gbp` 判定，**对卖单同样拒绝**（不裁剪） | `risk_gate.py` L297-299 |
| 8 | 最小名义值在 `risk_gate._check_one` 内作为**拒绝**记录，不是在 `_diff_to_intents` 里静默跳过；后者只丢弃低于数量步进的尘埃 | `risk_gate.py` L291-296；`session_cycle.py` L652-657 |
| 9 | `order_ticker` 只认 18 条静态映射；`validate_mapping` 只校验 `trade_symbols` | `instruments.py` L94-174 |
| 10 | 精度拒绝为 `PermanentError`，本场次丢单不重试 | `order_router.py` L194-198 |
| 11 | 市价单 POST 约 0.583 单每秒（实测 16 单 26 秒） | `client.py` L109-119 |
| 12 | 场所日历跨度实测仅 2026-08-17 至 2026-09-28 | 本回合实测 |
| 13 | `write_intraday` 删除同月兄弟文件，短窗刷新会截断该月完整 1h 文件 | `yahoo_bars.py` L308-310 |
| 14 | live 配置当前为 `live: true`、`dry_run: false`（A0 已武装实盘） | `t212.live.yaml` L1、L8 |
| 15 | paper 环境 daemon 从未完成一次 decide，`last_error` 为场所可用现金低于账本现金 | `execution_state_paper/daemon_status.json` |
| 16 | 仅 A0 路径的 decide 实测耗时 88 至 104 秒 | `WORKING_MEMORY.md` |

## 2. 实施顺序

§3（S1 与配置）、§4（S4）、§5（S2 与 S3）、§7（S5）、§6（意向）、§8（记录流）、§9（重排状态）、§10（迁移）。
前置：`fixplans/t212/a0/03_dst_hardening.md` 的两项先落地；事实 15 的 paper abort 先修。

## 3. 接缝 S1：参数拼装与策略选择

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `t212.example.yaml` 增 `execution.b0_live_from` 与 `strategy.{name: b0, version, intraday_name: b0, intraday_version}`（`00` §5.3） | 配置样例可被 `load_config` 解析 |
| 2 | 新增公开函数 `session_cycle.assemble_params(cfg) -> dict`（接缝 S1，语义见 `00` §2.1）；`_Cycle.__init__` 改为调用它。**`decide` 中的 `load_intraday_strategy(cycle.shim_name, …)` 调用行不改**（事实 1、`00` A1） | 单测：`name: a0` 配置下 params 与改动前逐位一致；`name: b0` 下含三层 |
| 3 | 通知文案由字面 `A0` 改为 `strategy_id`（`session_cycle`、`daemon`、`risk_gate`、`reconciler` 各处，按 grep 结果定位，不预设行号） | grep 无 `"A0 ` 字面 |
| 4 | `06_strategy_plugin.md` 追加变更记录：工厂注入对象为策略自定义的 dict，键由策略模块与入口层约定 | |

## 4. 接缝 S4：决策前刷新

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `market_data.group_for` 增磁盘回退：`UNIVERSE` 未命中时按 `_symbol_dir` 搜索三组目录 | 单测：候选池名字可解析 |
| 2 | 新增 `market_data.refresh_for_decision(params, session, key) -> list[str]`（接缝 S4）：先按现路径刷新 A0 的 18 只与状态标的、FX；再对 A1 在场名字做短窗 1h 刷新 | |
| 3 | 短窗刷新**不得用 `write_intraday` 的同月覆盖路径**（事实 13 会截断 A0 当月文件）：改为读出该月既有分区、与新数据按时间戳合并去重后整月写回，或为短窗单独使用 `<symbol>_<YYYYMM>_partial_<interval>.parquet` 命名并在 `load_frames` 中合并 | 单测：短窗刷新后该月完整 1h 文件的行数不减少 |
| 4 | 时间盒：整批短窗刷新不超过 120 秒，单标的最多 2 次尝试、退避基数 4 秒（不沿用 `_history` 的 6 次 8 至 128 秒退避）；超时或空返回的名字并入 `thin` | 单测：模拟超时后返回 `thin` 而非抛出 |
| 5 | 冻结视图：`assert_intraday_ready` 的现行语义**不改**（事实 4）；A1 名字缺**信息 bar** 时也降为 `thin` 而非 `problems`，仅此一处放宽 | 单测：A0 名字缺信息 bar 仍 raise；A1 名字缺信息 bar 记 `thin` |
| 6 | 验收改为**时刻约束**：`decide` 在 `submit_at − 600 秒`之前完成，实际完成时刻写入 `daemon_status.json` | 事实 16 表明固定秒数验收不可达 |

## 5. 接缝 S2 与 S3：场次序列与只读注入

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | 新增 `market_data.us_sessions(start, end) -> list[date]`（接缝 S2）：读 `us_etf/SPY/1d/` 的交易日，半日市计入 | 单测：2020-01-02 至 2026-08-28 返回 1,673 个 |
| 2 | 新增 `market_data.load_b0_injection(params, as_of, held) -> dict`（接缝 S3）：只读磁盘装配 `00` §2.3 的包。A1 排名表按 `a1_rank_path(前一场次)` 读取，缺失则回退最近一份并置 `rank_as_of` 与 `rank_stale_sessions`；陈旧超过 3 个场次置 `a1_frozen = True`；`a1_book` 取自最近一行 `a1_plan` 记录 | 单测：无网络调用、不取锁；缺文件时回退与冻结标志正确 |
| 3 | `session_cycle.decide` 改为：先调 S4 得到 `thin`，再调 S3 得到注入包并并入 `thin`，再把包交给 `load_intraday_strategy` | 单测：`name: a0` 路径不经过 S3 |
| 4 | `view` 的符号集为 A0 集合与 A1 在场名字的并集；A1 名字的 1h 只装最近 5 个场次 | |
| 5 | FX：A1 与 A0 同用壳的 `key−90m` bar 定量；成本路径仍用 `key−30m` | 单测断言两处取值 |

## 6. 意向生成与顺序

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `_diff_to_intents` 的顺序机制不改（事实 6），顺序由 B0 按 `00` A6 排好；入口增检查：存在买单排在任一卖单之前则记 WARNING，不重排 | 单测：B0 输出通过；打乱触发 WARNING |
| 2 | `residual_below_minimum`：在 `risk_gate._check_one` 内（与最小名义值判定同处，事实 8）新增规则——卖出后剩余名义低于 `min_order_value_gbp` 时把该卖单**放大为全平**而非拒绝 | 单测：剩余 £0.8 的减仓变为全平 |
| 3 | `max_order_notional_gbp` 对 `quantity < 0`（减仓与清零）**豁免**：卖出只减少敞口，符合只收紧契约。买单不变 | 单测：超限卖单通过、超限买单仍拒 |
| 4 | 吞吐守卫：在提交瞬间按 `session.close_utc − now − 安全余量` 与实测速率（`RATE_LIMITS['order_market']` 推算）截断本批意向，其余记 `deferred`，次场次由重定量自然重试 | 单测：以 `close_utc` 而非 `lead + grace` 断言 |

## 7. 映射、校验与精度

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | 新增 `instruments.ticker_map_for(symbols) -> dict[str, str]`（接缝 S5）：读 `03` §6 的映射文件并以 `A0_ORDER_TICKERS` 覆盖；`order_ticker` 改为查该合并表，签名与异常行为不变（`00` §3） | 单测：18 只 A0 名字返回原值；未映射名字不在返回值内 |
| 2 | `validate_mapping` 的输入改为 A0 18 只与 A1 在场名字的并集（不超过约 40 只）；对账 ticker 表改为 `ticker_map_for(账本持仓与目标名字的并集)`；`reconcile` 签名不变 | 单测：一次 `client.instruments()` 覆盖 40 只 |
| 3 | `schedule_divergences` 的 id 集合取在场名字；分歧时**只把分歧名字剔出本场次目标**并记 `schedule_divergent`，A0 的 18 只仍按原规则整场 abort | 单测：一只异日历名字被剔除，其余照常 |
| 4 | 精度：`QTY_STEP_OVERRIDES` 改为从 `execution_state[_env]/qty_steps.json` 装载（默认含 `INTC: 0.001`）；`order_router` 收到 `quantity-precision-mismatch` 时解析精度、原子写回该文件、记 `ORDER_SUBMIT_REJECTED(precision_learned)`；次场次按新步进 | 单测：一次拒绝后文件含该名字；次场次数量按新步进 |
| 5 | 精度探针：建 `scripts/2026XXXX_demo_precision_probe.py`。方法内联写明——在 **demo** 账户对目标名单逐只发 $7 市价买、随即市价卖，从错误响应 detail 解析精度（方法来源见 `data/reference/t212_demo_slippage_20260831.json` 的 `method` 与 `quantity_precision.discovery_method`；早前引用的 `scripts/20260831_demo_slippage_test.py` 在库内不存在）。**该脚本提交真实委托，每次运行须用户当轮授权**。产物写入 live 与 paper 两个 `qty_steps.json` | 名单全部有精度记录 |

## 8. 记录流与诊断

| 步 | 做什么 |
|---|---|
| 1 | `archive.STREAMS` 的两行已在 M1 合入（`00` §5.1） |
| 2 | 在**提交之前**调用 `b0_v0_0_1.signal_diagnostics`（接缝 S6）并保存其输出：提交后账本已记入本场次挂单，`held` 会变，`status` 与 `added`、`dropped` 会失真 |
| 3 | 提交之后写三条流：`signals`（既有字段加 `attribution` 与 `rebalance`）、`b0_allocation`（每场次）、`a1_plan`（仅重排日），字段见 `00` §5.1 |
| 4 | `daemon_status.json` 增 `a1_session_index`、`a1_next_rebalance`、`rank_as_of`、`rank_stale_sessions`、`decide_finished_utc` |

## 9. 重排状态

`a1_rebalance_state.json` 为**纯缓存**（`00` §5.2）：真值由接缝 S2 的场次序列与 `rebalance_anchor` 纯函数算出，
abort 的场次照常计数。每个场次（含 abort）由 `decide` 或 `daemon` 重算写入。看板读缓存免算。
锚点早于场所日历窗口不影响计算，因为场次序列来自本地 SPY 日线（事实 12）。

## 10. 账本迁移与回滚

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `shadow_ledger` 增事件 `BOOK_ADOPTED`（payload `from_strategy_id`、`positions`、`cash_gbp`、`at_utc`）与 `ShadowLedger.adopt_from(old)`；`ledger_store` 增 `restore_ledger(state_dir, strategy_id, stamp)`（把 `.retired-<stamp>` 改回原名，目标已存在即拒绝） | 单测：迁移后持仓与现金逐位相等；`restore` 可逆 |
| 2 | 迁移前置（任一不满足即拒绝）：源账本 `open_orders` 为空且未冻结、无 ambiguous intents；`daemon.lock` 未被持有；当前时刻在上一场次 settle 之后、下一决策键之前 | 单测：四个前置各一条 |
| 3 | CLI `run_a0 adopt-book --from a0_v0_0_1 --to b0_v0_0_1 --confirm`，**须用户当轮授权**；迁移后立即 `reconcile` 通过才算完成 | demo 环境先演练 |
| 4 | 现金口径：B0 账本现金为划给 B0 的额度；`_venue_cash_shortfall` 检查不变 | 事实 15 须先修 |

## 11. 测试

| # | 测试 | 捕捉的缺陷 |
|---|---|---|
| 1 | `assemble_params`：`name: a0` 下与改动前逐位一致；`name: b0` 下三层齐全且覆盖生效 | 破坏 A0、起用日错 |
| 2 | S2：1,673 个场次 | 场次真值 |
| 3 | S3：无网络、不取锁；排名表缺失回退与冻结标志 | 看板误触发网络 |
| 4 | S4：磁盘回退、短窗不截断当月文件、时间盒、`thin` 语义（A0 缺信息 bar 仍 raise） | 事实 4、13 |
| 5 | 意向：顺序检查、`residual_below_minimum` 全平、卖单豁免上限、按 `close_utc` 的吞吐截断 | §6 |
| 6 | S5：合并优先级、对账表覆盖全部持仓、分歧名字剔除 | §7 |
| 7 | 精度学习：拒绝、写文件、次场次新步进 | §7.4 |
| 8 | 记录流三条字段齐全；诊断在提交前调用（构造提交会改变 `held` 的用例） | `00` §5.1、§8.2 |
| 9 | 迁移四个前置与 `restore` 可逆 | §10 |
| 10 | `name: a0` 全链路回归，`tests/execution/test_backtest_equivalence.py` 不变 | 破坏 A0 |
