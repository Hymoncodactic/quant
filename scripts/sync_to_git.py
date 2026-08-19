#!/usr/bin/env python3
"""每日手动执行的 git 同步器：暂存全部改动、扫描密钥、提交、推送。

功能索引（改功能先看这里定位函数）：

    配置常量           EXPECTED_REMOTE / SECRET_PATH_PATTERNS / ...
    命令行入口         main()
    参数解析           build_arg_parser()
    仓库定位与校验     find_repo_root() / ensure_repo_initialised() / verify_remote()
    提交身份校验       verify_identity()
    暂存               stage_all()
    密钥闸（核心）     scan_staged_for_secrets() / iter_staged_files()
                       / classify_path() / scan_blob_text() / looks_like_secret_value()
    体积闸             scan_staged_for_large_files()
    提交与推送         make_commit() / push()
    git 调用封装       git() / git_ok()

设计约束（对应 CLAUDE.md）：
    §3.1  本脚本只做版本同步，绝不触碰交易所接口，不下任何委托。
    §3.2  密钥闸是硬闸：命中即中止，绝不推送。.gitignore 是第一道，
          本脚本的扫描是第二道，两道都过才允许 push。
    §3.4  不改动 data/ 下任何文件，只读 git 索引。

退出码：
    0  成功（含「无改动」）
    1  用法/环境错误（未装 git、身份未配、remote 不符、推送被拒等）
    2  密钥闸或体积闸命中 —— 已中止，未提交未推送
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

__all__ = [
    "main",
    "scan_staged_for_secrets",
    "scan_staged_for_large_files",
    "looks_like_secret_value",
]


# --------------------------------------------------------------------------
# 配置常量
# --------------------------------------------------------------------------

# 唯一允许推送的目标。actual origin 与此不符即中止，防止推错仓库。
EXPECTED_REMOTE = "https://github.com/Hymoncodactic/quant.git"
DEFAULT_BRANCH = "main"

# 单文件体积上限（字节）。data/ 已被 .gitignore 排除，此闸只防误配。
MAX_BLOB_BYTES = 10 * 1024 * 1024

# 路径级密钥闸：这些路径即便逃过 .gitignore 也一律拒绝入库。
SECRET_PATH_PATTERNS = (
    "secrets/*",
    "secrets/**",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.live.yaml",
    "*.live.yml",
    "*.paper.yaml",
    "*.paper.yml",
    "id_rsa",
    "id_ed25519",
    "*/id_rsa",
    "*/id_ed25519",
    "*.pypirc",
    "*credentials.json",
)

# 内容级密钥闸（硬命中，无歧义）：私钥块与各家已知令牌前缀。
HARD_CONTENT_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥块"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub 令牌"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"), "GitHub 细粒度 PAT"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key ID"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS 临时 Access Key ID"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack 令牌"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI 风格密钥"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic 密钥"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API 密钥"),
    (re.compile(r"://[^/\s:@]+:[^/\s:@]{6,}@"), "URL 内嵌账号密码"),
)

# 内容级密钥闸（软命中）：字段名像密钥 + 取值也像密钥，两者同时成立才拦。
# 注意：两端**不加** \b。`OKX_API_KEY` 的下划线本身是词字符，
# 加了 \b 会漏掉 `PREFIX_API_KEY` 这类最常见的命名（已由负例检验证实）。
SECRET_FIELD_RE = re.compile(
    r"(?i)("
    r"api[_-]?key|api[_-]?secret|secret[_-]?key|client[_-]?secret|"
    r"passphrase|password|passwd|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|bearer|"
    r"private[_-]?key|credential|secret|token|key"
    r")"
)
# 抓取 `field: value` / `field = value` / `field="value"` 右侧取值
ASSIGN_VALUE_RE = re.compile(r"""(?:[:=]\s*)(["']?)([^\s"'#,;)]{8,})\1""")

# 明显是占位符 / 引用而非真值的取值，直接放行。
PLACEHOLDER_RE = re.compile(
    r"(?i)^("
    r"\$\{.*\}|\$[A-Z_]+|<.*>|\{\{.*\}\}|"
    r"null|none|nil|true|false|~|-|0+|"
    r".*(?:change[_-]?me|your[_-]?|example|placeholder|replace|todo|tbd|xxx+|\.\.\.).*"
    r")$"
)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]{32,}$")


# --------------------------------------------------------------------------
# git 调用封装
# --------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True) -> str:
    """在 repo 下执行 git 命令，返回 stdout（已 strip）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（退出码 {proc.returncode}）：\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    """执行 git 命令，只关心是否成功。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------
# 仓库定位与校验
# --------------------------------------------------------------------------

def find_repo_root() -> Path:
    """仓库根 = 本脚本所在 scripts/ 的上一级，不依赖当前工作目录。"""
    return Path(__file__).resolve().parent.parent


def ensure_repo_initialised(repo: Path, branch: str) -> None:
    """无 .git 则 git init 并挂上 origin；已有则补齐缺失的 origin。"""
    if not (repo / ".git").exists():
        print(f"[init] {repo} 尚未纳入 git，执行 git init -b {branch}")
        git(repo, "init", "-b", branch)

    remotes = git(repo, "remote", check=False)
    if "origin" not in remotes.split():
        print(f"[init] 添加 origin -> {EXPECTED_REMOTE}")
        git(repo, "remote", "add", "origin", EXPECTED_REMOTE)


def verify_remote(repo: Path) -> None:
    """origin 必须正好是 EXPECTED_REMOTE，否则中止（防推错仓库）。"""
    actual = git(repo, "remote", "get-url", "origin")
    if actual.rstrip("/") not in (
        EXPECTED_REMOTE.rstrip("/"),
        EXPECTED_REMOTE.rstrip("/").removesuffix(".git"),
    ):
        raise RuntimeError(
            f"origin 不是预期仓库，已中止。\n"
            f"  预期：{EXPECTED_REMOTE}\n"
            f"  实际：{actual}\n"
            f"如确需改目标，修改本脚本顶部的 EXPECTED_REMOTE。"
        )


def verify_identity(repo: Path) -> None:
    """提交身份必须已配置。不猜、不代填 —— 作者信息会永久写进公开历史。"""
    name = git(repo, "config", "user.name", check=False)
    email = git(repo, "config", "user.email", check=False)
    if not name or not email:
        raise RuntimeError(
            "git 提交身份未配置，已中止（作者信息会永久留在提交历史里，本脚本不代为猜测）。\n"
            "在本仓库内配置，例如：\n"
            "  git -C %s config user.name  \"Hymoncodactic\"\n"
            "  git -C %s config user.email \"Hymoncodactic@users.noreply.github.com\""
            % (repo, repo)
        )


def verify_clean_state(repo: Path) -> None:
    """拒绝在 merge / rebase / cherry-pick 中途运行。"""
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    for marker, what in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
    ):
        if (git_dir / marker).exists():
            raise RuntimeError(f"仓库正处于 {what} 中途，请先收尾再同步。")


# --------------------------------------------------------------------------
# 暂存
# --------------------------------------------------------------------------

def stage_all(repo: Path) -> list[tuple[str, str]]:
    """暂存全部改动（.gitignore 生效），返回 [(状态码, 路径)]。"""
    git(repo, "add", "-A")
    raw = git(repo, "diff", "--cached", "--name-status", "-z", check=False)
    return _parse_name_status_z(raw)


def _parse_name_status_z(raw: str) -> list[tuple[str, str]]:
    """解析 `--name-status -z` 输出。R/C 状态多占一个字段（旧名+新名）。"""
    fields = [f for f in raw.split("\0") if f != ""]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[0] in ("R", "C"):
            out.append((status, fields[i + 2]))   # 记新名
            i += 3
        else:
            out.append((status, fields[i + 1]))
            i += 2
    return out


def iter_staged_files(repo: Path, staged: list[tuple[str, str]]):
    """产出 (path, blob_bytes)，跳过删除项。"""
    for status, path in staged:
        if status.startswith("D"):
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f":{path}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            continue
        yield path, proc.stdout


# --------------------------------------------------------------------------
# 密钥闸
# --------------------------------------------------------------------------

def classify_path(path: str) -> str | None:
    """路径命中密钥模式则返回命中的模式，否则 None。"""
    for pat in SECRET_PATH_PATTERNS:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, "*/" + pat):
            return pat
    return None


def looks_like_secret_value(value: str) -> bool:
    """取值是否「像真密钥」。

    判定：UUID、长十六进制，或长度 >=16 且字符类别 >=3
    （小写 / 大写 / 数字 / +/=- 符号）。
    这条把 `secret_name: trading212_api_key` 这类**引用名**排除在外
    —— 它只有小写+数字两类，不算密钥。
    """
    if PLACEHOLDER_RE.match(value):
        return False
    if UUID_RE.match(value) or HEX_RE.match(value):
        return True
    if len(value) < 16:
        return False
    classes = sum([
        bool(re.search(r"[a-z]", value)),
        bool(re.search(r"[A-Z]", value)),
        bool(re.search(r"[0-9]", value)),
        bool(re.search(r"[+/=~-]", value)),
    ])
    return classes >= 3


def scan_blob_text(path: str, text: str) -> list[str]:
    """扫一份文本，返回命中说明列表（空 = 干净）。"""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:            # 超长行（压缩/内联资源）不逐字扫
            line = line[:4000]
        for pattern, label in HARD_CONTENT_PATTERNS:
            if pattern.search(line):
                hits.append(f"{path}:{lineno}  [{label}]")
        if SECRET_FIELD_RE.search(line):
            for _, value in ASSIGN_VALUE_RE.findall(line):
                if looks_like_secret_value(value):
                    masked = value[:4] + "…" + value[-2:]
                    hits.append(f"{path}:{lineno}  [疑似密钥取值 {masked}]")
                    break
    return hits


def scan_staged_for_secrets(repo: Path, staged: list[tuple[str, str]]) -> list[str]:
    """对全部暂存内容跑密钥闸，返回命中清单（空 = 放行）。"""
    hits: list[str] = []
    for path, blob in iter_staged_files(repo, staged):
        pat = classify_path(path)
        if pat:
            hits.append(f"{path}  [路径命中密钥模式 {pat}]")
            continue                     # 路径已拦，不必再读内容
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue                     # 二进制不扫文本
        hits.extend(scan_blob_text(path, text))
    return hits


def scan_staged_for_large_files(repo: Path, staged: list[tuple[str, str]]) -> list[str]:
    """单文件体积闸。"""
    hits = []
    for path, blob in iter_staged_files(repo, staged):
        if len(blob) > MAX_BLOB_BYTES:
            hits.append(f"{path}  [{len(blob) / 1024 / 1024:.1f} MB，"
                        f"超过上限 {MAX_BLOB_BYTES // 1024 // 1024} MB]")
    return hits


# --------------------------------------------------------------------------
# 提交与推送
# --------------------------------------------------------------------------

def make_commit(repo: Path, message: str | None) -> str:
    """提交暂存内容，返回短哈希。"""
    msg = message or f"sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    git(repo, "commit", "-m", msg)
    return git(repo, "rev-parse", "--short", "HEAD")


def push(repo: Path, branch: str, overwrite: bool) -> None:
    """推送到 origin。overwrite=True 时用 --force 覆盖远端历史。"""
    args = ["push"]
    if overwrite:
        args.append("--force")
    args += ["--set-upstream", "origin", branch]

    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            "推送被拒。若远端有本地没有的提交（例如在网页端改过），"
            "确认可以丢弃远端内容后，用 --overwrite-remote 重跑。"
        )


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把本项目当前状态同步到 GitHub（.gitignore 中的内容不同步）。",
    )
    p.add_argument("-m", "--message", help="提交信息；缺省用 'sync: 时间戳'")
    p.add_argument("--branch", default=DEFAULT_BRANCH, help=f"分支名（默认 {DEFAULT_BRANCH}）")
    p.add_argument("--dry-run", action="store_true",
                   help="只暂存并跑闸门与清单，不提交不推送")
    p.add_argument("--overwrite-remote", action="store_true",
                   help="用 --force 覆盖远端历史。远端上不在本地的提交会永久丢失")
    p.add_argument("--yes", action="store_true",
                   help="跳过 --overwrite-remote 的交互确认（用于非交互执行）")
    p.add_argument("--i-have-verified-no-secrets", action="store_true",
                   help="密钥闸误报时的显式放行开关。放行前必须逐条人工核对命中项")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo = find_repo_root()

    try:
        ensure_repo_initialised(repo, args.branch)
        verify_remote(repo)
        verify_identity(repo)
        verify_clean_state(repo)
    except RuntimeError as exc:
        print(f"\n[中止] {exc}", file=sys.stderr)
        return 1

    print(f"[repo] {repo}")
    print(f"[remote] {git(repo, 'remote', 'get-url', 'origin')}")

    staged = stage_all(repo)
    if not staged:
        print("[结果] 无改动，无需提交。")
        return 0

    print(f"[暂存] {len(staged)} 项：")
    for status, path in staged:
        print(f"    {status:<3} {path}")

    # ---- 闸门 ----
    big = scan_staged_for_large_files(repo, staged)
    if big:
        print("\n[中止] 体积闸命中：", file=sys.stderr)
        for h in big:
            print(f"    {h}", file=sys.stderr)
        print("已保留暂存区，未提交未推送。", file=sys.stderr)
        return 2

    hits = scan_staged_for_secrets(repo, staged)
    if hits:
        print(f"\n[密钥闸] 命中 {len(hits)} 处：", file=sys.stderr)
        for h in hits:
            print(f"    {h}", file=sys.stderr)
        if not args.i_have_verified_no_secrets:
            print(
                "\n[中止] 未提交、未推送，暂存区已保留。\n"
                "  逐条核对：真密钥 -> 移入 secrets/ 或环境变量，并把路径加进 .gitignore；\n"
                "            确属误报 -> 用 --i-have-verified-no-secrets 重跑。",
                file=sys.stderr,
            )
            return 2
        print("\n[警告] 已按 --i-have-verified-no-secrets 放行上述命中，继续推送。",
              file=sys.stderr)
    else:
        print("\n[密钥闸] 通过，无命中。")

    if args.dry_run:
        print("[dry-run] 到此为止，未提交未推送。")
        return 0

    if args.overwrite_remote and not args.yes:
        print(f"\n[警告] --overwrite-remote 会用本地历史强制覆盖 "
              f"origin/{args.branch}，远端上不在本地的提交将永久丢失。")
        if input("确认覆盖？输入 yes 继续：").strip() != "yes":
            print("[中止] 已取消，未提交未推送。")
            return 1

    short = make_commit(repo, args.message)
    print(f"\n[提交] {short}")
    push(repo, args.branch, args.overwrite_remote)
    print(f"[完成] 已推送到 {EXPECTED_REMOTE} 的 {args.branch} 分支。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
