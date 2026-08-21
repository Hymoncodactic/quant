# `common/` 目录说明

## 1. 职责

本目录装两条交易线（`crypto_trading/`、`trading212/`）与 `backtest/` 共用的、
与交易场所无关的基础设施：路径构造、配置加载、密钥读取、日志初始化、网络退避
与限频、parquet 原子写入与读取。

不装的内容：任何场所特有的口径（费率、下单精度、交易日历、接口字段），任何策略
信号，任何回测逻辑，任何会发起下单请求的代码。场所差异一律留在
`crypto_trading/` 与 `trading212/`（`CLAUDE.md` §四目录约束表）。新增模块必须先在
`ARCHITECTURE.md` §2.1 登记再写代码。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `__init__.py` | 空文件（0 字节），把 `common` 声明为常规包 | 全项目所有 `from common.X import ...` 以它为包根。与 `crypto_trading/`、`trading212/`、`backtest/`、`tests/` 的同名文件写法一致；删除后 `common` 不再是显式包。当前内容为空，未按 `CLAUDE.md` §4.4 写模块头 docstring | 下列各行「谁在用」中的全部 import 点 |
| `paths.py` | 全项目唯一的路径来源。定义根路径常量（`ROOT`、`DIR_DATA`、`DIR_DOCS`、`DIR_LOGS`、`DIR_BACKTEST_RESULTS` 等）、两套互不混用的注册表（`VENUE_DIRS`/`VENUES` 为可下单场所，`DATA_SOURCES` 为数据源，后者含只读的 `binance`），以及 16 个分区路径构造函数。多数构造函数接受 `data_root` 注入，使代码在 git worktree 中运行时仍能指向主工作副本旁的数据湖 | 删除后每个 ingest 脚本、回测数据层与 manifest 生成器都要各自拼接分区路径。布局一旦分叉，写入方与读取方会指向不同目录，且不会报错 | `backtest/engine/results.py:32`, `backtest/engine/strategy_loader.py:30`, `backtest/okx/data_source.py:30`, `backtest/t212/data_source.py:35`, `trading212/ingest/yahoo_bars.py:44`, `common/config.py:23`, `common/logging_setup.py:22`, `common/secrets.py:23`, `scripts/` 下 5 个脚本, `tests/backtest/test_strategy_and_okx_source.py:16` |
| `config.py` | 加载 `<venue>/config/<venue>.<env>.yaml`。`current_env()` 按环境变量 `QUANT_ENV` 解析环境，未设置时返回 `paper`；`load_config()` 在按 live 加载而文件缺 `live: true` 时拒绝返回；`assert_live_allowed()` 是提交真实委托前的最后一道断言，要求「环境为 live」与「配置含 `live: true`」两个条件同时成立，任一不成立即抛 `RuntimeError` | `CLAUDE.md` §3.3 要求的环境隔离与实盘断言在此实现，且是唯一实现。删除后执行层没有统一的 paper/live 闸，误设环境变量即可直达实盘 | **当前无调用点**。执行层（`crypto_trading/execution/`、`trading212/execution/`）尚未编写。保留理由是它是 §3.3 的落地件，接实盘前必用 |
| `secrets.py` | 唯一的密钥读取入口。`get_secret()` 先读环境变量 `QUANT_SECRET_<NAME>`，未命中再读 `secrets/<name>.txt` 首行，凭据文件权限宽于 600 时抛 `PermissionError`；`mask()` 保留首尾各四位做脱敏，长度不足 12 位则全遮 | `CLAUDE.md` §3.2 规定业务代码不得自行读凭据文件或环境变量。删除后该约束失去唯一执行点，密钥读取会散落进各业务模块 | **当前无调用点**。尚无接入交易所鉴权的代码 |
| `logging_setup.py` | 日志初始化。`get_logger()` 返回配置好的 logger：时间戳为 UTC ISO 8601（显式替换 formatter 的 converter 为 `time.gmtime`），文件落 `logs/<模块名首段>_YYYYMMDD.log`，控制台只输出 WARNING 及以上，`propagate` 关闭，重复调用不叠加 handler | 统一日志格式与落点。删除后各模块各自初始化，长驻交易进程的故障定位失去一致的时间基准（`quant-error-handling` §3） | **当前无调用点**。现有脚本用 `print` 输出进度 |
| `net.py` | 网络原语。`backoff_seconds()` 计算重试等待：服务端给出的 `retry_after` 优先，否则按 `RETRY_BASE_SEC` 指数增长并乘抖动系数，上限 `RETRY_MAX_SEC`；`TokenBucket` 是线程安全令牌桶，按场所公布上限乘 `SAFETY_RATIO = 0.7` 取用；`TransientError`/`RateLimitError`/`PermanentError` 区分可重试与不可重试。模块显式声明不提供下单重试，理由是下单非幂等 | `backoff_seconds` 是 Binance 归档下载器的重试节拍来源，删除即断该下载链路 | `crypto_trading/ingest/binance_archive.py:46` 导入、`:129` 调用 `backoff_seconds`。`TokenBucket` 与三个异常类**当前无调用点** |
| `store.py` | 数据湖的 parquet 写入与读取。`write_table()` 先写 `.writing` 临时件再 `os.replace` 原子改名，写前按时间列排序，单调整数列走 `DELTA_BINARY_PACKED`、其余列走字典编码，zstd level 3、row group 131072；`is_readable_parquet()` 只读 footer 判完整性；`clear_stale_temps()` 清理中断残留；`read_dataset()` 用 DuckDB 就地查询分区目录；`parquet_stats()` 返回行数与字节数。压缩级别、row group 大小与排序的取值依据在文件内逐条注明实测数字 | 全项目所有 parquet 落地都经 `write_table`。删除后原子性与压缩口径同时失守：中断的下载会留下能通过存在性检查的截断文件，续跑会跳过它，损坏由此长期潜伏 | `backtest/engine/results.py:33`、`trading212/ingest/yahoo_bars.py:46`、`scripts/20260819_ingest_crypto_bookticker.py:49`、`scripts/20260819_ingest_crypto_phase1.py:32`、`scripts/20260819_ingest_equity.py:49`、`scripts/update_data.py:42`、`tests/backtest/test_strategy_and_okx_source.py:17`。其中 `read_dataset()` 与 `parquet_stats()` **当前无调用点** |

本轮检索得到的无调用点清单（检索范围为仓库内全部 `.py`，排除 `.venv/` 与
`.claude/worktrees/`）：模块整体无调用点的有 `config.py`、`secrets.py`、
`logging_setup.py`；对外函数无调用点的有 `paths.bar_path`、`paths.data_spec_path`、
`paths.gaps_path`、`net.TokenBucket`、`net.TransientError`、`net.RateLimitError`、
`net.PermanentError`、`store.read_dataset`、`store.parquet_stats`。这些均为已登记
的预留接口，不属于「无用件」，但在被首次调用前，其行为未经使用验证。

`ARCHITECTURE.md` §2.1 把 `net.py` 的职责记为「HTTP 会话、指数退避、令牌桶限频」，
而本目录实际实现只有退避与令牌桶，没有 HTTP 会话对象。该表述与代码不一致，
用途待确认：应确认是登记表待订正，还是 HTTP 会话为待实现项。

## 3. 子目录索引

无。`__pycache__/` 是 CPython 字节码缓存，不是项目目录，不登记（见 §5）。

## 4. 依赖关系

读：

- `paths.py` 由自身文件位置推导 `ROOT`，不读任何数据文件。
- `config.py` 读 `<venue>/config/<venue>.<env>.yaml` 与环境变量 `QUANT_ENV`。
- `secrets.py` 读环境变量 `QUANT_SECRET_*` 与 `secrets/<name>.txt`。
- `store.py` 读调用方给定路径下的 parquet。

写：

- `logging_setup.py` 写 `logs/<模块名首段>_YYYYMMDD.log`，目录不存在时创建。
- `store.py` 写调用方给定路径的 parquet 与同名 `.writing` 临时件。

被谁 import：见 §2 各行「谁在用」。

目录内部依赖：`config.py`、`logging_setup.py`、`secrets.py` 三者各自 import
`paths.py`；`paths.py`、`net.py`、`store.py` 不 import 本目录其他模块。

第三方依赖：`pyyaml`（`config.py`）、`pyarrow`（`store.py`）、`duckdb`
（`store.read_dataset` 内延迟 import，其 `.df()` 返回值依赖 pandas）。

反向约束：本目录不得 import `crypto_trading/`、`trading212/`、`backtest/` 的任何
模块，也不得含场所特有常量。

## 5. 产出与清理

- `__pycache__/`：CPython 字节码缓存，运行产物。已在 `.gitignore` 内，但按
  `CLAUDE.md` §4.2 第 3、4 条不应留在项目目录内，可随时删除，删除不影响功能。
  本轮未删除，依据 §4.2 第 7 条：删除既有文件先问用户。
- 本目录的 7 个源文件全部必须保留，无一次性产物、无临时件。
- 本目录代码写出的 `logs/` 与 `data/` 下产物不落在本目录，清理规则见各自目录说明。

## 6. 变更记录

2026-08-22 建立本文件，登记现有文件。
