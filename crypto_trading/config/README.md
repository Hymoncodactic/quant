# crypto_trading/config

## 1. 职责

装 OKX 侧的非密钥配置：连接端点、限频、费率、执行与风控参数、标的池，以及策略参数
基线子目录。不装密钥（`CLAUDE.md` §3.2，密钥只允许存放于 `secrets/` 或环境变量），
不装代码。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `okx.example.yaml` | 运行配置模板，共八个字段块：`live`（实盘必须显式为 true）、`account`（`instrument_type`、`quote_ccy`）、`endpoints`（`rest_base`、`ws_public`、`ws_private`）、`rate_limits`（三类每秒配额）、`fees`（maker/taker，单位 bps）、`execution`（`dry_run: true`、`order_timeout_sec: 30`、`max_replace_times: 3`、`cooldown_ok_sec`、`cooldown_fail_sec`、`stale_seconds: 5`）、`risk`（单笔金额、单标的仓位、总敞口、日内笔数、日内亏损、止损共六个上限）、`ingest`（`periods`、`start_date`） | 它是 `okx.paper.yaml` 与 `okx.live.yaml` 的唯一字段清单与生成模板。删除后无处得知 `load_config("okx")` 期望哪些键，也无处保留「哪些取值仍未取证」的标注 | 人工复制为 `okx.<env>.yaml` 后填写。`common/config.py:59` 只加载 `okx.paper.yaml` 或 `okx.live.yaml`，本模板不进运行路径 |
| `universe.example.yaml` | 标的池模板：`selection_rule`（`min_avg_daily_turnover_usdt`、`lookback_days: 90`、`exclude_leveraged_tokens: true`）、`effective_from`（本版成分生效日期）、`instruments`、`history`（成分变更留痕，供回测使用当时成分而非今日成分） | 无代码调用点：全仓 `.py` 检索未发现任何模块读取 universe 文件。保留理由是它是 `WORKING_MEMORY.md` 未决项 3（标的池筛选标准）与 `.claude/skills/market-data-pipeline/SKILL.md:156` 指定的落点与字段契约，删除后该未决项失去承载文件 | 当前无程序读取 |

取证状态：`okx.example.yaml` 的 `endpoints`、`rate_limits`、`fees` 三块与
`universe.example.yaml` 的阈值，文件内注释均已标注「未证实」，全部取值为占位符。
按 `CLAUDE.md` §1.1 的 S4，接入前须从 OKX 官方文档或接口取回真实值再填。

## 3. 子目录索引

| 子目录 | 说明文档 | 一句话职责 |
|---|---|---|
| `strategies/` | `strategies/README.md` | 策略参数基线 yaml，骨架待实现 |

## 4. 依赖关系

1. 被谁读：`common/config.py` 的 `load_config(venue, env)`（第 43 至 70 行）。它经
   `common/paths.py` 的 `config_dir("okx")` 拼出本目录下的 `okx.<env>.yaml`，
   `env` 由环境变量 `QUANT_ENV` 决定，未设置时解析为 `paper`（`common/config.py:37`）。
2. 以 `live` 加载时，文件缺少 `live: true` 即抛 `ValueError`（`common/config.py:68`）；
   执行层提交订单前再由 `assert_live_allowed()` 复核环境与该标志两个独立条件
   （`common/config.py:73`）。
3. 本目录不 import 任何模块，也不写任何路径。
4. 当前状态：`okx.paper.yaml` 与 `okx.live.yaml` 均不存在，因此此刻调用
   `load_config("okx")` 必然抛 `FileNotFoundError`（`common/config.py:61`）。

## 5. 产出与清理

无运行产物。`okx.paper.yaml` 与 `okx.live.yaml` 一旦生成即属本地长期件，由 `.gitignore`
的 `**/config/*.live.yaml` 与 `**/config/*.paper.yaml` 排除入库，不属于可清理的过程件；
`*.example.yaml` 由 `!**/config/*.example.yaml` 显式保留入库。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
