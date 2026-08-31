/* Main dashboard page.
   All visible wording comes from /assets/labels.json so the interface can be
   translated without touching logic. Charts are updated with Plotly.react so
   the library parses once and only the data changes on each poll. */
(function () {
  "use strict";

  var L = null;
  var STATE = null;
  var lastHistoryAt = 0;
  var SESSIONS = [];
  var LAST_ROWS = [];
  var LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  var TZ_CHOICES = [
    { id: "local", zone: LOCAL_TZ },
    { id: "london", zone: "Europe/London" },
    { id: "newyork", zone: "America/New_York" }
  ];
  var TZ_ID = localStorage.getItem("dash.tz") || "local";
  var RANGE_IDS = ["1D", "1W", "1M", "3M", "6M", "YTD",
                   "1Y", "2Y", "5Y", "10Y", "ALL"];
  var RANGE_ID = localStorage.getItem("dash.range") || "1D";

  function tzChoice() {
    for (var i = 0; i < TZ_CHOICES.length; i++) {
      if (TZ_CHOICES[i].id === TZ_ID) return TZ_CHOICES[i];
    }
    return TZ_CHOICES[0];
  }

  var TZ_FORMATTER = null;
  var TZ_FORMATTER_ZONE = null;

  function inZone(iso) {
    /* Plotly has no time-zone support, so a timestamp is converted to the
       chosen zone's wall clock and handed over as a naive string. The axis
       then reads as that zone, which is the whole point: the same curve is
       being watched from China, settled in London and traded in New York. */
    if (iso === null || iso === undefined) return iso;
    var zone = tzChoice().zone;
    if (TZ_FORMATTER_ZONE !== zone) {
      TZ_FORMATTER = new Intl.DateTimeFormat("sv-SE", {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
      });
      TZ_FORMATTER_ZONE = zone;
    }
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    // sv-SE already formats as YYYY-MM-DD HH:MM:SS, which Plotly parses.
    return TZ_FORMATTER.format(d).replace(",", "");
  }
  var chartsBuilt = false;
  var STATE_MS = 5000;
  var HISTORY_MS = 30000;

  function $(id) { return document.getElementById(id); }
  function text(id, value) { var el = $(id); if (el) el.textContent = value; }

  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); });
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dashboard-Nonce": window.DASH_TOKEN
      },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    });
  }

  function money(v) {
    if (v === null || v === undefined) return L.kpi.no_price;
    return L.app.currency_prefix + Number(v).toLocaleString("zh-CN",
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function num(v, digits) {
    if (v === null || v === undefined) return L.app.unknown;
    return Number(v).toLocaleString("zh-CN",
      { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function localTime(iso) {
    if (!iso) return L.app.never;
    var d = new Date(iso);
    return isNaN(d) ? L.app.never : d.toLocaleString("zh-CN", { hour12: false });
  }

  /* ---------------- static text ---------------- */

  function paintStatic() {
    text("strategy-heading", L.strategy.heading);
    text("strategy-note", L.strategy.note);
    text("strategy-start-btn", L.strategy.start);
    text("strategy-stop-btn", L.strategy.stop);
    text("signals-heading", L.signals.heading);
    text("signals-note", L.signals.note);
    text("signals-history-heading", L.signals.history_heading);
    text("watch-heading", L.watch.heading);
    text("watch-note", L.watch.note);
    document.title = L.app.title;
    text("app-title", L.app.title);
    text("nav-dash", L.app.nav_dashboard);
    text("nav-orders", L.app.nav_orders);
    text("setup-heading", L.setup.heading);
    text("setup-intro", L.setup.intro);
    text("money-label", L.setup.money_heading);
    text("money-hint", L.setup.money_hint);
    $("money-input").placeholder = L.setup.money_placeholder;
    text("money-btn", L.setup.money_create);
    text("save-btn", L.setup.save);
    text("adjust-label", L.setup.money_adjust_heading);
    text("adjust-hint", L.setup.money_adjust_hint);
    $("adjust-input").placeholder = L.setup.money_adjust_placeholder;
    text("adjust-btn", L.setup.money_adjust_btn);
    text("halt-heading", L.controls.halt_heading);
    text("halt-note", L.controls.halt_note);
    text("halt-raise-btn", L.controls.halt_raise);
    text("halt-clear-btn", L.controls.halt_clear);
    text("controls-heading", L.controls.heading);
    text("controls-note", L.controls.note);
    text("start-btn", L.controls.start);
    text("stop-btn", L.controls.stop);
    text("shutdown-btn", L.controls.shutdown);
    text("shutdown-note", L.controls.shutdown_note);
    text("equity-heading", L.charts.equity_title);
    text("book-vs-account", L.charts.book_vs_account);
    text("tz-label", L.charts.tz_label);
    text("reset-btn", L.setup.reset_btn);
    text("reset-hint", L.setup.reset_hint);
    text("range-label", L.charts.range_label);
    text("records-heading", L.records.heading);
    text("records-privacy", L.records.privacy);
    buildRangeBar();
    var sel = $("tz-select");
    sel.innerHTML = "";
    TZ_CHOICES.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.id;
      var wrap = L.charts.tz_local_wrap;
      opt.textContent = L.charts["tz_" + c.id] +
        (c.id === "local" ? wrap[0] + c.zone + wrap[1] : "");
      sel.appendChild(opt);
    });
    sel.value = TZ_ID;
    sel.onchange = function () {
      TZ_ID = sel.value;
      localStorage.setItem("dash.tz", TZ_ID);
      if (LAST_ROWS.length) drawEquity(LAST_ROWS);
    };
    text("gap-note", L.charts.gap_note);
    text("equity-empty", L.charts.empty);
    text("positions-heading", L.charts.positions_title);
    text("table-heading", L.table.heading);
    text("modal-ok", L.setup.modal_ok);
  }

  /* ---------------- settings form ---------------- */

  function buildFields(readiness) {
    var host = $("settings-fields");
    if (host.dataset.built === "1") {
      // Only refill from the server while the form is untouched. The state
      // poll runs every few seconds, and refilling an edited form would wipe
      // every field the reader had already typed except the focused one.
      if (host.dataset.dirty !== "1") fillFields(readiness);
      return;
    }
    host.innerHTML = "";
    readiness.fields.forEach(function (f) {
      var meta = L.setup.fields[f.id] || { label: f.id, hint: "" };
      var wrap = document.createElement("div");
      wrap.className = "field";
      wrap.id = "field-" + f.id;
      var label = document.createElement("label");
      label.textContent = meta.label;
      var hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = meta.hint;
      wrap.appendChild(label);
      wrap.appendChild(hint);
      if (f.kind === "switch") {
        var sw = document.createElement("div");
        sw.className = "switch";
        var box = document.createElement("input");
        box.type = "checkbox"; box.id = "in-" + f.id;
        sw.appendChild(box);
        wrap.appendChild(sw);
      } else {
        var input = document.createElement("input");
        input.type = "text"; input.inputMode = "decimal"; input.id = "in-" + f.id;
        wrap.appendChild(input);
      }
      var err = document.createElement("div");
      err.className = "err hidden"; err.id = "err-" + f.id;
      wrap.appendChild(err);
      host.appendChild(wrap);
    });
    host.dataset.built = "1";
    host.addEventListener("input", markDirty);
    host.addEventListener("change", markDirty);
    fillFields(readiness);
  }

  function markDirty() {
    $("settings-fields").dataset.dirty = "1";
    text("ready-flag", L.setup.unsaved);
  }

  function clearDirty() {
    $("settings-fields").dataset.dirty = "0";
  }

  function fillFields(readiness) {
    readiness.fields.forEach(function (f) {
      var el = $("in-" + f.id);
      if (!el || el === document.activeElement) return;
      if (f.kind === "switch") el.checked = !!f.value;
      else el.value = (f.value === null || f.value === undefined) ? "" : f.value;
    });
  }

  function collectFields() {
    var out = {};
    (STATE.readiness.fields || []).forEach(function (f) {
      var el = $("in-" + f.id);
      if (!el) return;
      out[f.id] = (f.kind === "switch") ? el.checked : el.value;
    });
    return out;
  }

  function clearProblemMarks() {
    (STATE.readiness.fields || []).forEach(function (f) {
      var wrap = $("field-" + f.id), err = $("err-" + f.id);
      if (wrap) wrap.classList.remove("bad");
      if (err) { err.classList.add("hidden"); err.textContent = ""; }
    });
  }

  function showProblems(problems) {
    clearProblemMarks();
    var items = [];
    problems.forEach(function (p) {
      var meta = L.setup.fields[p.field] || { label: p.field };
      var why = L.setup.problems[p.code] || p.code;
      var wrap = $("field-" + p.field), err = $("err-" + p.field);
      if (wrap) wrap.classList.add("bad");
      if (err) { err.textContent = why; err.classList.remove("hidden"); }
      items.push(meta.label + L.setup.problem_separator + why);
    });
    if (!STATE.readiness.ledger_ready) items.push(L.setup.ledger_missing);
    if (items.length) openModal(L.setup.modal_title, L.setup.modal_intro, items);
  }

  function openModal(title, intro, items) {
    text("modal-title", title);
    text("modal-intro", intro);
    var list = $("modal-list");
    list.innerHTML = "";
    items.forEach(function (t) {
      var li = document.createElement("li"); li.textContent = t; list.appendChild(li);
    });
    $("modal-back").classList.remove("hidden");
  }

  /* ---------------- status and KPIs ---------------- */

  function isoNorm(iso) {
    /* The daemon writes "YYYY-MM-DD HH:MM:SS+00:00"; Date() wants a T. */
    return iso ? String(iso).replace(" ", "T") : iso;
  }

  function paintStrategy() {
    var st = (STATE && STATE.strategy) || {};
    var status = st.status || {};
    var running = !!st.running;
    var stateEl = $("strategy-state");
    var pieces = [];
    pieces.push("<span class='pill " + (running ? "on'>" : "off'>") +
      (running ? L.strategy.running : L.strategy.stopped) + "</span>");
    if (running) {
      var phase = L.strategy.phases[status.phase] || status.phase || "";
      pieces.push("<span>" + phase + "</span>");
      if (status.dry_run === false) {
        pieces.push("<span class='badge armed'>" + L.strategy.armed + "</span>");
      } else if (status.dry_run === true) {
        pieces.push("<span class='badge rehearse'>" + L.strategy.rehearse +
                    "</span>");
      }
    }
    stateEl.innerHTML = pieces.join(" ");

    var nextEl = $("strategy-next");
    if (running && status.next_action_utc) {
      var at = new Date(isoNorm(status.next_action_utc));
      var secs = Math.max(0, Math.round((at - new Date()) / 1000));
      var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
      nextEl.textContent = L.strategy.next_prefix + " " +
        at.toLocaleString("zh-CN", { hour12: false }) + " | " +
        L.strategy.countdown_prefix + " " + h + "h" +
        String(m).padStart(2, "0") + "m";
    } else {
      nextEl.textContent = running ? "" : L.strategy.stopped_note;
    }

    var cyc = [];
    if (status.last_decide && status.last_decide.session) {
      var d = status.last_decide;
      cyc.push(L.strategy.last_decide + " " + d.session + ": " +
               ((d.result && (d.result.submit || d.result.reason)) || ""));
    }
    if (status.last_settle && status.last_settle.session) {
      var t = status.last_settle;
      cyc.push(L.strategy.last_settle + " " + t.session + ": " +
               ((t.result && (t.result.reconcile || t.result.reason)) || ""));
    }
    $("strategy-cycle").textContent = cyc.join("  |  ");
    $("strategy-start-btn").disabled = running;
    $("strategy-stop-btn").disabled = !running;
  }

  function paintGates(live) {
    var wrap = $("gates-wrap");
    if (!live || !live.gates) { wrap.innerHTML = ""; return; }
    function gateRow(title, fmt, gate, valueText) {
      var pill;
      if (!gate.enabled) {
        pill = "<span class='pill off'>" + L.signals.gate_disabled + "</span>";
      } else if (gate.blocking) {
        pill = "<span class='pill bad'>" + L.signals.gate_blocking + "</span>";
      } else {
        pill = "<span class='pill on'>" + L.signals.gate_open + "</span>";
      }
      return "<div class='gate-row'><b>" + title + "</b>" + pill +
             "<span>" + fmt + " " + valueText + "</span></div>";
    }
    var t = live.gates.trend || {};
    var tv = "";
    if (t.live_margin_pct !== undefined && t.live_margin_pct !== null) {
      tv = (t.live_margin_pct >= 0 ? "+" : "") + num(t.live_margin_pct, 2) +
           "%" + L.signals.live_suffix;
    } else if (t.margin_pct !== undefined && t.margin_pct !== null) {
      tv = (t.margin_pct >= 0 ? "+" : "") + num(t.margin_pct, 2) + "%";
    }
    var v = live.gates.vol || {};
    var vv = (v.percentile === null || v.percentile === undefined)
      ? L.signals.insufficient
      : num(v.percentile * 100, 1) + "% / " + num(v.threshold * 100, 0) + "%";
    wrap.innerHTML =
      gateRow(L.signals.gate_trend, L.signals.gate_trend_fmt, t, tv) +
      gateRow(L.signals.gate_vol, L.signals.gate_vol_fmt, v, vv);
  }

  function drawSignals(live) {
    var chart = $("signals-chart");
    if (!live || !live.symbols) { return; }
    var rows = [];
    Object.keys(live.symbols).forEach(function (sym) {
      var r = live.symbols[sym];
      var margin = (r.live_margin_pct !== undefined &&
                    r.live_margin_pct !== null)
        ? r.live_margin_pct : r.margin_pct;
      if (margin === undefined || margin === null) return;
      rows.push({ sym: sym, margin: margin, on: !!r.on,
                  live: r.live_margin_pct !== undefined &&
                        r.live_margin_pct !== null });
    });
    rows.sort(function (a, b) { return a.margin - b.margin; });
    var trace = {
      type: "bar", orientation: "h",
      y: rows.map(function (r) {
        return r.sym + (r.live ? " *" : "");
      }),
      x: rows.map(function (r) { return r.margin; }),
      marker: { color: rows.map(function (r) {
        return r.margin >= 0 ? "#2e8b57" : "#b34a4a";
      }) },
      hovertemplate: "%{y}: %{x:.2f}%<extra></extra>"
    };
    Plotly.react(chart, [trace], {
      margin: { l: 70, r: 20, t: 6, b: 32 },
      xaxis: { title: L.signals.chart_axis, zeroline: true,
               zerolinewidth: 2, zerolinecolor: "#888" },
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { color: "#ccc", size: 11 }
    }, { displayModeBar: false, responsive: true });
  }

  function paintSignalHistory(history) {
    var wrap = $("signals-history-wrap");
    if (!history || !history.length) {
      wrap.innerHTML = "<p class='note'>" + L.signals.history_empty + "</p>";
      return;
    }
    var cols = L.signals.history_cols;
    var head = "<tr>" + cols.map(function (c) {
      return "<th>" + c + "</th>";
    }).join("") + "</tr>";
    var body = history.slice(0, 10).map(function (row) {
      var targets = row.targets || {};
      var nonzero = Object.keys(targets).filter(function (k) {
        return Number(targets[k]) > 0;
      }).length;
      var submit = row.submit || {};
      var submitted = (submit.submitted || []).length;
      var dry = (submit.dry_run || []).length;
      var mode = row.dry_run ? L.signals.history_dry : L.signals.history_real;
      return "<tr><td>" + (row.session || "") + "</td><td>" + nonzero +
             "</td><td>" + (submitted || dry) + "</td><td>" + mode +
             "</td></tr>";
    }).join("");
    wrap.innerHTML = "<table>" + head + body + "</table>";
  }

  function refreshSignals() {
    return getJSON("/api/signals").then(function (payload) {
      if (payload.ok && payload.live) {
        paintGates(payload.live);
        drawSignals(payload.live);
        text("signals-asof", L.signals.asof_prefix + " " +
             localTime(isoNorm(payload.live.as_of)));
      } else {
        $("gates-wrap").innerHTML = "<p class='note'>" + L.signals.empty +
                                    "</p>";
      }
      paintSignalHistory(payload.history || []);
    }).catch(function () {});
  }

  var watchBuilt = false;
  function refreshWatch() {
    return getJSON("/api/watch").then(function (payload) {
      var grid = $("watch-grid");
      var series = payload.series || {};
      var quotes = payload.quotes || {};
      var symbols = (payload.symbols || []).filter(function (s) {
        return s.indexOf("=") === -1;   /* FX gets no cell of its own */
      });
      if (!watchBuilt) {
        grid.innerHTML = symbols.map(function (sym) {
          return "<div class='watch-cell'><div class='watch-head'>" +
                 "<b id='wh-" + sym + "'>" + sym + "</b>" +
                 "<span id='wq-" + sym + "'></span></div>" +
                 "<div class='mini' id='wc-" + sym + "'></div></div>";
        }).join("");
        watchBuilt = true;
      }
      var any = false;
      symbols.forEach(function (sym) {
        var points = series[sym] || [];
        var quote = quotes[sym] || {};
        var head = $("wq-" + sym);
        if (head) {
          var price = quote.ok ? num(quote.price, 2) : L.app.unknown;
          var change = "";
          if (points.length > 1 && quote.ok) {
            var first = points[0][1];
            if (first) {
              var pct = (quote.price / first - 1) * 100;
              change = " " + (pct >= 0 ? "+" : "") + num(pct, 2) + "%";
            }
          }
          head.textContent = price + change;
          head.style.color = quote.stale ? "#777" : "";
        }
        if (!points.length) return;
        any = true;
        Plotly.react($("wc-" + sym), [{
          type: "scatter", mode: "lines",
          x: points.map(function (pt) { return inZone(pt[0]); }),
          y: points.map(function (pt) { return pt[1]; }),
          line: { width: 1.4 }
        }], {
          margin: { l: 40, r: 6, t: 4, b: 18 },
          paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
          font: { color: "#999", size: 9 },
          xaxis: { showgrid: false, nticks: 4 },
          yaxis: { showgrid: false, nticks: 3 }
        }, { displayModeBar: false, responsive: true });
      });
      if (!any) {
        text("watch-note", L.watch.empty);
      } else {
        text("watch-note", L.watch.note);
      }
    }).catch(function () {});
  }

  function paintStatus() {
    var c = STATE.collector, snap = STATE.snapshot;
    var brokerOk = snap && snap.account && snap.account.ok;
    var parts = [
      ["<b>" + L.status.env + "</b> " + STATE.env],
      ["<b>" + L.status.strategy + "</b> " + STATE.strategy_id],
      ["<b>" + L.status.collector + "</b> <span class='pill " +
        (c.running ? "on'>" + L.status.collector_on : "off'>" + L.status.collector_off) +
        "</span>"],
      ["<b>" + L.status.broker + "</b> <span class='pill " +
        (brokerOk ? "on'>" + L.status.broker_ok : "bad'>" + L.status.broker_bad) +
        "</span>"],
      ["<b>" + L.status.halt + "</b> <span class='pill " +
        (STATE.halted ? "bad'>" + L.status.halt_on : "off'>" + L.status.halt_off) +
        "</span>"],
      ["<b>" + L.status.updated + "</b> " + localTime(snap && snap.ts)],
      ["<b>" + L.status.ticks + "</b> " + c.ticks + L.status.ticks_unit]
    ];
    $("statusbar").innerHTML = parts.map(function (p) {
      return "<span>" + p[0] + "</span>";
    }).join("");
    $("start-btn").disabled = c.running;
    $("stop-btn").disabled = !c.running;
  }

  function paintKPIs() {
    var snap = STATE.snapshot || {}, book = snap.book || STATE.book || {};
    var account = snap.account || {};
    var summary = (account.ok && account.summary) ? account.summary : null;
    var cash = summary ? (summary.cash || {}) : {};
    var rows = [
      [L.kpi.equity, book.equity_gbp === null || book.equity_gbp === undefined
        ? L.kpi.no_price : money(book.equity_gbp)],
      [L.kpi.cash, book.cash_gbp === null || book.cash_gbp === undefined
        ? L.app.unknown : money(book.cash_gbp)],
      [L.kpi.holdings, book.holdings_gbp === null || book.holdings_gbp === undefined
        ? L.kpi.no_price : money(book.holdings_gbp)],
      [L.kpi.positions, String(Object.keys(book.positions || {}).length)],
      [L.kpi.account_total, summary ? money(summary.totalValue) : L.app.unknown],
      [L.kpi.account_free, summary ? money(cash.availableToTrade) : L.app.unknown]
    ];
    $("kpis").innerHTML = rows.map(function (r) {
      var dim = (r[1] === L.app.unknown || r[1] === L.kpi.no_price) ? " dim" : "";
      return "<div class='kpi'><div class='k'>" + r[0] +
             "</div><div class='v" + dim + "'>" + r[1] + "</div></div>";
    }).join("");
  }

  function paintBrokerWhy() {
    /* When the broker is unreachable, say WHY. The four common causes need
       four different actions, and the raw error text distinguishes none of
       them for a reader. */
    var el = $("broker-why");
    var snap = STATE.snapshot || {};
    var account = snap.account || {};
    if (account.ok !== false) { el.classList.add("hidden"); return; }
    var d = account.diagnosis || {};
    var text = (L.status.diagnosis || {})[d.cause] ||
               (L.status.diagnosis || {}).unknown;
    var ev = d.evidence || {};
    if (d.cause === "dns_mismatch" && ev.os_addresses) {
      text += "  " + L.status.dns_detail + " " + ev.os_addresses.join(", ") +
              L.status.dns_joiner + L.status.dns_should_be + " " +
              (ev.public_addresses || []).join(", ");
    }
    el.textContent = L.status.broker_why + L.status.why_separator + text;
    el.classList.remove("hidden");
  }

  function paintFunding() {
    var f = STATE.funding || {};
    var el = $("funding-note");
    el.classList.toggle("hidden", !f.over_account);
    if (f.over_account) el.textContent = L.setup.over_account;
  }

  function paintRecords(stats, directory) {
    text("records-note", L.records.note + " " + directory);
    var wrap = $("records-wrap");
    var names = L.records.names || {};
    var head = "<tr><th>" + L.records.stream + "</th><th>" + L.records.rows +
      "</th><th>" + L.records.size + "</th><th>" + L.records.last + "</th></tr>";
    var body = Object.keys(stats).map(function (k) {
      var r = stats[k];
      var kb = r.bytes > 1024 ? (r.bytes / 1024).toFixed(1) + " KB"
                              : r.bytes + " B";
      return "<tr><td>" + (names[k] || k) + "</td><td>" + r.rows +
        "</td><td>" + kb + "</td><td>" +
        (r.last_write_utc ? localTime(r.last_write_utc) : L.app.never) +
        "</td></tr>";
    }).join("");
    wrap.innerHTML = "<table>" + head + body + "</table>";
  }

  function refreshRecords() {
    return getJSON("/api/records").then(function (r) {
      paintRecords(r.streams || {}, r.directory || "");
    }).catch(function () { });
  }

  function paintTable() {
    var book = (STATE.snapshot && STATE.snapshot.book) || STATE.book || {};
    var wrap = $("table-wrap");
    if (!book.exists) { wrap.innerHTML = "<p class='note'>" + L.table.book_missing + "</p>"; return; }
    if (book.usable === false) {
      wrap.innerHTML = "<p class='note'>" + L.table.book_frozen + (book.reason || "") + "</p>";
      return;
    }
    var marked = book.marked || {};
    var keys = Object.keys(marked).filter(function (k) { return marked[k].qty !== 0; });
    if (!keys.length) { wrap.innerHTML = "<p class='note'>" + L.table.empty + "</p>"; return; }
    keys.sort(function (a, b) { return (marked[b].value_gbp || 0) - (marked[a].value_gbp || 0); });
    var head = "<tr><th>" + L.table.symbol + "</th><th>" + L.table.qty +
      "</th><th>" + L.table.price + "</th><th>" + L.table.value +
      "</th><th>" + L.table.fresh + "</th></tr>";
    var body = keys.map(function (k) {
      var m = marked[k];
      return "<tr><td>" + k + "</td><td>" + num(m.qty, 4) + "</td><td>" +
        (m.price_usd === null ? L.app.unknown : num(m.price_usd, 2)) + "</td><td>" +
        (m.value_gbp === null ? L.app.unknown : num(m.value_gbp, 2)) + "</td><td>" +
        "<span class='pill " + (m.stale ? "warn'>" + L.table.fresh_stale
                                        : "on'>" + L.table.fresh_ok) + "</span></td></tr>";
    }).join("");
    wrap.innerHTML = "<table>" + head + body + "</table>";
  }

  /* ---------------- charts ---------------- */

  var LAYOUT_BASE = {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#98a1b3", size: 11 },
    margin: { l: 56, r: 16, t: 10, b: 36 },
    hovermode: "x unified",
    xaxis: { gridcolor: "#262b36", zeroline: false },
    yaxis: { gridcolor: "#262b36", zeroline: false },
    legend: { orientation: "h", y: 1.12, x: 0 },
    showlegend: true
  };
  var CONFIG = { displaylogo: false, responsive: true,
                 modeBarButtonsToRemove: ["lasso2d", "select2d"] };

  function splitOnGaps(rows, field) {
    /* Insert nulls at gap markers so a stopped period shows as a break in
       the line rather than a straight segment through unobserved hours. */
    var x = [], y = [];
    rows.forEach(function (r) {
      if (r.gap) { x.push(inZone(r.ts)); y.push(null); return; }
      x.push(inZone(r.ts));
      y.push(r[field] === undefined ? null : r[field]);
    });
    return { x: x, y: y };
  }

  function drawEquity(rows) {
    LAST_ROWS = rows;
    var empty = !rows.length;
    $("equity-empty").classList.toggle("hidden", !empty);
    $("equity-chart").classList.toggle("hidden", empty);
    if (empty) return;
    var eq = splitOnGaps(rows, "equity_gbp");
    var ca = splitOnGaps(rows, "cash_gbp");
    var ho = splitOnGaps(rows, "holdings_gbp");
    var ac = splitOnGaps(rows, "account_total");
    var traces = [
      { x: eq.x, y: eq.y, name: L.charts.equity_series, type: "scatter",
        mode: "lines", line: { color: "#4c8dff", width: 2 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.equity_series + "</extra>" },
      { x: ca.x, y: ca.y, name: L.charts.cash_series, type: "scatter",
        mode: "lines", line: { color: "#38b26b", width: 1.4 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.cash_series + "</extra>" },
      { x: ho.x, y: ho.y, name: L.charts.holdings_series, type: "scatter",
        mode: "lines", line: { color: "#d99a2b", width: 1.4 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.holdings_series + "</extra>" },
      { x: ac.x, y: ac.y, name: L.charts.account_series, type: "scatter",
        mode: "lines", line: { color: "#98a1b3", width: 1.2, dash: "dot" },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.account_series + "</extra>" }
    ];
    var layout = JSON.parse(JSON.stringify(LAYOUT_BASE));
    layout.yaxis.title = { text: L.charts.equity_y, font: { size: 11 } };
    layout.shapes = sessionBands(rows);
    Plotly.react("equity-chart", traces, layout, CONFIG);
  }

  function drawPositions() {
    var book = (STATE.snapshot && STATE.snapshot.book) || {};
    var marked = book.marked || {};
    var keys = Object.keys(marked).filter(function (k) {
      return marked[k].value_gbp !== null && marked[k].value_gbp !== undefined;
    });
    keys.sort(function (a, b) { return marked[b].value_gbp - marked[a].value_gbp; });
    var layout = JSON.parse(JSON.stringify(LAYOUT_BASE));
    layout.showlegend = false;
    layout.yaxis.title = { text: L.charts.positions_y, font: { size: 11 } };
    Plotly.react("positions-chart", [{
      x: keys, y: keys.map(function (k) { return marked[k].value_gbp; }),
      type: "bar", marker: { color: "#4c8dff" },
      hovertemplate: "%{x}<br>%{y:.2f} " + L.app.currency_prefix + "<extra></extra>"
    }], layout, CONFIG);
  }

  /* ---------------- polling ---------------- */

  function refreshState() {
    return getJSON("/api/state").then(function (s) {
      STATE = s;
      buildFields(s.readiness);
      paintStatus(); paintStrategy(); paintKPIs(); paintFunding();
      paintBrokerWhy(); paintTable(); drawPositions();
      if ($("settings-fields").dataset.dirty !== "1") {
        text("ready-flag", s.readiness.ready ? L.setup.ready : L.setup.not_ready);
      }
      var hasBook = !!s.readiness.ledger_ready;
      $("money-field").classList.toggle("hidden", hasBook);
      $("adjust-field").classList.toggle("hidden", !hasBook);
      var book = s.book || {};
      if (book.cash_gbp !== null && book.cash_gbp !== undefined) {
        text("adjust-current", L.setup.money_current + " " + money(book.cash_gbp));
      }
      text("halt-state", s.halted ? L.controls.halt_on : L.controls.halt_off);
      $("halt-raise-btn").disabled = !!s.halted;
      $("halt-clear-btn").disabled = !s.halted;
    }).catch(function () { /* transient; the next poll retries */ });
  }

  function buildRangeBar() {
    var bar = $("range-bar");
    bar.innerHTML = "";
    RANGE_IDS.forEach(function (id) {
      var btn = document.createElement("button");
      btn.textContent = (L.charts.range_names || {})[id] || id;
      btn.dataset.range = id;
      if (id === RANGE_ID) btn.className = "on";
      btn.onclick = function () {
        RANGE_ID = id;
        localStorage.setItem("dash.range", id);
        [].forEach.call(bar.children, function (b) {
          b.className = b.dataset.range === id ? "on" : "";
        });
        refreshHistory(true);
      };
      bar.appendChild(btn);
    });
  }

  function paintRangeMeta(payload) {
    var src = payload.source === "daily"
      ? L.charts.source_daily : L.charts.source_ticks;
    var parts = [src, payload.points + L.charts.points];
    if (payload.available_from) {
      parts.push(L.charts.available_from + " " + inZone(payload.available_from));
    }
    text("range-meta", parts.join(L.charts.meta_separator));
  }

  function refreshHistory(force) {
    var now = Date.now();
    if (!force && now - lastHistoryAt < HISTORY_MS) return Promise.resolve();
    lastHistoryAt = now;
    return getJSON("/api/sessions").then(function (s) {
      SESSIONS = s.sessions || [];
    }).catch(function () { SESSIONS = []; }).then(function () {
      return getJSON("/api/history?range=" + encodeURIComponent(RANGE_ID));
    }).then(function (h) {
      drawEquity(h.rows || []);
      paintRangeMeta(h);
      chartsBuilt = true;
    }).catch(function () { });
  }

  function sessionBands(rows) {
    /* Shade each regular US session so the curve can be read against the
       hours it was actually produced in. Without them the axis is just wall
       clock in whatever zone the reader happens to sit in, and a flat
       overnight stretch looks the same as a flat trading day. */
    // Past a couple of weeks the bands are thinner than the line itself and
    // turn the plot into a solid wash, so they are dropped rather than drawn.
    if (!rows.length || !SESSIONS.length) return [];
    if (["1D", "1W"].indexOf(RANGE_ID) === -1) return [];
    var first = new Date(rows[0].ts).getTime();
    var last = new Date(rows[rows.length - 1].ts).getTime();
    return SESSIONS.filter(function (s) {
      return s.is_full &&
             new Date(s.close_utc).getTime() >= first &&
             new Date(s.open_utc).getTime() <= last;
    }).map(function (s) {
      return { type: "rect", xref: "x", yref: "paper",
               x0: inZone(s.open_utc), x1: inZone(s.close_utc), y0: 0, y1: 1,
               fillcolor: "rgba(76,141,255,0.10)", line: { width: 0 },
               layer: "below" };
    });
  }

  /* ---------------- wiring ---------------- */

  function wire() {
    $("modal-ok").onclick = function () { $("modal-back").classList.add("hidden"); };
    $("modal-back").onclick = function (e) {
      if (e.target === $("modal-back")) $("modal-back").classList.add("hidden");
    };
    $("save-btn").onclick = function () {
      postJSON("/api/settings", collectFields()).then(function (r) {
        if (r.status === 200) {
          STATE.readiness = r.body.readiness;
          clearProblemMarks();
          clearDirty();
          fillFields(r.body.readiness);
          text("ready-flag", r.body.readiness.ready
            ? L.setup.saved_ready : L.setup.saved_need_ledger);
        } else {
          showProblems(r.body.problems || []);
        }
        refreshState();
      });
    };
    $("money-btn").onclick = function () {
      var v = $("money-input").value;
      postJSON("/api/ledger/init", { cash_gbp: v }).then(function (r) {
        var err = $("money-err");
        if (r.status === 200) { err.classList.add("hidden"); clearDirty(); refreshState(); }
        else {
          err.textContent = L.setup.problems[r.body.problem] || r.body.problem;
          err.classList.remove("hidden");
        }
      });
    };
    $("adjust-btn").onclick = function () {
      var err = $("adjust-err");
      postJSON("/api/ledger/allocation",
               { delta_gbp: $("adjust-input").value }).then(function (r) {
        if (r.status === 200) {
          err.classList.add("hidden");
          $("adjust-input").value = "";
          refreshState();
        } else {
          err.textContent = L.setup.problems[r.body.problem] || r.body.problem;
          err.classList.remove("hidden");
        }
      });
    };
    $("reset-btn").onclick = function () {
      if (!window.confirm(L.setup.reset_confirm)) return;
      postJSON("/api/ledger/reset", { confirm: true }).then(function (r) {
        if (r.status === 200) {
          text("reset-state", L.setup.reset_done);
          refreshState();
          return;
        }
        var items = (r.body.blockers || []).map(function (b) {
          return (L.setup.reset_checks[b.check] || b.check) +
                 (b.detail ? L.controls.halt_check_separator + b.detail : "");
        });
        openModal(L.setup.reset_blocked_title, L.setup.reset_blocked_intro,
                  items.length
                    ? items
                    : [L.setup.problems[r.body.problem] || r.body.problem]);
      });
    };
    $("halt-raise-btn").onclick = function () {
      if (!window.confirm(L.controls.halt_raise_confirm)) return;
      postJSON("/api/halt", { action: "raise" }).then(refreshState);
    };
    $("halt-clear-btn").onclick = function () {
      postJSON("/api/halt", { action: "clear" }).then(function (r) {
        if (r.status === 200) { refreshState(); return; }
        var items = (r.body.blockers || []).map(function (b) {
          return (L.controls.halt_checks[b.check] || b.check) +
                 (b.detail ? L.controls.halt_check_separator + b.detail : "");
        });
        openModal(L.controls.halt_blocked_title,
                  L.controls.halt_blocked_intro,
                  items.length ? items : [r.body.problem || ""]);
      });
    };
    $("strategy-start-btn").onclick = function () {
      var status = ((STATE || {}).strategy || {}).status || {};
      var readiness = (STATE || {}).readiness || {};
      var dryField = (readiness.fields || []).filter(function (f) {
        return f.id === "dry_run";
      })[0];
      var armed = dryField ? dryField.value === false : false;
      if (armed && !window.confirm(L.strategy.confirm_start_armed)) return;
      postJSON("/api/strategy", { action: "start" }).then(refreshState);
    };
    $("strategy-stop-btn").onclick = function () {
      if (!window.confirm(L.strategy.confirm_stop)) return;
      postJSON("/api/strategy", { action: "stop" }).then(function () {
        setTimeout(refreshState, 800);
      });
    };
    $("start-btn").onclick = function () {
      postJSON("/api/collector", { action: "start" }).then(function () {
        refreshState(); refreshHistory(true);
      });
    };
    $("stop-btn").onclick = function () {
      postJSON("/api/collector", { action: "stop" }).then(refreshState);
    };
    $("shutdown-btn").onclick = function () {
      if (!window.confirm(L.controls.shutdown_confirm)) return;
      postJSON("/api/shutdown", {}).then(function () {
        document.body.innerHTML = "<main><p class='note'>" +
          L.controls.shutdown_note + "</p></main>";
      });
    };
  }

  getJSON("/assets/labels.json").then(function (labels) {
    L = labels;
    paintStatic();
    wire();
    refreshState().then(function () { return refreshHistory(true); })
      .then(refreshRecords);
    setInterval(function () { refreshState(); refreshHistory(false); }, STATE_MS);
    setInterval(refreshRecords, 30000);
    refreshSignals(); refreshWatch();
    setInterval(refreshSignals, 30000);
    setInterval(refreshWatch, 30000);
  });
})();
