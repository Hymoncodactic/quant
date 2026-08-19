"""密钥读取的唯一入口（CLAUDE.md §4.2）。

规则：
    - 密钥只允许来自环境变量或 secrets/ 目录，且 secrets/ 已 gitignore。
    - 业务代码一律经本模块取用，禁止自行读文件或读环境变量。
    - 任何日志、异常、报告中出现密钥，一律先过 mask()。

对外函数：
    get_secret(name, required=True)  按名取密钥（环境变量优先，其次 secrets/<name>.txt）
    mask(value)                      脱敏，只留前 4 后 4 位
"""

from __future__ import annotations

__all__ = ["get_secret", "mask"]

import os
import stat

from common.paths import DIR_SECRETS

_MIN_MASK_LEN = 12

def mask(value: str | None) -> str:
    """脱敏：只保留前 4 后 4 位。短于阈值的一律全遮。"""
    if not value:
        return "<empty>"
    if len(value) < _MIN_MASK_LEN:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"

def get_secret(name: str, *, required: bool = True) -> str | None:
    """按名取密钥。

    查找顺序：
        1. 环境变量 QUANT_SECRET_<NAME 大写>
        2. secrets/<name>.txt（首行，strip）

    Args:
        name: 密钥名，小写下划线，如 "okx_api_key"、"trading212_api_key"。
        required: 取不到时是否抛异常。

    Returns:
        密钥字符串；未找到且 required=False 时返回 None。

    Raises:
        FileNotFoundError: required=True 且未找到。
        PermissionError: 密钥文件权限宽于 600。
    """
    env_key = f"QUANT_SECRET_{name.upper()}"
    if env_key in os.environ:
        return os.environ[env_key].strip()

    path = DIR_SECRETS / f"{name}.txt"
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"密钥文件权限过宽 {path} (mode={mode:o})，请执行 chmod 600"
            )
        return path.read_text(encoding="utf-8").splitlines()[0].strip()

    if required:
        raise FileNotFoundError(
            f"未找到密钥 {name!r}：既无环境变量 {env_key}，也无 {path}"
        )
    return None
