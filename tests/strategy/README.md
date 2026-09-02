# tests/strategy/ 目录说明

## 1. 职责

装 `trading212/strategy/` 下各策略模块的单元测试。测试只构造内存数据，
不读 `data/`、不发网络请求、不写磁盘。

不装：回测引擎与撮合的测试（在 `tests/backtest/`）、执行层测试
（在 `tests/execution/`）、看板测试（在 `tests/dashboard/`）。
与真实数据对照的复现验收不在这里，在 `scripts/20260903_a1_module_backtest.py`
与 `scripts/20260903_b0_module_backtest.py`。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `conftest.py` | 共享夹具：`FakeBar`、`FakeView`、`FakePortfolio`、`make_panel`、`sessions`、`ramp` | 三个测试模块共用同一套 bar 形状与日期约定；各写一份必然漂移 | 本目录全部测试 |
| `test_a1_v0_0_1.py` | A1 模块 12 条测试：因果切断、分数偏移、五条准入边界、缓冲带、重排日历、无价名字、插入顺序、空字典、`a1_book` 来源、`rank_as_of` 断言、身份校验、诊断结构 | 每条对应 `fixplans/t212/b0/01_strategy_a1.md` §6 的一个缺陷类别；删掉即失去该类别的回归防护 | `pytest tests/strategy/` |
| `test_b0_v0_0_1.py` | B0 模块 11 条测试：合成视图、归属与 `priority`、`C1` 公式、免动带与冻结、闸关、提交顺序、清零、空字典条件、活跃集口径、诊断结构 | 同上，对应 `02_strategy_b0.md` §6 | 同上 |
| `__init__.py` | 空文件，声明为包 | 与 `tests/execution/`、`tests/backtest/` 一致，避免模块名冲突 | pytest 收集 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：`trading212/strategy/a1_v0_0_1.py`、`b0_v0_0_1.py`、`a0_v0_0_1.py`、
`a0_intraday_v0_0_1.py`、`trading212/execution/strategy_loader.py`。
写：无。被谁 import：无，pytest 直接收集。

## 5. 产出与清理

无运行产物。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-09-03 | 建目录，随 A1 与 B0 模块落地 |
