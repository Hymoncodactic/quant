"""Venue configuration loading and the paper/live boundary guard.

Responsibility: resolve the active environment from the QUANT_ENV environment
variable, load <venue_dir>/config/<venue>.<env>.yaml for one venue, and refuse to
treat a file as a live configuration unless it carries an explicit `live: true`
flag (CLAUDE.md section 3.3). assert_live_allowed() is the last assertion the
execution layer performs before an order-submitting request, and it requires two
independent conditions to hold, so that neither a stray environment variable nor
an edited configuration file is sufficient on its own.

Out of scope: credential reading, which belongs to common/secrets.py, because a
configuration file never holds a credential; the configuration directory layout,
which belongs to common/paths.py; the order submission itself and its dry-run
switch, which belong to <venue>/execution/.

Public functions:
    current_env()                 Return "paper" or "live"; an unset QUANT_ENV means paper.
    load_config(venue, env=None)  Load one venue's configuration, validating the live flag.
    assert_live_allowed(cfg)      Raise unless both live conditions hold.

Constants:
    ENV_VAR     str    Name of the environment variable, "QUANT_ENV".
                       Source: CLAUDE.md section 3.3.
    ENV_PAPER   str    Simulation slug, "paper". It is also the default, so an
                       empty environment fails safe. Source: CLAUDE.md section 3.3.
    ENV_LIVE    str    Real-money slug, "live". Source: CLAUDE.md section 3.3.
    VALID_ENVS  tuple  The only two accepted values; anything else raises ValueError.

Inputs:
    Environment variable QUANT_ENV.
    <venue_dir>/config/<venue>.<env>.yaml, located through common.paths.config_dir().
Outputs:
    None. Nothing is written. The resolved environment and the source path are
    returned inside the configuration mapping under the _env and _path keys.

Change log:
    2026-08-22  Header expanded to the six-section spec.
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
