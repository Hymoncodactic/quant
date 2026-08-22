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
| `quotes.py` | 延迟行情拉取（与策略同一数据源） | T212 无行情接口；换数据源会让看板与策略对不上 |
| `settings.py` | 上交易前配置的读取、校验与写回 | 校验只出问题码，措辞交给界面，模块不掺表现层 |
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
