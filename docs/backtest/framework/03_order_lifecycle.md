# 订单生命周期计划：T212 契约与模拟器状态机

权威契约：Trading 212 Public API v0 OpenAPI 规范，2026-08-20 取回并本地抽验，
存 `data/reference/t212_openapi_v0_20260820.yaml`（下称「规范」）；
md 镜像 `data/reference/t212_api_docs_md_mirror_20260820.md`。
逐条事实与 URL 见 `data/reference/t212_research_20260820/order_api_contract.json`。

## 1. 真实 API 契约（模拟器对齐的目标）

### 1.1 端点与限频（规范 + docs.trading212.com/api.md「Rate limit」行）

| 动作 | 端点 | 限频 |
|---|---|---|
| 市价单 | POST /api/v0/equity/orders/market | 50 次/60s |
| 限价单 | POST /api/v0/equity/orders/limit | 1 次/2s |
| 止损单 | POST /api/v0/equity/orders/stop | 1 次/2s |
| 止损限价 | POST /api/v0/equity/orders/stop_limit | 1 次/2s |
| 撤单 | DELETE /api/v0/equity/orders/{id} | 50 次/60s |
| 挂单列表 | GET /api/v0/equity/orders | 1 次/5s |
| 单笔查询 | GET /api/v0/equity/orders/{id} | 1 次/1s |
| 历史订单 | GET /api/v0/equity/history/orders | 6 次/60s |

限频按账户计，与 Key/IP 无关；窗口内允许突发。另有功能上限：
每 ticker 每账户最多 50 笔挂单。

### 1.2 订单语义（规范）

1. 只支持按数量（strategy=QUANTITY）；按金额下单 API 不支持。
2. 无方向字段：quantity < 0 即卖出。
3. 市价单请求体 {ticker, quantity, extendedHours}；无 timeValidity。
   限价/止损/止损限价带 timeValidity ∈ {DAY, GOOD_TILL_CANCEL}。
   DAY = 交易所本地时区午夜过期；GTC = 无限期。
4. extendedHours 仅市价单接受（社区实证：限价单带该字段返回 400 Invalid
   payload，community 87988 post 163，2026-02）。
5. 止损触发以最新成交价 LTP 为准；触发后转市价（STOP）或转限价（STOP_LIMIT）。
6. 市价单闭市提交不拒单，排队至下一开盘执行；官方明示成交价可能与下单时价
   偏离（滑点无上界）。
7. 下单端点**非幂等**（beta 官方警告）：重试同一请求可能产生重复订单。
8. 撤单 200 仅表示请求受理，「订单已在成交流程中则不保证撤销」（规范原文）。
9. ticker 格式为内部代码（如 AAPL_US_EQ），与交易所代码不同。

### 1.3 状态机（规范枚举 11 态；逐态释义来自 T212 官方 labs 仓库
`vendor/agent-skills/plugins/trading212-api/skills/trading212-api/SKILL.md` §Order Statuses）

LOCAL（本地已建未发）→ UNCONFIRMED（已发交易所待确认）→ CONFIRMED（交易所已确认）
→ NEW（活跃待执行）→ {PARTIALLY_FILLED → FILLED} | CANCELLING → CANCELLED |
REJECTED | REPLACING → REPLACED。
官方未给出合法转移表（unresolved 项），以上顺序为 labs 释义推断，标注为近官方。

### 1.4 已证实的拒单错误码

| 错误 | 含义 | 来源 |
|---|---|---|
| InsufficientFreeForStocksBuy | 现金不足 | labs SKILL.md §Common Order Errors |
| SellingEquityNotOwned | 卖出超过可交易数量（含被挂单/止损占用） | 同上 + community 2025-10 实例 |
| MarketClosed | 非交易时段 | labs SKILL.md |
| /api-errors/quantity-precision-mismatch | 数量小数位超限（实测 4 位上限） | community 87988 post 125，2026-01 |
| /api-errors/entity-not-found | ticker 不在 API 标的表 | community 87988 post 108，2025-12 |
| {"code":"UNDEFINED"} | 不明拒单，重试可成 | community 61788 post 121，2023-09 |

## 2. 模拟器状态机（本项目实现）

简化为可观测终态等价：提交 → NEW →（每根 bar 评估）→
PARTIALLY_FILLED/FILLED | CANCELLED（含 DAY 过期，过期原因记入订单元数据）|
REJECTED。LOCAL/UNCONFIRMED/CONFIRMED 三个前置态在 bar 粒度不可分辨，
由延迟模型（`t212_faults/02_latency_model.md`）以「提交后 N 根 bar 内不参与撮合」
表达。REPLACING/REPLACED 不实现（API 无改单端点）。

### 2.1 撮合规则（按订单类型）

记 t 为提交所在 bar，`next_open` 模式下撮合从 t+1 起（禁止同 bar 成交）；`same_close` 模式下市价单改在 t 收盘成交（见变更记录 2026-08-22）：

| 类型 | 成交条件与定价 |
|---|---|
| MARKET | t+1 开盘价 ± 半点差 ± 滑点（买加卖减）。闭市提交则为下一交易时段首根 bar 开盘 |
| LIMIT 买 | open ≤ limit → open 成交（跳空即成）；否则须**严格穿透**（low < limit）才按 limit 成交，触及（low = limit）不成交——bar 极值上的挂单排在队尾，触及即全成会系统性收割 bar 极值。成交价加半点差后以 limit 封顶（限价保障） |
| LIMIT 卖 | 镜像（high > limit 严格穿透） |
| STOP 买 | high ≥ stop 触发 → max(open, stop) + 半点差 + 滑点。触发为单向转换：部分成交或延迟后余量按市价腿继续，不再复验 stop；执行延迟在**触发时**抽取（提交时抽取会漏掉延迟窗内的触发事件） |
| STOP 卖 | 镜像 |
| STOP_LIMIT | 触发同 STOP，触发后转 LIMIT。**可即成腿**（买 limit ≥ stop / 卖 limit ≤ stop）bar 内触发按 stop 触及价成交，禁止给出 bar 未交易过的价格；**非可即成腿**须触发后证据：买用 low < limit（严格穿透；O-H-L-C 下 L 在触发之后），卖只能用 close > limit（high 在触发之前，不得采信） |

bar 内先后顺序按 O-H-L-C；同根内止损与限价均可触发时取不利侧
（`01_architecture.md` §3.3）。

### 2.2 提交时校验（按真实拒单行为，顺序固定）

1. ticker 在标的表（否则 entity-not-found 类拒单）。
2. 数量 > 0（绝对值）、小数位 ≤ 配置精度（默认 4，依据 §1.4）。
3. 最小订单价值 ≥ 1.00 GBP（Wayback 官方帮助页 + 员工确认，见
   `04_cost_model.md` §5；标注：现行页面已下线，属弱证据，参数可配）。
4. 买入：预估成本（含费用与 FX）≤ 可用现金 × 缓冲系数（故障目录 F9）。
   现金不足硬拒单，绝不允许负现金。
5. 卖出：数量 ≤ 持仓 − 已被其它未成交卖单占用的数量（SellingEquityNotOwned 语义）。
6. 每 ticker 挂单数 ≤ 50。
7. 限频：限价/止损类提交按 1 次/2s、市价按 50 次/分钟折算为每 bar 提交上限；
   超出部分以 `pacing_deferred` 理由拒单，由引擎在下一根 bar 依据目标持仓
   差分自动重新提交（与内部排队等待等效，且拒单进入订单审计表可追溯）。

### 2.3 资金占用

买入挂单冻结预估成本（对应 API cash.reservedForOrders 字段，规范
AccountSummary.Cash），冻结额计入占用资金序列，用于本金口径
（`05_metrics_reporting.md` §1）。

## 3. 与真实平台的已知差距（结论限定用）

1. 前置三态不建模，延迟以 bar 粒度近似（bar 细到 1m 时误差 < 1 bar）。
2. 真实部分成交拆片数不可知（实例：15 片，community 53459），模拟按
   成交量参与上限逐 bar 结转，不模拟片内价格分布。
3. DAY 过期时刻取交易所本地午夜（规范原文），但当日无后续 bar 时等价于
   收盘后即不再参与撮合。
4. 订单状态语义无官方转移表，LOCAL/UNCONFIRMED/CONFIRMED 释义为近官方
   （labs 仓库），已在 §1.3 标注。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-20 | 初版 |
| 2026-08-20 | 审查后修订：§2.1 STOP 触发单向持久化 + 触发时抽延迟；STOP_LIMIT 可即成/非可即成两种腿的撮合语义（卖侧只认 close 证据）；§2.2.7 限频语义定为拒单 + 引擎差分重试；撤单竞态一次性裁决（不逐 bar 重掷）；执行时资金闸扣除其它挂单的冻结额 |
| 2026-08-21 | 保守性二次复核：挂单（LIMIT 与 STOP_LIMIT 非可即成腿）由「触及即成」改为「严格穿透才成」——触及即全成属成交概率乐观（复核发现 2）；新增不同订单间冷却期（§2.2 准入后、撮合资格处强制，参数见 04 §3 冷却行） |
| 2026-08-22 | 新增成交时序模式 `fill_timing`：`next_open`（默认）与 `same_close`（决策 bar 收盘成交，收盘前 1 分钟下单口径，用户裁定 `research/decisions/20260822_close_execution_timing.md`）；same_close 仅作用于市价单，延迟超窗口/闭市即回落 next_open 路径；成交记账抽出 `t212/fills.py`，同收盘逻辑在 `t212/same_close.py` |
