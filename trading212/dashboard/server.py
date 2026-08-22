"""The dashboard's local web server.

Responsibility: serve two pages and a small JSON surface on the loopback
interface, own the sampler's lifetime, and stop cleanly. Built on the
standard library so the dashboard adds no dependency to a repository whose
other half places real orders.

Binding and origin: the socket is bound to 127.0.0.1, so nothing outside
this machine can reach it. That alone does not stop a web page you happen to
be visiting from posting to it, so every write route also requires a header
carrying a token minted at start-up and embedded only in the pages this
server itself serves. A cross-origin page cannot read that token, and the
custom header forces a preflight this server refuses.

Sampling lifetime: the sampler starts with the server and stops with it, and
the interface can stop it on its own. Stopping the sampler stops only the
polling; the strategy runs in a separate scheduled process that this server
neither starts, stops, nor talks to.

Out of scope: what the routes do, which belongs to api.py; sampling, which
belongs to collector.py; page markup, which belongs to assets/.

Public functions:
    build_server(env, port)   Construct the server and its sampler.
    main(argv=None)           Command-line entry point.

Constants:
    DEFAULT_PORT   int  8787. A fixed high port so the bookmark keeps
                        working between runs.
    HOST           str  "127.0.0.1". Loopback only, never a wildcard bind:
                        this interface can place real orders.
    DASH_HEADER   str  "X-Dashboard-Nonce".
    PLOTLY_ASSET   str  The charting library, served from the installed
                        plotly package rather than copied into the
                        repository, so nothing large is committed and the
                        browser caches it once.

Inputs:
    trading212/dashboard/assets/*
    the installed plotly package's bundled plotly.min.js
Outputs:
    None directly; the routes write through api.py.

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["build_server", "main", "DEFAULT_PORT", "HOST", "DASH_HEADER"]

import argparse
import json
import secrets
import signal
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common.logging_setup import get_logger
from trading212.dashboard import api
from trading212.dashboard.collector import Collector
from trading212.dashboard.context import AppContext

log = get_logger("t212.dashboard")

DEFAULT_PORT = 8787
HOST = "127.0.0.1"
DASH_HEADER = "X-Dashboard-Nonce"
PLOTLY_ASSET = "plotly.min.js"

_ASSETS = Path(__file__).resolve().parent / "assets"
_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".js": "application/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".json": "application/json; charset=utf-8",
                  ".svg": "image/svg+xml"}


def _plotly_path() -> Path | None:
    """Locate the plotly bundle inside the installed package."""
    try:
        import plotly
        candidate = Path(plotly.__file__).parent / "package_data" / PLOTLY_ASSET
        return candidate if candidate.is_file() else None
    except Exception:
        return None


class _Handler(BaseHTTPRequestHandler):
    """One request. Server-wide state hangs off the server object."""

    server_version = "QuantDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        log.info("[http] %s", fmt % args)

    def _send(self, status: int, body: bytes, content_type: str,
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authorized(self) -> bool:
        """Writes need the token this server minted and embedded in its pages."""
        return self.headers.get(DASH_HEADER) == self.server.token

    # -- routes --------------------------------------------------------

    def do_GET(self) -> None:                       # noqa: N802  stdlib name
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        ctx, collector = self.server.ctx, self.server.collector
        try:
            if route in ("/", "/index.html"):
                return self._page("index.html")
            if route in ("/orders", "/orders.html"):
                return self._page("orders.html")
            if route.startswith("/assets/"):
                return self._asset(route[len("/assets/"):])
            if route == "/api/state":
                return self._json(*api.get_state(ctx, collector))
            if route == "/api/history":
                return self._json(*api.get_history(
                    ctx, int(_first(query, "days", 3)),
                    int(_first(query, "max", 1500))))
            if route == "/api/settings":
                return self._json(*api.get_settings(ctx))
            if route == "/api/instruments":
                return self._json(*api.get_instruments(ctx))
            if route == "/api/manual":
                return self._json(*api.get_manual(ctx))
            if route == "/api/sessions":
                return self._json(*api.get_sessions(
                    ctx, int(_first(query, "days", api.SESSION_WINDOW_DAYS))))
            self._json(404, {"problem": "unknown_route", "route": route})
        except Exception as exc:                    # never leak a traceback
            log.error("[http] GET %s failed: %r", route, exc)
            self._json(500, {"problem": "server_error", "detail": repr(exc)[:300]})

    def do_POST(self) -> None:                      # noqa: N802  stdlib name
        route = urlparse(self.path).path
        ctx, collector = self.server.ctx, self.server.collector
        if not self._authorized():
            return self._json(403, {"problem": "bad_token"})
        body = self._body()
        try:
            if route == "/api/settings":
                return self._json(*api.post_settings(ctx, body))
            if route == "/api/collector":
                return self._json(*api.post_collector(ctx, collector, body))
            if route == "/api/ledger/init":
                return self._json(*api.post_ledger_init(ctx, body))
            if route == "/api/ledger/allocation":
                return self._json(*api.post_allocation(ctx, body))
            if route == "/api/halt":
                return self._json(*api.post_halt(ctx, body))
            if route == "/api/manual":
                return self._json(*api.post_manual(ctx, body))
            if route == "/api/shutdown":
                threading.Thread(target=self.server.request_shutdown,
                                 daemon=True).start()
                return self._json(200, {"ok": True})
            self._json(404, {"problem": "unknown_route", "route": route})
        except Exception as exc:
            log.error("[http] POST %s failed: %r", route, exc)
            self._json(500, {"problem": "server_error", "detail": repr(exc)[:300]})

    # -- static --------------------------------------------------------

    def _page(self, name: str) -> None:
        """Serve a page with this run's token substituted in."""
        path = _ASSETS / name
        if not path.is_file():
            return self._json(404, {"problem": "page_missing", "page": name})
        html = path.read_text(encoding="utf-8").replace("__DASH_TOKEN__",
                                                        self.server.token)
        self._send(200, html.encode("utf-8"), _CONTENT_TYPES[".html"])

    def _asset(self, name: str) -> None:
        if name == PLOTLY_ASSET:
            path = _plotly_path()
            if path is None:
                return self._json(404, {"problem": "plotly_not_installed"})
            # Immutable for a year: the file is version-pinned by the
            # installed package, so the browser should parse it once ever.
            return self._send(200, path.read_bytes(), _CONTENT_TYPES[".js"],
                              cache="public, max-age=31536000, immutable")
        path = (_ASSETS / name).resolve()
        if not path.is_file() or _ASSETS.resolve() not in path.parents:
            return self._json(404, {"problem": "asset_missing", "asset": name})
        content = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), content, cache="no-cache")


def _first(query: dict, key: str, default):
    values = query.get(key)
    return values[0] if values else default


class _Server(ThreadingHTTPServer):
    """The server plus the state its handlers need."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, ctx: AppContext,
                 collector: Collector) -> None:
        super().__init__(address, handler)
        self.ctx = ctx
        self.collector = collector
        self.token = secrets.token_urlsafe(24)

    def request_shutdown(self) -> None:
        """Stop sampling, then stop serving. Order matters: a sampler left
        running against a closed server would keep polling the venue."""
        self.collector.stop()
        self.shutdown()


def build_server(env: str | None = None, port: int = DEFAULT_PORT):
    """Construct the context, sampler and server without starting them."""
    ctx = AppContext(env)
    collector = Collector(ctx)
    return _Server((HOST, port), _Handler, ctx, collector)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard", description="Trading 212 local dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--env", default=None,
                        help="override QUANT_ENV for this run")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--no-sampling", action="store_true",
                        help="serve without starting the sampler")
    args = parser.parse_args(argv)

    server = build_server(args.env, args.port)
    url = f"http://{HOST}:{args.port}/"
    if not args.no_sampling:
        server.collector.start()

    def _bye(_signum, _frame):
        threading.Thread(target=server.request_shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)

    print(f"dashboard: {url}")
    print(f"environment: {server.ctx.env}   strategy: {server.ctx.strategy_id}")
    print("press Ctrl-C to stop")
    if not args.no_open:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.collector.stop()
        server.ctx.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
