"""KDJ entry + MACD filter, US tech daily, long only.

Exploratory factor research. Frozen judgement criteria live in
research/prereg/20260820_kdj_macd_daily_prereg.md; the ruling lives in
research/decisions/20260820_kdj_macd_daily_ruling.md.

Modules:
    data.py        Daily bar loading from data/t212/curated/us_equity
    indicators.py  KDJ and MACD, causal recursions only
    rules.py       Entry and exit boolean series, one function per arm
    engine.py      Trade extraction, look-ahead assertions
    benchmark.py   Matched-horizon unconditional win rate and significance
    run_backtest.py  Runner, writes results/
    acceptance.py  Discriminating-power acceptance checks
"""
