---
name: quant-error-handling
description: 错误处理、重试限频、日志规范与故障定位。覆盖异常分类、交易所 API 的重试与退避、幂等下单、结构化日志格式、问题定位决策树、资源清理。当编写与外部 API 交互的代码、构建容错机制、排查运行异常、设计日志或调试长驻进程时调用。触发词：报错、异常、重试、限频、429、超时、断线、日志、排查、定位问题。
---

# 错误处理、日志与故障定位

> 前置：`CLAUDE.md` §1 推断纪律与 §3 资金红线全程生效。
> 由团队 QMT 错误处理与调试模块改写，已剥离 QMT 订单状态码等平台特有内容。

---

## 一、异常分类与处置策略

| 类别 | 典型场景 | 处置 | 可否自动重试 |
|---|---|---|---|
| **网络** | 连接超时、DNS、连接重置 | 指数退避重试 | 可 |
| **限频** | HTTP 429、交易所限频码 | 按 `Retry-After` 退避；无该头则指数退避 | 可 |
| **服务端** | 5xx、交易所维护 | 退避重试，超次数告警 | 可 |
| **认证** | 401/403、签名错误、时间戳偏移 | ⛔ **不重试**，立即停并告警 | 否 |
| **业务拒单** | 余额不足、数量低于最小值、价格超限、标的停牌 | ⛔ **不重试**，记录并交上层决策 | 否 |
| **数据** | 空返回、字段缺失、NaN、时间戳跳变 | 校验拦截；缺口登记，不静默填充 | 否 |
| **配置** | 参数缺失、环境不符 | 启动即失败（fail fast） | 否 |
| **状态不一致** | 本地持仓 ≠ 柜台持仓 | 停止下新单，触发全量对账 | 否 |

**铁律**：**只有幂等操作才允许自动重试。** 下单不是幂等操作——
必须带客户端订单号（`clOrdId` / client order id）才可重试，否则重试 = 重复下单。

### 1.1 自定义异常

```python
class QuantError(Exception):
    """本项目异常基类。"""
    def __init__(self, message: str, code: str | None = None, ctx: dict | None = None):
        super().__init__(message)
        self.code, self.ctx = code, ctx or {}


class TransientError(QuantError):
    """可重试：网络 / 限频 / 5xx。"""


class RateLimitError(TransientError):
    def __init__(self, endpoint: str, retry_after: float | None = None):
        super().__init__(f"限频 {endpoint}", code="RATE_LIMIT",
                         ctx={"endpoint": endpoint, "retry_after": retry_after})
        self.retry_after = retry_after


class PermanentError(QuantError):
    """不可重试：认证 / 业务拒单 / 配置。"""


class OrderRejectedError(PermanentError): ...
class PositionMismatchError(PermanentError): ...
class DataIntegrityError(PermanentError): ...
```

⛔ 禁止裸 `except:`；⛔ 禁止 `except Exception: pass`（至少要记日志）。

---

## 二、重试与限频

### 2.1 退避

```python
# 依据：OKX 官方文档 Rate Limit 节 / T212 文档限频节，取回日期须注明
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_SEC = 0.5
RETRY_MAX_SEC = 30.0

def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """第 attempt 次重试前应等待的秒数（attempt 从 1 起）。

    优先采用服务端给出的 retry_after；否则指数退避 + 抖动，避免同步重试风暴。
    """
    if retry_after is not None:
        return min(retry_after, RETRY_MAX_SEC)
    import random
    return min(RETRY_BASE_SEC * (2 ** (attempt - 1)), RETRY_MAX_SEC) * (0.5 + random.random())
```

### 2.2 主动限频

⛔ 不要靠「撞到 429 再退避」当限频策略——那会被交易所计入违规并可能封 IP。
必须在客户端侧主动限速（令牌桶），速率取官方上限的 **70%** 留余量。
每个端点分组独立计数（下单、查询、行情各一组）。

### 2.3 下单的幂等

```python
def place_order(...):
    """下单。**必须**由调用方传入 client_order_id 以保证重试幂等。

    重试前先按 client_order_id 查询订单是否已存在；
    存在则返回既有订单，不再提交。
    """
```

⛔ **没有幂等保证的下单路径，禁止任何自动重试**——宁可失败让人介入，
也不能重复下单。这是资金红线的直接延伸。

---

## 三、日志规范

### 3.1 格式

```
%(asctime)s | %(levelname)-8s | %(name)s | %(message)s      时间为 UTC ISO8601
```

- 每个模块一路 logger：`logging.getLogger("okx.ingest")`。
- 文件落 `logs/<模块>_YYYYMMDD.log`，UTF-8；控制台只输出 WARNING 以上。
- ⛔ **日志中绝不出现 API Key / Secret / Passphrase / 签名串 / Authorization 头**。
  统一经脱敏函数：只留前 4 后 4 位。
- ⛔ 不用 `print` 代替日志。

### 3.2 级别

| 级别 | 用于 |
|---|---|
| DEBUG | 中间变量、请求响应体（**须脱敏**） |
| INFO | 关键节点：下单、成交、撤单、信号触发、数据落地完成 |
| WARNING | 可恢复：重试、数据缺口、快照过期 |
| ERROR | 需关注：下单失败、对账不一致、任务失败 |
| CRITICAL | 立即处理：认证失效、风控熔断、持仓不一致 |

### 3.3 关键事件必含字段

```python
log.info("[下单] venue=%s inst=%s side=%s qty=%s price=%s client_id=%s dry_run=%s",
         venue, inst, side, qty, price, client_id, DRY_RUN)
log.info("[成交] client_id=%s filled=%s avg_price=%s fee=%s", client_id, filled, avg, fee)
log.warning("[风控] rule=%s current=%s threshold=%s action=%s", rule, cur, thr, action)
log.error("[异常] type=%s msg=%s ctx=%s", type(e).__name__, e, ctx)
```

**下单日志必须显式带 `dry_run` 字段**，否则事后无法区分演练与实盘。

---

## 四、问题定位决策树

```
现象
├─ 进程起不来
│   └─ 配置/密钥/依赖 → 检查 QUANT_ENV、secrets/ 权限、requirements
├─ 报错但能继续
│   ├─ 429 / 超时      → 查限频器速率是否超过官方 70%；查是否多进程共用 IP
│   ├─ 401 / 签名错误   → 查本机时钟偏移（NTP）、passphrase、密钥权限位
│   └─ 空数据          → 查请求区间是否非交易时段/未上市；查分页参数方向
├─ 不报错但结果不对   ← 最危险，优先排这类
│   ├─ 数字量级差 10^n → 单位错（张/手/币、bps/百分比、秒/毫秒）
│   ├─ 时间错位一根 bar → ts 取的是开盘还是收盘；时区；bar 对齐方向
│   ├─ 回测好实盘差    → 未来函数、滑点/费率缺失、成交假设过松
│   └─ 持仓对不上      → 部分成交未回写、撤单竞态、手工操作未纳入
└─ 长驻进程一段时间后异常
    ├─ 内存增长        → 无限增长容器；未清理终态订单
    ├─ 连接断掉不恢复   → WS 心跳/重连逻辑；重连后未重新订阅
    └─ 半夜行为异常     → UTC 跨日、夏令时、交易所日结维护窗
```

**排查纪律**：每一步都要给出**可复现的证据**（命令 + 输出片段），
不得凭「应该是」下结论。定位不到就如实说定位不到。

---

## 五、长驻进程的资源与退出

```python
def shutdown(ctx) -> None:
    """优雅退出：顺序不可颠倒。"""
    try:
        ctx.stop_new_signals()          # 1. 先停新信号，防止边关边下单
        ctx.cancel_open_orders()        # 2. 撤销所有未成交委托（DRY_RUN 下只记日志）
        ctx.unsubscribe_all()           # 3. 退订行情
        ctx.reconcile_positions()       # 4. 落地最终持仓快照，供下次启动对账
    except Exception as e:
        log.error("[退出] 清理异常 type=%s msg=%s", type(e).__name__, e)
    finally:
        ctx.close_clients()             # 5. 关连接与文件句柄
        logging.shutdown()
```

- 必须响应 SIGINT / SIGTERM 走同一条退出路径。
- 崩溃重启后**第一件事是对账**，不是继续下单。

---

## 六、审查触发规则

| 级别 | 触发条件 |
|---|---|
| CRITICAL | 下单路径存在自动重试但无 client order id 幂等保证 |
| CRITICAL | 日志/异常信息中可能打印密钥或签名 |
| CRITICAL | 认证类或业务拒单类错误被纳入自动重试 |
| HIGH | 裸 `except:` 或 `except Exception: pass` |
| HIGH | 无客户端主动限频，仅靠 429 被动退避 |
| HIGH | 长驻进程无优雅退出，或退出不撤未成交委托 |
| HIGH | 崩溃重启后未先对账 |
| MEDIUM | 用 `print` 代替日志；日志缺关键字段（含 `dry_run`） |
| MEDIUM | 重试无抖动（同步重试风暴风险） |
