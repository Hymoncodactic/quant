"""Configuration loading. QUANT_ENV selects paper or live; paper is the default
(CLAUDE.md section 3.3).

Configuration lives at <venue_dir>/config/<venue>.<env>.yaml and never holds
credentials.

Public functions:
    current_env()                 Active environment, "paper" unless QUANT_ENV says otherwise
    load_config(venue, env=None)  Load venue configuration, validating the live flag
    assert_live_allowed(cfg)      Final guard before submitting a real order
"""

from __future__ import annotations

__all__ = ["current_env", "load_config", "assert_live_allowed",
           "ENV_VAR", "ENV_PAPER", "ENV_LIVE", "VALID_ENVS"]

import os
from typing import Any

import yaml

from common.paths import config_dir

ENV_VAR = "QUANT_ENV"
ENV_PAPER = "paper"
ENV_LIVE = "live"
VALID_ENVS = (ENV_PAPER, ENV_LIVE)


def current_env() -> str:
    """Return the active environment.

    An unset QUANT_ENV resolves to paper. The default must never be live: an
    accidentally empty environment has to fail safe.
    """
    env = os.environ.get(ENV_VAR, ENV_PAPER).strip().lower()
    if env not in VALID_ENVS:
        raise ValueError(f"{ENV_VAR}={env!r} is not recognized, expected one of {VALID_ENVS}")
    return env


def load_config(venue: str, env: str | None = None) -> dict[str, Any]:
    """Load the configuration for one venue.

    Args:
        venue: Venue slug, "okx" or "t212".
        env: Override the environment; None resolves via current_env().

    Returns:
        The configuration mapping, with the resolved environment and source path
        recorded under the _env and _path keys.

    Raises:
        FileNotFoundError: The configuration file is absent.
        ValueError: Loading as live but the file lacks its explicit `live: true` flag.
    """
    env = env or current_env()
    path = config_dir(venue) / f"{venue}.{env}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"no configuration at {path}; copy {venue}.example.yaml alongside it and fill it in"
        )
    cfg: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg["_env"] = env
    cfg["_path"] = str(path)

    if env == ENV_LIVE and cfg.get("live") is not True:
        raise ValueError(f"{path} lacks `live: true`; refusing to load it as a live configuration")
    return cfg


def assert_live_allowed(cfg: dict[str, Any]) -> None:
    """Guard the boundary between simulation and real money (CLAUDE.md section 3.3).

    The execution layer calls this immediately before any order-submitting request.
    Two independent conditions must both hold, so that neither a stray environment
    variable nor an edited configuration file is sufficient on its own.
    """
    if cfg.get("_env") != ENV_LIVE:
        raise RuntimeError(f"environment is {cfg.get('_env')!r}; real orders are not permitted")
    if cfg.get("live") is not True:
        raise RuntimeError(f"{cfg.get('_path')} lacks `live: true`; real orders are not permitted")
