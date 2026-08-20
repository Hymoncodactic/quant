# T212 平台缺陷目录与故障注入模型

来源：GitHub issues、community.trading212.com（含员工帖）、官方文档中的明示缺陷。
逐条原始出处（URL、日期、原文摘录）存
`data/reference/t212_research_20260820/bug_instances_repos.json`（30 条）与
`latency_execution.json`。本文件把可影响**回测成交结果**的缺陷折算为引擎故障开关；
仅影响账户数据读取/对账的缺陷列入 §4，v0 不进引擎。

## 1. 折算原则

1. 引擎以 bar 为最小时间粒度，故障以「拒单 / 延迟 N 根 bar / 窗口禁用 /
   数量约束」四种原语表达。
2. 每个开关默认值给出依据；无量化依据的参数标「推断」，进敏感性而非当事实。
3. 随机性一律走显式种子的 `numpy.random.Generator`（`validation/02_test_plan.md` §3.4）。

## 2. 引擎故障开关（v0 实现集）

| ID | 开关 | 行为 | 默认 | 依据（详见 bug_instances_repos.json 对应条目） |
|---|---|---|---|---|
| F1 | latency_normal | 市价单基线延迟：细粒度 bar 下顺延 0–1 根（秒级延迟的 bar 折算） | 开 | 官方「usually within seconds」+ 社区 20s 实例（2021-03） |
| F2 | latency_volatile | 触发条件（bar 振幅 > k×滚动中位振幅 或 成交量 > k×中位量）时市价单延迟抽样 1–26 分钟折算 bar 数，成交价取延迟后 bar 的开盘 | 开，k=3（推断） | 2020-09-14 TSLA 14–26 分钟 + 2021-01-04 5–15 分钟（社区多例）；延迟期价格漂移天然实现 0.8% 级不利偏移实例 |
| F3 | outage_window | 配置日期窗口内提交一律失败（订单不存在，信号错失） | 开，v0 仅支持**显式配置窗口**（默认空），抽样发生器列为待办（见变更记录）。日线模式下提交时刻为交易日零点，窗口须配成整日才能命中 | IB 中介宕机两例（官方员工帖 + 新闻） |
| F4 | reduce_only_window | 配置(标的, 窗口)内买入拒单、卖出放行 | 开，作为能力（默认无活动窗口）；GME/AMC 2021-01-28~02-02 为校准实例 | 官方员工帖（David，2021-01-28） |
| F5 | reject_random | 每笔有效订单以概率 p 拒单（UNDEFINED/500 类），下一根 bar 重试成功 | 开，p=0.02（推断；2023-06「大多数失败」为 beta 期极端值，不作默认） | community 61788 posts 63/66/121；87988 post 80 |
| F6 | duplicate_on_retry | F5 拒单中以概率 q「实际已受理」：若策略层重试则产生双份订单 | 开，q=0.1（推断） | 官方非幂等警告（规范原文「may result in duplicate orders」） |
| F7 | cancel_race | 撤单请求所在 bar 内订单若可成交，以概率 r 先成交后撤单失败 | 开，r=0.5（推断） | 规范原文「Cancellation is not guaranteed…」+ labs SKILL.md cancel caveat |
| F8 | quantity_precision | 下单数量仅受理 ≤4 位小数；持仓可 8 位；全额卖出须向下取整留尘 | 开，4 位 | community 87988 posts 125/151（2026-01） |
| F9 | buying_power_buffer | 可用购买力 = 可用现金 × k，介于 k×cash 与 cash 之间的买单拒（InsufficientFreeForStocksBuy） | 开，k=0.95 | community 2025-10 实例 + 官方 95% 数量↔金额单转换阈值（helpcentre 7897588388125）互证 |
| F10 | sell_reservation | 未成交卖向订单（含止损）占用可卖数量，超出部分拒（SellingEquityNotOwned） | 开（非故障，为真实语义） | labs SKILL.md + community 2025-10 根因帖 |
| F11 | stale_ticker | 配置标的集在 API 不可下单（entity-not-found），策略只能跳过 | 开，作为能力（默认空集） | community 87988 post 108（NBIS/OKLO，2025-12）+ SOFI→IPOE 旧代码实例 |
| F12 | submit_pacing | 限价/止损类每 2s 一笔、市价 50/分钟折算为每 bar 提交上限；超出以 `pacing_deferred` 拒单，引擎按目标持仓差分下一 bar 自动重提（与顺延等效且可审计） | 开 | 规范限频表（官方） |
| F13 | partial_fill | 成交量参与上限 10%，按**标的 × bar 聚合**共享（多笔并发订单合计不得超限，zipline 同语义），剩余跨 bar 结转；结转期价格随行情走 | 开 | 执行政策「rare partial fill」+ AMAT 15 片实例（2021-12）+ zipline 参与上限模式 |
| F14 | auth_outage_window | 配置窗口内一切提交失败（401 类，等价 F3 的长窗版本） | 开，作为能力（默认无） | community 87988 posts 179/181（2026-04，10 天 401） |
| F15 | day_expiry | DAY 单交易所本地午夜过期转 CANCELLED | 开（真实语义） | 规范 TimeValidity 描述 |
| F16 | market_closed_queue | 闭市提交的市价单排队至下一开盘首根 bar | **结构性恒开，非开关**：撮合必须有 bar，缺 bar 即排队，任何配置都改变不了；不入开关注册表，敏感性分析不含此项 | 规范 + helpcentre 5781955715613 |

F10/F15 是真实平台语义而非缺陷，纳入注册表以便敏感性开关与审计；F16 见其行内说明。

## 3. 实现形态

`backtest/t212/faults.py`：开关注册表为 `FAULT_SWITCH_DEFAULTS: dict[str, bool]`
（承担 §4.6 分派表的「加变体 = 加条目」职责与结果元数据审计）；各规则实现为
`FaultEngine` 的方法，在 broker_sim 的固定钩子点（准入 / 资格 / 撮合 / 撤单）
被消费。随机数纪律：每个会掷随机数的规则**无论开关状态都消耗等量随机数**
（reject_roll、cancel_succeeds、latency 均如此），保证敏感性运行中关掉一个
开关不重排其它故障的抽样序列。`FaultConfig` dataclass 持全部参数与种子；
完整配置进结果元数据（`validation/02_test_plan.md` §3.3）。权威口径 =
全部默认开（`framework/04_cost_model.md` §6）；理想执行对照档全部关。

## 4. 不进 v0 引擎的缺陷（对账层适用，留档）

| 缺陷 | 原因 |
|---|---|
| 历史订单端点连续多日 500；游标分页丢 8 个月记录；transactions 第 3 页 404；订单+成交行重复计数；现金字段与 App 差便士级 | 影响实盘对账与数据回读，不影响回测成交模拟。将来 `trading212/execution/` 对账模块按此清单设计容错 |
| 限频 429 实际以 403 缺 scope 形态返回 | 客户端层缺陷注入，属实盘执行层测试 |
| WAF 对默认 User-Agent 返回 HTML 403 | 同上 |
| Pies 端点整族下线 | 本项目不用 Pies |
| GBX 便士单位混淆 | 属数据层正确性问题，已由 `02_data_layer.md` §5 与测试 U8/U2 覆盖 |

## 5. 已知无法证实项（不虚构）

1. 各故障的真实发生概率与持续时长分布——社区实例只给出存在性与量级，
   p/q/r/k 默认值全部标「推断」，须做敏感性并在报告限定节披露。
2. 「常规时段内 MarketClosed 误拒」「429 早于配额到来」两类传闻未找到实例，
   不进目录（研究员 unresolved 项原文留档）。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-20 | 初版 |
| 2026-08-20 | 审查后修订：F3 抽样发生器降为待办（v0 显式窗口）并注明日线整日窗口约束；F12 语义定为拒单+引擎重提；F13 上限改按标的×bar 聚合；F16 判定为结构性恒开并移出开关注册表；§3 改述实现形态（注册表 + FaultEngine 方法 + 随机数消耗纪律）；F2/F1 的延迟折算按时间制资格执行（`framework/01` 变更记录同批） |
