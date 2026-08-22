/* Manual order page. Separate from the dashboard on purpose: placing an
   order by hand is a different act from watching the strategy, and mixing
   the two invites a mis-click. Nothing here submits without an explicit
   confirmation in the dialog. */
(function () {
  "use strict";

  var L = null;

  function $(id) { return document.getElementById(id); }
  function text(id, v) { var el = $(id); if (el) el.textContent = v; }

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

  function localTime(iso) {
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("zh-CN", { hour12: false });
  }

  function paintStatic() {
    document.title = L.app.orders_title;
    text("app-title", L.app.orders_title);
    text("nav-dash", L.app.nav_dashboard);
    text("nav-orders", L.app.nav_orders);
    text("orders-heading", L.orders.heading);
    text("orders-warning", L.orders.warning);
    text("orders-attribution", L.orders.attribution);
    text("pick-label", L.orders.pick);
    text("custom-label", L.orders.custom);
    text("qty-label", L.orders.qty);
    text("qty-hint", L.orders.qty_hint);
    text("rehearse-label", L.orders.rehearse);
    text("confirm-label", L.orders.confirm_box);
    text("submit-btn", L.orders.submit);
    text("history-heading", L.orders.history);
    text("modal-go", L.orders.confirm_go);
    text("modal-cancel", L.orders.confirm_cancel);
  }

  function paintStatus(state) {
    var parts = [
      "<span><b>" + L.status.env + "</b> " + state.env + "</span>",
      "<span><b>" + L.status.halt + "</b> <span class='pill " +
        (state.halted ? "bad'>" + L.status.halt_on : "off'>" + L.status.halt_off) +
        "</span></span>"
    ];
    $("statusbar").innerHTML = parts.join("");
  }

  function loadInstruments() {
    return getJSON("/api/instruments").then(function (r) {
      var sel = $("pick");
      sel.innerHTML = "<option value=''>" + L.app.dash + "</option>";
      (r.instruments || []).forEach(function (i) {
        var opt = document.createElement("option");
        opt.value = i.ticker;
        opt.textContent = i.symbol + "  (" + i.ticker + ")";
        sel.appendChild(opt);
      });
    });
  }

  function loadHistory() {
    return getJSON("/api/manual").then(function (r) {
      var rows = r.entries || [];
      var wrap = $("history-wrap");
      if (!rows.length) {
        wrap.innerHTML = "<p class='note'>" + L.orders.history_empty + "</p>";
        return;
      }
      var t = L.orders.table;
      var head = "<tr><th>" + t.time + "</th><th>" + t.ticker + "</th><th>" +
        t.qty + "</th><th>" + t.outcome + "</th><th>" + t.detail + "</th></tr>";
      var body = rows.map(function (e) {
        var outcome = L.orders.outcome[e.outcome] || e.outcome;
        var reason = L.orders.reasons[e.reason] || (e.reason || "");
        return "<tr><td>" + localTime(e.ts) + "</td><td>" + e.ticker +
          "</td><td>" + e.quantity + "</td><td>" + outcome +
          "</td><td>" + reason + "</td></tr>";
      }).join("");
      wrap.innerHTML = "<table>" + head + body + "</table>";
    });
  }

  function chosenTicker() {
    var custom = $("custom-ticker").value.trim();
    return custom || $("pick").value;
  }

  function submit() {
    var ticker = chosenTicker();
    var qty = $("qty").value.trim();
    var real = !$("rehearse").checked;
    var confirmed = $("confirm").checked;
    if (!ticker || !qty) {
      text("outcome", L.orders.outcome.refused);
      return;
    }
    var line = (real ? L.orders.confirm_real : L.orders.confirm_rehearse) +
               " " + ticker + " " + qty;
    text("modal-title", L.orders.confirm_title);
    text("modal-line", line);
    $("modal-back").classList.remove("hidden");
    $("modal-go").onclick = function () {
      $("modal-back").classList.add("hidden");
      postJSON("/api/manual", {
        ticker: ticker, quantity: qty, real: real, confirm: confirmed
      }).then(function (r) {
        var res = (r.body && r.body.result) || {};
        var outcome = L.orders.outcome[res.outcome] || res.outcome || "";
        var reason = L.orders.reasons[res.reason] || res.reason || "";
        var wrap = L.orders.reason_wrap;
        text("outcome", outcome + (reason ? wrap[0] + reason + wrap[1] : ""));
        $("confirm").checked = false;
        loadHistory();
      });
    };
  }

  function wire() {
    $("submit-btn").onclick = submit;
    $("modal-cancel").onclick = function () {
      $("modal-back").classList.add("hidden");
    };
    $("modal-back").onclick = function (e) {
      if (e.target === $("modal-back")) $("modal-back").classList.add("hidden");
    };
  }

  getJSON("/assets/labels.json").then(function (labels) {
    L = labels;
    paintStatic();
    wire();
    loadInstruments();
    loadHistory();
    getJSON("/api/state").then(paintStatus).catch(function () { });
  });
})();
