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
  var TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
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

  function paintFunding() {
    var f = STATE.funding || {};
    var el = $("funding-note");
    el.classList.toggle("hidden", !f.over_account);
    if (f.over_account) el.textContent = L.setup.over_account;
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
      if (r.gap) { x.push(r.ts); y.push(null); return; }
      x.push(r.ts); y.push(r[field] === undefined ? null : r[field]);
    });
    return { x: x, y: y };
  }

  function drawEquity(rows) {
    var empty = !rows.length;
    $("equity-empty").classList.toggle("hidden", !empty);
    $("equity-chart").classList.toggle("hidden", empty);
    if (empty) return;
    var eq = splitOnGaps(rows, "equity_gbp");
    var ca = splitOnGaps(rows, "cash_gbp");
    var ho = splitOnGaps(rows, "holdings_gbp");
    var traces = [
      { x: eq.x, y: eq.y, name: L.charts.equity_series, type: "scatter",
        mode: "lines", line: { color: "#4c8dff", width: 2 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.equity_series + "</extra>" },
      { x: ca.x, y: ca.y, name: L.charts.cash_series, type: "scatter",
        mode: "lines", line: { color: "#38b26b", width: 1.4 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.cash_series + "</extra>" },
      { x: ho.x, y: ho.y, name: L.charts.holdings_series, type: "scatter",
        mode: "lines", line: { color: "#d99a2b", width: 1.4 },
        hovertemplate: "%{y:.2f} " + L.app.currency_prefix + "<extra>" + L.charts.holdings_series + "</extra>" }
    ];
    var layout = JSON.parse(JSON.stringify(LAYOUT_BASE));
    layout.yaxis.title = { text: L.charts.equity_y, font: { size: 11 } };
    layout.shapes = sessionBands(rows);
    Plotly.react("equity-chart", traces, layout, CONFIG);
    text("tz-note", L.charts.tz_note + " " + TZ + " " + L.charts.tz_suffix);
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
      paintStatus(); paintKPIs(); paintFunding(); paintTable(); drawPositions();
      if ($("settings-fields").dataset.dirty !== "1") {
        text("ready-flag", s.readiness.ready ? L.setup.ready : L.setup.not_ready);
      }
      $("money-btn").disabled = !!s.readiness.ledger_ready;
      if (s.readiness.ledger_ready) text("money-btn", L.setup.money_done);
      $("adjust-field").classList.toggle("hidden", !s.readiness.ledger_ready);
      var book = s.book || {};
      if (book.cash_gbp !== null && book.cash_gbp !== undefined) {
        text("adjust-current", L.setup.money_current + " " + money(book.cash_gbp));
      }
      text("halt-state", s.halted ? L.controls.halt_on : L.controls.halt_off);
      $("halt-raise-btn").disabled = !!s.halted;
      $("halt-clear-btn").disabled = !s.halted;
    }).catch(function () { /* transient; the next poll retries */ });
  }

  function refreshHistory(force) {
    var now = Date.now();
    if (!force && now - lastHistoryAt < HISTORY_MS) return Promise.resolve();
    lastHistoryAt = now;
    return getJSON("/api/sessions").then(function (s) {
      SESSIONS = s.sessions || [];
    }).catch(function () { SESSIONS = []; }).then(function () {
      return getJSON("/api/history?days=3&max=1500");
    }).then(function (h) {
      drawEquity(h.rows || []);
      chartsBuilt = true;
    }).catch(function () { });
  }

  function sessionBands(rows) {
    /* Shade each regular US session so the curve can be read against the
       hours it was actually produced in. Without them the axis is just wall
       clock in whatever zone the reader happens to sit in, and a flat
       overnight stretch looks the same as a flat trading day. */
    if (!rows.length || !SESSIONS.length) return [];
    var first = new Date(rows[0].ts).getTime();
    var last = new Date(rows[rows.length - 1].ts).getTime();
    return SESSIONS.filter(function (s) {
      return s.is_full &&
             new Date(s.close_utc).getTime() >= first &&
             new Date(s.open_utc).getTime() <= last;
    }).map(function (s) {
      return { type: "rect", xref: "x", yref: "paper",
               x0: s.open_utc, x1: s.close_utc, y0: 0, y1: 1,
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
    refreshState().then(function () { return refreshHistory(true); });
    setInterval(function () { refreshState(); refreshHistory(false); }, STATE_MS);
  });
})();
