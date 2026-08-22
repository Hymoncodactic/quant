"""Assemble template, data and plotly.min.js into one self-contained HTML report.

Responsibility: read the report template and the data payload that sit beside
this file, read plotly.min.js out of the installed plotly package, substitute
the two placeholders and write the delivered single-file report. Every "</" in
the JSON text is rewritten to its backslash-escaped form before injection, so no
string value inside the payload can close the enclosing script tag early; that
rule comes from /html-report section 1. After substitution the result is checked
for leftover placeholders and the run exits non-zero rather than shipping a page
with an unsubstituted marker in it. Run from the repository root as
"python research/regime_lab/report/build_a0_1h_report.py".

Out of scope: deriving or checking any number, which belongs to
make_a0_report_data.py in this directory; all rendering, chart construction and
theming, which belong to a0_1h_report_template.html. Project-wide path constants
live in common/paths.py, but this script resolves its own paths from __file__
because it is a standalone command-line entry point that no module imports.

Public functions:
    main()   Substitute both placeholders, write OUT_HTML, return an exit code.

Constants:
    HERE      Path  Directory of this script, the template and the data JSON.
    ROOT      Path  Repository root, resolved as HERE.parents[2].
    OUT_HTML  Path  Delivered report, ROOT/reports/a0_1h_20260822.html. The
                    date in the file name is the date of the reported run.
                    Source: literal at the declaration, no external basis.

Inputs:
    research/regime_lab/report/a0_1h_report_template.html   placeholders
                                                         __PLOTLY_JS__ and
                                                         __DATA_JSON__
    research/regime_lab/report/a0_1h_report_data.json       payload
    <installed plotly package>/package_data/plotly.min.js
Outputs:
    reports/a0_1h_20260822.html   self-contained, roughly 5 MB, git-ignored

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main", "OUT_HTML"]

from pathlib import Path

import plotly

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT_HTML = ROOT / "reports" / "a0_1h_20260822.html"


def main() -> int:
    template = (HERE / "a0_1h_report_template.html").read_text()
    data = (HERE / "a0_1h_report_data.json").read_text().replace("</", r"<\/")
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
