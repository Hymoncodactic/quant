# A0 实盘交易时段测试计划

本计划规定必须在美股交易时段内执行的测试。时段外可完成的测试属批次一，
已于 2026-08-28 执行完毕，结果见 §7。

驱动脚本：`scripts/20260829_live_probe.py`（只读，不含任何下单调用）。
被测代码：`trading212/execution/`、`trading212/client.py`、`trading212/dashboard/`。

---

## 1. 时钟对照（本机时区为 Asia/Shanghai，UTC+8）

美股常规时段的三个关键时刻，以 2026-08-31（周一）会话为例：

| 事件 | 纽约 | UTC | 伦敦 | **本机（CST）** |
|---|---|---|---|---|
| 决策键（15:30 bar 开） | 08-31 15:30 | 08-31 19:30 | 08-31 20:30 | **09-01 03:30** |
| 提交时刻（收盘前 60 秒） | 08-31 15:59 | 08-31 19:59 | 08-31 20:59 | **09-01 03:59** |
| 收盘（成交口径基准） | 08-31 16:00 | 08-31 20:00 | 08-31 21:00 | **09-01 04:00** |

本机时区在美国夏令时期间与纽约差 12 小时。夏令时结束后（美国 2026-11-01
回拨），上述本机时刻整体后移一小时至 04:30 / 04:59 / 05:00。冬夏令时的
换算不得靠固定偏移推算，每次以 `scripts/20260829_live_probe.py rehearse`
输出的 `decision_key_utc` 为准。

---

## 2. 两条测试路线，按优先级

### 路线 A：模拟盘（demo）全链路下单测试 —— 首选，不动真钱

前置条件：**已于 2026-08-29 全部满足**。模拟盘凭据（key + secret 两个文件）
已就位并实测可用（demo 账户 id 20528113，练习金 £5,000）。

凭据制度（2026-08-29 证实，官方文档 docs.trading212.com/api §Authentication）：
2025 年 10 月起新签发的凭据为 **API Key + API Secret 对**，鉴权为 HTTP Basic
（key 为用户名、secret 为密码）；旧式单钥头在规格中名为 `legacyApiKeyHeader`，
无官方保留期承诺。secret 只在生成时显示一次，删除即刻永久失效。
本项目四个凭据文件均在 `secrets/`（api/secret × live/demo），权限一律 600——
**权限不是 600 时 `common/secrets.py` 会拒绝读取**（Finder/编辑器保存的文件
默认 644，凭据轮换后第一个故障通常就是它，看板诊断会显示处置命令）。
`trading212/config/t212.{live,paper}.yaml` 均已配置 `api_secret_name`。

模拟盘可测而实盘不宜测的项：真实 POST 往返延迟、订单生命周期状态流转、
成交回报延迟、`walletImpact` 字段结构、最小下单量与数量精度的真实边界、
拒单错误码。这些是本计划的主要目标。

### 路线 B：实盘小额测试 —— 模拟盘不可用时的退路

在实盘以最小名义额验证同样的链路。代价是真实成本与真实持仓，
且不可撤销。仅在路线 A 不可用且用户当轮明确授权时执行。

---

## 3. 交易时段测试项

标注「A」的仅模拟盘可做，「A/B」两条路线均可做，「只读」不涉及下单。

### T0 时段判定（先做这个）

`decide` 只在会话内工作，休市运行必然返回
`no regular US session is open right now` 并中止——这是正确行为，不是故障。
判断当前是否在窗口内：

`QUANT_ENV=paper ./.venv/bin/python -m trading212.execution.run_a0 status`

输出的 `session_now` 非 null 即在会话内；`next_full_session.decision_key_utc`
是下一次可决策的时刻。注意**日志时间戳是 UTC**（`common/logging_setup.py`），
与本机 CST 差 8 小时，不要拿日志里的时刻当本机时刻读。

### T1 会话内只读基线（只读，任意交易时段）

命令：`QUANT_ENV=live ./.venv/bin/python scripts/20260829_live_probe.py latency --rounds 8`

目的：对照批次一的休市基线，测量交易时段的端点延迟是否劣化。
判据：`min_ms` 与休市基线（§7）相差不超过一倍；出现 429 则记录其频次。

### T2 决策窗口内的完整演练（只读，须在 15:30–15:59 纽约时间内）

命令：`QUANT_ENV=live ./.venv/bin/python scripts/20260829_live_probe.py rehearse`

目的：在真实窗口内跑完整条链路，与休市演练结果对比。
记录：`market_data.seconds`、`strategy.seconds`、`whole_rehearsal_seconds`、
`intents` 笔数与名义额、`risk_gate` 判定。
判据：整轮耗时须显著小于窗口余量；`symbols_without_decision_bar` 为空；
风控闸未关闭。

### T3 真实提交与延迟测量（A，或 B 经授权）

前置（2026-08-29 已就绪）：模拟盘状态与实盘**物理隔离**——账本、halt 旗标、
手工单日志在 `data/t212/execution_state_paper/`，记账归档在
`trading212/records/paper/`，demo 成交不可能污染实盘账本与成本归档。
模拟盘账本已初始化 £1,000（与实盘同额，风控闸行为可比）。
模拟盘看板：双击根目录 `dashboard_demo.command`（端口 8788，可与实盘看板并跑）。

命令（模拟盘）：
`QUANT_ENV=paper ./.venv/bin/python -m trading212.execution.run_a0 decide --allow-orders`
（须先把 `t212.paper.yaml` 的 `execution.dry_run` 改为 `false`——即便是模拟盘，
向场所提交订单仍按当轮授权执行）

测量指标，全部从 `logs/t212_YYYYMMDD.log` 与账本 journal 提取：

| 指标 | 提取方式 | 关注点 |
|---|---|---|
| 单笔 POST 往返延迟 | `[order] intent=` 与 `[order] submitted` 两条日志的时间差 | 与批次一只读基线 314–921 ms 对比 |
| 整批提交耗时 | 首笔 intent 到末笔 submitted | 必须小于 `submit_lead_sec`（60 秒），否则末几笔会跨过收盘 |
| 限频实际表现 | 是否出现 429 与其退避时长 | `order_market` 配置为 50/分钟 × 70% 安全系数 |
| 提交时刻精度 | `submitted_at_utc` 与 `close_utc` 之差 | 应落在收盘前 30–60 秒 |

判据：整批提交耗时留有至少一倍余量；无 429；无歧义冻结。

### T4 成交与成本实测（A，或 B 经授权）

命令：`QUANT_ENV=paper ./.venv/bin/python -m trading212.execution.run_a0 settle`
（收盘后执行，最长等待 90 分钟）

测量指标：

| 指标 | 提取方式 | 判据 |
|---|---|---|
| 成交回报延迟 | `fill.filledAt` 减去订单 `createdAt` | 记录分布；超过 4 小时会触发成交时序违规自动急停 |
| 成交价与决策价偏离 | `fill.price` 对比 intent 的 `ref_price_usd` | 这是 15:30 决策价到 16:00 收盘价的真实滑移，直接对应回测的 `close_gap_bps` |
| 实收费用 | `walletImpact.taxes` 逐项 | 与批次一历史实测中位 15.0 bps 对比 |
| 实际汇率 | `walletImpact.fxRate` | 与决策所用 `fx_usd_per_gbp` 对比，差额即 FX 时点风险 |
| 账本与账户一致 | settle 输出的 `reconcile` | 必须 CLEAN |

### T5 最小下单量与精度边界（A 专属，模拟盘才可做）

场所不发布 `minTradeQuantity` 与数量精度（2026-08-28 实证：instruments 端点
仅返回 `maxOpenQuantity`），故这两个边界至今未证实（`WORKING_MEMORY.md` 未决项 13）。

**本项不受交易时段限制**：场所在提交时刻就做参数校验，拒单立即返回，
故休市时段即可测出边界（被接受的单会排到下一个开盘，不影响边界结论）。

方法：在模拟盘用看板手动下单页逐次试探，每次只改一个量：
小数位 4 位、5 位、6 位；名义额 £1、£0.5、£0.1。记录场所接受与拒绝的分界，
以及拒单返回的错误文本。

前置（2026-08-29 修复）：手动下单页原先无条件要求配置 `live: true`，
而模拟盘配置按定义是 `live: false`，会把全部 demo 手动单拒掉。已改为
与 `client._assert_order_allowed` 同口径——`live: true` 只在 live 环境断言。

产出：把证实结果写入 `data/reference/`，并据此复核
`trading212/config/t212.live.yaml` 的 `min_order_value_gbp`。

### T6 急停在真实挂单下的行为（A 专属）

在模拟盘存在未成交挂单时点击看板急停按钮，验证：

1. 旗标立起后，新的 decide 立即中止。
2. **已在场所的挂单不会被撤销**（设计如此，撤单是人工动作）。
3. 看板解除按钮在存在未结订单时拒绝解除，并列出阻塞项。

### T7 中途中断恢复（A 专属）

在模拟盘提交完成、settle 之前，用 `kill -9` 结束进程，然后重跑 settle，
验证成交被正确收割、账本与账户对账 CLEAN。
再做一次更严格的：在 `decide --allow-orders` 提交阶段 `kill -9`，
验证下次运行触发悬空意向冻结（批次一已用临时账本验证过机制，
此项验证真实场所订单下的端到端行为）。

---

## 4. 执行顺序与时间安排

以 2026-08-31 会话为例，本机时刻：

| 本机时间 | 动作 |
|---|---|
| 09-01 03:00 | 双击 `dashboard_demo.command`（模拟盘看板，8788）；`QUANT_ENV=paper run_a0 status` 确认账户与账本；T1 |
| 09-01 03:35 | T2 决策窗口内演练（只读） |
| 09-01 03:50 | 若路线 A 就绪：T3 模拟盘真实提交 |
| 09-01 04:00 后 | T4 settle 与成本实测 |
| 任意时段 | T5、T6、T7 |

模拟盘的会话时钟与实盘一致，故 T3/T4 同样须在上述窗口内进行。

---

## 5. 中止条件

出现下列任一情形，停止全部测试并记录现场，不得继续：

1. 账本进入冻结（歧义未解）。
2. 对账出现 MISMATCH。
3. 出现非预期的真实订单（实盘环境下的任何未授权提交）。
4. 时钟偏移超过 10 秒（decide 会自行中止，但须查明原因）。

---

## 6. 记录要求

每项测试记录：命令、开始与结束时刻（UTC 与本机）、输出摘要、判据结论
（通过／未通过／未执行及原因）。全部指标汇总后与回测口径逐项对照，
差异写入 `research/decisions/` 下的裁定件。

---

## 7. 批次一结果（休市时段，2026-08-28 执行）

| 项 | 结果 |
|---|---|
| 端点延迟（原始网络往返，min） | positions 593 ms、pending_orders 358 ms、account_summary 314 ms、exchanges 401 ms、instruments 921 ms |
| 端点延迟（含本方限频，中位） | account_summary 7.14 s、pending_orders 7.34 s、exchanges 22.3 s —— 由令牌桶决定，非场所延迟 |
| 时钟偏移 | +0.75 秒（含单程网络与 HTTP Date 头 1 秒截断，真实偏差近零），闸上界 10 秒，不阻断 |
| 历史成交成本（546 笔） | 全体中位 15.0 bps；US 股 505 笔中位 15.0 bps（买 15.0 / 卖 15.01）；唯一列项为 `CURRENCY_CONVERSION_FEE`，未见 FINRA/SEC 列项；82 笔无费用（本币无需换汇） |
| 标的池 | 18 个全部解析、全部 USD STOCK；**DELL、ORCL、TSM 的交易日历 id 为 56（NYSE），其余为 71（NASDAQ）**；两张日历在缓存的 29 个会话上逐字段一致 |
| 完整决策演练（2026-08-28 会话） | 15 笔意向、全为买入、单笔 £54.25–55.08、合计 £822.23；风控 15 通过 0 拒绝；行情刷新 26.7 秒，策略计算 0.04 秒，整轮 27.3 秒 |
| settle 全链路 | 干净返回，对账 CLEAN，退出码 0 |
| 急停演练 | 旗标立起后 decide 立即中止，演练后旗标已清除 |
| 崩溃恢复演练 | 悬空意向被冻结、重载后仍冻结、歧义锚定在意向时刻、拒绝新意向 |
| 账本完整性 | 撕裂的 journal 尾行被检出，不被静默吸收 |
| 看板安全面 | 5 个写端点无 nonce 一律 403；跨源请求 403；仅回环监听，局域网 IP 拒绝连接 |

批次一发现并已修复：标的池跨两张交易所日历而执行层只按一张计时，
现已加入日历分歧闸（`trading212/execution/instruments.py::schedule_divergences`，
decide 中调用，分歧即中止）。

---

## 8. 凭据轮换后的复测（2026-08-29 执行）

用户轮换全部凭据（实盘与模拟盘均换为 key+secret 对）后，受鉴权影响的项复测：

| 项 | 结果 |
|---|---|
| 实盘鉴权 | Basic 方案 200；status 正常（账户 £1,000） |
| 模拟盘鉴权 | Basic 方案 200；status 正常（demo 账户 £5,000，独立 id） |
| 时钟偏移 | 0.45–0.69 秒，不阻断 |
| 端点延迟（min） | account_summary 539 ms、positions 606 ms、exchanges 451 ms、pending_orders 1206 ms、instruments 964 ms，与轮换前同量级 |
| 标的池 | 18 个全部解析；日历分歧核验（未来 5 个会话 × 两张日历）零分歧 |

成本（§7 的 546 笔历史实测）不受凭据轮换影响，无需复测。
