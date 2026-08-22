"""Result persistence: the trades, equity and meta triplet of one run.

Responsibility: write deterministic, self-describing result files under
backtest/results/ (docs/backtest/framework/05_metrics_reporting.md section 4), and
derive the canonical file-name stem that carries the strategy name and version,
the arm, the window, the fee tier and the seed. Determinism is the point: no
wall-clock timestamp enters any file, so re-running an identical configuration
on one commit reproduces byte-identical parquet and JSON, which is what the
baseline byte-compare discipline of backtest-discipline section 7.2 depends on.
A lookahead-probe run is branded with a PROBE suffix so it can never be
mistaken for a reportable result.

Out of scope: computing anything. The frames come from engine.py and the
statistics from metrics.py. The quick-look chart belongs to report.py and is
deliberately excluded from the byte-identity guarantee.

Public functions:
    run_name(config)   Canonical file-name stem with the run parameters
                       embedded (quant-code-standards section 4.5.1).
    write_run(result, config, metrics, extra_meta, out_dir)
                       Write the triplet and return the written paths.

Constants: None.

Inputs:
    git rev-parse HEAD and git status --porcelain, run in ROOT by
    _code_version() to stamp the producing revision. Failure yields None
    instead of aborting the write.
Outputs:
    <out_dir>/<stem>.trades.parquet   One row per fill, sorted by step then
                                      order_id.
    <out_dir>/<stem>.equity.parquet   The per-step equity and occupancy record,
                                      sorted by step.
    <out_dir>/<stem>.meta.json        Configuration, metrics, run summary,
                                      order audit and code version.
    out_dir defaults to common.paths.DIR_BACKTEST_RESULTS, which is
    backtest/results/. Empty frames are skipped rather than written.

Change log:
    2026-08-22  Header expanded to the six-section spec.
    2026-08-22  Result stem carries fill-<timing> so same_close runs can never
                be mistaken for the conservative default.
"""

from __future__ import annotations

__all__ = ["run_name", "write_run"]

import json
import subprocess
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from backtest.engine.engine import RunResult
from backtest.engine.types import EngineConfig
from common.paths import DIR_BACKTEST_RESULTS, ROOT
from common.store import write_table


def _code_version() -> dict[str, Any]:
    """Git commit and dirty flag of the code that produced the result
    (plan 05 section 4.2). Stable per revision, so byte-identical reruns on
    one commit stay byte-identical; None when git is unavailable."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10,
                                check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                    cwd=ROOT, capture_output=True, text=True,
                                    timeout=10, check=True).stdout.strip())
        return {"git_commit": commit, "git_dirty": dirty}
    except Exception:
        return {"git_commit": None, "git_dirty": None}


def run_name(config: EngineConfig) -> str:
    """Canonical stem: strategy, version, arm, window, fee tier, fill
    timing, seed.

    Parameters go into the name (quant-code-standards section 4.5.1); a probe
    run is branded so it can never be mistaken for a reportable result.
    """
    version = config.strategy_version.replace(".", "_")
    stem = (f"{config.strategy_name}_v{version}_{config.arm}"
            f"_{config.start}_{config.end}"
            f"_fee-{config.fee_tier}_fill-{config.fill_timing}"
            f"_seed{config.seed}")
    if config.lookahead_probe:
        stem += "_PROBE"
    return stem


def write_run(result: RunResult, config: EngineConfig, metrics: dict,
              extra_meta: dict[str, Any] | None = None,
              out_dir: Path | str | None = None) -> dict[str, Path]:
    """Write <stem>.trades.parquet, <stem>.equity.parquet, <stem>.meta.json.

    Returns the written paths. out_dir defaults to backtest/results/.
    """
    out = Path(out_dir) if out_dir else DIR_BACKTEST_RESULTS
    out.mkdir(parents=True, exist_ok=True)
    stem = run_name(config)
    paths = {
        "trades": out / f"{stem}.trades.parquet",
        "equity": out / f"{stem}.equity.parquet",
        "meta": out / f"{stem}.meta.json",
    }
    if not result.trades.empty:
        write_table(pa.Table.from_pandas(result.trades, preserve_index=False),
                    paths["trades"], sort_by=["step", "order_id"])
    if not result.equity.empty:
        write_table(pa.Table.from_pandas(result.equity, preserve_index=False),
                    paths["equity"], sort_by="step")
    meta = {
        "config": _jsonable(asdict(config)),
        "metrics": _jsonable(metrics),
        "run": _jsonable(result.meta),
        "orders": _jsonable(result.orders.to_dict(orient="records")),
        "code_version": _code_version(),
    }
    if extra_meta:
        meta["extra"] = _jsonable(extra_meta)
    paths["meta"].write_text(json.dumps(meta, indent=1, sort_keys=True,
                                        ensure_ascii=False, default=str),
                             encoding="utf-8")
    return paths


def _jsonable(value: Any) -> Any:
    """Decimals to strings, sets to sorted lists, recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    return value
