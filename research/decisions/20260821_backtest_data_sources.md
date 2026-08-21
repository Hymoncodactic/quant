# 回测数据源裁定：双线接入范围

日期：2026-08-21。性质：用户明言裁定（S6），约束 `backtest/` 各 runner 的
数据接入配置；本文件为该口径的权威记录（`CLAUDE.md` §六：回测口径以
`research/decisions/` 最新裁定为准）。

## 一、裁定

| 线 | 项 | 取值 |
|---|---|---|
| 股票（t212） | 分组 | uk_tradable（11）、us_equity（24）、us_etf（17）全部接入 |
| 股票（t212） | 周期 | **1h** |
| crypto（okx） | 数据源 | binance **spot 1m K 线**（9 个 USDT 对）+ **um bookTicker L1**（BTC/ETH） |
| 框架位置 | — | 已并入 main（commit `adcb85b`，2026-08-21） |

## 二、随裁定生效的限定（结论披露时必须携带）

1. **1h 历史深度仅约 730 天**（Yahoo 源硬限制，`docs/data/t212/DATA_SPEC.md`
   §5）。样本不足以覆盖多个制度周期；长周期结论仍须以 1d 数据另行验证。
2. **us_etf 组英国零售不可交易**（PRIIPs 无 KID + FSMA 2000 s238），
   只作研究对照，其结果不得进入任何可执行策略的业绩主表。
3. **us_equity 组含存活者偏差**：Yahoo 清除退市股历史（14 只实测 13 只无数据，
   见 `research/decisions/20260821_paid_data_sources.md` §二）。以现存标的池
   回测的选股类结论系统性偏乐观；消除须付费源（同文件 §一）。
4. **混交易所 1h 时间轴**：美股 bar 在 :30 网格、伦敦在 :00 网格，撮合资格
   按时间制执行（`fixplans/framework/01_architecture.md` §2），引擎带日内
   成交时间断言。
5. **crypto 线当前只有数据读取层**（`backtest/okx/data_source.py`）。
   OKX 撮合/成本适配器未建，须先按 S4 取证 OKX 现货费率档、最小下单量、
   精度与限频，另任务实施后 crypto 回测方可运行。bookTicker 的用途为
   点差/滑点校准与 L1 特征，不是可交易标的的价格主序列。
6. um（期货）数据仅作数据源：FCA 自 2021-01-06 禁止英国零售交易加密衍生品，
   合约不可为交易标的。

## 三、待办

1. okx 撮合/成本适配器（S4 取证 + 实施 + 判别力测试），完成前 crypto 线
   不产出任何回测结论。
2. 1m K 线与 bookTicker 的联合馈送设计（bar 主序列 + L1 校准）写入
   `fixplans/framework/02_data_layer.md` 的后续修订。
