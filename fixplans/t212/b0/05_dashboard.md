# 05 看板 B0 适配实施步骤（对话 D）

前置：先读 `00_coordination.md` §1.3（展示段）、§2（接缝，只可调 S1、S2、S3、S5）、§3（既有签名冻结）、
§4（所有权）、§5（数据契约）。本文件不定义接缝、口径或数据结构，只写看板怎么实现。

## 0. 指向的交易代码

| 文件 | 状态 | 本文件约束的部分 |
|---|---|---|
| `trading212/dashboard/context.py` | 改 | §3 |
| `trading212/dashboard/signal_view.py` | 改 | §4 |
| `trading212/dashboard/collector.py`、`snapshots.py` | 改 | §5 |
| `trading212/dashboard/api.py`、`server.py` | 改 | §6、§8 |
| `trading212/dashboard/assets/{index.html,app.js,style.css,labels.json}` | 改 | §7 |
| `trading212/dashboard/settings.py` | 改 | §9 |
| `tests/dashboard/**`（含新建 `README.md`、`fixtures/`） | 改 / 新建 | §10 |
| `tests/test_dashboard_diagnostics.py` | 改 | §10 |
| `trading212/dashboard/README.md` | 改 | 登记 |

**只读**：`trading212/{strategy,execution,config,ingest,archive.py}`、`common/**`、`scripts/**`。

## 1. 现状事实（逐条已核）

| # | 事实 | 出处 |
|---|---|---|
| 1 | 一个看板进程对应一个 `strategy_id` 与一本账；参数按扁平 yaml 读 | `context.py` L83-90 |
| 2 | `watch_symbols()` 为 `params.trade_symbols` 加状态标的加 FX；B0 的 yaml 无扁平 `trade_symbols` 时列表塌缩为仅 FX | `context.py` L109 |
| 3 | 任一持仓无报价即 `priced_all=False`，权益 KPI 为空、日汇总跳过、持仓图漏名字 | `collector.py` L275-288；`snapshots.py` L192-194 |
| 4 | `signal_view` 要求模块有 `signal_diagnostics` 且为 A0 形状 | `signal_view.py` L67-68、L93-95 |
| 5 | `get_records` 对 `name` 做 `archive.STREAMS` 白名单校验，不在则返回 400 | `api.py` L240-256 |
| 6 | 成交按 fill 一行写在 **`orders`** 流（键 `fill_id`），**没有** `fills` 流 | `archive.py` L80-86 |
| 7 | `get_instruments` 与 `_halt_clear_locked` 只认 18 条静态映射 | `api.py` L329、L613-615 |
| 8 | 启动与停止绑定模块名 `run_a0`（T 不改名） | `api.py` L428-429、L450、L473 |
| 9 | 文案全部在 `labels.json`，代码保持 ASCII | `README.md` L11-13 |
| 10 | 既有缺陷：`api.get_manual` 有未定义名引用；`snapshots.mark_gap` 丢 env | `api.py` L340-341；`snapshots.py` L241-244 |

## 2. D1 与 T 的解耦（开工前提）

D1 只依赖 M0 与 M1（`archive.STREAMS` 两行）。为不等 T 的其余交付：

| 依赖 | D1 的处理 |
|---|---|
| `b0_v0_0_1.yaml` 未建 | 测试用临时目录内的 yaml fixture 并 monkeypatch `config_dir` |
| 接缝 S1、S3、S5、S6 未实现 | 测试 monkeypatch 这四个名字，返回 `fixtures/` 内的样例；生产代码按 §4、§6 正常调用 |
| `common/paths.a1_rank_path` 未建 | 在函数体内**延迟 import**，缺失时该路由返回 `problem: rank_unavailable` |
| 两条新流未进 `STREAMS` | 由 T 在 M1 合入（`00` §5.1、§8）；D 不 monkeypatch |

## 3. 策略身份与关注列表（`context.py`）

| 步 | 做什么 | 验收 |
|---|---|---|
| 1 | `reload_config` 改为调接缝 S1 `session_cycle.assemble_params(cfg)` 取参数（不再自行扁平读 yaml），`self.params` 为其返回值 | 单测：`a0` 与 `b0` 两种 `strategy_id` 都能构造 |
| 2 | `watch_symbols()` 为以下并集：`params.a0_params.trade_symbols`、账本当前持仓、最近一行 `b0_allocation` 的 `a1_names`、状态标的、FX；`a0` 模式下结果与改动前一致 | 单测：B0 模式下全部持仓可定价 |
| 3 | 报价沿用 `quotes.py` 的批量拉取，名单上限约 60 只 | 30 秒周期内完成 |

## 4. 信号面板（`signal_view.py`）

| 步 | 做什么 |
|---|---|
| 1 | `live_signals` 在 `ctx.signal_name == "b0"` 时：调接缝 S2 取最近一个场次作为 `as_of`，调接缝 S3 `load_b0_injection`（只读，不刷新、不取锁），再调接缝 S6 `b0_v0_0_1.signal_diagnostics(view, portfolio, params, injection)`；输出按 `00` §2.6 原样透传，并对 `a0` 子树复用现有的延迟报价重算 |
| 2 | `a0` 模式路径不变 |
| 3 | 失败仍返回 `{ok: false, problem}`；新增 problem 码 `b0_injection_unavailable`（排名表缺失）、`rank_unavailable`（路径未就绪），各配文案 |
| 4 | **不得**调用接缝 S4（`00` §9.5） |

## 5. 持仓归属与资金分配采样（`collector.py`、`snapshots.py`）

| 步 | 做什么 |
|---|---|
| 1 | `_build_sample` 读最近一行 `b0_allocation` 的 `attribution`，给每个持仓打 `owner`；无记录时该持仓 `owner` 为 `unknown` 并在界面标注，**不自行按名单推断**（推断规则随 `priority` 变化，看板不得维护第二套，`00` §2.6） |
| 2 | 采样增字段 `a0_value_gbp`、`a1_value_gbp`、`cash_gbp` 与三者占比；`_thin` 与日汇总同步增列 |
| 3 | 收益归因：按 **`orders` 流**（事实 6）的成交行按标的分持仓段，段内已实现盈亏按 `owner` 累计；未实现按当前市值减成本。输出 `pnl_a0_gbp`、`pnl_a1_gbp`，实现与未实现分列 |

## 6. 新路由（`api.py` 与 `server.py`）

| 路由 | 返回 | 依赖 |
|---|---|---|
| `GET /api/b0/allocation` | 最近 N 行 `b0_allocation` | 经 `/api/records` 同一读取路径 |
| `GET /api/b0/a1_plan` | 最近 N 次重排的 `a1_plan` 行 | 同上 |
| `GET /api/b0/rank` | 当日排名表前 60 行 | 延迟 import `common/paths.a1_rank_path`；列与枚举见 `00` §5.2 |

读路由不得调用场所或发起网络请求。写路由不新增。

## 7. 面板（`index.html`、`app.js`、`labels.json`、`style.css`）

| 面板 | 内容 | 来源 |
|---|---|---|
| 资金分配 | A0 份额、A1 份额、现金的堆叠面积与当前三格 KPI | §5 采样与日汇总 |
| 持仓表 | 归属标签、股数、市值、成本、未实现盈亏、持有天数 | 账本与采样 |
| 收益归因 | 实现与未实现按 A0、A1 分列，期内累计柱状 | §5.3 |
| A1 名单与排名 | 当期 20 只（rank、score、status）、下一批候选、前 20 与前 40 边界分数、准入数 | 接缝 S6 的 `a1` 子树与 `/api/b0/rank` |
| 重排倒计时 | `sessions_until_next`、上次与下次重排、`rank_as_of`、`rank_stale_sessions` | `a1.rebalance` 子树 |
| A0 子面板 | 现有闸与阈值条原样 | `a0` 子树 |
| 已决策表 | 增 A0 与 A1 目标数、重排标记列，按 `strategy_id` 过滤 | `signals` 流新增键（`00` §5.1） |
| 记录页 | 自动列出两条新流 | `paintRecords` 按 `STREAMS` 枚举 |
| 文案 | 标题改为「策略看板」并显示 `strategy_id`；A0 字面改为数据驱动；新增枚举（`status`、`elig_reason`、problem 码、`owner` 的 `unknown`）逐项配文案 | `labels.json` |

## 8. halt 清除与手动下单

`_halt_clear_locked` 与 `get_instruments` 的 ticker 表改为调接缝 S5
`instruments.ticker_map_for(账本持仓、A0 名单、当期 A1 名单的并集)`。手动下单页的标的列表同源。

## 9. 设置

不新增字段，不新增配置键。风控项的提示文案保持现状（**撤销**早前「显示建议区间」的设计：
没有任何契约承载建议值，且 D 不得新增配置键）。

## 10. 测试与 fixture

`tests/dashboard/fixtures/` 内四个文件，命名与 `00` §5 的流名一致：
`b0_signal_diagnostics.json`（接缝 S6 输出）、`b0_allocation.jsonl`、`a1_plan.jsonl`、`a1_rank.parquet`。

| # | 测试 | 捕捉的缺陷 |
|---|---|---|
| 1 | 全部 B0 面板在 fixture 上渲染且键齐全 | 契约漂移 |
| 2 | `watch_symbols()` 在 B0 模式覆盖全部持仓；`a0` 模式不变 | 定价缺失 |
| 3 | 无 `attribution` 记录时 `owner` 为 `unknown` 且界面有标注，看板不自行推断 | 第二套归因 |
| 4 | 收益归因读 `orders` 流而非不存在的 `fills` 流 | 事实 6 |
| 5 | `signal_view` 在 B0 模式不调用接缝 S4（spy 断言） | `00` §9.5 |
| 6 | 新枚举全部有文案（沿用 `tests/test_dashboard_diagnostics.py` 的枚举测试模式） | 漏文案 |
| 7 | `a1_rank_path` 缺失时 `/api/b0/rank` 返回 `rank_unavailable` 而非 import 失败 | D1 解耦 |
| 8 | `test_b0_records_consistency.py`：看板显示的分配与归因同 `b0_allocation` 记录逐笔一致，差异不超过 £0.01 | `06` §4 的演练核对 |
| 9 | `a0` 模式全部现有测试不变 | 回归 |
| 10 | 修复事实 10 的两处既有缺陷 | 既有缺陷 |

新建 `tests/dashboard/README.md`（六节）。其在 `tests/README.md` §3 的索引行由 **T** 在合并 D1 时补写
（`tests/README.md` 归 T，`00` §4）。

## 11. 顺序

1. D1：§2 解耦措施、§3、§7（fixture 上）、§5、§6、§10。
2. D2（M4.5 之后）：接真实接缝与记录流，paper 环境验收（`06` §4）。
3. 每节完成在 `trading212/dashboard/README.md` 登记；`WORKING_MEMORY.md` 时间线追加一行标 `D`。
