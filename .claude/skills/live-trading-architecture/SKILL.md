---
name: live-trading-architecture
description: 实盘执行进程的架构规范：主循环、行情订阅与重连、订单状态机、撤单重报、持仓对账、优雅退出、模拟到实盘的上线路径。当设计或编写常驻交易进程、接入交易所 WebSocket/REST、实现下单执行层或排查执行侧问题时使用。触发词：写执行层、主循环、下单、撤单重报、状态机、对账、接实盘、上线。
---

# 实盘执行进程架构

前置约束：

1. `CLAUDE.md` §3 资金红线是本 skill 的上位约束。
2. 写代码走 `/verified-dev`，代码文本按 `/quant-code-standards` §零 用美式英文。
3. 完成后必须过 `/live-trading-risk-check` 整表。

本 skill 由团队 MiniQMT 外部模式架构规范改写。原文的 xtdata 与 xttrader 已替换为
交易所 REST 加 WebSocket；分层、状态机与对账纪律原样保留，该部分与平台无关。

---

## 一、进程分层

| 组件 | 输入 | 输出 | 职责 | 禁止 |
|---|---|---|---|---|
| MarketFeed | WS 行情推送 | 快照缓存（带 ts，供新鲜度校验） | 订阅、重连、缓存 | 交易决策 |
| Strategy | 快照 + 持仓 | 目标仓位 | 纯函数信号计算 | 读网络、写状态、下单 |
| RiskGate | 目标仓位 + 持仓 + 快照 | 执行计划 | 拒绝或削减 | 放大数量、改变方向 |
| OrderRouter | 执行计划 | REST 下单与撤单请求 | 订单提交、状态机推进 | 信号计算 |
| Reconciler | WS 订单推送 + REST 查询 | 持仓与委托的对账结论 | 启动对账、周期对账 | 自动覆盖本地状态 |

数据流向：MarketFeed 到 Strategy 到 RiskGate 到 OrderRouter；OrderRouter 与 Reconciler
双向交换订单与成交状态。

硬性条款：

1. `Strategy` 层必须是纯函数（输入快照与持仓，输出目标仓位），不读网络、不写状态、
   不下单。这是回测与实盘复用同一份信号代码的前提。
2. `RiskGate` 只能收紧，即拒单或削减数量，永远不能放大数量或改变方向。
3. 只有 `OrderRouter` 能调下单接口，且必须过 `DRY_RUN` 与 `live: true` 断言。

---

## 二、主循环

```python
def run(ctx) -> None:
    ctx.load_config()                  # 1. Config and secrets, fail fast.
    ctx.connect()                      # 2. Build REST client and WS connection.
    ctx.reconcile_on_start()           # 3. Startup reconciliation against the venue.
    ctx.subscribe_market()             # 4. Subscribe to market data.
    install_signal_handlers(ctx)       # 5. SIGINT/SIGTERM routed to shutdown().

    while ctx.running:
        try:
            snap = ctx.feed.latest()
            if not ctx.is_fresh(snap):         # Staleness gate.
                continue
            if ctx.risk.tripped():             # Circuit-breaker gate.
                continue
            target = ctx.strategy.decide(snap, ctx.positions)
            plan = ctx.risk.apply(target, ctx.positions, snap)
            ctx.router.execute(plan)
            ctx.state_machine.poll()           # Advance non-terminal orders.
            if ctx.due_reconcile():
                ctx.reconcile()                # Periodic reconciliation.
        except TransientError as e:
            log.warning("[main_loop] recoverable error: %s", e)
            ctx.sleep_backoff()
        except PermanentError as e:
            log.critical("[main_loop] unrecoverable error: %s, stopping", e)
            ctx.running = False
        except Exception as e:
            log.error("[main_loop] unexpected error type=%s msg=%s",
                      type(e).__name__, e, exc_info=True)
            # An unknown state in the execution layer is more dangerous than a halt.
            ctx.risk.trip("unexpected_exception")

    shutdown(ctx)
```

要点：未预期异常一律先熔断，不允许「记一条日志然后继续跑」。执行层处于未知状态的
风险高于停机。

---

## 三、行情：订阅与重连

1. WS 断线必须自动重连并重新订阅。重连后的第一帧不得直接用于下单，
   须等快照时间戳恢复正常再放行。
2. 心跳：按交易所要求发 ping；超过 N 秒无数据视为断线，主动重连。
3. 新鲜度闸：`now - snapshot.ts > STALE_SECONDS` 即放弃本次信号。
   `STALE_SECONDS` 按场所分别设定，写进配置，不硬编码。
4. REST 兜底：WS 中断期间可用 REST 轮询降级，但须在日志标记降级状态，
   且降级期间收紧下单条件，或直接停单，由用户裁定。

---

## 四、订单状态机

### 4.1 状态与转移

| 当前状态 | 允许转移到 | 触发条件 |
|---|---|---|
| `LOCAL` | `PENDING` | 提交请求已发出 |
| `PENDING` | `LIVE` / `REJECTED` | 交易所受理或拒绝 |
| `LIVE` | `PARTIAL` / `FILLED` / `CANCELING` / `EXPIRED` | 成交推进、撤单请求、到期 |
| `PARTIAL` | `FILLED` / `CANCELING` / `EXPIRED` | 继续成交、撤单请求、到期 |
| `CANCELING` | `CANCELED` / `FILLED` | 撤单确认，或撤单前已全部成交 |

终态集合：`FILLED`、`CANCELED`、`REJECTED`、`EXPIRED`。

1. 终态集合显式定义为常量；只有终态订单才能从活跃表移除。
2. 每个非终态订单必须有超时兜底：超过 `ORDER_TIMEOUT_SEC` 未变化即主动查询；
   查不到则按交易所返回裁定，不得假设它已成交或已撤销。
3. 状态名用美式拼写 `CANCELED` / `CANCELING`，全项目一致（`/quant-code-standards` §0.3）。

### 4.2 撤单重报四态纪律

撤单重报是执行层最容易产生重复下单的环节，按四态严格执行：

| 态 | 含义 | 允许的下一步 |
|---|---|---|
| 待撤 | 已发撤单请求，未收到确认 | 只能等待确认，禁止重报 |
| 已撤 | 交易所确认撤销，剩余量明确 | 可按剩余量重报 |
| 部成待撤 | 撤单时已部分成交 | 必须先确认成交量，再按剩余量重报 |
| 撤单失败 | 订单已成交或已进入终态 | 禁止重报，按实际成交处置 |

未收到撤单确认就重报等于双倍持仓，禁止发生；宁可等到超时由人工介入。
重报次数须有上限（`MAX_REPLACE_TIMES`），达到上限即停止并告警。

---

## 五、持仓对账

1. 启动必对账：拉取柜台持仓与未成交委托，与本地持久化状态比对。
2. 不一致时停止下新单并告警，禁止自动「以柜台为准」覆盖本地。自动覆盖会掩盖 bug，
   也可能把手工操作误认为策略持仓。
3. 周期性对账：运行中按固定间隔复核，间隔写进配置。
4. 识别外部持仓：账户里可能存在非本策略的仓位（手工买入、其他策略）。
   本策略只管自己的部分，须用 client order id 前缀或独立子账户区分。
   是否具备子账户能力属 S4 事实，按场所现查官方文档，不得凭记忆假设。
5. 持仓快照落地到本地文件，进程崩溃后可恢复。

---

## 六、上线路径（硬性顺序）

| 阶段 | 门槛 |
|---|---|
| 1 回测 | 过 `/backtest-discipline` 全套，含真实费率、滑点、最小下单量取整 |
| 2 DRY_RUN | 接真实行情，只打日志不下单，跑满一个完整周期，核对「本该下的单」是否合理 |
| 3 模拟或沙盒 | 接交易所模拟环境实下，跑到对账连续一致，核对手续费计算与实际扣费一致 |
| 4 小额实盘 | 用户明确授权后，以最小可行规模上线，风控上限设到很低 |
| 5 逐步放量 | 每次放量都需要新的用户授权，不自动扩大 |

跳过任何一阶段都不允许。每一阶段的结论写进 `research/decisions/`。

---

## 七、两场所的差异（不得互相套用）

| 维度 | OKX（加密） | Trading 212（英股） |
|---|---|---|
| 交易时段 | 7×24 | 交易所时段加假期日历 |
| 结算 | 即时 | T+N，须按 S4 现查 |
| 计价 | USDT / USD 等 | GBP，跨币种标的有 FX 换算与费用 |
| 特有成本 | 资金费率（合约）、提币费 | 印花税、FX 费、监管费，须按 S4 现查 |
| 数量单位 | 基础币，小数步进 | 股数，是否支持碎股须现查 |
| 接口 | 官方 REST 加 WS，文档完整 | API 可用性、限频、下单能力未证实，须现查官方文档 |

表中标注「须现查」与「未证实」的项目均为未证实事实，接入前必须按 `CLAUDE.md` §1.1 S4
取回权威事实并落到 `data/reference/`，不得凭本表推进。
