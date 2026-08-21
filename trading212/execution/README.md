# trading212/execution/ 目录说明

## 1. 职责

按 `ARCHITECTURE.md` §2 的分层，本目录是 Trading 212 一侧的**执行层**：
下单、撤单、订单状态机、持仓对账。

不装：信号计算（在 `trading212/strategy/`，执行层 import 同一份，不得另写一份）、
撮合模拟（在 `backtest/t212/broker_sim.py`）、行情下载（在 `trading212/ingest/`）。

**当前状态：本目录尚无任何实现代码。** 两个文件都是 0 字节。

未实现的原因见 `WORKING_MEMORY.md`：T212 的实盘下单 API 于 2025-10-01 才开放，
POST 到 FILLED 的实测延迟无公开数据（未决项 14）；`t212.paper.yaml` 与
`t212.live.yaml` 尚未生成，`common/config.py::load_config("t212")` 现在调用会抛
`FileNotFoundError`。

动工前的硬性前置：`CLAUDE.md` §3.1（绝不主动发出真实委托，`DRY_RUN` 默认 True）、
`.claude/skills/live-trading-architecture/SKILL.md`（架构规范）、
`.claude/skills/live-trading-risk-check/SKILL.md`（接实盘前必过的整表复查）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 空文件（0 字节），把本目录声明为常规 Python 包 | 目录内尚无模块，当前没有任何 `from trading212.execution import ...` 语句。**无调用点。** 存在意义是与 `ingest/`、`strategy/` 保持同一包结构，使将来新增模块时无需先补包声明 | 无 |

两个文件均为 0 字节，本目录因而是 `CLAUDE.md` §4.2.3 所述「只含占位文件的目录」。
是否保留该目录待裁定：本目录已在 `ARCHITECTURE.md` §2 登记为正式分层，
删目录会使分层表与磁盘不一致；保留则违反 §4.2.3 的字面规定。

## 3. 子目录索引

无。

## 4. 依赖关系

当前无任何 import 关系，既不读也不写。

实现后的预期依赖方向（`ARCHITECTURE.md` §2，单行不可反向）：
本目录将 import `trading212/strategy/`（信号唯一副本）、
`trading212/client.py`（REST 客户端，尚未创建）、
`common/config.py`（含 `assert_live_allowed()` 下单前置断言）、
`common/secrets.py`（唯一的密钥读取入口）、`common/logging_setup.py`、`common/net.py`。
不得 import `backtest/`。

## 5. 产出与清理

无产物。实现后的日志预期落 `logs/`（gitignore）。

必须保留：暂无实质内容需要保留。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
