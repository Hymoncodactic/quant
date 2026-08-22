# T212 实盘执行层操作说明

模块职责见 `ARCHITECTURE.md` §2.3。本文件是运行、排期、恢复的操作规程。
资金红线以 `CLAUDE.md` §3 为准；本层全部默认值满足「不动真钱」。

## 1. 相位与排期

A0 是持有版日频策略：以 t-1 收盘信息决策，市价单在休市时提交，
由场所排队至下一常规开盘成交（venue 文档明示的排队语义）。两个相位：

| 相位 | 命令 | 时间窗（前提条件由代码强制） | 动作 |
|---|---|---|---|
| decide | `python -m trading212.execution.run_a0 decide` | 美股常规时段休市中，且伦敦日期已越过决策日（保证 GBPUSD 日线收敛） | 刷新日线、对账、算目标、差分、风控闸、提交（默认 dry-run） |
| settle | `python -m trading212.execution.run_a0 settle` | 排队单成交之后（下一开盘后任意时刻） | 轮询挂单离场、从账单收割成交与税费、退休订单、复核对账 |

参考排期（本地 UTC+8）：每日 08:30 先 `settle`（收割前一晚开盘的成交），
随后 `decide`（为当晚开盘排队新单）。同一决策日重复 decide 会被拒绝；
风控闸失效关闭导致的中止不占用决策日，修正配置后可当日重跑。

辅助命令：`status`（只读总览）、`init-ledger --cash-gbp <N>`（建账本，一次性）、
`halt`（落停牌旗标；旗标文件无代码删除路径，解除须人工删除
`data/t212/execution_state/halt`）。

## 2. 上线路径与武装条件

按 `/live-trading-architecture` §六的顺序，不得跳步：

1. 回测已完成（`research/decisions/20260821_a0_framework_comparison.md`）。
2. DRY_RUN：`QUANT_ENV=live` + `execution.dry_run: true`（现状）。每日跑
   decide/settle，核对 journal 中的意向是否合理，跑满一个完整周期。
3. 模拟盘：需要 demo 账户的 practice API key（现有 key 仅 live 有效），
   `QUANT_ENV=paper` 指向 demo 主机实下单，对账连续一致后过闸。
4. 小额实盘：用户当轮明确授权后，同时满足全部条件才会真实提交：
   `QUANT_ENV=live`、配置含 `live: true`、`execution.dry_run: false`、
   `risk` 区块全部限额为正、命令行带 `--allow-orders`（逐次武装，不延续）。
5. 放量：每次放量都是新的授权。

风控限额（`t212.live.yaml` 的 risk 区块）当前全为 0：闸门失效关闭，
任何委托被整体拒绝。取值属用户决策，未裁定前不得填写。

## 3. 账本与恢复

账本为事件溯源结构：`a0_v0_0_1_journal.jsonl`（只追加）+
`a0_v0_0_1_snapshot.json`（原子替换），事件按 event_id 幂等。恢复规则：

| 情形 | 系统行为 | 人工处置 |
|---|---|---|
| 快照缺失而日志存在 | 拒绝装载（不以空基底重建） | 依据 journal 逐事件核对后重建快照，或从备份恢复 |
| 提交结果歧义（超时/5xx/断网） | 账本冻结，批次中止 | 跑 `settle`：以 venue 在案证据自动裁定；「判无」有 10 分钟最小年龄门 |
| 崩溃于 POST 之后、回执落账之前 | 下次 decide 的对账检出「API 来源的未知挂单」并拒绝开新仓 | 核对该挂单归属后人工并账（编辑快照须先备份并在 journal 补记 NOTE） |
| 账本持仓 > 账户持仓 | 对账 MISMATCH，拒绝开新仓 | 查 journal 与账单核差；不得自动以账户覆盖账本 |
| settle 截止仍有挂单 | 只告警，不自动撤单 | 人工在 App 处置或延后重跑 settle |

对账为单向：账户多于账本视为手工持仓，不告警。归因假定本进程是该账户
唯一的 API 下单来源；若引入第二个 API 工具，该假定失效，须重新设计归因。

## 4. 已知未证实项（接实盘前须实测）

1. `walletImpact.netValue` 的符号约定与是否已含税费：以 demo 首笔成交
   对照账户现金流核定（`order_monitor._apply_fill` 现按「买出卖入」施号）。
2. 下单数量精度上限（现按 4 位小数地板）与最小订单价值（现按配置项）。
3. API POST 到成交的实测延迟（公开资料无数据）。
4. 场所对排队市价单的成交价口径（开盘竞价还是首笔行情）。
