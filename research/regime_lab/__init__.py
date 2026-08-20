"""Regime-gated low-frequency signals on US tech, hourly-or-slower decisions.

Study: high-frequency-informed market-state layer + daily signal layer,
long-only/long-flat, zero costs at the signal-validation stage. Frozen protocol
in research/prereg/20260820_regime_lf_prereg.md; ruling in
research/decisions/20260820_regime_lf_ruling.md.

Modules:
    data.py        Daily and intraday loading for us_equity and us_etf groups
    vol.py         Range-based estimators (Yang-Zhang, Parkinson, GK) and
                   realized volatility from hourly bars
    metrics.py     Performance and risk statistics, stationary bootstrap
    rigor.py       SPA test (Hansen 2005) and Deflated Sharpe Ratio
    engine.py      Vectorized daily portfolio backtest over config grids
    configs.py     The frozen strategy family
    run_search.py  Two-stage search runner
    validate_rv_proxy.py  Yang-Zhang vs hourly realized vol on the overlap
"""
