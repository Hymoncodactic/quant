# tests/ingest/ 目录说明

## 1. 职责

装 `trading212/ingest/` 下取数与派生层的单元测试。测试只构造内存数据或写
`tmp_path`，不读 `data/`、不发网络请求。

不装：策略逻辑测试（在 `tests/strategy/`）、执行层测试（在 `tests/execution/`）、
与真实数据湖对照的验收（那是 `scripts/update_data.py` 的实跑）。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `test_yahoo_bars_guard.py` | 半截 bar 守卫三条：当日行被丢弃、按交易所本地日切而非 UTC、伦敦挂牌用伦敦时区 | 2026-08-31 曾把 1,475 个未收盘 bar 当成终值入库，且此后按时间戳跳过、永不覆盖。删掉这三条即失去该缺陷的回归防护 | `pytest tests/ingest/` |
| `test_a1_rank.py` | 排名 pass 六条：委托给 `a1_v0_0_1.rank_table`（spy 断言）、覆盖率闸拒绝半更新场次、默认场次取最近完整者、无覆盖时报错、E5 与因果切断、parquet 列契约往返 | 排名表是实盘 A1 名单的唯一输入；覆盖率闸若失效，一份只含 15 只的表会让整池轮动进那 15 只 | 同上 |
| `__init__.py` | 空文件，声明为包 | 与其他测试目录一致，避免模块名冲突 | pytest 收集 |

## 3. 子目录索引

无。

## 4. 依赖关系

读：`trading212/ingest/yahoo_bars.py`、`trading212/ingest/a1_rank.py`、
`tests/strategy/conftest.py` 的面板构造夹具。写：仅 `tmp_path`。

## 5. 产出与清理

无运行产物。

## 6. 变更记录

| 日期 | 改动 |
|---|---|
| 2026-09-03 | 建目录，随半截 bar 守卫与 A1 排名 pass 落地 |
