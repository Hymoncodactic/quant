# trading212/config/ 目录说明

## 1. 职责

装 Trading 212 一侧的 yaml 配置：环境配置（paper / live）、标的池定义，
以及子目录 `strategies/` 下的策略参数基线。

不装：任何密钥（`CLAUDE.md` §3.2，密钥只在 `secrets/` 或环境变量）、
任何 Python 代码、任何数据字节。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `t212.example.yaml` | 环境配置模板。字段覆盖 `live` 开关、`account.base_ccy: GBP`、`endpoints`（含 `secret_name: trading212_api_key`，只传名字不传值）、`rate_limits`、`costs`、`execution`（`dry_run: true`）、`risk`、`ingest` | `common/config.py:59` 加载的是 `t212.<env>.yaml`（`env` 由 `QUANT_ENV` 决定，默认 `paper`）；文件缺失时 `common/config.py:62` 抛出的 `FileNotFoundError` 明确指名「copy `t212.example.yaml`」。删除后缺失配置时无可复制的样板，且 paper/live 两份配置的字段集合失去唯一登记处 | `common/config.py:62`（错误提示按名指向本文件）。没有任何代码读取本文件本身 |
| `universe.example.yaml` | 标的池模板。字段为 `selection_rule`（`min_avg_daily_turnover_gbp`、`lookback_days`、`markets`）、`effective_from`、`instruments`、`history`（成分变更留痕，防幸存者偏差） | **无调用点，当前无产出**。全仓检索未发现任何代码读取本文件或 `universe.yaml`。保留的唯一理由是它承载 `WORKING_MEMORY.md` 未决项 3（标的池筛选标准待用户裁定）与 `.claude/skills/market-data-pipeline/SKILL.md:156`（标的池定义须落 `<venue>/config/universe.yaml`）所要求的字段结构。是否保留待裁定 | 无 |

两个模板文件内的数值当前**全部是未证实占位符**，`t212.example.yaml` 头部注释
自述这一点。接入前须按 `CLAUDE.md` §1.1 的 S4 逐项取证。

`.gitignore` 规则：`**/config/*.live.yaml` 与 `**/config/*.paper.yaml` 排除入库，
`!**/config/*.example.yaml` 反向放行模板。当前磁盘上不存在
`t212.paper.yaml` 与 `t212.live.yaml`，`common/config.py::load_config("t212")`
现在调用会抛 `FileNotFoundError`。

## 3. 子目录索引

| 子目录 | 职责 | 说明文档 |
|---|---|---|
| `strategies/` | 策略参数基线，一策略一版本一文件 | `strategies/README.md` |

## 4. 依赖关系

读：无。本目录是被读方。

写：无。

被谁读取：

1. `common/config.py::load_config(venue, env)` 经 `common/paths.py::config_dir("t212")`
   定位本目录，读 `t212.<env>.yaml`。该函数当前在 `common/` 之外无调用点，
   执行层尚未实现。
2. `common/config.py::assert_live_allowed(cfg)` 校验配置里的 `live: true` 字段，
   是提交真实委托前的最后一道闸（`CLAUDE.md` §3.3）。
3. `strategies/` 下的参数文件由入口脚本直接读，见 `strategies/README.md` §4。

## 5. 产出与清理

无运行产物。

`trading212/config/.DS_Store` 是系统产物，`CLAUDE.md` §4.2.3 列为禁止留存，
`.gitignore` 已排除但文件仍在磁盘上。

必须保留：两个 `*.example.yaml` 模板（是否保留 `universe.example.yaml` 待裁定，
见 §2）。将来生成的 `t212.paper.yaml` 与 `t212.live.yaml` 不入库但必须留在磁盘上。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
