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

## 6. 变更记录

2026-08-21 建立执行层：日频决策、休市提交、次开成交。
2026-08-22 改为小时频（`fixplans/t212/a0/02_execution.md`）：场次内 15:30 决策、
信息止于 14:30 bar、收盘前 60 秒提交、按当日收盘价成交。`daily_cycle.py` 更名
`session_cycle.py`；`instruments.py` 换为场次模型；`market_data.py` 换为 1h 装配与
FX 前 90 分钟断言；新增 `strategy_loader.py`（策略包不再导出符号）。
