# crypto_trading/execution

## 1. 职责

装 OKX 的委托执行：下单、撤单、订单状态机、撤单重报、持仓与资金对账。不装信号计算
（在 `crypto_trading/strategy/`），不装数据下载（在 `crypto_trading/ingest/`）。

当前为骨架，无任何实现文件。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 0 字节空文件，把本目录声明为常规 Python 包 | 本目录尚无实现模块，全仓检索无任何导入点。保留理由是包边界与 `CLAUDE.md` §4.4 的模块头 docstring 落点；当前文件为空，该 docstring 尚未写 | 无调用点 |

骨架待实现，将来放什么（依据 `ARCHITECTURE.md` §2 与
`.claude/skills/live-trading-architecture/SKILL.md`）：常驻主循环、行情订阅与断线重连、
订单状态机、撤单重报、持仓对账、优雅退出。三条硬性约束：

1. 提交任何真实委托前必须调用 `common/config.py:73` 的 `assert_live_allowed(cfg)`。
   该函数校验两个独立条件：配置的 `_env` 等于 `live`，且文件内 `live` 为 `true`。
   任一不满足即抛 `RuntimeError`，使一个走偏的环境变量或一份被改动的配置都不足以单独放行。
2. `dry_run` 的默认值是 `True`（`crypto_trading/config/okx.example.yaml` 的 `execution` 段）。
   把它改为 `False` 属于影响实盘下单行为的参数变更。
3. 风控闸的取值全部来自同一配置文件的 `risk` 段，闸只能收紧不能放大。

定位参照：`.claude/skills/quant-code-standards/SKILL.md:339` 把「改 OKX 的下单重试逻辑」
指向本目录下的 `order_router.py`，该文件尚未创建。
改动本目录下任何文件都会触发 `.claude/skills/live-trading-risk-check/SKILL.md:11`
规定的风险审查。

## 3. 子目录索引

无。

## 4. 依赖关系

本目录当前不 import 任何模块，也不被任何模块 import。按 `ARCHITECTURE.md` §2 的单行依赖，
将来允许 import `common/`、`crypto_trading/client.py`（尚未创建）与
`crypto_trading/strategy/`；禁止 import `backtest/`，也禁止在本目录内另写一份信号逻辑。

## 5. 产出与清理

无运行产物。将来的运行日志落根目录 `logs/`（`common/paths.py:70` 的 `DIR_LOGS`），
不落本目录。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-22 删除 `.gitkeep` 占位件，本目录已有实体文件与本说明，占位不再起作用（`CLAUDE.md` §4.2 第 6、8 条）。
