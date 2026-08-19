"""The single entry point for reading credentials (CLAUDE.md section 3.2).

Rules:
    - Credentials may only come from environment variables or the secrets/
      directory, which is git-ignored.
    - Business code always goes through this module. Reading credential files or
      environment variables directly elsewhere is prohibited.
    - Anything that could carry a credential into a log, an exception or a report
      must be passed through mask() first.

Public functions:
    get_secret(name, required=True)  Read a credential by name
    mask(value)                      Redact a value, keeping first and last four
"""

from __future__ import annotations

__all__ = ["get_secret", "mask"]

import os
import stat

from common.paths import DIR_SECRETS

_MIN_MASK_LEN = 12


def mask(value: str | None) -> str:
    """Redact a credential for display, keeping only the first and last four characters.

    Values shorter than the threshold are fully obscured, since keeping eight of
    ten characters would not redact anything meaningful.
    """
    if not value:
        return "<empty>"
    if len(value) < _MIN_MASK_LEN:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def get_secret(name: str, *, required: bool = True) -> str | None:
    """Read a credential by name.

    Resolution order:
        1. Environment variable QUANT_SECRET_<NAME in upper case>
        2. secrets/<name>.txt, first line, stripped

    Args:
        name: Credential name in lower snake case, e.g. "okx_api_key".
        required: Whether a missing credential should raise.

    Returns:
        The credential, or None when it is absent and required is False.

    Raises:
        FileNotFoundError: Required but neither source held the credential.
        PermissionError: The credential file is readable by group or others.
    """
    env_key = f"QUANT_SECRET_{name.upper()}"
    if env_key in os.environ:
        return os.environ[env_key].strip()

    path = DIR_SECRETS / f"{name}.txt"
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"credential file {path} is too permissive (mode={mode:o}); run chmod 600"
            )
        return path.read_text(encoding="utf-8").splitlines()[0].strip()

    if required:
        raise FileNotFoundError(
            f"credential {name!r} not found: no {env_key} in the environment and no {path}"
        )
    return None
