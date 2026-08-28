# `tests/` 目录说明

## 1. 职责

本目录是全项目的自动化测试根，只装 pytest 可收集的测试代码与其夹具。

不装的内容：任何被生产代码 import 的模块、任何真实数据、任何会发起网络请求或
下单的代码。用真实落地数据做的冒烟检验不在此处，按
`docs/backtest/validation/02_test_plan.md` §2 的裁定放在 `scripts/` 下带日期前缀的
一次性脚本里（现有两例：`scripts/20260820_t212_backtest_smoke.py`、
`scripts/20260821_a0_framework_backtest.py`）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `conftest.py` | 全局自动夹具：把 `common/alerts.py` 的通知在测试中替换为捕获列表 | 测试触发的对账失配与歧义路径会弹真实 macOS 通知，与真实交易告警无法区分，且无头环境下每次可卡 5 秒 | pytest 全部用例自动生效 |
| `__init__.py` | 空文件（0 字节），把 `tests` 声明为常规包 | 测试模块之间用绝对路径互相 import（例如 `tests/backtest/test_broker.py` 的 `from tests.backtest.conftest import ...`），该 import 要求 `tests` 可作为包解析；本文件把它声明为常规包，使这条路径在任何 import 模式下都成立。当前内容为空，未按 `CLAUDE.md` §4.4 写模块头 docstring | pytest 收集时的包解析；`tests/backtest/` 下 5 个测试文件的绝对 import 路径以它为根 |

## 3. 子目录索引

| 子目录 | 说明文档 |
|---|---|
| `backtest/` | `tests/backtest/README.md` |
| `live/` | `tests/live/README.md` —— 交易时段的人工测试计划（文档，无代码） |

## 4. 依赖关系

读入：本目录自身不读任何文件。子目录的依赖见其各自 README。

写出：无。子目录中涉及写文件的用例一律写进 pytest 的 `tmp_path` 临时目录，
不在项目目录内落地。

被谁 import：无生产代码 import `tests/`。唯一的消费方是 pytest 收集器。

运行方式：仓库根目录下执行 `./.venv/bin/python -m pytest tests/`。
项目内无 `pyproject.toml`、`pytest.ini`、`setup.cfg` 等 pytest 配置文件，
测试收集完全依赖默认发现规则；`python -m pytest` 会把当前工作目录置于 `sys.path` 首位，故须在仓库根目录下运行，`tests.backtest.conftest` 才能解析。
2026-08-22 收集结果为 100 项，全部来自 `tests/backtest/`。

## 5. 产出与清理

| 产物 | 落点 | 处置 |
|---|---|---|
| `__pycache__/` | 本目录及子目录 | 工具产物，按 `CLAUDE.md` §4.2 不得留在项目目录，可随时删除 |
| `.pytest_cache/` | 仓库根（若产生） | 同上 |

测试用例写出的文件全部落在 pytest 的 `tmp_path` 下，测试结束由 pytest 自行回收，
项目目录内无需清理。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
2026-08-29 新增 `test_dashboard_diagnostics.py`：锁定连接失败的原因判定，覆盖实盘密钥被
demo 主机拒绝、本机 DNS 被劫持、CDN 边缘地址不同不得误判为劫持三类情形。
2026-08-29 新增 `test_audit_defenses.py`（20 项）：悬空意向冻结、归因过滤、批内急停、
场所现金核对、settle 急停路径与负现金告警、时钟偏移解析。`test_dashboard.py` 增补
策略运行中账本变更须 409 的互斥测试。
2026-08-29 复审轮：`test_audit_defenses.py` 增至 25 项（时间锚回归、近失候选拒缺席、手工日志污点）；新增根 `conftest.py` 静音告警通道。
2026-08-29 新增 `live/` 子目录：交易时段测试计划。发起网络请求的驱动脚本按 §一留在 `scripts/`。
