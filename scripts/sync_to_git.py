#!/usr/bin/env python3
"""Daily manual git synchroniser: stage every change, scan for credentials, commit, push.

Function index (start here when changing behaviour):

    Configuration constants   EXPECTED_REMOTE / SECRET_PATH_PATTERNS / ...
    Command-line entry        main()
    Argument parsing          build_arg_parser()
    Repository location       find_repo_root() / ensure_repo_initialised() / verify_remote()
    Commit identity check     verify_identity()
    Staging                   stage_all()
    Credential gate (core)    scan_staged_for_secrets() / iter_staged_files()
                              / classify_path() / scan_blob_text() / looks_like_secret_value()
    Size gate                 scan_staged_for_large_files()
    Commit and push           make_commit() / push()
    git call wrappers         git() / git_ok()

Design constraints (see CLAUDE.md):
    3.1  This script only synchronises version control. It never touches a venue
         API and never submits an order.
    3.2  The credential gate is a hard gate: a hit aborts the run and nothing is
         pushed. .gitignore is the first line of defence and this scan is the
         second; both must pass before a push happens.
    3.4  Nothing under data/ is modified. Only the git index is read.

Exit codes:
    0  Success, including the "no changes" case
    1  Usage or environment error (git missing, identity unset, wrong remote,
       push rejected)
    2  Credential gate or size gate hit; aborted without committing or pushing
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
# Configuration constants
# --------------------------------------------------------------------------

# The only permitted push target. An actual origin that differs from this aborts
# the run, which is what stops the project being pushed to the wrong repository.
EXPECTED_REMOTE = "https://github.com/Hymoncodactic/quant.git"
DEFAULT_BRANCH = "main"

# Per-file size ceiling, in bytes. data/ is already excluded by .gitignore, so
# this gate only guards against a misconfiguration.
MAX_BLOB_BYTES = 10 * 1024 * 1024

# Path-level credential gate: these paths are refused even if they slip past
# .gitignore.
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

# Content-level credential gate, hard hits with no ambiguity: private key blocks
# and the known token prefixes of individual vendors.
HARD_CONTENT_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key block"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"), "GitHub fine-grained PAT"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key ID"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS temporary Access Key ID"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    # The label deliberately avoids the words this module's own SECRET_FIELD_RE
    # matches: this line contains "://", so a label carrying one of them would
    # make the file trip its own soft rule on every run.
    (re.compile(r"://[^/\s:@]+:[^/\s:@]{6,}@"), "login details inline in a URL"),
)

# Content-level credential gate, soft hits: the field name has to look like a
# credential and the value has to look like one too before anything is blocked.
# Note: neither end carries \b. The underscore in `OKX_API_KEY` is itself a word
# character, so a \b anchor would miss `PREFIX_API_KEY`, the most common naming
# of all. Confirmed by a negative test.
SECRET_FIELD_RE = re.compile(
    r"(?i)("
    r"api[_-]?key|api[_-]?secret|secret[_-]?key|client[_-]?secret|"
    r"passphrase|password|passwd|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|bearer|"
    r"private[_-]?key|credential|secret|token|key"
    r")"
)
# Captures the right-hand side of `field: value` / `field = value` / `field="value"`
ASSIGN_VALUE_RE = re.compile(r"""(?:[:=]\s*)(["']?)([^\s"'#,;)]{8,})\1""")

# Values that are plainly placeholders or references rather than real secrets
# are let straight through.
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
# git call wrappers
# --------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command inside repo and return its stdout, stripped."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit code {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    """Run a git command and report only whether it succeeded."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------
# Repository location and checks
# --------------------------------------------------------------------------

def find_repo_root() -> Path:
    """Repository root: the parent of the scripts/ directory holding this file, not the cwd."""
    return Path(__file__).resolve().parent.parent


def ensure_repo_initialised(repo: Path, branch: str) -> None:
    """Run git init and attach origin when .git is absent; otherwise add a missing origin."""
    if not (repo / ".git").exists():
        print(f"[init] {repo} is not under git yet, running git init -b {branch}")
        git(repo, "init", "-b", branch)

    remotes = git(repo, "remote", check=False)
    if "origin" not in remotes.split():
        print(f"[init] adding origin -> {EXPECTED_REMOTE}")
        git(repo, "remote", "add", "origin", EXPECTED_REMOTE)


def verify_remote(repo: Path) -> None:
    """origin must be exactly EXPECTED_REMOTE; anything else aborts (wrong-repository guard)."""
    actual = git(repo, "remote", "get-url", "origin")
    if actual.rstrip("/") not in (
        EXPECTED_REMOTE.rstrip("/"),
        EXPECTED_REMOTE.rstrip("/").removesuffix(".git"),
    ):
        raise RuntimeError(
            f"origin is not the expected repository, aborted.\n"
            f"  expected: {EXPECTED_REMOTE}\n"
            f"  actual:   {actual}\n"
            f"To change the target, edit EXPECTED_REMOTE at the top of this script."
        )


def verify_identity(repo: Path) -> None:
    """Require a configured commit identity.

    The identity is never guessed and never filled in automatically: author
    details are written permanently into a public history.
    """
    name = git(repo, "config", "user.name", check=False)
    email = git(repo, "config", "user.email", check=False)
    if not name or not email:
        raise RuntimeError(
            "the git commit identity is not configured, aborted (author details stay in the "
            "commit history permanently, so this script does not guess them).\n"
            "Configure it inside this repository, for example:\n"
            "  git -C %s config user.name  \"Hymoncodactic\"\n"
            "  git -C %s config user.email \"Hymoncodactic@users.noreply.github.com\""
            % (repo, repo)
        )


def verify_clean_state(repo: Path) -> None:
    """Refuse to run part-way through a merge, rebase or cherry-pick."""
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    for marker, what in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
    ):
        if (git_dir / marker).exists():
            raise RuntimeError(f"the repository is part-way through a {what}; finish that first.")


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------

def stage_all(repo: Path) -> list[tuple[str, str]]:
    """Stage every change (.gitignore applies) and return [(status code, path)]."""
    git(repo, "add", "-A")
    raw = git(repo, "diff", "--cached", "--name-status", "-z", check=False)
    return _parse_name_status_z(raw)


def _parse_name_status_z(raw: str) -> list[tuple[str, str]]:
    """Parse `--name-status -z` output. R and C take one extra field (old name plus new name)."""
    fields = [f for f in raw.split("\0") if f != ""]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[0] in ("R", "C"):
            out.append((status, fields[i + 2]))   # record the new name
            i += 3
        else:
            out.append((status, fields[i + 1]))
            i += 2
    return out


def iter_staged_files(repo: Path, staged: list[tuple[str, str]]):
    """Yield (path, blob_bytes), skipping deletions."""
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
# Credential gate
# --------------------------------------------------------------------------

def classify_path(path: str) -> str | None:
    """Return the credential path pattern the path matches, or None."""
    for pat in SECRET_PATH_PATTERNS:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, "*/" + pat):
            return pat
    return None


def looks_like_secret_value(value: str) -> bool:
    """Report whether a value looks like a real credential.

    A value qualifies when it is a UUID, a long hexadecimal string, or at least
    16 characters long with at least three character classes present (lower
    case / upper case / digits / the +/=- symbols).

    The character-class rule is what keeps a reference name such as
    `secret_name: trading212_api_key` out of the results: it carries lower case
    and digits only, two classes, so it does not count as a credential.
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
    """Scan one text blob and return the hit descriptions (an empty list means clean)."""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:            # minified or inline assets: scan a prefix only
            line = line[:4000]
        for pattern, label in HARD_CONTENT_PATTERNS:
            if pattern.search(line):
                hits.append(f"{path}:{lineno}  [{label}]")
        if SECRET_FIELD_RE.search(line):
            for _, value in ASSIGN_VALUE_RE.findall(line):
                if looks_like_secret_value(value):
                    masked = value[:4] + "…" + value[-2:]
                    hits.append(f"{path}:{lineno}  [suspected credential value {masked}]")
                    break
    return hits


def scan_staged_for_secrets(repo: Path, staged: list[tuple[str, str]]) -> list[str]:
    """Run the credential gate over everything staged; an empty list means the gate passes."""
    hits: list[str] = []
    for path, blob in iter_staged_files(repo, staged):
        pat = classify_path(path)
        if pat:
            hits.append(f"{path}  [path matches credential pattern {pat}]")
            continue                     # already blocked by path, no need to read the content
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue                     # binary content is not scanned as text
        hits.extend(scan_blob_text(path, text))
    return hits


def scan_staged_for_large_files(repo: Path, staged: list[tuple[str, str]]) -> list[str]:
    """Per-file size gate."""
    hits = []
    for path, blob in iter_staged_files(repo, staged):
        if len(blob) > MAX_BLOB_BYTES:
            hits.append(f"{path}  [{len(blob) / 1024 / 1024:.1f} MB, over the "
                        f"{MAX_BLOB_BYTES // 1024 // 1024} MB ceiling]")
    return hits


# --------------------------------------------------------------------------
# Commit and push
# --------------------------------------------------------------------------

def make_commit(repo: Path, message: str | None) -> str:
    """Commit what is staged and return the short hash."""
    msg = message or f"sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    git(repo, "commit", "-m", msg)
    return git(repo, "rev-parse", "--short", "HEAD")


def push(repo: Path, branch: str, overwrite: bool) -> None:
    """Push to origin. overwrite=True uses --force and replaces the remote history."""
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
            "the push was rejected. If the remote holds commits the local repository does "
            "not (an edit made through the web interface, for example), rerun with "
            "--overwrite-remote once discarding the remote content is acceptable."
        )


# --------------------------------------------------------------------------
# Command-line entry point
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Synchronise the current project state to GitHub "
                    "(anything matched by .gitignore is excluded).",
    )
    p.add_argument("-m", "--message", help="commit message; defaults to 'sync: <timestamp>'")
    p.add_argument("--branch", default=DEFAULT_BRANCH,
                   help=f"branch name (default {DEFAULT_BRANCH})")
    p.add_argument("--dry-run", action="store_true",
                   help="stage, run the gates and print the list only; no commit, no push")
    p.add_argument("--overwrite-remote", action="store_true",
                   help="replace the remote history with --force; remote commits that are "
                        "absent locally are lost permanently")
    p.add_argument("--yes", action="store_true",
                   help="skip the interactive confirmation for --overwrite-remote "
                        "(for non-interactive runs)")
    p.add_argument("--i-have-verified-no-secrets", action="store_true",
                   help="explicit override for a false positive from the credential gate; "
                        "every hit must be checked by hand before it is used")
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
        print(f"\n[abort] {exc}", file=sys.stderr)
        return 1

    print(f"[repo] {repo}")
    print(f"[remote] {git(repo, 'remote', 'get-url', 'origin')}")

    staged = stage_all(repo)
    if not staged:
        print("[result] no changes, nothing to commit.")
        return 0

    print(f"[staged] {len(staged)} entries:")
    for status, path in staged:
        print(f"    {status:<3} {path}")

    # ---- Gates ----
    big = scan_staged_for_large_files(repo, staged)
    if big:
        print("\n[abort] size gate hit:", file=sys.stderr)
        for h in big:
            print(f"    {h}", file=sys.stderr)
        print("The index has been left staged. Nothing was committed or pushed.", file=sys.stderr)
        return 2

    hits = scan_staged_for_secrets(repo, staged)
    if hits:
        print(f"\n[secret-gate] {len(hits)} hits:", file=sys.stderr)
        for h in hits:
            print(f"    {h}", file=sys.stderr)
        if not args.i_have_verified_no_secrets:
            print(
                "\n[abort] nothing committed, nothing pushed; the index has been left staged.\n"
                "  Check every hit: a real credential -> move it into secrets/ or an\n"
                "  environment variable, then add the path to .gitignore;\n"
                "  a confirmed false positive -> rerun with --i-have-verified-no-secrets.",
                file=sys.stderr,
            )
            return 2
        print("\n[warning] the hits above were let through by --i-have-verified-no-secrets, "
              "continuing to push.",
              file=sys.stderr)
    else:
        print("\n[secret-gate] passed, no hits.")

    if args.dry_run:
        print("[dry-run] stopping here, nothing was committed or pushed.")
        return 0

    if args.overwrite_remote and not args.yes:
        print(f"\n[warning] --overwrite-remote force-replaces origin/{args.branch} with the "
              f"local history; remote commits that are absent locally are lost permanently.")
        if input("Confirm the overwrite? Type yes to continue: ").strip() != "yes":
            print("[abort] cancelled, nothing was committed or pushed.")
            return 1

    short = make_commit(repo, args.message)
    print(f"\n[commit] {short}")
    push(repo, args.branch, args.overwrite_remote)
    print(f"[done] pushed to {EXPECTED_REMOTE}, branch {args.branch}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
