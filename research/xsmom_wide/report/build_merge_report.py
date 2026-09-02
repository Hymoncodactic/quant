"""Assemble the A0+A1 merge report into one self-contained HTML file.

Responsibility: substitute plotly.min.js (from the installed plotly package)
and merge_report_data.json into merge_report_template.html and write
reports/a0_a1_merge_20260902.html. Every "</" in the JSON is escaped so no
payload string can close the script tag early (/html-report section 1); a
leftover placeholder aborts the run.

Out of scope: numbers (make_merge_report_data.py) and rendering (template).

Public functions:
    main()   Build OUT_HTML, return an exit code.

Constants:
    OUT_HTML  Path  ROOT/reports/a0_a1_merge_20260902.html.

Change log:
    2026-09-02  Created.
"""

from __future__ import annotations

__all__ = ["main"]

from pathlib import Path

import plotly

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT_HTML = ROOT / "reports" / "a0_a1_merge_20260902.html"


def main() -> int:
    template = (HERE / "merge_report_template.html").read_text()
    data = (HERE / "merge_report_data.json").read_text().replace("</", r"<\/")
    js = (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_text()
    html = template.replace("__PLOTLY_JS__", js).replace("__DATA_JSON__", data)
    if "__PLOTLY_JS__" in html or "__DATA_JSON__" in html:
        raise SystemExit("placeholder left unsubstituted")
    OUT_HTML.parent.mkdir(exist_ok=True)
    OUT_HTML.write_text(html)
    print(f"written {OUT_HTML} ({OUT_HTML.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
