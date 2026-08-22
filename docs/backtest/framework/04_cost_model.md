# 成本模型计划：T212 Invest 账户（GBP）

逐条事实与 URL 见 `data/reference/t212_research_20260820/fees_costs_calendar.json`
与 `latency_execution.json`。费用项命名对齐 API 的 Tax.name 枚举
（规范 `t212_openapi_v0_20260820.yaml` components.schemas.Tax：COMMISSION_TURNOVER,
CURRENCY_CONVERSION_FEE, FINRA_FEE, FRENCH_TRANSACTION_TAX, PTM_LEVY, STAMP_DUTY,
STAMP_DUTY_RESERVE_TAX, TRANSACTION_FEE），使回测成本列可与真实账户
`GET /equity/history/orders` 的 walletImpact.taxes 逐项对账。

## 1. 佣金与点差

| 项 | 取值 | 依据 |
|---|---|---|
| 佣金 | 0 | helpcentre 文章 11471996799517「Trading commission: Free」（official） |
| T212 附加点差 | 0 | 执行政策 PDF（trading212.com/legal-documentation/en/order-execution-policy.pdf，2022-01 版）§2 Costs：「does not charge any commission or apply additional spread」 |
| 实际点差成本 | 参考交易所触及点差的一半/边 | 同 PDF §7：场外（含碎股）成交价「no worse than the prevailing best Bid/Offer」于参考交易所；bar 数据无盘口，以实测触及点差表近似（§4） |

## 2. FX 费（CURRENCY_CONVERSION_FEE）

1. 费率 0.15%，Invest 账户，凡结算币种 ≠ 标的计价币种即收
   （helpcentre 360018909758，official）。
2. 收取机制：**劣化汇率**而非单列扣费——买入用 rate×(1−0.0015) 折算所得外币，
   卖出用 rate×(1+0.0015)（同文worked example：1.41 → 1.412115）。
   实现按此机制并同时把费额单列入 CURRENCY_CONVERSION_FEE 成本列。
3. API 下单一律以主币种 GBP 结算（api.md「Orders can be executed only in the
   primary account currency」）→ 所有 USD 计价标的双边收 0.15%。
   GBP 与 GBp 计价标的不收（GBp 为便士，无换汇）。
4. 换汇即时发生，模拟器不持外币现金（`01_architecture.md` §4）。
5. 汇率源：官方仅称 spot rate，供应商未披露（unresolved）。回测用
   GBPUSD=X 序列的最近可得值，方向约定见 `02_data_layer.md` §5.4。

## 3. 交易税费（按标的属性开关）

| 成本列 | 规则 | 适用范围 | 依据 |
|---|---|---|---|
| STAMP_DUTY_RESERVE_TAX | 买入金额 × 0.5% | 仅 LSE 上市**股票**买入；ETF/ETC/金边债免；多数 AIM 免 | helpcentre 360007081637（official）。当前标的池 uk_tradable 全为 ETF/ETC → 全部为 0，引擎仍实现该规则 |
| PTM_LEVY | 单笔**订单** > £10,000 时每单 £1.50，买卖双边；阈值按订单累计成交额判定，跨部分成交只收一次 | T212 费用页写为 LSE 通用 | helpcentre 360007081637（official）。冲突：`research/notes/20260819_t212_execution_and_liquidity.md` §三判定 ETF 属收购委员会豁免、T212 页面写错。**T212 对 ETF 实际是否代扣未证实** → 配置项 `ptm_levy_on_etf`，默认 true（保守，费用多计不利于策略），并在结论限定节披露 |
| FINRA_FEE | 卖出股数 × $0.000195 | 美股与美 ETF 卖出 | 同 helpcentre 文章（official） |
| TRANSACTION_FEE（SEC §31） | 卖出金额 × 0.00206% | 美股卖出 | 同文。官方两页写法不一致（0.00206% vs $0.00206），取百分比解释；量级 0.2bp，方向按费计不利 |
| FRENCH_TRANSACTION_TAX | 买入 × 0.4% | 法国大市值股 | 同文（official）。当前标的池无 → 实现但默认不触发 |

## 4. 点差与滑点参数（bar 数据近似盘口）

1. 每标的半点差（bps）优先取 2026-08-19 伦交所接口实测触及点差的一半
   （`research/notes/20260819_t212_execution_and_liquidity.md` §二表：
   CSPX 0.72bp、VUSA 1.39bp、IB01 1.65bp、IGLN 2.07bp、SGLN 3.21bp、
   XSPS 3.68bp、EQQQ 2.63bp、IDTL 13.18bp、IBTL 17.87bp、IUCS 14.62bp 全为
   双边触及价差，半点差取其半）。美股大盘股无实测 → 默认 1bp 半边（推断值）；
   无实测的伦敦标的默认取**最差实测半点差上取整**（IBTL 8.94 → 9.0bp），
   保证未实测标的不被假设优于任何已观测值。均为敏感性参数。
2. 附加滑点：固定 bps 可配，默认 5bp（zipline FixedBasisPointsSlippage
   默认值，vendor/zipline-reloaded slippage.py），最坏档口径用之，
   实际档可降为 0（点差已单独计）。
3. 成交量参与上限：单 bar 成交量的 10%（zipline volume_limit 默认），
   超出部分跨 bar 结转（`/backtest-discipline` §二.5 禁止外推）。
4. 时段修正：伦敦标的在 13:30–15:30 GMT 之外的时段点差按乘数放大
   （依据 `research/notes/20260819_t212_execution_and_liquidity.md` §四.1：
   美股闭市时做市商按期货定价主动放宽价差；乘数默认 2×，推断值，可配）。
   开盘竞价时段禁止成交假设不建模，由「不在开盘首根 bar 下单」的策略约束承担。
   `avoid_first_bar` 引擎开关 v0 不实现（列为待办，见变更记录）：日线模式下
   每笔成交本就在开盘，无从回避；日内模式由策略层自行避开首根 bar。

## 5. 数量与金额约束

| 项 | 取值 | 依据 |
|---|---|---|
| 最小订单价值 | 1.00 GBP | Wayback 2024-02-24 帮助页 360008095497 + 员工确认（community 15654，2020-08）。现行页面 302 到登录（弱证据，可配） |
| 数量小数位 | 4 位（下单）；持仓可 8 位 | community 87988 post 125：8 位持仓卖出报 invalid quantity precision 4（2026-01）。官方无文档（minTradeQuantity 已从规范移除，Wayback 2025-04 对照确认） |
| 取整方向 | 向下取整到精度；取整后价值 < £1 → 废单 | `/backtest-discipline` §二.6 |
| 卖出残余 | 卖后残余价值 < £1 的卖单被拒（除非全额卖出） | Wayback 帮助页同上 |

## 6. 双档口径（/backtest-discipline §二.4）

| 档 | 参数 |
|---|---|
| 最坏档（权威口径） | 实测/默认半点差 + 5bp 滑点 + 时段点差放大开 + ptm_levy_on_etf=true + 全部故障开关按目录默认开 |
| 实际档（对照） | 实测半点差 + 0 滑点 + 时段放大关 + 故障关 |

两档必须在结果文件名与元数据中显式标注（fee=worst / fee=actual）。

## 7. 未建模成本（结论限定节固定内容）

1. 股息预扣税与股息再投资时点（价格已复权，见 `02_data_layer.md` §6）。
   **方向与量级（2026-08-21 复核补记，均偏乐观）**：复权价按毛额即时再投资
   ——美股经 W-8BEN 预扣 15%，幻影收益约 0.15 × 股息率 ≈ 20–25bp/年
   （1.5% 股息率标的）；另加换汇 0.15% 与到账时滞。爱尔兰累积型 UCITS
   （CSPX/SGLN 等）近零，us_equity 组与分红线（VUSA）最大。
2. ETF 的 TER 年费——持有期成本，回测期内会体现在复权价里？否：TER 从基金
   净值内扣，复权价已含其影响，无需另计（此为价格内生成本，说明即可）。
3. 换汇的汇率源与 T212 实际 spot 源的差（unresolved）。
4. 深度消耗：bar 数据无 L2，参与上限只是近似（`/backtest-discipline` §二冷却期条款）。

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-20 | 初版 |
| 2026-08-20 | 审查后修订：PTM 阈值明确按订单累计、跨部分成交只收一次；未实测伦敦标的默认半点差由 4.0 改 9.0bp（最差实测上取整，原注释与取值不符）；`avoid_first_bar` v0 不实现改由策略层承担（待办）；时段点差放大窗口改按两交易所本地时钟判定（固定 UTC 窗在 GMT 月份错一小时）；法国 FTT 已实现（security_kind = stock_fr 触发） |
| 2026-08-21 | 保守性二次复核：新增冷却期参数 `cooldown_bars`（硬清单第 7 条；worst 档 2、actual 档 1=结构下限，同订单 F13 结转豁免）；新增**清算价值估值**——账本每步双列（mid 诊断列 + `equity_liq_gbp` 清算列：卖侧点差 + 滑点 + FX 费 + 卖侧税逐份估值，PTM 平头费无逐份形式记为未建模），权威回撤与终值改读清算列；§7.1 股息乐观量级补记 |
| 2026-08-22 | 新增 `close_gap_bps`（same_close 模式的收盘临近滑点，worst 11 / actual 5，依据 1m 数据实测 1,061 样本 P75/中位）与 `close_window_sec`（60） |
