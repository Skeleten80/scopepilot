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
    GET  /api/align-stars   suggested alignment stars with true az/alt
    GET  /api/tonight       tonight's best-placed targets for the site
    GET  /api/identify      nearest catalog object to current pointing
    GET  /api/user-objects  saved user objects
    POST /api/user-objects  {name, ra_hours, dec_deg} or {name, use_current}
    DELETE /api/user-objects?name=  delete one
    POST /api/undo-goto     slew back to the pre-goto position
    GET  /api/hc            hand-controller info (model/fw/clock/GPS/bus)
    POST /api/hc-sync       set HC clock + site from this computer
    GET  /api/backlash      ?axis=az|alt&direction=1|-1 -> current value
    POST /api/backlash      {axis, direction, value} anti-backlash 0-99
    GET  /api/cordwrap      {enabled}
    POST /api/cordwrap      {enabled} set cordwrap on/off

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
from scopepilot.pointing import suggest_alignment_stars

log = logging.getLogger(__name__)
_HTML_PATH = Path(__file__).with_name("dash.html")


class _ScopeState:
    def __init__(self, controller: TelescopeController, make_rig=None) -> None:
        self.controller = controller
        self.make_rig = make_rig
        self._lock = threading.Lock()
        self._claim = {"claimed": False, "by": None, "at": None}
        self._center = {"running": False, "events": [], "report": None}

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

    # -- closed-loop centering runs in a worker thread -------------------
    def center_snapshot(self) -> dict:
        with self._lock:
            return {"running": self._center["running"],
                    "events": list(self._center["events"]),
                    "report": self._center["report"]}

    def center_start(self, name: str, **kwargs) -> bool:
        """Start a centering run; False when one is already running."""
        with self._lock:
            if self._center["running"]:
                return False
            self._center = {"running": True, "events": [], "report": None}
        thread = threading.Thread(target=self._center_run,
                                  args=(name, kwargs), daemon=True)
        thread.start()
        return True

    def _center_run(self, name: str, kwargs: dict) -> None:
        events: list[str] = []
        report: dict | None = None
        try:
            if self.make_rig is None:
                raise RuntimeError("no camera rig configured on this server")
            rig = self.make_rig()
            rig.connect()
            try:
                report = self.controller.center_target(
                    name, rig, on_event=events.append, **kwargs)
            finally:
                rig.disconnect()
        except Exception as exc:  # report, don't kill the server
            events.append(f"ERROR: {exc}")
            report = {"converged": False, "iters": 0,
                      "final_sep_arcmin": None, "events": events,
                      "error": str(exc)}
        with self._lock:
            self._center = {"running": False, "events": events,
                            "report": report}


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
        "undo_available": state.controller._pre_goto is not None,
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
            elif parsed.path == "/api/center-status":
                self._send_json({"ok": True,
                                 **self.state.center_snapshot()})
            elif parsed.path == "/api/pointing":
                self._send_json({"ok": True,
                                 "pointing":
                                 self.state.controller.pointing_status()})
            elif parsed.path == "/api/align-stars":
                ctl = self.state.controller
                if ctl.site_lat is None or ctl.site_lon is None:
                    self._fail(ValueError(
                        "no site configured; set site_lat/site_lon"), 400)
                else:
                    stars = suggest_alignment_stars(ctl.site_lat, ctl.site_lon)
                    self._send_json({"ok": True,
                                     "stars": [{"name": name,
                                                "az_deg": round(az, 1),
                                                "alt_deg": round(alt, 1)}
                                               for name, az, alt in stars]})
            elif parsed.path == "/api/tonight":
                ctl = self.state.controller
                qs = parse_qs(parsed.query)
                lat = qs.get("lat", [None])[0]
                lon = qs.get("lon", [None])[0]
                lat = float(lat) if lat else ctl.site_lat
                lon = float(lon) if lon else ctl.site_lon
                if lat is None or lon is None:
                    self._fail(ValueError(
                        "no site configured; pass ?lat=&lon="), 400)
                else:
                    items, src = bridge.tonight_list(lat, lon)
                    self._send_json({"ok": True, "targets": items,
                                     "source": src})
            elif parsed.path == "/api/identify":
                ctl = self.state.controller
                st = ctl.status()
                hit = bridge.identify(st.ra_hours, st.dec_deg)
                self._send_json({"ok": True, "result": hit,
                                 "ra_hours": st.ra_hours,
                                 "dec_deg": st.dec_deg})
            elif parsed.path == "/api/user-objects":
                self._send_json({"ok": True,
                                 "objects":
                                 self.state.controller.list_user_objects()})
            elif parsed.path == "/api/hc":
                self._send_json({"ok": True,
                                 "hc": self.state.controller.hc_info()})
            elif parsed.path == "/api/backlash":
                ctl = self.state.controller
                qs = parse_qs(parsed.query)
                axis = qs.get("axis", ["az"])[0]
                direction = int(qs.get("direction", ["1"])[0])
                self._send_json({"ok": True,
                                 "value": ctl.get_backlash(axis, direction)})
            elif parsed.path == "/api/cordwrap":
                self._send_json({"ok": True, "enabled":
                                 self.state.controller.cordwrap_enabled()})
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
            elif parsed.path == "/api/sync-star":
                star = ctl.record_sync_star(body.get("name", ""))
                self._ok(star={"name": star.name,
                               "true_az": star.true_az, "true_alt": star.true_alt,
                               "reported_az": star.reported_az,
                               "reported_alt": star.reported_alt},
                         pending=len(ctl._sync_stars))
            elif parsed.path == "/api/align-fit":
                model = ctl.fit_pointing()
                ctl.save_pointing()
                self._ok(pointing=ctl.pointing_status())
            elif parsed.path == "/api/align-clear":
                ctl.clear_pointing()
                self._ok()
            elif parsed.path == "/api/undo-goto":
                ctl.undo_goto(wait=False)
                self._ok()
            elif parsed.path == "/api/user-objects":
                name = (body.get("name") or "").strip()
                if not name:
                    self._fail(ValueError("need {name}"), 400)
                elif body.get("use_current"):
                    obj = ctl.save_current_as(name)
                    self._ok(object=obj)
                elif "ra_hours" in body and "dec_deg" in body:
                    obj = ctl.add_user_object(
                        name, float(body["ra_hours"]),
                        float(body["dec_deg"]))
                    self._ok(object=obj)
                else:
                    self._fail(ValueError(
                        "need {name, use_current} or {name, ra_hours, dec_deg}"),
                        400)
            elif parsed.path == "/api/goto-user":
                name = (body.get("name") or "").strip()
                if not name:
                    self._fail(ValueError("need {name}"), 400)
                else:
                    ra, dec, _settled = ctl.goto_user_object(name, wait=False)
                    self._ok(ra_hours=ra, dec_deg=dec)
            elif parsed.path == "/api/hc-sync":
                clock = ctl.sync_clock()
                site = None
                if ctl.site_lat is not None and ctl.site_lon is not None:
                    ctl.set_site(ctl.site_lat, ctl.site_lon)
                    site = {"lat": ctl.site_lat, "lon": ctl.site_lon}
                self._ok(clock=clock, site=site)
            elif parsed.path == "/api/backlash":
                ctl.set_backlash(body.get("axis", "az"),
                                 int(body.get("direction", 1)),
                                 int(body.get("value", 0)))
                self._ok()
            elif parsed.path == "/api/cordwrap":
                ctl.set_cordwrap(bool(body.get("enabled")))
                self._ok(enabled=ctl.cordwrap_enabled())
            elif parsed.path == "/api/center":
                name = body.get("name", "")
                if not name:
                    self._fail(ValueError("need {name}"), 400)
                elif not self.state.center_start(
                        name,
                        exposure_s=float(body.get("exposure_s", 5.0)),
                        tolerance_arcmin=float(
                            body.get("tolerance_arcmin", 1.0)),
                        max_iters=int(body.get("max_iters", 4))):
                    self._fail(RuntimeError("a centering run is already "
                                            "in progress"), 409)
                else:
                    self._ok(started=name)
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except AlignmentError as exc:
            self._fail(exc, 409)
        except (NexStarError, ValueError, LookupError, KeyError) as exc:
            self._fail(exc, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(exc)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/user-objects":
                name = parse_qs(parsed.query).get("name", [""])[0]
                if not name:
                    self._fail(ValueError("missing ?name="), 400)
                else:
                    self.state.controller.delete_user_object(name)
                    self._ok()
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except (NexStarError, ValueError, LookupError, KeyError) as exc:
            self._fail(exc, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(exc)


def create_server(
    controller: TelescopeController,
    host: str = "127.0.0.1",
    port: int = 8765,
    make_rig=None,
) -> ThreadingHTTPServer:
    """Build (not yet serving) the console server.

    *make_rig* is an optional zero-arg factory returning a
    ``capture_and_solve`` callable for ``POST /api/center``.
    """
    handler = type("_BoundHandler", (_Handler,),
                   {"state": _ScopeState(controller, make_rig)})
    server = ThreadingHTTPServer((host, port), handler)
    return server


def serve(
    controller: TelescopeController,
    host: str = "127.0.0.1",
    port: int = 8765,
    make_rig=None,
) -> None:
    """Serve the console until Ctrl-C."""
    server = create_server(controller, host, port, make_rig)
    addr = server.server_address
    print(f"ScopePilot console on http://{addr[0]}:{addr[1]}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        server.server_close()
