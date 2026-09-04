---
name: quant-code-standards
description: 本项目 Python 代码规范：语言（代码一律美式英文）、命名、格式、注释、模块化与可定位性、配置外置、数值与精度、性能与资源。核心目标是「改一个功能能在一分钟内定位到那个文件的那个函数」。凡编写或审查本项目代码、要改某个功能、拆文件、重构、新建模块或做规范检查时调用。触发词：代码规范、命名规范、格式检查、写模块、新建文件、代码审查、模块化、拆文件、重构、定位代码、改功能、这个功能在哪。
---

# 量化项目 Python 代码规范

本 skill 由团队 QMT 规范 V2.8 的通用部分改写而来，已剥离全部 QMT/ContextInfo/
GBK/Python3.6 相关条款。本项目环境：Python 3.11+、UTF-8、macOS。

---

## 零、语言（先于一切其他规范）

对应 `AGENTS.md` §2.3 语言纪律。本节是该纪律在代码侧的落地口径。

### 0.1 分工

| 载体 | 语言 | 范围 |
|---|---|---|
| 代码文本 | 美式英文 | `.py` `.yaml` `.yml` `.json` `.toml` `.sh` `.sql` 及一切源文件中的：标识符、注释、docstring、日志消息、异常消息、CLI 帮助与参数说明、测试名、数据列名、状态枚举值、配置键名 |
| md 文档 | 中文 | `AGENTS.md`、`ARCHITECTURE.md`、`WORKING_MEMORY.md`、`.agents/skills/*/SKILL.md`、`DATA_SPEC.md`、`research/`、`reports/` |
| 交付物 | 用户指定 | 用户明确要求用中文的输出按中文；未指定的技术交付物按 md 文档口径 |

### 0.2 硬性条款

1. 代码文件内不出现中文字符，注释与 docstring 同样适用。唯一例外是必须原样保留的
   外部数据（例如交易所返回的中文字段值），此类值只能作为字符串常量出现并注明来源。
   代码内允许出现的非 ASCII 字符只有章节引用符号 `§`（用于引用 `AGENTS.md` 的条款号）。
2. 拼写取美式，不取英式。常用对照见 §0.3。同一概念在项目内只用一种拼写。
3. 第三方 API 的字段名、参数名、方法名照抄外部拼写，不得按本节改动。那是外部契约，
   改了就错。例：交易所返回的 `instId`、`lotSz` 原样使用。
4. 禁止拼音标识符（`jiage`、`chicang`）与中英混写标识符（`btc_价格`）。
5. 注释与 docstring 写成完整英文句子，首字母大写，句末加句号。不写句子片段堆砌。
6. 日志与异常消息用英文，含结构化字段；消息文本不随环境改变。
7. md 文档中嵌入的代码块同样受本节约束：示例代码的注释与 docstring 一律英文。

### 0.3 美式拼写对照（项目高频词）

| 美式（采用） | 英式（禁止） | 出现场景 |
|---|---|---|
| `canceled` / `canceling` / `CANCELED` | `cancelled` / `cancelling` | 订单状态机、撤单路径 |
| `normalize` / `standardize` / `serialize` / `initialize` | `normalise` 等 `-ise` 形 | 数据处理、序列化 |
| `analyze` | `analyse` | 分析函数 |
| `modeling` / `labeled` / `signaling` / `totaled` | `modelling` / `labelled` 等双写 l | 成本建模、标注 |
| `color` / `behavior` / `favor` | `colour` / `behaviour` / `favour` | 绘图、行为描述 |
| `center` / `meter` | `centre` / `metre` | 统计、绘图 |
| `catalog` | `catalogue` | 标的清单 |
| `license` | `licence` | 依赖与授权说明 |
| `gray` | `grey` | 绘图配色 |
| `judgment` | `judgement` | 注释中的判定表述 |
| `fulfill` | `fulfil` | 委托成交表述 |
| `defense` | `defence` | 防御性检查表述 |
| `while` | `whilst` | 一般连词 |
| `deserialize` | `deserialise` | 反序列化 |

审查时用词表扫描，不用 `-ise\b` 这类宽泛模式（`raise`、`otherwise`、`premise`
会全部误命中，检查失去判别力）：

```bash
grep -rnE '(normalis|standardis|serialis|initialis|optimis|organis|recognis|analys[ei]|authoris|summaris|utilis|synchronis|prioritis|categoris|minimis|maximis|customis|visualis|behaviour|colour|favour|labour|centre\b|metre\b|cancell|modelling|labelled|signalling|grey\b|licence|catalogue|judgement|defence|offence|practise|fulfil\b|instalment)' \
  --include='*.py' --include='*.yaml' --include='*.sh' . | grep -v '\.venv'
```

命中项逐条判定：属英式拼写即改；属第三方库 API 名（不可改）则在该行注明来源。

---

## 一、命名

### 1.1 变量与函数

| 类型 | 规则 | 合规 | 禁止 |
|---|---|---|---|
| 普通变量 | 小写 + 下划线 | `bid_price`, `fill_qty` | `Data1`, `priceBTC`, 中文名, 拼音名 |
| 私有 | 前缀单下划线 | `_retry_after` | 无 |
| 函数 | 小写 + 下划线，动词开头 | `fetch_candles()`, `calc_zscore()` | `getData()`, `处理()` |
| 类 | 大驼峰 | `OrderRouter`, `KlineStore` | `order_router` |
| 异常类 | `Error` 后缀 | `RateLimitError` | `RateLimitException` |

函数分组前缀（见名知副作用）：

| 前缀 | 含义 |
|---|---|
| `get_*` / `fetch_*` | 只读；`get_` 本地取，`fetch_` 走网络 |
| `calc_*` | 纯函数，无副作用，无 IO |
| `load_*` / `save_*` | 本地读写 |
| `check_*` / `is_*` | 返回 bool |
| `place_*` / `cancel_*` | 产生真实委托，一律受 `AGENTS.md` §3.1 约束 |

### 1.2 常量

模块级常量全大写加下划线，统一放模块顶部的常量区。
禁止 Magic Number：任何裸数字必须提为命名常量，并在同行注明依据出处。

```python
# Source: GET /api/v5/public/instruments?instId=BTC-USDT, field minSz, fetched 2026-08-19.
# Stored at: data/reference/okx_instruments_20260819.json
BTC_MIN_ORDER_SIZE = 0.00001
```

常量前缀表：

| 前缀 | 含义 | 示例 |
|---|---|---|
| `OKX_` / `T212_` | 场所特有 | `OKX_REST_BASE` |
| `MAX_` / `MIN_` | 上下限 | `MAX_DAILY_ORDERS` |
| `FEE_` | 费率 | `FEE_TAKER_BPS` |
| `TIMEOUT_` / `RETRY_` | 网络行为 | `TIMEOUT_REST_SEC` |
| `PATH_` / `DIR_` | 路径 | `DIR_RAW_OKX` |

### 1.3 场所命名统一

代码中场所标识固定两个 slug：`okx`、`t212`。禁止混用 `trading212`、`T212`、`tr212`
作为代码标识符。目录名 `trading212/` 是既定例外，不再扩大。

---

## 二、格式

| 规则 | 标准 |
|---|---|
| 缩进 | 4 空格，禁止 Tab |
| 行宽 | 不超过 100 字符 |
| 编码 | UTF-8，无 BOM，LF 换行 |
| import 顺序 | 标准库、第三方、本项目，三组之间各空一行 |
| 空行 | 顶层定义之间空两行，类内方法之间空一行 |
| 章节分隔 | `# ===...===`（占满 76 列）标章节，`# ---...---` 标子节 |
| 类型注解 | 公开函数的参数与返回值必须标注 |

```python
# ============================================================================
# [2] Data acquisition
# ============================================================================

import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from common.config import load_config
from common.retry import with_backoff
```

---

## 三、注释与文档字符串

### 3.1 模块头：文件的说明文档（强制）

对应 `AGENTS.md` §4.4。模块头 docstring 是该文件的说明文档，作用等同于目录 `README.md`。
一切 `.py` 文件（含 `__init__.py`）必须有，一律美式英文。

固定六节，按序排列，无内容的节写 `None.` 而不是省略：

| # | 节 | 内容 |
|:--:|---|---|
| 1 | Responsibility | 一句话说清本文件做什么 |
| 2 | Out of scope | 不做什么，并指向承担该职责的文件 |
| 3 | Public functions / Public classes | 对外符号逐项一行，名称加一句作用，顺序与 `__all__` 一致 |
| 4 | Parameters / Constants | 关键参数与常量：名称、单位、取值依据出处 |
| 5 | Inputs / Outputs | 读哪些路径或接口、写哪些路径 |
| 6 | Change log | 日期、改了什么、为什么。只增不改 |

```python
"""OKX spot kline downloader.

Responsibility: fetch historical klines from /api/v5/market/history-candles and
persist them as parquet under the raw layer.

Out of scope: cleaning, resampling and gap backfill, which belong to
common/store.py; path construction, which belongs to common/paths.py.

Public functions:
    fetch_candles(inst, period, start, end)   Fetch one range, return a DataFrame.
    backfill(inst, period, since)             Catch up incrementally, persist.
    list_missing(inst, period)                Return the list of gap ranges.

Constants:
    PAGE_LIMIT      int   Rows per request, 100. Source: official doc, Get
                          Candlesticks History, fetched 2026-08-19.
    RATE_PER_SEC    float Requests per second, 10. Source: official doc, Rate
                          Limit section, fetched 2026-08-19. TokenBucket applies
                          a further 0.7 safety margin.

Inputs:
    GET /api/v5/market/history-candles
Outputs:
    data/okx/raw/<inst>/<period>/year=YYYY/YYYYMMDD.parquet
    docs/data/okx/MANIFEST.jsonl  one line per partition

Change log:
    2026-08-19  Created. Initial fetch and backfill paths.
    2026-08-21  Bar timestamp switched from close time to open time; the venue
                field table is authoritative. Existing curated output must be
                regenerated.
"""

__all__ = ["fetch_candles", "backfill", "list_missing"]
```

使用时机，三个硬性节点：

1. 改代码前第一件事读头部注释，先于读函数体。
2. 改完后核对头部与实现是否一致。不一致即为缺陷，当场修正；不允许留着「文档说 A、
   代码做 B」的状态。
3. 借这次核对做精简判断：不再被调用的函数、无人使用的参数、重复实现的逻辑，
   一律删除而非保留。删除结果写进 Change log。

判断「是否还有人用」用调用点检索，不靠印象：

```bash
grep -rn "函数名" --include='*.py' . | grep -v '\.venv'
```

调用点为 0 且不在 `__all__` 中的函数，直接删。在 `__all__` 中但无调用点的，
先确认是否属对外契约，再决定删除或保留并在头部注明保留理由。

### 3.2 函数 docstring（强制）

```python
def calc_position_size(equity: float, price: float, risk_pct: float) -> float:
    """Compute order size from a fixed fraction of equity at risk.

    Args:
        equity: Account equity in quote currency (USDT or GBP).
        price: Reference price, quote currency per base currency.
        risk_pct: Fraction of equity risked on a single trade, in (0, 1].

    Returns:
        Order size in base currency. Minimum size and lot step are NOT applied
        here; the caller must pass the result through align_order_size().

    Raises:
        ValueError: If price <= 0 or risk_pct is outside (0, 1].
    """
```

凡涉及金额、数量、价格的函数，docstring 必须声明单位与是否已做精度对齐。
这是本项目最高频的错误来源。

### 3.3 禁止

1. 代码注释与技术文档中不用 emoji 及装饰性符号（`AGENTS.md` §2.1.2）。
2. 不写复述代码的注释（例如 `# add one`）。注释解释为什么这样写，不解释写了什么。
3. 不留无跟进的 `# TODO`。需要跟进的事项写进 `WORKING_MEMORY.md` 的未决项。
4. 注释中不出现中文（§0.2）。

---

## 四、模块化与可定位性（本项目重点）

本节的唯一目标：拿到一句「把 X 功能改成 Y」，能在一分钟内定位到哪个文件的哪个函数，
改动只落在那一个函数里，不需要全局搜索、不需要读完整个文件、不需要同时改三处。
下列每一条都服务于该目标。

### 4.1 分层职责（对应 `ARCHITECTURE.md` §2）

| 层 | 位置 | 允许做 | 禁止做 |
|---|---|---|---|
| 基础 | `common/` | 路径、配置、密钥、日志、网络与限频、存储、指标 | 任何场所特有的口径 |
| 客户端 | `<venue>/client.py` | REST/WS 连接、鉴权、签名、限频 | 业务决策 |
| 接入 | `<venue>/ingest/` | 调 API、落 raw、清洗到 curated | 交易决策 |
| 策略 | `<venue>/strategy/` | 信号计算，纯函数 | 下单、读网络、写状态 |
| 执行 | `<venue>/execution/` | 下单、撤单、状态机、对账 | 信号计算 |
| 回测 | `backtest/` | 撮合模拟、成本建模、绩效 | 调用任何交易所接口 |

`<venue>` 指 `crypto_trading/` 与 `trading212/` 两个目录。依赖方向单行，禁止反向导入。

分层的作用是缩小搜索范围：一句需求先落到一层，搜索范围即从整个项目缩到一个目录。
因此层的边界必须干净。执行层一旦混入信号计算，后续查找信号需要在两个位置各查一遍。

### 4.2 一个文件一件事，一个函数一个动作

| 对象 | 硬上限 | 超限处置 |
|---|---|---|
| 文件 | 400 行 | 按职责拆成多个文件，不按行数机械切分 |
| 函数 | 50 行 | 抽出子函数；超限通常意味着函数内含 2 至 3 个独立动作 |
| 函数参数 | 6 个 | 用 dataclass 打包，或确认该函数承担了过多职责 |
| 嵌套层数 | 3 层 | 提前 return、抽子函数 |
| 类方法数 | 15 个 | 拆类 |

自查命令：

```bash
find . -name '*.py' -not -path './.venv/*' | xargs wc -l | sort -rn | head -20
```

判据不是行数本身，而是这项检验：该文件的职责能否用一句不含「和」「以及」的话说清楚。
说不清即应拆分。函数同理，函数名中出现 `_and_` 即为职责过重的信号。

### 4.3 模块头必须有功能索引（强制）

每个模块的 docstring 末尾列出对外函数，一行一个。这样 `head -40 <file>` 即可回答
该文件能做什么，不必通读。

```python
"""OKX kline downloader.

Responsibility: call history-candles, persist klines as parquet.
Out of scope: cleaning, resampling, gap backfill (see common/store.py).

Public functions:
    fetch_candles(inst, period, start, end)   Fetch a range, return DataFrame.
    backfill(inst, period, since)             Incrementally catch up, persist.
    list_missing(inst, period)                Return the list of gap ranges.
"""

__all__ = ["fetch_candles", "backfill", "list_missing"]
```

`__all__` 必须写，它是该文件的公开契约。不在 `__all__` 中的函数一律以 `_` 开头。
改私有函数无需排查外部调用方，改公开函数必须先查调用点；该区分让改动的影响面直接可见。

### 4.4 文件内部固定结构

每个模块按同一顺序排列，使跳转位置可预期：

```
模块 docstring（含对外函数索引）
from __future__ import annotations   若有，必须紧跟 docstring，属语言要求
__all__
imports：标准库 / 第三方 / 本项目
# [1] Constants
# [2] Data structures (dataclass / TypedDict)
# [3] Public functions (顺序与 __all__ 一致)
# [4] Internal helpers (以 _ 开头，被谁调用就排在谁之后)
```

禁止常量散落在文件中段；禁止对外函数与私有函数交错排列。

### 4.5 命名要可预测

给定一个功能名，应当能推出文件路径，而不是依赖搜索。

| 需求 | 应当推出的位置 |
|---|---|
| 改 OKX 的下单重试逻辑 | `crypto_trading/execution/order_router.py` |
| 改 K 线缺口的判定 | `crypto_trading/ingest/klines.py` 的 `list_missing()` |
| 改回测的滑点假设 | `backtest/okx/costs.py` |
| 改日志格式 | `common/logging_setup.py` |

1. 模块文件用 `snake_case.py`，名词或名词短语，见名知职责。
2. 禁止 `utils.py`、`common.py`、`helpers.py`、`misc.py`、`temp.py`、`final.py`、
   `new_*.py`、`test1.py`。`utils` 表示职责尚未归位，该文件会持续吸纳无关函数，
   使后续需求无法定位。
3. 一次性脚本放 `scripts/`，带日期前缀：`scripts/20260819_backfill_okx_1h.py`。

### 4.6 变体用分派表，不用 if 链

新增一个策略变体、一个数据源或一种成本模型时，改动应当是增加一个条目，
而不是修改一条 if 链。if 链会使同一改动散落到多个文件的多处。

```python
# Anti-pattern: every new variant requires editing this branch chain,
# and the edits spread across multiple functions.
def calc_signal(name, df):
    if name == "ma_cross":
        ...
    elif name == "zscore":
        ...

# Preferred: a new variant is one new file plus one registry line;
# no other code changes.
SIGNALS: dict[str, Callable[[pd.DataFrame, dict], pd.Series]] = {
    "ma_cross": ma_cross.compute,
    "zscore": zscore.compute,
}

def calc_signal(name: str, df: pd.DataFrame, params: dict) -> pd.Series:
    if name not in SIGNALS:
        raise ValueError(f"unregistered signal {name!r}; available: {sorted(SIGNALS)}")
    return SIGNALS[name](df, params)
```

同一做法适用于：场所（okx/t212）、周期、成本模型、执行算法。

### 4.7 一处定义，禁止复制粘贴

同一个逻辑出现第二遍时即抽出为一处定义。复制粘贴是「改一个功能要改三个地方」的
直接成因；改了两处漏了一处，会使回测与实盘的行为分叉。

1. 两个场所的同名逻辑：可共用的下沉到 `common/`；确实不同的，在两边各自文件里写清
   与另一侧的差异是什么、为什么不同。
2. 回测与实盘的信号只有一份，放在 `<venue>/strategy/`（`ARCHITECTURE.md` §2.0）。

### 4.8 配置外置

1. 策略参数、标的池、时间窗、阈值一律不写死在代码里，走 `<venue>/config/*.yaml`。
2. 代码里只允许物理常量（API 路径、字段名、精度、限频），且须注明依据出处。
3. 读配置只经 `common/config.py`；读密钥只经 `common/secrets.py`。
4. 配置读取只在入口层做一次，往下传值。禁止在业务函数中段调 `load_config()`，
   否则「这个参数从哪里来」需要全局搜索才能回答。

### 4.9 改功能的标准定位流程

拿到「把 X 改成 Y」时按序执行，并把结果写进最终输出：

1. 定层：X 属于基础、客户端、接入、策略、执行、回测中的哪一层。搜索范围缩到一个目录。
2. 定文件：查 `ARCHITECTURE.md` §2.1 的模块表，加上该目录下各文件 docstring 的功能索引。
   缩到一个文件。
3. 定函数：查该文件的 `__all__` 与函数索引。缩到一个函数。
4. 查调用点：`grep -rn "函数名" .`。调用点超过 5 处或跨层，说明该函数职责过重，
   先说明再改。
5. 改：改动应当落在一个函数体内。若必须改多处，在输出中写明为什么无法收敛到一处；
   该情形通常源于 §4.7 被违反。

任何一步走不通，均属代码结构的缺陷，不是搜索能力的问题，须在输出中记为待改进项。

### 4.10 可定位性反模式（见到即记为缺陷）

| 反模式 | 后果 |
|---|---|
| 1000 行的 `main.py` 或 `strategy.py` | 定位需要通读，改动的影响面不可控 |
| `utils.py` / `helpers.py` | 无关函数持续堆积，后续需求全部指向该文件 |
| 一个函数做「取数 + 计算 + 落盘 + 下单」 | 改任一环都要动它，且无法单测 |
| 同一逻辑在两个场所各写一份 | 改一处漏一处，回测与实盘分叉 |
| 业务函数中段读配置或读环境变量 | 参数来源不可见，改配置无从下手 |
| 靠字符串比较散落各处做分支 | 新增变体需要修改 N 个文件 |
| 模块无 docstring 索引、无 `__all__` | 只能靠通读或全局搜索定位 |

---

## 五、数值与精度（本项目专项）

1. 金额禁用二进制浮点做累加。持仓、成本、盈亏用 `decimal.Decimal` 或整数最小单位；
   `float` 只用于统计与绘图。
2. 下单量与价格必须过对齐函数，且对齐方向明确：数量向下取整到 `lotSz`；
   买价向下、卖价向上取整到 `tickSz`。对齐后须重新校验不小于 `minSz`，
   不满足则放弃该笔，不得下一个非法单。
3. 时间统一 UTC，只在展示层转本地时区。变量名带单位后缀：`ts_ms`、`ts_s`、`dt_utc`。
   禁止裸 `datetime.now()`，用 `datetime.now(timezone.utc)`。
4. 除法前必检分母；均值与标准差计算前必检样本量下限。
5. 比较浮点用容差，不用 `==`。

---

## 六、性能与资源

1. 向量化优先：pandas/numpy 能一次算完的，不写 Python 逐行循环。
2. 定长容器防内存泄漏：滚动窗口用 `collections.deque(maxlen=N)`，
   禁止用无限增长的 list；订单字典须定期清理终态订单。
3. 网络会话复用：`httpx.Client` 建一次复用，禁止在循环内新建连接。
4. 必须释放的资源：WebSocket 连接、HTTP client、文件句柄、日志 handler。
   一律用 `with` 或在 `finally` 中释放；长驻进程退出前撤销所有未成交委托。
5. 大数据落地用 parquet 加分区，不用单个巨型 CSV。

---

## 七、审查触发规则（按严重度）

| 级别 | 触发条件 |
|---|---|
| CRITICAL | 明文密钥出现在代码、配置、日志或文档中 |
| CRITICAL | `place_*` / `cancel_*` 路径缺 `DRY_RUN` 或 `live` 断言 |
| CRITICAL | 下单量或价格未过对齐函数 |
| HIGH | Magic Number 硬编码，或常量无依据出处注释 |
| HIGH | 金额用 float 累加 |
| HIGH | 裸 `datetime.now()`，或时区未声明 |
| HIGH | 反向依赖（下层 import 上层） |
| HIGH | 无限增长的容器；循环内新建连接 |
| HIGH | 同一逻辑被复制到两处（§4.7），尤其回测与实盘各写一份信号 |
| HIGH | 业务函数中段读配置或读环境变量（§4.8） |
| HIGH | 文件超过 400 行，或函数超过 50 行（§4.2） |
| HIGH | 代码文件内出现中文，或标识符用拼音（§0.2） |
| MEDIUM | 模块缺 docstring 功能索引或缺 `__all__`（§4.3） |
| MEDIUM | 新增变体需改 if 链而非加注册条目（§4.6） |
| MEDIUM | 公开函数缺 docstring 或缺单位声明 |
| MEDIUM | 出现 `utils.py` / `helpers.py` 一类无职责命名 |
| MEDIUM | 文件内部结构不按 §4.4 的固定顺序 |
| MEDIUM | 代码中出现英式拼写（§0.3） |
| HIGH | 模块头缺失，或缺 §3.1 六节中的任意一节 |
| HIGH | 模块头与实现不一致（对外函数、参数、输出路径对不上） |
| HIGH | 所在目录缺 `README.md`（`AGENTS.md` §4.3） |
| MEDIUM | 存在无调用点且不在 `__all__` 中的函数，未删除也未说明 |
| MEDIUM | 改动后未更新模块头 Change log |
| LOW | 行宽超过 100；import 顺序混乱 |
