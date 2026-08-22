---
name: quant-error-handling
description: 错误处理、重试限频、日志规范与故障定位。覆盖异常分类、交易所 API 的重试与退避、幂等下单、结构化日志格式、问题定位决策树、资源清理。当编写与外部 API 交互的代码、构建容错机制、排查运行异常、设计日志或调试长驻进程时调用。触发词：报错、异常、重试、限频、429、超时、断线、日志、排查、定位问题。
---

# 错误处理、日志与故障定位

前置约束：

1. `CLAUDE.md` §1 推断纪律与 §3 资金红线全程生效。
2. 代码文本一律美式英文，含日志与异常消息（`/quant-code-standards` §零）。

本 skill 由团队 QMT 错误处理与调试模块改写，已剥离 QMT 订单状态码等平台特有内容。

---

## 一、异常分类与处置策略

| 类别 | 典型场景 | 处置 | 可否自动重试 |
|---|---|---|---|
| 网络 | 连接超时、DNS 失败、连接重置 | 指数退避重试 | 可 |
| 限频 | HTTP 429、交易所限频码 | 按 `Retry-After` 退避；无该头则指数退避 | 可 |
| 服务端 | 5xx、交易所维护 | 退避重试，超次数告警 | 可 |
| 认证 | 401/403、签名错误、时间戳偏移 | 不重试，立即停止并告警 | 否 |
| 业务拒单 | 余额不足、数量低于最小值、价格超限、标的停牌 | 不重试，记录并交上层决策 | 否 |
| 数据 | 空返回、字段缺失、NaN、时间戳跳变 | 校验拦截；缺口登记，不静默填充 | 否 |
| 配置 | 参数缺失、环境不符 | 启动即失败（fail fast） | 否 |
| 状态不一致 | 本地持仓与柜台持仓不等 | 停止下新单，触发全量对账 | 否 |

铁律：只有幂等操作才允许自动重试。下单不是幂等操作，必须带客户端订单号
（`clOrdId` 或 client order id）才可重试，否则重试等于重复下单。

### 1.1 自定义异常

```python
class QuantError(Exception):
    """Base exception for this project."""

    def __init__(self, message: str, code: str | None = None, ctx: dict | None = None):
        super().__init__(message)
        self.code, self.ctx = code, ctx or {}


class TransientError(QuantError):
    """Retryable: network, rate limit, 5xx."""


class RateLimitError(TransientError):
    def __init__(self, endpoint: str, retry_after: float | None = None):
        super().__init__(f"rate limited on {endpoint}", code="RATE_LIMIT",
                         ctx={"endpoint": endpoint, "retry_after": retry_after})
        self.retry_after = retry_after


class PermanentError(QuantError):
    """Not retryable: authentication, order rejection, configuration."""


class OrderRejectedError(PermanentError): ...
class PositionMismatchError(PermanentError): ...
class DataIntegrityError(PermanentError): ...
```

禁止裸 `except:`；禁止 `except Exception: pass`，至少要记日志。

---

## 二、重试与限频

### 2.1 退避

```python
# Source: OKX official doc, Rate Limit section; T212 doc, rate limit section.
# Record the fetch date next to each value when filling these in.
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_SEC = 0.5
RETRY_MAX_SEC = 30.0


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Return the wait in seconds before retry number `attempt` (1-based).

    A server-provided retry_after takes precedence. Otherwise use exponential
    backoff with jitter, which prevents synchronized retry storms.
    """
    if retry_after is not None:
        return min(retry_after, RETRY_MAX_SEC)
    import random
    return min(RETRY_BASE_SEC * (2 ** (attempt - 1)), RETRY_MAX_SEC) * (0.5 + random.random())
```

### 2.2 主动限频

禁止把「撞到 429 再退避」当作限频策略，该做法会被交易所计入违规并可能封禁 IP。
必须在客户端侧主动限速（令牌桶），速率取官方上限的 70% 留余量。
每个端点分组独立计数，下单、查询、行情各一组。

### 2.3 下单的幂等

```python
def place_order(...):
    """Submit an order. The caller MUST pass client_order_id for retry idempotency.

    Before any retry, query the venue by client_order_id to check whether the
    order already exists; if it does, return the existing order instead of
    submitting again.
    """
```

没有幂等保证的下单路径禁止任何自动重试。宁可失败让人介入，也不能重复下单。
这是资金红线的直接延伸。

---

## 三、日志规范

### 3.1 格式

```
%(asctime)s | %(levelname)-8s | %(name)s | %(message)s      时间为 UTC ISO8601
```

1. 每个模块一路 logger：`logging.getLogger("okx.ingest")`。
2. 文件落 `logs/<module>_YYYYMMDD.log`，UTF-8；控制台只输出 WARNING 以上。
3. 日志中不得出现 API Key、Secret、Passphrase、签名串、Authorization 头。
   统一经脱敏函数处理，只保留前 4 位与后 4 位。
4. 不用 `print` 代替日志。
5. 日志消息一律英文（`/quant-code-standards` §零）。

### 3.2 级别

| 级别 | 用于 |
|---|---|
| DEBUG | 中间变量、请求与响应体，须脱敏 |
| INFO | 关键节点：下单、成交、撤单、信号触发、数据落地完成 |
| WARNING | 可恢复情形：重试、数据缺口、快照过期 |
| ERROR | 需关注：下单失败、对账不一致、任务失败 |
| CRITICAL | 立即处理：认证失效、风控熔断、持仓不一致 |

### 3.3 关键事件必含字段

```python
log.info("[order] venue=%s inst=%s side=%s qty=%s price=%s client_id=%s dry_run=%s",
         venue, inst, side, qty, price, client_id, DRY_RUN)
log.info("[fill] client_id=%s filled=%s avg_price=%s fee=%s", client_id, filled, avg, fee)
log.warning("[risk] rule=%s current=%s threshold=%s action=%s", rule, cur, thr, action)
log.error("[error] type=%s msg=%s ctx=%s", type(e).__name__, e, ctx)
```

下单日志必须显式带 `dry_run` 字段，否则事后无法区分演练与实盘。

---

## 四、问题定位决策树

按现象分类逐层排查：

| 现象 | 子类 | 首查项 |
|---|---|---|
| 进程起不来 | 配置、密钥、依赖 | 检查 `QUANT_ENV`、`secrets/` 权限、requirements |
| 报错但能继续 | 429 或超时 | 查限频器速率是否超过官方上限 70%；查是否多进程共用 IP |
| 报错但能继续 | 401 或签名错误 | 查本机时钟偏移（NTP）、passphrase、密钥权限位 |
| 报错但能继续 | 空数据 | 查请求区间是否落在非交易时段或未上市期；查分页参数方向 |
| 不报错但结果不对 | 数字量级差 10 的 n 次方 | 单位错：张与手与币、bps 与百分比、秒与毫秒 |
| 不报错但结果不对 | 时间错位一根 bar | ts 取的是开盘还是收盘；时区；bar 对齐方向 |
| 不报错但结果不对 | 回测好实盘差 | 未来函数、滑点或费率缺失、成交假设过松 |
| 不报错但结果不对 | 持仓对不上 | 部分成交未回写、撤单竞态、手工操作未纳入 |
| 长驻进程运行一段时间后异常 | 内存增长 | 无限增长容器；未清理终态订单 |
| 长驻进程运行一段时间后异常 | 连接断掉不恢复 | WS 心跳与重连逻辑；重连后未重新订阅 |
| 长驻进程运行一段时间后异常 | 夜间行为异常 | UTC 跨日、夏令时、交易所日结维护窗 |

「不报错但结果不对」一类优先排查，其危害高于显式报错。

排查纪律：每一步都要给出可复现的证据，即命令加输出片段，不得凭「应该是」下结论。
定位不到就如实说定位不到。

---

## 五、长驻进程的资源与退出

```python
def shutdown(ctx) -> None:
    """Graceful shutdown. The order of these steps must not change."""
    try:
        ctx.stop_new_signals()      # 1. Stop new signals first, so nothing is sent
                                    #    while the process is closing down.
        ctx.cancel_open_orders()    # 2. Cancel all open orders (log only under DRY_RUN).
        ctx.unsubscribe_all()       # 3. Unsubscribe from market data.
        ctx.reconcile_positions()   # 4. Persist the final position snapshot for the
                                    #    next startup reconciliation.
    except Exception as e:
        log.error("[shutdown] cleanup failed type=%s msg=%s", type(e).__name__, e)
    finally:
        ctx.close_clients()         # 5. Close connections and file handles.
        logging.shutdown()
```

1. 必须响应 SIGINT 与 SIGTERM，且走同一条退出路径。
2. 崩溃重启后第一件事是对账，不是继续下单。

---

## 六、审查触发规则

| 级别 | 触发条件 |
|---|---|
| CRITICAL | 下单路径存在自动重试但无 client order id 幂等保证 |
| CRITICAL | 日志或异常信息中可能打印密钥或签名 |
| CRITICAL | 认证类或业务拒单类错误被纳入自动重试 |
| HIGH | 裸 `except:` 或 `except Exception: pass` |
| HIGH | 无客户端主动限频，仅靠 429 被动退避 |
| HIGH | 长驻进程无优雅退出，或退出时不撤未成交委托 |
| HIGH | 崩溃重启后未先对账 |
| MEDIUM | 用 `print` 代替日志；日志缺关键字段，含 `dry_run` |
| MEDIUM | 重试无抖动，存在同步重试风暴风险 |
