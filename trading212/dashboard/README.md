# trading212/dashboard/ 目录说明

## 1. 职责

Trading 212 一侧的**本地看板**：把策略状态、账户情况和延迟行情画出来，把上交易
前必须填的配置集中到一个页面，并提供一个独立的手动下单界面。

看板只读也只画。它**不启动策略、不停止策略、不代替策略下单**：策略是另一个按
计划运行的进程（`trading212/execution/run_a0.py`），关掉看板对它没有任何影响。

界面语言为中文（用户当轮指定，`CLAUDE.md` §2.3 表格「用户指定的输出」一行）。
为不破坏「代码内不出现中文」的硬性条款，**全部中文文案集中在 `assets/labels.json`
这一份数据文件里**，Python 与 JavaScript 源码保持纯 ASCII。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 |
|---|---|---|
| `__init__.py` | 声明为常规 Python 包 | 不 import 任何子模块，避免导入包就拉进 pandas 与网络客户端 |
| `context.py` | 进程内共享状态：配置、券商客户端、账本读取、关注标的 | 采集线程与请求处理器共用同一个客户端，才共用同一套限频 |
| `collector.py` | 秒更采集：各数据源独立轮询，采样只读缓存 | 慢或不可达的数据源不能拖慢界面刷新 |
| `snapshots.py` | 最新快照与逐日采样落盘、降采样读取 | 浏览器不该拿到几万个点；停机形成的断点必须可见 |
| `diagnostics.py` | 把连不上券商的失败判成具体原因（环境用错、DNS 被劫持、限流、密钥失效） | 「连不上券商」对读者不可行动；四种原因对应四种完全不同的处置 |
| `quotes.py` | 延迟行情拉取（与策略同一数据源） | T212 无行情接口；换数据源会让看板与策略对不上 |
| `settings.py` | 上交易前配置的读取、校验与写回 | 校验只出问题码，措辞交给界面，模块不掺表现层 |
| `api.py` 的 `post_halt` | 紧急停止的落旗与解除 | 落旗随时可做；解除要先过检查，否则等于带着问题重新开始交易 |
| `api.py` 的 `post_allocation` | 调整策略资金 | 账上钱变了要能改额度；写成账本事件而不是改快照，账本才仍能自证其现金 |
| `api.py` 的 `get_sessions` | 供图表标注美股交易时段 | 读日历缓存，开页面不消耗券商的元数据配额 |
| `api.py` 的 `post_ledger_reset` | 清空（归档）策略账本 | 策略资金只在建账本时定一次，换数额只能重建；账本是持仓归属的唯一记录，故改名归档而非删除 |
| `server.py` 的 `acquire_instance_lock` | 单实例保护，锁内记端口 | 券商按账户计限频，两个看板会把彼此都挤成「连不上」；锁在绑定端口**之前**取，否则第二次启动会先崩在 bind 上，根本走不到提示 |
| `manual_orders.py` | 手动下单与独立留痕 | 手动单不进策略账本，否则会污染策略的归因 |
| `api.py` | 路由处理，返回纯数据 | 路由可在无套接字的情况下被测试 |
| `server.py` | 本地 HTTP 服务、静态资源、采集生命周期 | 只绑 127.0.0.1；写操作需本次运行的令牌 |
| `assets/labels.json` | 全部中文文案 | 见 §1 |
| `assets/index.html` `app.js` | 总览页 | |
| `assets/orders.html` `orders.js` | 手动下单页 | 与总览分开，避免误点 |
| `assets/style.css` | 样式 | |
| `README.md` | 本文件 | |

`plotly.min.js` **不入库**：由 `server.py` 从已安装的 plotly 包里取出并以一年期
不可变缓存头下发，浏览器只解析一次，仓库里也不多出 4.8 MB。

## 3. 子目录索引

| 子目录 | 内容 |
|---|---|
| `assets/` | 页面、脚本、样式与中文文案 |

## 4. 依赖关系

import：`trading212/client.py`、`trading212/execution/{instruments,session_cycle,
shadow_ledger,ledger_store}`、`common/{config,paths,logging_setup}`、`yfinance`、
`plotly`（只用其打包的 js）。不 import `backtest/`。没有任何模块 import 本目录。

## 5. 产出与清理

写 `data/t212/dashboard/`（`live_snapshot.json` 与 `samples/YYYY-MM-DD.jsonl`）、
`logs/`；经 `settings.py` 写 `trading212/config/t212.<env>.yaml`；经 `api.py` 的
账本初始化写 `data/t212/execution_state/`；手动下单写
`data/t212/execution_state/manual_orders.jsonl`。

可以随时删除的：`data/t212/dashboard/` 整棵，只损失历史曲线。
必须保留的：`manual_orders.jsonl`（手动下单的唯一留痕）。

## 6. 变更记录

2026-08-22 建立看板：总览页、配置校验、秒更采集、手动下单页、根目录启动件
`dashboard.command`。
2026-08-23 修复轮询覆盖表单导致的保存失败；新增紧急停止按钮（解除须过检查）、
策略资金调整、资产曲线的美股交易时段底色与时区标注、账本额度对比账户可用现金的提示。
2026-08-24 修复重复启动崩溃：锁提到绑定端口之前，第二次启动改为打开已在运行的那个
看板（按锁文件记录的真实端口，而非本次请求的端口）；端口被其他程序占用时给出可操作
的提示而不是 traceback。
2026-08-23 曲线区分账本与账户：三条账本线改名并加「账户实际总值」虚线，附文字说明；
坐标轴时区可在本地/伦敦/纽约之间切换并记住选择；新增清空账本（归档式）按钮，建账本
输入栏在账本存在时隐藏；新增单实例锁。
2026-08-29 新增 `diagnostics.py`：账户轮询失败时判定原因并在界面上写明原因，覆盖 QUANT_ENV 默认 paper 导致实盘密钥被 demo 主机拒绝（401）与本机 DNS 解析到无关地址两类已实际发生的故障。
2026-08-29 账本类写路由（调整策略资金、清空账本）改为先取 run_a0 的执行锁，策略进程运行中一律 409 拒绝，杜绝双进程交错写账本；文案新增 strategy_running。
