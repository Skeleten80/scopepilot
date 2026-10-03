"""Web console: hand-controller dashboard + JSON API (stdlib only).

Endpoints::

    GET  /                  dashboard page
    GET  /api/state         mount snapshot + manual_override claim (JSON)
    GET  /api/targets?q=    catalog search (JSON)
    GET  /api/plan?path=    AstroCapture night-plan targets (JSON)
    POST /api/goto          {ra_hours,dec_deg} | {az_deg,alt_deg} | {name}
    POST /api/sync          {ra_hours,dec_deg} | {name}
    POST /api/jog           {direction: up|down|left|right, rate, seconds?}
    POST /api/jog_stop      {axis?: az|alt}
    POST /api/stop          abort goto + stop jog
    POST /api/track         {mode: off|alt-az|eq-north|eq-south}
    POST /api/park          park the mount
    POST /api/unpark        clear parked, resume tracking
    POST /api/claim         {by} mark the mount operator-driven
    POST /api/release       clear the manual-override claim

The manual-override claim is the coexistence signal: while claimed,
AstroCapture's sequencer should pause instead of slewing against the
operator (see ``scopepilot.bridge.check_manual_override``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scopepilot import bridge
from scopepilot.controller import AlignmentError, TelescopeController
from scopepilot.nexstar import NexStarError

log = logging.getLogger(__name__)
_HTML_PATH = Path(__file__).with_name("dash.html")


class _ScopeState:
    def __init__(self, controller: TelescopeController) -> None:
        self.controller = controller
        self._lock = threading.Lock()
        self._claim = {"claimed": False, "by": None, "at": None}

    def claim_snapshot(self) -> dict:
        with self._lock:
            return dict(self._claim)

    def set_claim(self, by: str | None) -> dict:
        with self._lock:
            if by:
                self._claim = {"claimed": True, "by": by, "at": time.time()}
            else:
                self._claim = {"claimed": False, "by": None, "at": None}
            return dict(self._claim)


def _snapshot(state: _ScopeState) -> dict:
    st = state.controller.status()
    return {
        "ok": True,
        "backend": st.backend,
        "model": st.model,
        "connected": st.connected,
        "aligned": st.aligned,
        "tracking_mode": st.tracking_mode,
        "ra_hours": st.ra_hours,
        "dec_deg": st.dec_deg,
        "az_deg": st.az_deg,
        "alt_deg": st.alt_deg,
        "slewing": st.slewing,
        "parked": st.parked,
        "note": st.note,
        "manual_override": state.claim_snapshot(),
        "server_time": time.time(),
    }


class _Handler(BaseHTTPRequestHandler):
    state: _ScopeState  # set by create_server

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter than the default stderr spew
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _ok(self, **extra) -> None:
        self._send_json({"ok": True, **extra})

    def _fail(self, err: Exception, code: int = 500) -> None:
        self._send_json({"ok": False, "error": str(err)}, code=code)

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._serve_dash()
            elif parsed.path == "/api/state":
                self._send_json(_snapshot(self.state))
            elif parsed.path == "/api/targets":
                q = parse_qs(parsed.query).get("q", [""])[0]
                self._send_json({"ok": True,
                                 "results": bridge.search_targets(q)})
            elif parsed.path == "/api/plan":
                path = parse_qs(parsed.query).get("path", [""])[0]
                if not path:
                    self._fail(ValueError("missing ?path="), 400)
                else:
                    self._send_json({"ok": True,
                                     "targets": bridge.read_night_plan(path)})
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # never leak a traceback to the UI
            self._fail(exc)

    def _serve_dash(self) -> None:
        body = _HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: C901 - routing table
        parsed = urlparse(self.path)
        ctl = self.state.controller
        try:
            body = self._read_json()
            if parsed.path == "/api/goto":
                if "name" in body:
                    ra, dec, src, _settled = ctl.goto_target(body["name"], wait=False)
                    self._ok(ra_hours=ra, dec_deg=dec, source=src)
                elif "ra_hours" in body:
                    ctl.goto_radec(float(body["ra_hours"]),
                                   float(body["dec_deg"]), wait=False)
                    self._ok()
                elif "az_deg" in body:
                    ctl.goto_altaz(float(body["az_deg"]),
                                   float(body["alt_deg"]), wait=False)
                    self._ok()
                else:
                    self._fail(ValueError(
                        "need {name} or {ra_hours,dec_deg} or {az_deg,alt_deg}"),
                        400)
            elif parsed.path == "/api/sync":
                if "name" in body:
                    ra, dec, src = ctl.sync_target(body["name"])
                    self._ok(ra_hours=ra, dec_deg=dec, source=src)
                else:
                    ctl.sync_here(float(body["ra_hours"]),
                                  float(body["dec_deg"]))
                    self._ok()
            elif parsed.path == "/api/jog":
                ctl.jog(body.get("direction", "up"),
                        int(body.get("rate", 5)),
                        body.get("seconds"))
                self._ok()
            elif parsed.path == "/api/jog_stop":
                ctl.backend.jog_stop(body.get("axis"))
                self._ok()
            elif parsed.path == "/api/stop":
                ctl.abort()
                self._ok()
            elif parsed.path == "/api/track":
                mode = ctl.set_tracking(body.get("mode", "alt-az"))
                self._ok(tracking_mode=mode)
            elif parsed.path == "/api/park":
                ctl.park(wait=False)
                self._ok()
            elif parsed.path == "/api/unpark":
                ctl.unpark()
                self._ok()
            elif parsed.path == "/api/claim":
                self._ok(manual_override=self.state.set_claim(
                    body.get("by") or "operator"))
            elif parsed.path == "/api/release":
                self._ok(manual_override=self.state.set_claim(None))
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except AlignmentError as exc:
            self._fail(exc, 409)
        except (NexStarError, ValueError, LookupError, KeyError) as exc:
            self._fail(exc, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(exc)


def create_server(
    controller: TelescopeController,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Build (not yet serving) the console server."""
    handler = type("_BoundHandler", (_Handler,), {"state": _ScopeState(controller)})
    server = ThreadingHTTPServer((host, port), handler)
    return server


def serve(
    controller: TelescopeController,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve the console until Ctrl-C."""
    server = create_server(controller, host, port)
    addr = server.server_address
    print(f"ScopePilot console on http://{addr[0]}:{addr[1]}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        server.server_close()
