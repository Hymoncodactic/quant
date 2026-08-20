---
name: quant-code-standards
description: 本项目 Python 代码规范：命名、格式、注释、模块化与可定位性、配置外置、数值与精度、性能与资源。核心目标是「改一个功能能在一分钟内定位到那个文件的那个函数」。凡编写或审查本项目代码、要改某个功能、拆文件、重构、新建模块或做规范检查时调用。触发词：代码规范、命名规范、格式检查、写模块、新建文件、代码审查、模块化、拆文件、重构、定位代码、改功能、这个功能在哪。
---

# 量化项目 Python 代码规范

> 本 skill 由团队 QMT 规范 V2.8 的通用部分改写而来，已剥离全部 QMT/ContextInfo/
> GBK/Python3.6 相关条款。本项目环境：Python 3.11+、UTF-8、macOS。

---

## 一、命名

### 1.1 变量与函数

| 类型 | 规则 | 合规 | 禁止 |
|---|---|---|---|
| 普通变量 | 小写 + 下划线 | `bid_price`, `fill_qty` | `Data1`, `priceBTC`, 中文名 |
| 私有 | 前缀单下划线 | `_retry_after` | — |
| 函数 | 小写 + 下划线，动词开头 | `fetch_candles()`, `calc_zscore()` | `getData()`, `处理()` |
| 类 | 大驼峰 | `OrderRouter`, `KlineStore` | `order_router` |
| 异常类 | `Error` 后缀 | `RateLimitError` | `RateLimitException` |

**函数分组前缀**（见名知副作用）：

| 前缀 | 含义 |
|---|---|
| `get_*` / `fetch_*` | 只读；`get_` 本地取，`fetch_` 走网络 |
| `calc_*` | 纯函数，无副作用，无 IO |
| `load_*` / `save_*` | 本地读写 |
| `check_*` / `is_*` | 返回 bool |
| `place_*` / `cancel_*` | **产生真实委托**，一律受 `CLAUDE.md` §3.1 约束 |

### 1.2 常量

- 模块级常量全大写 + 下划线，**统一放模块顶部的常量区**。
- ⛔ **禁止 Magic Number**。任何裸数字必须提为命名常量，并在同行注明依据出处。

```python
# 依据：GET /api/v5/public/instruments?instId=BTC-USDT 返回 minSz，取回 2026-08-19
# 落地：data/reference/okx_instruments_20260819.json
BTC_MIN_ORDER_SIZE = 0.00001
```

**常量前缀表**：

| 前缀 | 含义 | 示例 |
|---|---|---|
| `OKX_` / `T212_` | 场所特有 | `OKX_REST_BASE` |
| `MAX_` / `MIN_` | 上下限 | `MAX_DAILY_ORDERS` |
| `FEE_` | 费率 | `FEE_TAKER_BPS` |
| `TIMEOUT_` / `RETRY_` | 网络行为 | `TIMEOUT_REST_SEC` |
| `PATH_` / `DIR_` | 路径 | `DIR_RAW_OKX` |

### 1.3 场所命名统一

代码中场所标识固定两个 slug：`okx`、`t212`。⛔ 不得混用 `trading212`/`T212`/`tr212`
作为代码标识符（目录名 `trading212/` 是例外，已固定）。

---

## 二、格式

| 规则 | 标准 |
|---|---|
| 缩进 | 4 空格，严禁 Tab |
| 行宽 | ≤ 100 字符 |
| 编码 | UTF-8，无 BOM，LF 换行 |
| import 顺序 | 标准库 → 第三方 → 本项目，组间空一行 |
| 空行 | 顶层定义之间空两行，类内方法之间空一行 |
| 章节分隔 | `# ===...===`（占满 76 列）标章节，`# ---...---` 标子节 |
| 类型注解 | 公开函数的参数与返回值必须标注 |

```python
# ============================================================================
# [2] 数据获取
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

### 3.0 注释语言：英文 + 美式拼写（硬性）

**代码中的一切注释、docstring、`__all__` 说明、日志字符串、异常消息一律用英文书写，
且采用美式拼写。** 项目文档（md 件）仍用中文。

美式拼写要点，写之前对照：

| 美式（用） | 英式（禁） |
|---|---|
| normalize / normalization | normalise / normalisation |
| initialize | initialise |
| serialize / deserialize | serialise / deserialise |
| analyze | analyse |
| behavior | behaviour |
| color | colour |
| center | centre |
| canceled / canceling | cancelled / cancelling |
| modeled / modeling | modelled / modelling |
| license（名词与动词同形） | licence / license |
| catalog | catalogue |
| defense | defence |
| fulfill | fulfil |
| gray | grey |
| while | whilst |

例外（不改，照抄外部拼写）：第三方 API 的字段名、参数名、方法名。
那是外部契约，改了就错。

```python
def align_order_size(qty: Decimal, lot_sz: Decimal, min_sz: Decimal) -> Decimal:
    """Round an order quantity down to the venue's lot step.

    Rounding is always downwards so the resulting order can never exceed the
    intended notional. The caller must re-check the result against min_sz:
    a quantity that rounds below the minimum is dropped, not submitted.

    Args:
        qty: Desired quantity, base-currency units.
        lot_sz: Quantity step from the venue's instrument spec.
        min_sz: Minimum order quantity from the venue's instrument spec.

    Returns:
        Aligned quantity, base-currency units. Zero if it falls below min_sz.
    """
```


### 3.1 模块头（强制）

```python
"""OKX 现货 K 线下载器。

职责：调 /api/v5/market/history-candles 拉取历史 K 线，落地为 parquet。
不负责：清洗、重采样、缺口补齐（见 common/store/）。

数据口径：
    ts       int64  bar 开始时刻，UTC 毫秒（依据：OKX API 文档 Get Candlesticks 字段表）
    o/h/l/c  float64 价格，计价币单位
    vol      float64 成交量，基础币单位
    volCcy   float64 成交额，计价币单位

限频：20 次/2 秒/IP（依据：官方文档 Rate Limit 节，取回 2026-08-19）
"""
```

### 3.2 函数 docstring（强制）

```python
def calc_position_size(equity: float, price: float, risk_pct: float) -> float:
    """按固定风险比例计算下单数量。

    Args:
        equity: 账户权益，计价币单位（USDT / GBP）。
        price: 参考价格，计价币/基础币。
        risk_pct: 单笔风险占权益比例，0~1。

    Returns:
        下单数量，基础币单位。**未做最小下单量与步进对齐**，
        调用方须再过 `align_order_size()`。

    Raises:
        ValueError: price <= 0 或 risk_pct 不在 (0, 1]。
    """
```

**凡涉及金额、数量、价格的函数，docstring 必须声明单位与是否已做精度对齐。**
这是本项目最高频的错误来源。

### 3.3 禁止

- ⛔ 技术文档与代码注释中不用 emoji（`CLAUDE.md` §2.1.2）。
- ⛔ 不写「这里加 1」这类复述代码的注释；注释解释**为什么**，不解释是什么。
- ⛔ 不留 `# TODO` 而无跟进；要留就写进 `WORKING_MEMORY.md` 的未决项。

---

## 四、模块化与可定位性（本项目重点）

**本节的唯一目标**：拿到一句「把 X 功能改成 Y」，能在一分钟内定位到
**哪个文件的哪个函数**，改动只落在那一个函数里，不需要全局搜索、
不需要读完整个文件、不需要同时改三处。

下面每一条都是为这个目标服务的，不是抽象的「好设计」。

### 4.1 分层职责（对应 `ARCHITECTURE.md` §2）

| 层 | 位置 | 允许做 | 禁止做 |
|---|---|---|---|
| 基础 | `common/` | 路径、配置、密钥、日志、网络与限频、存储、指标 | 任何场所特有的口径 |
| 客户端 | `<venue>/client.py` | REST/WS 连接、鉴权、签名、限频 | 业务决策 |
| 接入 | `<venue>/ingest/` | 调 API、落 raw、清洗到 curated | 交易决策 |
| 策略 | `<venue>/strategy/` | 信号计算，**纯函数** | 下单、读网络、写状态 |
| 执行 | `<venue>/execution/` | 下单、撤单、状态机、对账 | 信号计算 |
| 回测 | `backtest/` | 撮合模拟、成本建模、绩效 | 调用任何交易所接口 |

`<venue>` 实际就是 `crypto_trading/` 与 `trading212/` 两个目录。
**依赖方向单行**，禁止反向导入。

分层的意义就是缩小搜索范围：一句需求先落到一层，搜索范围立刻从整个项目缩到一个目录。
所以**层的边界必须干净**——一旦执行层里混进了信号计算，下次找信号就得两个地方都翻。

### 4.2 一个文件一件事，一个函数一个动作

| 对象 | 硬上限 | 超了怎么办 |
|---|---|---|
| 文件 | 400 行 | 按职责拆成多个文件，不是按行数机械切 |
| 函数 | 50 行 | 抽出子函数；通常是里面藏了 2~3 个独立动作 |
| 函数参数 | 6 个 | 用 dataclass 打包，或承认该函数做了太多事 |
| 嵌套层数 | 3 层 | 提前 return、抽子函数 |
| 类方法数 | 15 个 | 拆类 |

自查命令：

```bash
find . -name '*.py' -not -path './.venv/*' | xargs wc -l | sort -rn | head -20
```

**判据不是行数本身，是这句话**：这个文件的职责能不能用**一句不含「和」「以及」的话**
说清楚？说不清就是该拆了。函数同理——函数名里出现 `_and_` 一律是信号。

### 4.3 模块头必须有功能索引（强制）

每个模块的 docstring 末尾列出对外函数，一行一个。这样 `head -40 <file>` 就能回答
「这个文件能干什么」，不必通读。

```python
"""OKX K 线下载器。

职责：调 history-candles 拉取历史 K 线，落地为 parquet。
不负责：清洗、重采样、缺口补齐（见 common/store.py）。

对外函数：
    fetch_candles(inst, period, start, end)   拉取指定区间，返回 DataFrame
    backfill(inst, period, since)             增量补齐到最新，落盘
    list_missing(inst, period)                返回缺口区间列表
"""

__all__ = ["fetch_candles", "backfill", "list_missing"]
```

- `__all__` **必须写**，它是这个文件的公开契约。
- 不在 `__all__` 里的一律 `_` 开头。改私有函数不必担心外部调用方，改公开函数必须先查
  调用点——这个区分让改动的影响面一眼可见。

### 4.4 文件内部固定结构

每个模块**按同一顺序**排，跳转时肌肉记忆才生效：

```
模块 docstring（含对外函数索引）
from __future__ import annotations   ← 若有，必须紧跟 docstring（语言要求）
__all__
imports：标准库 / 第三方 / 本项目
# [1] 常量
# [2] 数据结构（dataclass / TypedDict）
# [3] 对外函数（顺序与 __all__ 一致）
# [4] 内部实现（_ 开头，被谁调用就排在谁后面）
```

禁止常量散落在文件中段；禁止对外函数与私有函数交错排列。

### 4.5 命名要可预测

**给定一个功能名，应该能猜出文件路径，而不是靠搜索。**

| 需求 | 应该猜到的位置 |
|---|---|
| 「改 OKX 的下单重试逻辑」 | `crypto_trading/execution/order_router.py` |
| 「改 K 线缺口的判定」 | `crypto_trading/ingest/klines.py` 的 `list_missing()` |
| 「改回测的滑点假设」 | `backtest/okx/costs.py` |
| 「改日志格式」 | `common/logging_setup.py` |

- 模块文件：`snake_case.py`，名词或名词短语，见名知职责。
- 禁止 `utils.py`、`common.py`、`helpers.py`、`misc.py`、`temp.py`、`final.py`、
  `new_*.py`、`test1.py`。`utils` 是「我没想清楚这属于哪一层」的同义词；
  它一旦出现就会变成垃圾桶，之后所有需求都指向它。
- 一次性脚本放 `scripts/`，带日期前缀：`scripts/20260819_backfill_okx_1h.py`。

### 4.5.1 策略必须带版本号（硬性）

**策略的身份是「名字 + 版本」，不是名字。** 没有版本号就无法回答
「这份回测结果对应哪版逻辑」，而这个问题在半年后一定会被问到。

当前起点：**V0.0.1**。

| 位置 | 格式 | 示例 |
|---|---|---|
| 策略文件 | `<venue>/strategy/<name>_v<M>_<m>_<p>.py` | `crypto_trading/strategy/ma_cross_v0_0_1.py` |
| 模块常量 | `STRATEGY_NAME` + `STRATEGY_VERSION` | `"ma_cross"` + `"0.0.1"` |
| 配置 | `<venue>/config/strategies/<name>_v<M>_<m>_<p>.yaml` | 同名对应 |
| 回测结果 | `backtest/results/<name>_v<M>_<m>_<p>_<臂>_<窗口>.parquet` | |
| 预注册 / 裁定 | `research/prereg/<日期>_<name>_v<M>_<m>_<p>.md` | 与 `decisions/` 同名成对 |
| 报告 | `reports/<日期>_<name>_v<M>_<m>_<p>.html` | |

文件名里用下划线（`v0_0_1`），常量里用点（`"0.0.1"`）——文件名一律 ASCII 且不含点号
以外的分隔混用，见 §4.1。

**版本号语义（按对结果的影响定，不按代码改动量定）：**

| 位 | 何时递增 | 后果 |
|---|---|---|
| **MAJOR** `1.0.0` | 信号逻辑改变（入场/出场条件、特征集、模型形式） | 旧结果**作废**，全部重跑；旧版本文件保留作血缘 |
| **MINOR** `0.1.0` | 参数、阈值、标的池、周期变更，逻辑不变 | 结果可比但数值不同；须与旧版并列报告 |
| **PATCH** `0.0.2` | 重构、修 bug、加注释，**意图上不改变行为** | **必须证明输出与前一版逐字节一致**；不一致说明这不是 patch，应升 MINOR 或 MAJOR |

PATCH 那条是本表最有用的一条：它把「我只是重构一下」变成一个可证伪的断言。
改完跑一次基线回测，与前一版结果 `cmp` 比对，不一致就说明你改的不止是形式。

**旧版本不删。** 策略文件按版本并存，`__init__.py` 里用注册表指向当前版本
（§4.6 的分派表模式）。回测结果引用哪个版本要在文件名里写死，
不能靠「当时用的应该是那版」回忆。

### 4.6 变体用分派表，不用 if 链

新增一个策略变体 / 一个数据源 / 一种成本模型时，应当是**加一个条目**，
不是**改一条 if 链**。if 链会让同一个改动散落到多个文件的多处。

```python
# 反例：每加一个变体都要回来改这里，且散落在多个函数中
def calc_signal(name, df):
    if name == "ma_cross":
        ...
    elif name == "zscore":
        ...

# 正例：加变体 = 加一个文件 + 注册一行，其他代码一律不动
SIGNALS: dict[str, Callable[[pd.DataFrame, dict], pd.Series]] = {
    "ma_cross": ma_cross.compute,
    "zscore": zscore.compute,
}

def calc_signal(name: str, df: pd.DataFrame, params: dict) -> pd.Series:
    if name not in SIGNALS:
        raise ValueError(f"未注册的信号 {name!r}，可选 {sorted(SIGNALS)}")
    return SIGNALS[name](df, params)
```

同理适用于：场所（okx/t212）、周期、成本模型、执行算法。

### 4.7 一处定义，禁止复制粘贴

**同一个逻辑出现第二遍时就抽出来。** 复制粘贴是「改一个功能要改三个地方」的唯一成因，
也是本项目最危险的缺陷模式——改了两处漏了一处，回测与实盘就分叉了。

- 两个场所的同名逻辑：能共用的下沉到 `common/`；确实不同的，在两边各自文件里写清
  「与另一侧的差异是什么、为什么」。
- 回测与实盘的信号：**只有一份**，在 `<venue>/strategy/`（`ARCHITECTURE.md` §2.0）。

### 4.8 配置外置

- 策略参数、标的池、时间窗、阈值一律不写死在代码里，走 `<venue>/config/*.yaml`。
- 代码里只允许**物理常量**（API 路径、字段名、精度、限频），且须注明依据出处。
- 读配置只经 `common/config.py`；读密钥只经 `common/secrets.py`。
- 配置读取**只在入口层做一次**，往下传值。禁止在业务函数中段调 `load_config()`——
  那会把「这个参数从哪来的」变成全局搜索题。

### 4.9 改功能的标准定位流程

拿到「把 X 改成 Y」时按序走，并把结果写进最终输出：

1. **定层**：X 属于 基础 / 客户端 / 接入 / 策略 / 执行 / 回测 哪一层？
   搜索范围缩到一个目录。
2. **定文件**：查 `ARCHITECTURE.md` §2.1 的模块表，加上该目录下各文件 docstring 的
   功能索引。缩到一个文件。
3. **定函数**：查该文件的 `__all__` 与函数索引。缩到一个函数。
4. **查调用点**：`grep -rn "函数名" .`。调用点超过 5 处或跨层，说明该函数职责过重，
   先说明再改。
5. **改**：改动应当落在**一个函数体内**。若必须改多处，在输出中明确写
   「为什么无法收敛到一处」——通常是 §4.7 被违反了。

**任何一步走不通，都是代码结构的缺陷，不是搜索能力的问题**，须在输出中记为待改进项。

### 4.10 可定位性反模式（见到即记为缺陷）

| 反模式 | 为什么致命 |
|---|---|
| 1000 行的 `main.py` / `strategy.py` | 定位靠通读，改动靠祈祷 |
| `utils.py` / `helpers.py` | 什么都往里塞，最后所有需求都指向它 |
| 一个函数做「取数 + 计算 + 落盘 + 下单」 | 改任一环都要动它，且无法单测 |
| 同一逻辑在两个场所各写一份 | 改一处漏一处，回测实盘分叉 |
| 业务函数中段读配置或读环境变量 | 参数来源不可见，改配置无从下手 |
| 靠字符串比较散落各处做分支 | 新增变体要改 N 个文件 |
| 模块无 docstring 索引、无 `__all__` | 只能靠通读或全局搜索 |

---

## 五、数值与精度（本项目专项）

1. **金额禁用二进制浮点做累加**。持仓、成本、盈亏用 `decimal.Decimal` 或整数最小单位；
   `float` 只用于统计与绘图。
2. **下单量与价格必须过对齐函数**，且对齐方向明确：
   数量向下取整到 `lotSz`、买价向下、卖价向上取整到 `tickSz`。对齐后须重新校验
   `>= minSz`，否则放弃该笔而不是下一个非法单。
3. **时间统一 UTC**，只在展示层转本地时区。变量名带单位后缀：
   `ts_ms`、`ts_s`、`dt_utc`。⛔ 禁止裸 `datetime.now()`，用 `datetime.now(timezone.utc)`。
4. **除法前必检分母**；均值/标准差前必检样本量下限。
5. 比较浮点用容差，不用 `==`。

---

## 六、性能与资源

1. **向量化优先**：pandas/numpy 能一次算完的，不写 Python 逐行循环。
2. **定长容器防内存泄漏**：滚动窗口用 `collections.deque(maxlen=N)`，
   ⛔ 不用无限增长的 list；订单字典须定期清理终态订单。
3. **网络会话复用**：`httpx.Client` 建一次复用，⛔ 不在循环里新建连接。
4. **必须释放的资源**：WebSocket 连接、HTTP client、文件句柄、日志 handler。
   一律用 `with` 或在 `finally` 中释放；长驻进程退出前撤销所有未成交委托。
5. 大数据落地用 parquet + 分区，不用单个巨型 CSV。

---

## 七、审查触发规则（按严重度）

| 级别 | 触发条件 |
|---|---|
| CRITICAL | 明文密钥出现在代码/配置/日志/文档 |
| CRITICAL | `place_*` / `cancel_*` 路径缺 `DRY_RUN` 或 `live` 断言 |
| CRITICAL | 下单量/价格未过对齐函数 |
| HIGH | Magic Number 硬编码，或常量无依据出处注释 |
| HIGH | 金额用 float 累加 |
| HIGH | 裸 `datetime.now()` / 时区未声明 |
| HIGH | 反向依赖（下层 import 上层） |
| HIGH | 无限增长的容器；循环内新建连接 |
| HIGH | 同一逻辑被复制到两处（§4.7）——尤其回测与实盘各写一份信号 |
| HIGH | 业务函数中段读配置或读环境变量（§4.8） |
| HIGH | 文件 > 400 行 或 函数 > 50 行（§4.2） |
| MEDIUM | 模块缺 docstring 功能索引或缺 `__all__`（§4.3） |
| MEDIUM | 新增变体需改 if 链而非加注册条目（§4.6） |
| MEDIUM | 公开函数缺 docstring 或缺单位声明 |
| MEDIUM | 出现 `utils.py` / `helpers.py` 一类无职责命名 |
| MEDIUM | 文件内部结构不按 §4.4 的固定顺序 |
| LOW | 行宽 > 100；import 顺序混乱 |
