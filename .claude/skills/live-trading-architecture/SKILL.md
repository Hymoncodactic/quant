---
name: live-trading-architecture
description: 实盘执行进程的架构规范：主循环、行情订阅与重连、订单状态机、撤单重报、持仓对账、优雅退出、模拟到实盘的上线路径。当设计或编写常驻交易进程、接入交易所 WebSocket/REST、实现下单执行层或排查执行侧问题时使用。触发词：写执行层、主循环、下单、撤单重报、状态机、对账、接实盘、上线。
---

# 实盘执行进程架构

> 前置：`CLAUDE.md` §3 资金红线；写代码走 `/verified-dev`；
> 完成后必须过 `/live-trading-risk-check` 整表。
> 本 skill 由团队 MiniQMT 外部模式架构规范改写——原文的 xtdata/xttrader 已替换为
> 交易所 REST + WebSocket，但**分层、状态机与对账纪律原样保留**，那部分与平台无关。

---

## 一、进程分层

```
                 ┌──────────────┐
   WS 行情推送 → │  MarketFeed  │ → 快照缓存（带 ts，供新鲜度校验）
                 └──────────────┘
                        ↓
                 ┌──────────────┐
                 │   Strategy   │  纯函数：快照 + 持仓 → 目标仓位
                 └──────────────┘
                        ↓
                 ┌──────────────┐
                 │  RiskGate    │  风控闸：拒绝或削减，绝不放大
                 └──────────────┘
                        ↓
                 ┌──────────────┐
   REST 下单  ← │ OrderRouter  │ ← 订单状态机
                 └──────────────┘
                        ↕
                 ┌──────────────┐
   WS 订单推送→ │ Reconciler   │  持仓/委托对账
                 └──────────────┘
```

**硬性**：
- `Strategy` 层必须是**纯函数**（输入快照与持仓，输出目标仓位），
  不读网络、不写状态、不下单。这样才能在回测与实盘复用同一份信号代码。
- `RiskGate` 只能**收紧**（拒单、削减数量），⛔ 永远不能放大或改方向。
- 只有 `OrderRouter` 能调下单接口，且必须过 `DRY_RUN` 与 `live: true` 断言。

---

## 二、主循环

```python
def run(ctx) -> None:
    ctx.load_config()                  # 1. 配置与密钥（fail fast）
    ctx.connect()                      # 2. 建 REST client + WS
    ctx.reconcile_on_start()           # 3. 启动对账：拉柜台持仓与挂单，比对本地
    ctx.subscribe_market()             # 4. 订阅行情
    install_signal_handlers(ctx)       # 5. SIGINT/SIGTERM → shutdown()

    while ctx.running:
        try:
            snap = ctx.feed.latest()
            if not ctx.is_fresh(snap):         # 新鲜度闸
                continue
            if ctx.risk.tripped():             # 熔断闸
                continue
            target = ctx.strategy.decide(snap, ctx.positions)
            plan = ctx.risk.apply(target, ctx.positions, snap)
            ctx.router.execute(plan)
            ctx.state_machine.poll()           # 推进未终态订单
            if ctx.due_reconcile():
                ctx.reconcile()                # 周期性对账
        except TransientError as e:
            log.warning("[主循环] 可恢复 %s", e); ctx.sleep_backoff()
        except PermanentError as e:
            log.critical("[主循环] 不可恢复 %s，停机", e); ctx.running = False
        except Exception as e:
            log.error("[主循环] 未预期 %s: %s", type(e).__name__, e, exc_info=True)
            ctx.risk.trip("unexpected_exception")   # 未知异常一律先熔断

    shutdown(ctx)
```

**要点**：未预期异常**先熔断再说**，不能「记个日志继续跑」——
执行层的未知状态比停机危险得多。

---

## 三、行情：订阅与重连

1. WS 断线必须**自动重连并重新订阅**；重连后的第一帧不得直接用于下单，
   要等快照时间戳恢复正常再放行。
2. 心跳：按交易所要求发 ping；超过 N 秒无数据视为断线，主动重连。
3. **新鲜度闸**：`now - snapshot.ts > STALE_SECONDS` 即放弃本次信号。
   `STALE_SECONDS` 按场所分别设定，写进配置，不硬编码。
4. REST 兜底：WS 中断期间可用 REST 轮询降级，但须在日志标记降级状态，
   且降级期间收紧下单条件（或直接停单，由用户裁定）。

---

## 四、订单状态机

### 4.1 状态

```
        submit
LOCAL ────────→ PENDING ──→ LIVE ──┬──→ PARTIAL ──┬──→ FILLED    (终态)
                   │                │              │
                   │                └──→ CANCELING ┴──→ CANCELED  (终态)
                   └──→ REJECTED (终态)                 EXPIRED   (终态)
```

- 终态集合显式定义为常量；只有终态订单才能从活跃表移除。
- **每个非终态订单必须有超时兜底**：超过 `ORDER_TIMEOUT_SEC` 未变化即主动查询，
  查不到则按交易所返回裁定，绝不假设它已成交或已撤销。

### 4.2 撤单重报四态纪律

撤单重报是执行层最容易出重复下单的地方，按四态严格走：

| 态 | 含义 | 允许的下一步 |
|---|---|---|
| 待撤 | 已发撤单请求，未确认 | 只能等确认，⛔ 不得重报 |
| 已撤 | 交易所确认撤销，剩余量明确 | 可按剩余量重报 |
| 部成待撤 | 撤单时已部分成交 | 必须先确认成交量，再按**剩余量**重报 |
| 撤单失败 | 订单已成交或已终态 | ⛔ 不得重报，按实际成交处置 |

⛔ **未收到撤单确认就重报 = 双倍持仓**。宁可等到超时人工介入。
重报次数须有上限（`MAX_REPLACE_TIMES`），达到即停并告警。

---

## 五、持仓对账

1. **启动必对账**：拉柜台持仓与未成交委托，与本地持久化状态比对。
2. **不一致时停止下新单并告警**，⛔ 不自动「以柜台为准」覆盖本地——
   自动覆盖会掩盖 bug，也可能把手工操作误认为策略持仓。
3. **周期性对账**：运行中按固定间隔复核，间隔写进配置。
4. **识别外部持仓**：账户里可能有非本策略的仓位（手工买入、其他策略）。
   本策略只管自己的部分，须用 client order id 前缀或独立子账户区分。
   ⚠️ 是否有子账户能力属 S4 事实，按场所现查官方文档，不得凭记忆假设。
5. 持仓快照落地到本地文件，进程崩溃后可恢复。

---

## 六、上线路径（硬性顺序）

| 阶段 | 门槛 |
|---|---|
| 1 回测 | 过 `/backtest-discipline` 全套；含真实费率、滑点、最小下单量取整 |
| 2 DRY_RUN | 接真实行情，只打日志不下单，跑满一个完整周期；核对「本该下的单」是否合理 |
| 3 模拟/沙盒 | 接交易所模拟环境实下，跑到**对账连续一致**；核对手续费计算与实际扣费一致 |
| 4 小额实盘 | 用户明确授权后，以最小可行规模上线，风控上限设到很低 |
| 5 逐步放量 | 每次放量都是新的用户授权，不自动扩大 |

⛔ 跳过任何一阶段都不允许。每一阶段的结论写进 `research/decisions/`。

---

## 七、两场所的差异（不得互相套用）

| 维度 | OKX（加密） | Trading 212（英股） |
|---|---|---|
| 交易时段 | 7×24 | 交易所时段 + 假期日历 |
| 结算 | 即时 | T+N（须按 S4 现查） |
| 计价 | USDT / USD 等 | GBP，**跨币种标的有 FX 换算与费用** |
| 特有成本 | 资金费率（合约）、提币费 | 印花税、FX 费、监管费（须按 S4 现查） |
| 数量单位 | 基础币，小数步进 | 股数；是否支持碎股须现查 |
| 接口 | 官方 REST + WS，文档完整 | ⚠️ API 可用性、限频、下单能力须现查官方文档确认 |

表中标 ⚠️ 的项目**均为未证实**，接入前必须按 `CLAUDE.md` §1.1 S4 取回权威事实
并落到 `data/reference/`，不得凭本表推进。
