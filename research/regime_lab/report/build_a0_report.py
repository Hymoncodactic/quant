"""Assemble template + data + plotly.min.js into one self-contained HTML.

Responsibility: substitution only. No data derivation, no chart logic.
Escapes "</" inside the injected JSON so a value can never close the script tag
early (html-report section 1).

Usage:
    python research/regime_lab/report/build_a0_report.py
"""

from __future__ import annotations

__all__ = ["main", "OUT_HTML"]

from pathlib import Path

import plotly

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT_HTML = ROOT / "reports" / "a0_cap1000_20260821.html"


def main() -> int:
    template = (HERE / "a0_report_template.html").read_text()
    data = (HERE / "a0_report_data.json").read_text().replace("</", r"<\/")
    plotly_js = (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_text()

    html = template.replace("__PLOTLY_JS__", plotly_js).replace("__DATA_JSON__", data)
    if "__PLOTLY_JS__" in html or "__DATA_JSON__" in html:
        raise SystemExit("placeholder left unsubstituted")

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html)
    print(f"wrote {OUT_HTML} ({OUT_HTML.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
