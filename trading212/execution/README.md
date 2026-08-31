# trading212/execution/ 目录说明

## 1. 职责

Trading 212 一侧的**执行层**：把 `trading212/strategy/` 算出的目标持仓变成真实
委托，并把成交记回策略自己的账本。分层见 `ARCHITECTURE.md` §2，模块登记见 §2.3，
时序与数据装配的规格见 `fixplans/t212/a0/02_execution.md`。

不装：信号计算（在 `trading212/strategy/`，本层 import 同一份，不得另写）、
撮合模拟（在 `backtest/t212/`）、行情下载实现（在 `trading212/ingest/`）。
本层**不得 import `backtest/`**。

资金红线以 `CLAUDE.md` §3 为准；本层全部默认值满足「不动真钱」。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 |
|---|---|---|
| `__init__.py` | 声明为常规 Python 包 | 使 `from trading212.execution import ...` 可解析 |
| `instruments.py` | 标的映射与场次日历（`Session`、半日市、15:30 决策键） | 决策时刻必须由交易所日历判定，不能靠 bar 缺失反推 |
| `market_data.py` | 1h/1d 刷新与读取、日内截止视图、新鲜度闸 | T212 无行情接口，行情须另接且口径必须与回测一致 |
| `strategy_loader.py` | 按路径加载策略模块并校验身份；支持日内壳的工厂注入 | 策略包被裁定为不导出符号，注册表不能放在那里 |
| `shadow_ledger.py` | 事件溯源影子账本（现金、持仓、在途、幂等事件） | 账户与手工交易共用，且 API 无 client order id，归因必须本地保存 |
| `ledger_store.py` | 账本落盘与装载完整性规则 | 把「怎么写盘」与「记什么账」分开，二者的失效模式不同 |
| `risk_gate.py` | 只收紧的下单前风控闸 | 限额是用户裁定项，缺失必须失效关闭 |
| `order_router.py` | 唯一下单出口，写前意向与歧义冻结 | 下单接口非幂等，任何重试都可能产生重复单 |
| `order_monitor.py` | 挂单轮询与账单收割 | 成交与税费的权威来源是账单，不是下单回执 |
| `reconciler.py` | 账本与账户对账、歧义裁定 | 对不上必须停手，且绝不自动改账 |
| `session_cycle.py` | 两相位编排与全部闸门顺序 | 闸门顺序本身就是口径的一部分 |
| `run_a0.py` | CLI 入口，含单实例锁 | 配置只在入口层读一次；并发实例会重复下单 |
| `daemon.py` | 常驻调度器：随时启动，自动等每场决策窗口跑 decide、收盘后跑 settle，日复一日 | 用户裁定（2026-08-31）盯钟是机器的事；是否武装由配置的 dry_run 每周期现读，看板开关即刻生效 |
| `README.md` | 本文件 | |

## 3. 子目录索引

无。

## 4. 依赖关系

本目录 import：`trading212/strategy/`（经 `strategy_loader`）、`trading212/client.py`、
`trading212/ingest/yahoo_bars.py`、`common/{config,secrets,logging_setup,net,paths}`。
不 import `backtest/`。被 `dashboard/` 只读地引用（看板不下单，手动下单页经本层的
client 与账本）。

## 5. 产出与清理

写 `data/t212/execution_state/`（账本日志与快照、场次状态、`halt` 旗标、日历缓存）
与 `logs/`（均 gitignore）。刷新行情时写 `data/t212/curated/`。

必须保留：`<strategy_id>_journal.jsonl` 与 `_snapshot.json` 是账本本体，删除等于
丢失全部策略持仓归因。`halt` 旗标只能人工删除。

## 6. 状态与锁文件操作卡

全部状态文件在 `data/t212/execution_state/`（实盘）；模拟盘（paper）另有一套完全独立的
同名文件在 `data/t212/execution_state_paper/`，记账归档在 `trading212/records/paper/`，
两套互不可见——demo 成交不可能进实盘账本，demo 急停不影响实盘。「解锁」的第一原则：**先弄清为什么
锁上，再动文件**；每个冻结都在 logs/ 与账本 journal 里留有原因。

| 文件 | 含义 | 解锁/恢复操作 |
|---|---|---|
| `run_a0.lock` | run_a0 单实例锁，看板的账本类写操作也短暂持有同一把锁。锁是进程持有的 flock，**文件在退出后留在磁盘上属正常**，不代表占用 | 报「execution lock is held」时：错误消息里带文件内容（pid 行）；文件为空或存疑时用 `pgrep -f trading212.execution.run_a0` 找进程，`ps -p <pid>` 查看；确属挂死才 `kill <pid>`。run_a0 启动自带约 2 秒重试，能穿过看板的瞬时持锁。**禁止用删文件解锁**——flock 随进程消亡自动释放，删文件反而让第二实例锁到新 inode，破坏互斥 |
| `dashboard.lock` | 看板单实例锁，内容含 pid 与端口 | 同上 |
| `halt` | 急停旗标，**存在即停**（内容无意义）。写入者：`run_a0 halt`、看板急停按钮、settle 的成交时序违规自动急停 | 清除前依次核对：(1) logs/ 与 journal 里为何被立起；(2) `run_a0 status` 显示无未结订单、无歧义、未冻结；(3) reconcile 干净。然后用看板的解除按钮（它会替你重查上述三条，不干净拒绝解除），或人工 `rm` |
| `a0_v0_0_1_journal.jsonl` | 账本事件日志，只增不改，**永不编辑** | 载入报 journal 尾行损坏：备份后仅删除撕裂的最后一行。报「journal ahead of snapshot」：崩溃发生在记账与快照之间，按报错指引把最后一个事件手工并入快照 |
| `a0_v0_0_1_snapshot.json` | 账本快照。`ambiguous_intents` 非空 = 冻结，全系统拒绝新单 | 冻结时先跑 `run_a0 settle`——它按场所证据解冻（找到订单，或 600 秒后证明不存在）。仍冻结则人工到 T212 App 核对挂单与历史，不要直接改文件 |
| `a0_v0_0_1_cycle.json` | 当日已决策去重与当日订单计数 | 仅当**确认当日未提交任何真单**（查 signals.jsonl 与 `run_a0 status`）才可把 `last_decide_session` 置 null 以重跑；已提交则严禁动它 |
| `exchange_calendar.json` | 交易所日历缓存 | 随时可删，下次运行重取。会话时刻显示异常时删它即可 |
| `manual_orders.jsonl` | 手动下单审计日志，唯一把手工场所订单与下单人对上的记录 | 永不编辑删除；对账的「多余持仓推定为手工」与歧义排除都以它为证据 |
| `*.writing` | 原子写入的中间文件，残留 = 写入时崩溃，目标文件仍是上一个完好版本 | 确认目标文件可解析后删除残留；**严禁**手工把 `.writing` 改名成目标文件 |
| `*.retired-<时间戳>` | 清空账本时移开的旧账本 | 永久保留，不挡任何操作 |

## 7. 变更记录

2026-08-21 建立执行层：日频决策、休市提交、次开成交。
2026-08-22 改为小时频（`fixplans/t212/a0/02_execution.md`）：场次内 15:30 决策、
信息止于 14:30 bar、收盘前 60 秒提交、按当日收盘价成交。`daily_cycle.py` 更名
`session_cycle.py`；`instruments.py` 换为场次模型；`market_data.py` 换为 1h 装配与
FX 前 90 分钟断言；新增 `strategy_loader.py`（策略包不再导出符号）。
2026-08-29 依实盘前审查加固：settle 补 `_Cycle.halted()`（原缺失致 settle 必崩且成交时序自动急停为死代码）；新增悬空意向冻结（进程死于 POST 与记账之间时下次运行冻结而非重发）；歧义证据池排除「字段明示非 API」与看板手工单（字段缺失仍保留，546 笔实证 83 笔缺字段）；decide 增加场所可用现金对账本现金的失效关闭核对与时钟偏移闸（上界 10 秒）；提交批内逐单复查急停旗标；POST 到记账的临界区屏蔽 SIGINT/SIGTERM；settle 负现金 CRITICAL 告警；CRITICAL 事件经 `common/alerts.py` 弹系统通知。
2026-08-29 对抗复审后二次加固：悬空意向冻结的时间锚改为意向的 journal 时刻（原用冻结时刻会把真单排除在证据窗外并在 600 秒后误判「从未到达」，等于重开双重下单——复审以脚本复现）；解冻器新增「仅因时间窗被排除的同票同向同量候选存在即拒绝判缺席」与「手工日志污点回避」两道防线；信号屏蔽扩展到提交的全部分支（原异常分支在屏蔽解除后才记账）；看板解除急停改为持执行锁；场所现金核对计入 reservedForOrders（挂着的手工限价单不再造成整日误停）；run_a0 锁文件不再在抢锁前清空且带约 2 秒重试。
2026-08-29 实盘前批次一测试发现标的池跨两张交易所日历（DELL/ORCL/TSM 为 56=NYSE，其余 71=NASDAQ），而执行层只按 71 推导决策键与收盘时刻。两张日历在缓存的 29 个会话上逐字段一致，故当前行为正确；新增 `instruments.schedule_divergences()` 并在 decide 中调用，分歧即中止，把这一隐式假设变为受检约束。
2026-08-29 执行状态与记账归档按环境物理隔离：`execution_state_dir`/`records_dir` 增加 env 参数（live 路径不变，paper 走 `execution_state_paper/` 与 `records/paper/`），run_a0 的实例锁、halt、账本、手工单日志、信号归档、看板采样全部随环境分离；run_a0 改为先读配置后取锁（锁按环境命名）。模拟盘账本已初始化 £1,000。新增根目录 `dashboard_demo.command`（paper 看板，端口 8788）。
2026-08-31 新增 `daemon.py` 常驻调度器（run_a0 daemon 子命令）：纯函数 plan_next 规划下一动作（11 项分支测试），daemon.lock 全程持有、执行锁仅在 decide/settle 期间短持（看板账本操作不再被闲置的调度器挡住）；启动时自恢复（有未结订单或冻结先 settle）；状态落 daemon_status.json 供看板读取。武装口径变更（用户裁定）：守护进程按 execution.dry_run 决定是否真实提交，不再需要 --allow-orders——该旗标仅保留于一次性 CLI 调用。
