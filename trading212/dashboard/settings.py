"""Pre-trading configuration: read, validate and write, in plain fields.

Responsibility: expose the handful of numbers a person must set before the
strategy may trade as a flat list of fields, check them, and write them back
into the venue configuration file. Validation returns machine-readable
problem codes rather than sentences, so the interface can render them in the
user's language and this module can stay free of presentation.

The gate this feeds is real: trading212/execution/risk_gate.py fails closed
when any limit is missing or zero, so "not configured" already means "no
orders". This module exists to make that state visible and fixable before a
session rather than discoverable as a refusal during one.

Out of scope: enforcing the limits, which belongs to
trading212/execution/risk_gate.py; creating the strategy's book, which
belongs to trading212/execution/session_cycle.py init_ledger(); the wording
of any message, which belongs to trading212/dashboard/assets/labels.json.

Public functions:
    describe(cfg, ledger_ready)   Field list with current values.
    validate(values)              Problem codes for a proposed set.
    apply(env, values)            Write validated values into the config file.
    config_path(env)              The file apply() writes.

Public constants:
    FIELDS   tuple  One entry per settable field: id, where it lives in the
                    configuration, its kind, and whether it must be positive.

Constants:
    MAX_FEE_BUFFER  float  0.2. A reserve above a fifth of the order value is
                           certainly a typo, not a fee estimate.

Inputs:
    trading212/config/t212.<env>.yaml
Outputs:
    trading212/config/t212.<env>.yaml

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["describe", "validate", "apply", "config_path", "FIELDS",
           "MAX_FEE_BUFFER"]

from pathlib import Path
from typing import Any

import yaml

from common.paths import config_dir

MAX_FEE_BUFFER = 0.2

# id, path inside the configuration, kind, must_be_positive
FIELDS: tuple[tuple[str, tuple[str, ...], str, bool], ...] = (
    ("max_order_notional_gbp", ("risk", "max_order_notional_gbp"), "money", True),
    ("max_gross_notional_gbp", ("risk", "max_gross_notional_gbp"), "money", True),
    ("max_daily_orders", ("risk", "max_daily_orders"), "count", True),
    ("min_order_value_gbp", ("risk", "min_order_value_gbp"), "money", True),
    ("fee_buffer", ("risk", "fee_buffer"), "ratio", False),
    ("submit_lead_sec", ("execution", "submit_lead_sec"), "count", True),
    ("dry_run", ("execution", "dry_run"), "switch", False),
)


def config_path(env: str) -> Path:
    """The venue configuration file for one environment."""
    return config_dir("t212") / f"t212.{env}.yaml"


def _dig(cfg: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = cfg
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def describe(cfg: dict[str, Any], ledger_ready: bool) -> dict[str, Any]:
    """Current value and readiness of every settable field.

    ledger_ready says whether the strategy's book exists, which is the one
    prerequisite that is not a number in this file: money is allocated by
    creating the book, and that is deliberately a separate, one-time act.
    """
    fields = []
    for field_id, path, kind, positive in FIELDS:
        value = _dig(cfg, path)
        fields.append({"id": field_id, "kind": kind, "value": value,
                       "must_be_positive": positive,
                       "filled": _is_filled(value, kind, positive)})
    missing = [f["id"] for f in fields if not f["filled"]]
    return {"fields": fields, "missing": missing,
            "ledger_ready": ledger_ready,
            "ready": not missing and ledger_ready,
            "live_flag": bool(cfg.get("live")),
            "env": cfg.get("_env")}


def _is_filled(value: Any, kind: str, positive: bool) -> bool:
    if kind == "switch":
        return isinstance(value, bool)
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if positive:
        return number > 0
    return number >= 0


def validate(values: dict[str, Any]) -> list[dict[str, str]]:
    """Return one problem per bad field: {field, code}.

    Codes, all rendered by the interface: "missing" (absent or blank),
    "not_a_number", "must_be_positive", "must_not_be_negative",
    "fee_buffer_too_large", "not_a_switch".
    """
    problems: list[dict[str, str]] = []
    for field_id, _path, kind, positive in FIELDS:
        raw = values.get(field_id)
        if kind == "switch":
            if not isinstance(raw, bool):
                problems.append({"field": field_id, "code": "not_a_switch"})
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            problems.append({"field": field_id, "code": "missing"})
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            problems.append({"field": field_id, "code": "not_a_number"})
            continue
        if positive and number <= 0:
            problems.append({"field": field_id, "code": "must_be_positive"})
        elif not positive and number < 0:
            problems.append({"field": field_id, "code": "must_not_be_negative"})
        elif field_id == "fee_buffer" and number >= MAX_FEE_BUFFER:
            problems.append({"field": field_id, "code": "fee_buffer_too_large"})
    return problems


def apply(env: str, values: dict[str, Any]) -> dict[str, Any]:
    """Write validated values into the configuration file, preserving the rest.

    The file is rewritten from the parsed mapping, so comments in it are lost;
    the template trading212/config/t212.example.yaml keeps the commentary and
    is never written by this function.

    Raises:
        ValueError: validate() found problems. Nothing is written.
    """
    problems = validate(values)
    if problems:
        raise ValueError(f"refusing to write an invalid configuration: {problems}")
    path = config_path(env)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for field_id, field_path, kind, _positive in FIELDS:
        raw = values[field_id]
        if kind == "switch":
            value: Any = bool(raw)
        elif kind == "count":
            value = int(float(raw))
        else:
            value = float(raw)
        node = cfg
        for part in field_path[:-1]:
            node = node.setdefault(part, {})
        node[field_path[-1]] = value
    tmp = path.with_suffix(".writing")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)
    return {"written": str(path), "values": values}
