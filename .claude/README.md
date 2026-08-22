# `.claude/` 目录说明

## 1. 职责

装 Claude Code 在本项目内的会话侧配置与流程文档，具体为两样：工具权限设置
`settings.local.json`，以及本项目 10 个 skill 所在的 `skills/`。

不装项目代码、配置模板、数据、回测结果与研究记录，这些分别在 `common/`、
`crypto_trading/`、`trading212/`、`backtest/`、`data/`、`docs/data/`、`research/`。
同样不装项目纪律本身：always-on 的纪律在根目录 `CLAUDE.md`，路径与分层地图在
`ARCHITECTURE.md`，跨会话状态在 `WORKING_MEMORY.md`，三者都不放进本目录。

## 2. 文件清单

| 文件 | 作用 | 存在必要性 | 谁在用 |
|---|---|---|---|
| `README.md` | 本文件。按 `CLAUDE.md` §4.3 的六节结构说明本目录职责、文件必要性、子目录索引、依赖、产出与清理、变更记录 | 删掉它，本目录即成为 §4.3 所禁止的「无 `README.md` 的既有目录」，改动目录内任何文件前的前置阅读动作（§4.3 时机第 3 条）失去依据 | 按 `/verified-dev` 阶段 1.2 第 1 步，改动 `.claude/` 下任何文件前须先读本文件与各级父目录的 `README.md` |
| `settings.local.json` | 会话权限设置。含三块：`permissions.defaultMode` 取值 `acceptEdits`；`permissions.allow` 列出 9 项直接放行的工具（`Bash`、`Edit`、`Write`、`Read`、`Glob`、`Grep`、`NotebookEdit`、`WebFetch`、`WebSearch`）；`permissions.deny` 列出 6 条拦截项，即 `Bash(rm -rf /)`、`Bash(rm -rf /*)`、`Bash(rm -rf ~)`、`Bash(rm -rf ~/*)` 四条删除拦截，与 `Read(./secrets/**)`、`Bash(cat ./secrets/*)` 两条密钥读取拦截 | 删掉它会同时失去三件东西：一是 `acceptEdits` 默认模式，编辑类操作退回逐次确认；二是四条 `rm -rf` 根目录与家目录删除拦截；三是针对 `secrets/` 的两条读取拦截，该拦截是 `CLAUDE.md` §3.2「密钥绝不进入会被读取展示的位置」在工具层面的兜底，失效后密钥文件可被普通读取路径取到 | Claude Code CLI 在会话启动时读取。仓库内无任何代码或脚本引用它：`grep -rn "settings\.local\|settings\.json"`（排除 `.venv`、`.claude/worktrees`、`vendor`、`.git`）返回 0 条命中 |

本目录当前不含其他文件。本文件建立前 `ls -A .claude` 的结果只有 `settings.local.json`、
`skills`、`worktrees` 三项，无 `.DS_Store`、`__pycache__/` 一类工具产物，
符合 `CLAUDE.md` §4.2 第 6 条。

## 3. 子目录索引

| 子目录 | 内容 | 说明文件 |
|---|---|---|
| `skills/` | 本项目 10 个 skill，每个占一个同名子目录，目录内一份 `SKILL.md`；另有本目录说明 `README.md` 一份 | `.claude/skills/README.md` |
| `worktrees/` | git worktree 的工作树挂载点。`git worktree list` 显示当前有 4 个活动工作树：`a0-strategy-live-trading-bb87ea`、`happy-ramanujan-f387cd`、`skills-language-standards-659ab4`、`trading212-backtest-framework-b729c3`，分别检出在各自的 `claude/*` 分支 | 无。`CLAUDE.md` §4.3 的豁免表把 `.claude/worktrees/` 列为不需要 `README.md` 的目录 |

## 4. 依赖关系

1. 读什么：本目录内的文件不读取项目内任何其他文件。`settings.local.json` 与
   `skills/*/SKILL.md` 都是被读方，由 Claude Code CLI 在会话内载入。
2. 写什么：本目录内的文件不写出任何内容。`worktrees/` 下的目录由 `git worktree add`
   创建、由 `git worktree remove` 移除，不是本目录内文件的产物。
3. 被谁 import：无。本目录不含 `.py` 或其他可执行源文件，不存在 import 关系。
4. 被谁引用（检索结果，非印象）：`ARCHITECTURE.md` 第 4 行「流程见 `.claude/skills/`」、
   §1 顶层表的 `.claude/skills/` 行、§4 的 Skills 索引表；`CLAUDE.md` §2.1.2
   （把 `.claude/skills/` 下各 SKILL.md 定为技术文档的文体范本）、§2.3 语言分工表、
   §4.3 豁免表、§四 目录树。`settings.local.json` 无任何引用点，见 §2。

## 5. 产出与清理

| 对象 | 性质 | 清理约定 |
|---|---|---|
| `settings.local.json` | 长期配置，已入库（`git ls-files .claude` 命中） | 必须保留 |
| `README.md`（本文件）与 `skills/README.md` | 长期文档，2026-08-22 新建，随下次提交入库 | 必须保留 |
| `skills/` 下 10 份 `SKILL.md` | 长期文档，已入库（`git ls-files .claude` 命中 10 条） | 必须保留 |
| `worktrees/<名称>/` | 运行产物。`.gitignore` 第 34 行 `.claude/worktrees/` 已排除整棵树，不入库 | 对应分支的任务结束、分支已合并或已废弃后移除。移除必须走 `git worktree remove`，因为每个工作树在主仓 `.git/worktrees/<名称>/` 另有一份登记（本机实测：`.claude/worktrees/skills-language-standards-659ab4/.git` 的内容是一行 `gitdir: /Users/hb/Desktop/quant/.git/worktrees/skills-language-standards-659ab4`），由 git 一并清理 |

## 6. 变更记录

- 2026-08-22 建立本文件，登记现有文件。
