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
| `__init__.py` | 空文件（0 字节），把 `tests` 声明为常规包 | 测试模块之间用绝对路径互相 import（例如 `tests/backtest/test_broker.py` 的 `from tests.backtest.conftest import ...`），该 import 要求 `tests` 可作为包解析；本文件把它声明为常规包，使这条路径在任何 import 模式下都成立。当前内容为空，未按 `CLAUDE.md` §4.4 写模块头 docstring | pytest 收集时的包解析；`tests/backtest/` 下 5 个测试文件的绝对 import 路径以它为根 |

## 3. 子目录索引

| 子目录 | 说明文档 |
|---|---|
| `backtest/` | `tests/backtest/README.md` |

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
