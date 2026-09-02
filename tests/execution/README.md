# tests/execution/ 目录说明

## 1. 职责

装 `trading212/execution/` 与 `trading212/{client,archive}.py` 的单元测试。
测试只构造内存对象或写 `tmp_path`：不发网络请求、不下单、不读 `data/`
（唯一例外 `test_backtest_equivalence.py`，见下）。

不装：策略逻辑测试（在 `tests/strategy/`）、取数与派生层测试（在 `tests/ingest/`）、
回测引擎测试（在 `tests/backtest/`）、看板测试（在 `tests/dashboard/`，归对话 D）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `conftest.py` | 两个账本夹具：`ledger`（£1,000 空账本）与 `funded_ledger`（持 2 股 NVDA、无未结单） | 半数用例都从「一个有持仓的账本」出发，各自搭建必然漂移 | 本目录多数测试 |
| `test_b0_execution.py` | B0 接入的 27 条：接缝 S1 至 S4 的语义、场次表在决策日的追加、排名陈旧回退与冻结、上期名单来自记录流、短窗刷新不截断整月、thin 与硬失败的分界、意向顺序告警、残量全平、卖单豁免单笔上限、按收盘截断吞吐、精度学习与持久化、日历分歧逐标的、两条记录流字段与去重、账本移交四个前置与 restore 可逆 | 每条对应 `fixplans/t212/b0/04_execution.md` §11 的一个缺陷类别 | `pytest tests/execution/` |
| `test_backtest_equivalence.py` | 实盘数据路径与引擎数据路径喂给同一策略模块，同一决策键下目标逐标的相等 | 口径漂移的唯一自动化守卫。**读真实数据湖**，故在无数据环境下跳过 | 同上 |
| `test_session_cycle_units.py` | 决策周期的纯函数单元：闸门顺序、等待提交时刻、现金短缺判定 | 闸门顺序本身就是口径 | 同上 |
| `test_risk_gate.py` | 风控闸：失效关闭、提交窗口、卖量钳制、数量步进、最小与最大名义 | 限额是用户裁定项，任何放宽都必须是显式的 | 同上 |
| `test_order_router.py` | 下单出口：写前意向、DRY_RUN 短路、未武装降级、歧义冻结与后续意向不再尝试 | 下单接口非幂等，歧义处理错一次就是重复下单 | 同上 |
| `test_order_monitor.py` | 挂单轮询与账单收割，含成交时序违规判定 | 成交与税费的权威来源是账单不是回执 | 同上 |
| `test_reconciler.py` | 对账与歧义解除的正证据规则 | 对不上必须停手，且绝不自动改账 | 同上 |
| `test_shadow_ledger.py` | 事件幂等、悬空意向冻结、组合视图、分配额变更 | 账本是策略持仓归因的唯一记录 | 同上 |
| `test_instruments_calendar.py` | 标的映射（含宽池映射文件的读取与 A0 覆盖）与场次日历、半日市、决策键 | 决策时刻必须由日历判定；映射错一次就是把单发到另一家交易所 | 同上 |
| `test_market_data_view.py` | 截止视图与日内新鲜度闸 | 视图多一根 bar 就是前视 | 同上 |
| `test_daemon_planner.py` | `plan_next` 的分支与睡过窗口的一次性通知 | 常驻调度器的决策是纯函数，必须逐分支钉死 | 同上 |
| `test_audit_defenses.py` | 实盘前审查加固项的回归：成交时序违规自动急停、负现金告警等 | 每条对应一次真实的审查发现 | 同上 |
| `test_client.py` | 限频、重试、分页修补 | 场所的分页缺陷不修就会请求到不存在的路径 | 同上 |
| `__init__.py` | 空文件，声明为包 | 与其他测试目录一致，避免模块名冲突 | pytest 收集 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：`trading212/execution/**`、`trading212/{client,archive}.py`、
`trading212/strategy/**`（经 `strategy_loader`）。
`test_backtest_equivalence.py` 另读 `data/t212/curated/`。
写：仅 `tmp_path`。被谁 import：无，pytest 直接收集。

## 5. 产出与清理

无运行产物。`__pycache__/` 为工具产物，可随时删除。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-09-03 | 建本文件（`fixplans/t212/b0/06_tests_and_rollout.md` §4 要求补齐）；同日新增 `test_b0_execution.py`，并因告警文案由字面 `A0` 改为 `strategy_id` 而更新 `test_audit_defenses.py`、`test_daemon_planner.py` 的期望字符串，因宽池映射覆盖 KO 而改写 `test_instruments_calendar.py` 的未映射用例 |
