# B0 上线：架构总图、接缝契约与双对话协作章程

本目录（`fixplans/t212/b0/`）把 B0（A0 与 A1 共用一个账户，规格 `trading212/strategy/b0_spec.md`）
做成可实盘代码。工作由两个并行对话承担：

| 对话 | 代号 | 负责的计划文件 |
|---|---|---|
| 交易代码 | **T** | `01_strategy_a1.md`、`02_strategy_b0.md`、`03_data_pipeline.md`、`04_execution.md`、`06_tests_and_rollout.md` |
| 看板 | **D** | `05_dashboard.md` |

本文件是唯一的骨架：§1 定数据流，§2 定接缝，§3 冻结既有签名，§4 定所有权，§5 定数据契约，
§6 定共享决定。其余六份文件只允许**引用**本文件，不得自行定义接缝、签名或口径。

## 1. 架构总图

系统分三段。每段的产物是下一段的唯一输入，段与段之间不共享内存状态。

### 1.1 盘前段（美股收盘之后，每个交易日一次）

| 步 | 动作 | 产物 | 实施 |
|---|---|---|---|
| 1 | 刷新全池日线（约 1,500 只 + A0 18 只 + QQQ + FX） | `data/t212/curated/<group>/<symbol>/1d/` | `03` §3 |
| 2 | 以刚收盘场次 T 的日线算 A1 排名表（准入、分数、名次） | `data/t212/curated/a1/rank/<T>.parquet` | `03` §4 |

排名表是盘前段的唯一交付物。决策段不重算排名，也不装载 1,500 只面板。

### 1.2 决策段（次一交易日 15:30 纽约时间，一个场次一次）

| 步 | 动作 | 副作用 | 实施 |
|---|---|---|---|
| 3 | 刷新 A0 的 18 只与 QQQ、FX 的 1h 与 1d；对 A1 在场名字做短窗 1h 刷新 | 网络、写数据湖、持锁 | `04` §4 |
| 4 | 装配注入包：A0 日线行、A1 排名表（口径为 T−1）、A1 上期名单、场次序列 | 只读磁盘 | `04` §5 |
| 5 | B0 模块以纯函数算出目标股数 | 无 | `02` |
| 6 | 目标与账本差分为意向，过风控闸，经路由提交，记入账本 | 下单、写账本 | `04` §6 |
| 7 | 写三条记录流：`signals`、`a1_plan`（仅重排日）、`b0_allocation` | 追加写 | `04` §8 |

### 1.3 展示段（随时）

| 步 | 动作 | 实施 |
|---|---|---|
| 8 | 看板只读：账本快照、三条记录流、排名表、只读接缝（§2 中标注「只读」者） | `05` |

看板永不触发第 3 步（网络与锁），永不重算第 5 步的目标。

## 2. 接缝契约（冻结；两个对话共同依赖）

接缝是段与段之间的唯一通道。签名冻结，变更走 §7。

| # | 接缝 | 签名 | 副作用 | 产出 | 消费 |
|---|---|---|---|---|---|
| S1 | 参数拼装 | `session_cycle.assemble_params(cfg) -> dict` | 只读 yaml | T | T 的 `decide`、D 的 `signal_view` |
| S2 | 场次序列 | `market_data.us_sessions(start, end) -> list[date]` | 只读磁盘 | T | T、D |
| S3 | 只读注入 | `market_data.load_b0_injection(params, as_of, held) -> dict` | 只读磁盘 | T | T 的 `decide`、D 的 `signal_view` |
| S4 | 决策前刷新 | `market_data.refresh_for_decision(params, session, key) -> list[str]` | 网络、写湖、持锁 | T | **仅** T 的 `decide` |
| S5 | 标的映射 | `instruments.ticker_map_for(symbols) -> dict[str, str]` | 只读文件 | T | T、D |
| S6 | 诊断 | `b0_v0_0_1.signal_diagnostics(view, portfolio, params, injection) -> dict` | 纯函数 | T | D |
| S7 | 目标 | `b0_v0_0_1.make_strategy(injection)` 返回 `(view, portfolio, params) -> dict[str, Decimal]` | 纯函数 | T | T |

### 2.1 S1 参数拼装

`assemble_params(cfg)` 返回决策与诊断共用的一份参数：`b0_v0_0_1.yaml` 的全部键，
加 `DECISION_PARAM_OVERRIDES`（`decision_time_local`、`exchange_tz`、`bars_per_session`），
加 `a0_params`（`a0_v0_0_1.yaml`，其 `live_from` 覆盖为 `cfg.execution.b0_live_from`），
加 `a1_params`（`a1_v0_0_1.yaml`，其 `live_from` 与 `rebalance_anchor` 覆盖为同一值）。
`_Cycle.__init__` 与看板都调用它，不得各拼一份。

### 2.2 S2 场次序列

美股交易日的唯一真值是**本地 SPY 日线的交易日**（`data/t212/curated/us_etf/SPY/1d/`）。
半日市计为一个场次（与 `a1_spec.md` §5 一致）。
已核：2020-01-02 至 2026-08-28 为 1,673 个场次，与 `b0_spec.md` §5 的回测场次数逐位一致。

场所日历（`exchange_calendar.json`）**不得**用于场次计数：实测其跨度只有 2026-08-17 至 2026-09-28
（37 天），且 `instruments.refresh_calendar` 整体覆盖缓存而非合并。它只用于当日的开闭市与决策键判定。

### 2.3 S3 只读注入包

```
load_b0_injection(params, as_of, held) -> {
  "a0_rows":    {symbol: [(iso_date, o, h, l, c), ...]},   # A0 的 18 只与 QQQ，自 history_start
  "a1_rank":    DataFrame[symbol, ticker, close, score, eligible, elig_reason, rank],
  "rank_as_of": date,          # 排名表对应的场次，正常为 as_of 的前一场次
  "a1_book":    {symbol: weight},   # 上一次重排的名单，取自最近一行 a1_plan 记录；无记录为空字典
  "sessions":   [date, ...],   # S2 的场次序列，自 rebalance_anchor 至 as_of
  "as_of":      date,          # 决策日
  "thin":       [symbol, ...], # 无当日 1h bar 的名字，由 S4 返回后并入；只读调用时为空
}
```

只读：不发网络请求、不取锁、不写任何文件。看板与执行层调用同一函数。

### 2.4 S4 决策前刷新

只有 `session_cycle.decide` 调用。返回 `thin` 列表（缺当日决策键 1h bar 的名字），调用方并入 S3 的包。

### 2.6 S6 诊断输出（结构冻结）

```json
{
  "as_of": "2026-09-02", "strategy": "b0", "priority": "a1",
  "a0": { "gates": {...}, "symbols": {...}, "open_for_business": true },
  "a1": {
    "rebalance": {"anchor": "2026-09-15", "session_index": 13, "every": 21,
                  "sessions_until_next": 8, "last_rebalance": "2026-09-15",
                  "rank_as_of": "2026-09-01", "rank_stale_sessions": 0},
    "eligible_count": 1314,
    "book": [{"symbol": "PLTR", "rank": 1, "score": 4.87, "weight": 0.05, "status": "held"}],
    "next_in": [{"symbol": "ABC", "rank": 21, "score": 1.02}],
    "band_edge": {"rank_20_score": 1.05, "rank_40_score": 0.72}
  },
  "allocation": {"equity_gbp": 1234.56, "headroom": 0.99,
                 "a0_names": ["NVDA"], "a1_names": ["PLTR"], "overlap": ["NVDA"],
                 "a0_target_gbp": 137.2, "a1_target_gbp": 1085.0, "cash_target_gbp": 12.3},
  "attribution": {"positions": {"NVDA": "a1", "AMD": "a0"},
                  "a0_value_gbp": 68.6, "a1_value_gbp": 1150.2, "cash_gbp": 15.8}
}
```

`status` 取值 `held`、`held_in_band`、`entering`、`exiting`、`frozen`。
`attribution.positions` 取值 `a0`、`a1`、`other`，判定规则**与本次定量的实际归属一致**
（`priority = a1` 时重叠标的记 `a1`；`priority = a0` 时记 `a0`），不是按名单成员身份推断。
`a0` 子树为 `a0_v0_0_1.signal_diagnostics` 的原样输出。
键与枚举值冻结；新增键按 §7 走兼容流程。

## 3. 既有签名冻结（D 已在调用、T 计划要改）

以下函数当前的签名冻结，`04` 的改动只允许**追加带默认值的参数**，不得改变既有位置参数与返回类型。
任何不兼容变更走 §7。

| 函数 | 当前签名 | D 的调用点 |
|---|---|---|
| `reconciler.reconcile` | `(client, ledger, order_tickers) -> ReconcileVerdict` | `api._halt_clear_locked` |
| `session_cycle.init_ledger` | `(cfg, cash_gbp) -> dict` | `api.post_ledger_init` |
| `instruments.order_ticker` | `(symbol) -> str` | `api.get_instruments` |
| `instruments.load_calendar` / `session_events` / `sessions` / `last_full_session` | 现签名 | `api.get_sessions`、`context` |
| `market_data.load_frames` | `(symbols, interval, start, end) -> dict[str, DataFrame]` | `signal_view` |
| `market_data.build_view` | 现签名 | `signal_view` |
| `ShadowLedger.load` / `portfolio_view` | 现签名 | `context.ledger` |
| `archive.read_stream` / `stream_stats` / `STREAMS` | 现签名 | `api.get_records` |
| `daemon.read_status` | `(state_dir) -> dict \| None` | `api.get_state` |

`instruments.order_ticker` 保留并改为查 S5 的合并表；D 的新代码一律改用 S5。

## 4. 文件所有权（硬性）

| 路径 | 所有者 |
|---|---|
| `trading212/strategy/**` | T |
| `trading212/execution/**` | T |
| `trading212/config/**` | T |
| `trading212/ingest/**`、`scripts/**` | T |
| `trading212/archive.py` | T |
| `common/**`（含 `paths.py`、`README.md`） | T |
| `trading212/README.md` | T |
| `tests/strategy/**`、`tests/execution/**`、`tests/ingest/**`、`tests/backtest/**`、`tests/live/**` | T |
| `tests/README.md`、`tests/conftest.py`、`tests/__init__.py` | T |
| `trading212/dashboard/**`（含 `assets/`、`README.md`） | D |
| `tests/dashboard/**` | D |
| `tests/test_dashboard_diagnostics.py` | D |
| `docs/**`、`ARCHITECTURE.md`、`fixplans/**`（本文件 §10 除外） | T |
| `trading212/strategy/{a0,a1,b0}_spec.md` | T（按 `06` §6 的清单修订） |

`WORKING_MEMORY.md`：「当前状态」与「未决项」两节归 **T 独占**；D 的状态与未决写在
`trading212/dashboard/README.md` §6。时间线两侧各自追加一行并标注代号 T 或 D，
合并时**两行都保留、按时刻排序**，不视为冲突。

## 5. 数据契约

### 5.1 记录流（T 写，D 经 `/api/records` 读）

| 流名 | 键 | 何时写 | 字段 |
|---|---|---|---|
| `a1_plan` | `rebalance_date` | 每次 A1 重排 | `rebalance_date`、`session_index`、`eligible_count`、`book`（20 项 symbol/rank/score/weight/status）、`dropped`、`added`、`rank_as_of`、`universe_file`、`code_version` |
| `b0_allocation` | `decision_date` | 每个已决策场次 | `decision_date`、`equity_gbp`、`priority`、`a0_names`、`a1_names`、`overlap`、`a0_target_gbp`、`a1_target_gbp`、`cash_target_gbp`、`attribution`、`a0_value_gbp`、`a1_value_gbp`、`cash_gbp` |
| `signals` | 无 | 每个已决策场次 | 既有字段**加** `attribution`（symbol 至 a0/a1/other）与 `rebalance`（bool） |

`archive.STREAMS` 追加两行的改动由 T 在 **M1 即合入 main**（两行，无其他依赖），
使 D1 不必等待 M4 也能通过 `/api/records` 的白名单校验。

### 5.2 状态与产物文件（T 写，D 读）

| 文件 | 内容 |
|---|---|
| `execution_state[_paper]/<strategy_id>_snapshot.json` | 账本快照，结构不变 |
| `execution_state[_paper]/a1_rebalance_state.json` | `anchor`、`session_index`、`last_rebalance`、`rank_as_of`。**纯缓存**：真值由 S2 与 anchor 纯函数算出，此文件供看板免算 |
| `execution_state[_paper]/qty_steps.json` | 逐标的数量步进，默认含 `INTC: 0.001` |
| `data/t212/curated/a1/rank/<date>.parquet` | 列与枚举见 `03` §4.1，**该列表为冻结契约**，D 依此配文案 |
| `daemon_status.json` | 既有字段**加** `a1_session_index`、`a1_next_rebalance`、`rank_as_of`、`rank_stale_sessions` |

### 5.3 配置键（T 定义，D 只读）

```yaml
execution:
  b0_live_from: "YYYY-MM-DD"        # B0 起用日，同时是 A1 的 rebalance_anchor
  strategy:
    name: b0
    version: "0.0.1"
    intraday_name: b0               # 见 §6 A1：B0 模块本身就是被 loader 调用的那一个
    intraday_version: "0.0.1"
```

`strategy_id` 为 `b0_v0_0_1`。

## 6. 共享架构决定（冻结）

| # | 决定 | 依据 |
|---|---|---|
| A1 | 执行侧 loader 按 `intraday_name` 选择被调用模块（已核 `session_cycle.py` L305）。因此 **B0 模块自身即 `intraday_name`**，配置写 `intraday_name: b0`；`decide` 的调用行不改。B0 模块内部再调 `a0_intraday_v0_0_1.make_strategy` 与 `a1_v0_0_1.make_strategy` | 不改调用点即不会破坏 `name: a0` 的现行路径 |
| A2 | B0 以**单一账本** `b0_v0_0_1` 运行 | 对账、现金闸、歧义匹配均按单账本设计 |
| A3 | A1 排名与准入口径为**上一完整场次 T−1 的收盘**；定量价格为决策时刻可得的最新 1h bar | 15:30 窗口装不下全池刷新 |
| A4 | 场次序列与重排计数以 **S2（SPY 日线）** 为唯一真值，**半日市计入**（与 `a1_spec.md` §5 一致）；`session_index` 为纯函数，abort 的场次照常计数 | 已核 1,673 场次与回测一致；撤销早前「只数完整场次」的决定 |
| A5 | A1 准入增第五条 E5：**无已验证 ticker 的候选不准入**。该条须同步写入 `a1_spec.md` §3.2（`06` §6） | `order_ticker` 对未映射标的抛异常 |
| A6 | 目标字典插入顺序：先全部减仓与清零，再 A0 买入，最后 A1 买入 | 引擎按插入顺序提交，卖单在前可放出现金 |
| A7 | B0 的 `compute_targets` 在**正常美股场次**永不返回空字典；非美股交易日与 FX 缺失时返回空字典（此二者在实盘由更早的闸拦截，不会到达策略） | 空目标会被 `decide` 判为 abort |
| A8 | A0 的信号集经 `dataclasses.replace(portfolio, cash_gbp=…, available_cash_gbp=…)` 的合成视图读取 | `b0_spec.md` §3.1 |
| A9 | 数量精度默认 0.0001；被拒后写入 `qty_steps.json` 次日重试。上线前在 **demo 账户**做精度探针，**该探针会提交真实委托，须用户当轮授权** | `CLAUDE.md` §3.1 |
| A10 | 风控限额为用户裁定项，取值在 `06` §5 一次提请 | `CLAUDE.md` §1.4 |
| A11 | A0 实盘账本迁移到 B0 账本经新增事件 `BOOK_ADOPTED`，前置条件与回滚见 `04` §10，**须用户当轮授权** | 无现成迁移路径 |
| A12 | A1 名单 B1 与缓冲带的上期名单**只从注入包 `a1_book` 读取**，不从持仓推导 | `b0_spec.md` §3.6、§9.2 |

## 7. 契约变更流程

1. 提出方在 §10 追加一行：日期、代号、涉及的接缝或契约、原因、是否向后兼容。
2. 向后兼容（新增键、新增带默认值的参数）：提出方直接实现并追加记录。
3. 不兼容（删键、改义、改签名、改枚举）：在 `WORKING_MEMORY.md` 时间线追加一行标注
   「契约变更待另一方确认」，对方确认后才可实现。未确认前双方都不得改。
4. 每个对话开工第一件事：读 `WORKING_MEMORY.md` 与本文件 §10。

## 8. 里程碑

| 里程碑 | 内容 | 谁 | 前置 |
|---|---|---|---|
| M0 | 本目录冻结 | 已完成 | |
| M1 | `a1_v0_0_1.py` 与参数、单测、回测复现；**同时把 `archive.STREAMS` 两行合入 main** | T | M0 |
| M2 | `b0_v0_0_1.py` 与参数、`signal_diagnostics`、单测、回测复现 | T | M1 |
| M3 | 数据自动化：盘前排名表、映射文件、调度与锁 | T | M1 |
| M4 | 执行层接 B0：接缝 S1 至 S5、意向顺序、精度、记录流、重排状态 | T | M2、M3 |
| D1 | 看板全部 B0 面板在 fixture 上完成 | D | M0（`STREAMS` 需 M1） |
| M4.5 | T 在 main 上用真实 `signal_diagnostics` 输出重生成 `tests/dashboard/fixtures/`，并跑 `tests/dashboard/` | T | M4、D1 已合并 |
| D2 | 看板对接真实接缝与记录流，paper 环境验收 | D | M4.5 |
| M5 | 模拟盘演练（含至少一次重排） | T（D 配合） | D2 |
| M6 | 实盘启用 | 用户裁定 | M5 |

合并顺序：M1、M2 先入 main；D1 其后；冲突按 §4 所有权裁决。

## 9. 禁止事项

1. D 不改 T 所有的任何路径；T 不改 `trading212/dashboard/**` 与 `tests/dashboard/**`。
2. 双方都不改 `data/*/raw/`，不提交密钥，不把 `dry_run` 改为 false（用户裁定项）。
3. 双方都不在对方目录内放临时文件、fixture 或脚本。
4. 双方都不重命名或移动 §2、§3、§5 中列出的函数、文件与流。
5. 看板不得调用 S4，不得在读路由内发起网络请求或取锁。

## 10. 变更记录

| 日期 | 代号 | 契约 | 改动 | 兼容性 |
|---|---|---|---|---|
| 2026-09-02 | 统筹 | 全部 | 初版 | |
| 2026-09-02 | 统筹 | §1–§6 | 对抗复审 61 条发现后按 8 类根因重写：新增架构总图与接缝表；场次真值改为 SPY 日线且半日市计入（撤销原 A4）；B1 改为注入（A12）；`intraday_name: b0` 不改调用点（A1）；补全所有权与既有签名冻结；`STREAMS` 提前到 M1；新增 M4.5 | 不兼容，取代初版 |
| 2026-09-03 | T | §2.3、§2.6、§5.2 | 注入包实现时新增三个键：`a0_mode`（"rows" 实盘 / "view" 回测，A0 的活跃集与日线视图来源）、`view_symbols`（决策该装载哪些标的，= A0 集合 ∪ A1 在场名字 ∪ 持仓 ∪ FX）、`held`。诊断 `a1.rebalance` 增 `frozen` 布尔。全部为新增键 | 向后兼容 |
| 2026-09-03 | T | §2.3 | `sessions` 在决策日 SPY 自身日线尚未发布时追加当日场次。原定义「S2 的场次序列」在 15:30 会停在昨日，B0 读作「今日非场次」而返回空目标，`decide` 判 abort——每个场次都会如此。当日是不是常规场次已由 `decide` 的场所日历闸证明，故追加而非从价格反推 | 兼容，行为修正 |
| 2026-09-03 | T | §6 A5 / §2.3 | A1 腿在 `a1_frozen` 为真时**不轮动**（返回空目标），而非按陈旧排名重排；`a1_rank` 缺失时诊断降级为空排名表而不抛异常。原计划只写了「冻结」适用于 thin 名字，未定义排名表整体缺失时重排日的行为 | 兼容，补全未定义行为 |
| 2026-09-03 | T | §2.6 | B0 取 A1 当期名单用 A1 腿的 `book`（选中名单）而非「返回值中 q > 0 的键」（02 D4 原文）。二者仅在「被选中但当日无价」时不同：按 D4 原文该名字会掉出 B1，随后被清零规则卖出——正是 02 D7 冻结规则要防的事 | 兼容，修正 02 D4 |
| 2026-09-03 | T | §5.1 | `a1_plan` 与 `b0_allocation` 按各自的日期键去重（`archive._append_keyed`），重放同一场次不会写重复行 | 兼容 |
| 2026-09-03 | T | 06 §2 | 复现判据由「期末权益逐位一致」改为「模块与参考实现在同一进程同一面板上逐位一致」。原判据不可达：参考实现依赖 `PYTHONHASHSEED`（实测两种子相差 £1,626），且日线复权回溯使隔日数据不复现历史成交价。两份参考脚本已改为确定序 | **不兼容，取代 06 §2 的期末判据行** |
