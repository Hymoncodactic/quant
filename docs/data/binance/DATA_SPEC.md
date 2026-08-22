# Binance curated 数据说明

## 1. 来源

| 项 | 值 |
|---|---|
| 端点 | `https://data.binance.vision/<key>`（批量归档，公开 S3 + CloudFront，无鉴权） |
| 枚举 | `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=<p>` |
| 生成脚本 | `scripts/20260819_ingest_crypto_phase1.py` |
| 解析层 | `crypto_trading/ingest/{binance_archive,schemas}.py` |
| 取回时间 | 2026-08-19 |
| 完整性 | 每个 zip 附 SHA-256 `.CHECKSUM` 旁文件，下载时逐个校验，不匹配即抛错不落盘 |

注意：`api.binance.com` / `fapi.binance.com` / `dapi.binance.com` 从本机（英国）返回
**HTTP 451**，响应体明示 "restricted location"。归档端点 `data.binance.vision`、
只读行情 `data-api.binance.vision`、行情 WS `data-stream.binance.vision` 三者不受限。

## 2. 字段与单位

### spot/klines/<SYM>/{1m,1d}/

| 列 | 类型 | 单位 | 含义 |
|---|---|---|---|
| ts | timestamp UTC | — | bar **开始**时刻。实证：相邻 ts 差恒等于 bar 周期 |
| open/high/low/close | float64 | 计价币（USDT） | 开高低收 |
| volume | float64 | 基础币 | 成交量 |
| quote_volume | float64 | 计价币 | 成交额 |
| count | int64 | 笔 | 成交笔数 |
| taker_buy_volume | float64 | 基础币 | 主动买入量 |
| taker_buy_quote_volume | float64 | 计价币 | 主动买入额 |

注意，**时间戳单位陷阱**：现货归档自 **2025-01-01** 起为**微秒**，此前为**毫秒**；
期货始终毫秒。已在 `schemas.timestamp_unit()` 处理。按错误单位解析会把数据放到
公元 55000 年或 1970 年。

已丢弃列：`close_time`（= ts + 周期，可导出）、`ignore`（恒为 0）。

### um/metrics/<SYM>/  —— 5 分钟

`ts`、`symbol`、`sum_open_interest`（持仓量，基础币）、`sum_open_interest_value`
（持仓名义，计价币）、`count_toptrader_long_short_ratio`（大户多空账户数比）、
`sum_toptrader_long_short_ratio`（大户多空持仓量比）、`count_long_short_ratio`
（全市场多空账户数比）、`sum_taker_long_short_vol_ratio`（主动买卖量比）。

### um/bookDepth/<SYM>/  —— 30 秒

`ts`、`percentage`、`depth`、`notional`。**这不是订单簿**，是距中间价固定
百分比处的**累计挂单深度**。12 个档位：±0.2 / ±1 / ±2 / ±3 / ±4 / ±5 %。
每日 2,880 张快照 × 12 档 = 34,560 行。
定义档位的参考价（中间价 / 标记价 / 最新价）**Binance 未文档化**，属未证实。

### um/fundingRate/<SYM>/  —— 8 小时

`calc_time`（结算时刻）、`funding_interval_hours`、`last_funding_rate`。

## 3. 覆盖与缺口

见 `scripts/data_inventory.py` 输出。各标的起始日 = 其在 Binance 的上市日，非统一。
本轮无缺口登记项：8,353 个目标文件中 8,352 个一次成功，1 个因瞬时 DNS 失败已补下。

## 4. 已做的处理

- 按 ts 排序后写入（乱序写入实测大 5.2~5.5 倍）
- zstd level 3（pyarrow 默认是 level 1，显式指定省 8%）
- row group 131,072 行（选择性查询延迟最优区间）
- 丢弃精确可导出列（`quote_qty` = price × qty，实测 30 万行零差异）
- **未做**：复权、缺口填充、异常值剔除、重采样

## 5. 已知不存在的数据（不要再找）

- **现货归档没有任何订单簿数据**，连最优买卖价都没有。只有 trades / aggTrades / klines。
- `bookTicker`（逐笔最优买卖价含量）**仅期货有**，且 USD-M 于 **2024-03-30 停更**，
  COIN-M 于 2024-10-14 停更。窗口固定，不会再增长。
- 真正的 L2 全深度订单簿**在免费归档中从不存在**。只能自行录制（需 7×24 在线）
  或向 Crypto Lake / Tardis / Kaiko 购买。
