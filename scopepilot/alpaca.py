"""ASCOM Alpaca bridge: expose the mount as a standard Alpaca telescope.

Lets Windows software (N.I.N.A., SharpCap, Stellarium, ...) drive the 6SE
through ScopePilot without any Windows driver install:

    scopepilot alpaca            # Alpaca on TCP 11111, discovery on UDP 32227

In N.I.N.A.: Equipment -> Telescope -> "ASCOM Alpaca", enter this machine's
IP and port 11111, connect. The Alpaca device is telescope 0.

Conformance notes (honest): this implements the Alpaca v1 Telescope
profile's operational surface. Device errors are returned as HTTP 200
with ErrorNumber/ErrorMessage in the body (per the Alpaca spec -- HTTP
4xx is reserved for malformed requests). Only one server may own the
mount at a time: run *either* ``scopepilot dash`` *or* ``scopepilot
alpaca``, not both.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scopepilot.astro import (
    altaz_to_radec,
    datetime_to_jd,
    gmst_hours,
    radec_to_altaz,
    utcnow,
    wrap180,
)
from scopepilot.nexstar import NexStarError

log = logging.getLogger(__name__)

# Alpaca error numbers (0x400 block, per the Alpaca specification).
ERR_DRIVER = 0x400
ERR_INVALID_VALUE = 0x401
ERR_VALUE_NOT_SET = 0x402
ERR_NOT_CONNECTED = 0x403
ERR_INVALID_OPERATION = 0x406
ERR_NOT_IMPLEMENTED = 0x408
ERR_ACTION_NOT_IMPLEMENTED = 0x40C

_API_VERSIONS = [1]
_DEVICE_TYPE = "telescope"
_DEVICE_NUMBER = 0

#: 6SE optical facts (SI units, as Alpaca expects).
_APERTURE_M = 0.150
_FOCAL_M = 1.500
_APERTURE_AREA_M2 = round(math.pi * (_APERTURE_M / 2) ** 2, 5)

#: Alpaca DriveRate -> ScopePilot differential rate name.
_DRIVE_RATES = {0: "sidereal", 1: "lunar", 2: "solar"}


class AlpacaError(Exception):
    """Device-level error -> HTTP 200 envelope with ErrorNumber."""

    def __init__(self, number: int, message: str):
        super().__init__(message)
        self.number = number
        self.message = message


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


class AlpacaTelescope:
    """Alpaca v1 Telescope device backed by a TelescopeController."""

    def __init__(self, controller):
        self.controller = controller
        self._server_txn = 0
        self._lock = threading.Lock()
        self._target_ra: float | None = None
        self._target_dec: float | None = None
        self._pulse_guiding = False
        self._does_refraction = False
        self._slew_settle_s = 0

    # -- envelope ---------------------------------------------------------
    def _envelope(self, value, client_txn: int, err: AlpacaError | None):
        with self._lock:
            self._server_txn += 1
            txn = self._server_txn
        return {
            "Value": value,
            "ClientTransactionID": client_txn,
            "ServerTransactionID": txn,
            "ErrorNumber": err.number if err else 0,
            "ErrorMessage": err.message if err else "",
        }

    def _need_conn(self):
        if not self.controller._connected:
            raise AlpacaError(ERR_NOT_CONNECTED, "device not connected")

    def _status(self):
        self._need_conn()
        return self.controller.status()

    # -- common device ----------------------------------------------------
    def g_connected(self, p):
        return self.controller._connected

    def p_connected(self, p):
        want = _bool(p.get("Connected", "false"))
        if want and not self.controller._connected:
            self.controller.connect()
        elif not want and self.controller._connected:
            self.controller.track_stop()
            self.controller.disconnect()
        return None

    # -- properties ---------------------------------------------------------
    def g_altitude(self, p):
        return self._status().alt_deg

    def g_azimuth(self, p):
        return self._status().az_deg

    def g_rightascension(self, p):
        return self._status().ra_hours

    def g_declination(self, p):
        return self._status().dec_deg

    def g_slewing(self, p):
        return self._status().slewing

    def g_tracking(self, p):
        return self._status().tracking_mode != "off"

    def p_tracking(self, p):
        self._need_conn()
        self.controller.set_tracking(
            "alt-az" if _bool(p.get("Tracking", "false")) else "off")
        return None

    def g_trackingrate(self, p):
        name = self.controller.rate_tracking
        inv = {v: k for k, v in _DRIVE_RATES.items()}
        return inv.get(name, 0)

    def p_trackingrate(self, p):
        self._need_conn()
        try:
            rate = int(p.get("TrackingRate", "0"))
        except ValueError:
            raise AlpacaError(ERR_INVALID_VALUE, "TrackingRate must be 0-2")
        if rate not in _DRIVE_RATES:
            raise AlpacaError(ERR_NOT_IMPLEMENTED,
                              f"drive rate {rate} not implemented "
                              "(0=sidereal, 1=lunar, 2=solar)")
        self.controller.set_tracking("alt-az")
        self.controller.track_rate(_DRIVE_RATES[rate])
        return None

    def g_trackingrates(self, p):
        return [{"Value": k, "Name": v} for k, v in _DRIVE_RATES.items()]

    def g_targetrightascension(self, p):
        if self._target_ra is None:
            raise AlpacaError(ERR_VALUE_NOT_SET, "no slew target yet")
        return self._target_ra

    def g_targetdeclination(self, p):
        if self._target_dec is None:
            raise AlpacaError(ERR_VALUE_NOT_SET, "no slew target yet")
        return self._target_dec

    def g_athome(self, p):
        return False

    def g_atpark(self, p):
        return self._status().parked

    def g_alignmentmode(self, p):
        return 0  # algAltAz

    def g_equatorialsystem(self, p):
        return 0  # equJ2000

    def g_canpark(self, p): return True
    def g_canunpark(self, p): return True
    def g_canslew(self, p): return True
    def g_canslewaltaz(self, p): return True
    def g_canslewasync(self, p): return True
    def g_cansync(self, p): return True
    def g_canpulseguide(self, p): return True
    def g_canfindhome(self, p): return False
    def g_cansetpark(self, p): return False
    def g_cansetpierside(self, p): return False
    def g_cansetguiderates(self, p): return False
    def g_cansetdeclinationrate(self, p): return False
    def g_cansetrightascensionrate(self, p): return False

    def g_sideofpier(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED,
                          "SideOfPier is equatorial-only")

    def g_declinationrate(self, p): return 0.0
    def g_rightascensionrate(self, p): return 0.0

    def p_declinationrate(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED,
                          "use TrackingRate (0/1/2) instead")

    def p_rightascensionrate(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED,
                          "use TrackingRate (0/1/2) instead")

    def g_guideratedeclination(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "guide rates not settable")

    def g_guideraterightascension(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "guide rates not settable")

    def g_ispulseguiding(self, p):
        return self._pulse_guiding

    def g_doesrefraction(self, p):
        return self._does_refraction

    def p_doesrefraction(self, p):
        self._does_refraction = _bool(p.get("DoesRefraction", "false"))
        return None  # stored; no refraction model applied

    def g_slewsettletime(self, p):
        return self._slew_settle_s

    def p_slewsettletime(self, p):
        try:
            v = float(p.get("SlewSettleTime", "0"))
        except ValueError:
            raise AlpacaError(ERR_INVALID_VALUE, "SlewSettleTime numeric")
        if v < 0:
            raise AlpacaError(ERR_INVALID_VALUE, "SlewSettleTime >= 0")
        self._slew_settle_s = v
        return None

    def g_aperturediameter(self, p): return _APERTURE_M
    def g_aperturearea(self, p): return _APERTURE_AREA_M2
    def g_focallength(self, p): return _FOCAL_M

    def g_siteelevation(self, p): return 0.0

    def p_siteelevation(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "site elevation not modeled")

    def g_sitelatitude(self, p):
        return self.controller.site_lat

    def g_sitelongitude(self, p):
        return self.controller.site_lon

    def p_sitelatitude(self, p):
        self._need_conn()
        try:
            lat = float(p["SiteLatitude"])
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE, "SiteLatitude numeric")
        if not -90.0 <= lat <= 90.0:
            raise AlpacaError(ERR_INVALID_VALUE, "latitude -90..90")
        self.controller.site_lat = lat
        return None

    def p_sitelongitude(self, p):
        self._need_conn()
        try:
            lon = float(p["SiteLongitude"])
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE, "SiteLongitude numeric")
        if not -180.0 <= lon <= 180.0:
            raise AlpacaError(ERR_INVALID_VALUE, "longitude -180..180")
        self.controller.site_lon = lon
        return None

    def g_utcdate(self, p):
        return datetime.now(timezone.utc).isoformat()

    def p_utcdate(self, p):
        self._need_conn()
        try:
            dt = datetime.fromisoformat(p["UTCDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE, "UTCDate must be ISO-8601")
        try:
            self.controller.set_hc_time(
                dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        except (NotImplementedError, AttributeError):
            pass  # sim/INDI: clock not settable; accept anyway
        return None

    def g_siderealtime(self, p):
        lat, lon = self.controller.site_lat, self.controller.site_lon
        if lat is None or lon is None:
            raise AlpacaError(ERR_VALUE_NOT_SET, "site not set")
        jd = datetime_to_jd(utcnow())
        return (gmst_hours(jd) + lon / 15.0) % 24.0

    # -- methods ------------------------------------------------------------
    def _slew_radec(self, p):
        self._need_conn()
        try:
            ra = float(p["RightAscension"])
            dec = float(p["Declination"])
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE,
                              "RightAscension (h) and Declination (deg) required")
        if not 0.0 <= ra <= 24.0:
            raise AlpacaError(ERR_INVALID_VALUE, "RightAscension 0..24")
        if not -90.0 <= dec <= 90.0:
            raise AlpacaError(ERR_INVALID_VALUE, "Declination -90..90")
        self._target_ra, self._target_dec = ra, dec
        try:
            self.controller.goto_radec(ra, dec, wait=False)
        except NexStarError as exc:
            raise AlpacaError(ERR_DRIVER, str(exc))
        return None

    def p_slewtocoordinates(self, p):
        return self._slew_radec(p)

    def p_slewtocoordinatesasync(self, p):
        return self._slew_radec(p)

    def p_slewtoaltaz(self, p):
        return self._slew_altaz(p)

    def p_slewtoaltazasync(self, p):
        return self._slew_altaz(p)

    def _slew_altaz(self, p):
        self._need_conn()
        try:
            az = float(p["Azimuth"])
            alt = float(p["Altitude"])
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE,
                              "Azimuth and Altitude (deg) required")
        if not 0.0 <= az < 360.0:
            raise AlpacaError(ERR_INVALID_VALUE, "Azimuth 0..360")
        try:
            self.controller.goto_altaz(az, alt, wait=False)
        except NexStarError as exc:
            raise AlpacaError(ERR_DRIVER, str(exc))
        return None

    def p_abortslew(self, p):
        self._need_conn()
        self.controller.abort()
        self.controller.track_stop()
        return None

    def p_park(self, p):
        self._need_conn()
        self.controller.track_stop()
        self.controller.park()
        return None

    def p_unpark(self, p):
        self._need_conn()
        self.controller.unpark()
        return None

    def p_synctocoordinates(self, p):
        self._need_conn()
        try:
            ra = float(p["RightAscension"])
            dec = float(p["Declination"])
        except (KeyError, ValueError):
            raise AlpacaError(ERR_INVALID_VALUE,
                              "RightAscension (h) and Declination (deg) required")
        try:
            self.controller.sync_here(ra, dec)
        except NexStarError as exc:
            raise AlpacaError(ERR_DRIVER, str(exc))
        return None

    def p_synctotarget(self, p):
        self._need_conn()
        if self._target_ra is None:
            raise AlpacaError(ERR_VALUE_NOT_SET, "no slew target yet")
        try:
            self.controller.sync_here(self._target_ra, self._target_dec)
        except NexStarError as exc:
            raise AlpacaError(ERR_DRIVER, str(exc))
        return None

    def p_synctoaltaz(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "sync is equatorial-only")

    def p_findhome(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "no home sensor")

    def p_setpark(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "park position is fixed")

    def p_setpierside(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "alt-az mount")

    def p_moveaxis(self, p):
        """MoveAxis: axis 0=RA 1=Dec 2=Az 3=Alt, rate deg/s (0 stops)."""
        self._need_conn()
        try:
            axis = int(p.get("Axis", "-1"))
            rate_dps = float(p.get("Rate", "0"))
        except (ValueError, TypeError):
            raise AlpacaError(ERR_INVALID_VALUE, "Axis int, Rate numeric")
        if axis not in (0, 1, 2, 3):
            raise AlpacaError(ERR_INVALID_VALUE, "Axis 0..3")
        rate_as = rate_dps * 3600.0
        if axis == 2:
            az_rate, alt_rate = rate_as, 0.0
        elif axis == 3:
            az_rate, alt_rate = 0.0, rate_as
        else:
            az_rate, alt_rate = self._radec_axis_rates(axis, rate_as)
        try:
            self.controller.backend.variable_rate("az", az_rate)
            self.controller.backend.variable_rate("alt", alt_rate)
        except NotImplementedError:
            raise AlpacaError(ERR_NOT_IMPLEMENTED,
                              "MoveAxis needs the serial/sim backend")
        return None

    def _radec_axis_rates(self, axis: int, rate_as: float
                          ) -> tuple[float, float]:
        """Project an RA/Dec axis rate onto az/alt via local Jacobian."""
        st = self._status()
        lat, lon = self.controller.site_lat, self.controller.site_lon
        if lat is None or lon is None:
            raise AlpacaError(ERR_VALUE_NOT_SET, "site not set")
        t = utcnow()
        ra, dec = st.ra_hours, st.dec_deg
        dra_h, ddec = (1e-3, 0.0) if axis == 0 else (0.0, 0.01)
        az0, alt0 = radec_to_altaz(ra, dec, lat, lon, t)
        az1, alt1 = radec_to_altaz(ra + dra_h, dec + ddec, lat, lon, t)
        # rate_as is "/s along the axis; convert via the Jacobian
        scale = rate_as / ((dra_h * 15 * 3600) if axis == 0 else ddec * 3600)
        return (wrap180(az1 - az0) * 3600.0 * scale,
                (alt1 - alt0) * 3600.0 * scale)

    def p_pulseguide(self, p):
        """Timed guide pulse; direction 0=N 1=S 2=E 3=W, duration ms."""
        self._need_conn()
        try:
            direction = int(p.get("Direction", "-1"))
            duration_ms = float(p.get("Duration", "0"))
        except (ValueError, TypeError):
            raise AlpacaError(ERR_INVALID_VALUE,
                              "Direction int 0..3, Duration numeric ms")
        if direction not in (0, 1, 2, 3):
            raise AlpacaError(ERR_INVALID_VALUE, "Direction 0..3")
        if not 0 < duration_ms <= 10000:
            raise AlpacaError(ERR_INVALID_VALUE, "Duration 1..10000 ms")
        axis = "alt" if direction < 2 else "az"
        sign = 1.0 if direction in (0, 2) else -1.0
        try:
            self.controller.backend.variable_rate("az", 0.0)
            self.controller.backend.variable_rate("alt", 0.0)
        except NotImplementedError:
            raise AlpacaError(ERR_NOT_IMPLEMENTED,
                              "PulseGuide needs the serial/sim backend")

        def pulse():
            try:
                self.controller.backend.variable_rate(axis, sign * 15.0)
                time.sleep(duration_ms / 1000.0)
            finally:
                try:
                    self.controller.backend.variable_rate(axis, 0.0)
                except Exception:
                    pass
                self._pulse_guiding = False

        self._pulse_guiding = True
        threading.Thread(target=pulse, daemon=True,
                         name="scopepilot-pulseguide").start()
        return None

    def p_action(self, p):
        raise AlpacaError(ERR_ACTION_NOT_IMPLEMENTED,
                          f"unknown action {p.get('Action', '')!r}")

    def _commandx(self, p):
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "CommandBlind/Bool/String")

    p_commandblind = _commandx
    p_commandbool = _commandx
    p_commandstring = _commandx

    # -- dispatch -------------------------------------------------------------
    def handle(self, member: str, is_put: bool, params: dict):
        prefix = "p_" if is_put else "g_"
        fn = getattr(self, prefix + member.lower(), None)
        # some members are PUT-only or GET-only; try the other prefix
        if fn is None:
            fn = getattr(self, ("g_" if is_put else "p_") + member.lower(),
                         None)
        if fn is None:
            raise AlpacaError(ERR_NOT_IMPLEMENTED,
                              f"telescope.{member} not implemented")
        return fn(params)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ScopePilot-Alpaca/1.0"

    def log_message(self, *a):
        pass

    def _send(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if self.command == "PUT":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                body = self.rfile.read(length).decode("utf-8", "replace")
                q.update({k: v[0]
                          for k, v in parse_qs(body).items()})
        return parsed.path.rstrip("/"), q

    def _route(self):
        device: AlpacaTelescope = self.server.alpaca_device  # type: ignore
        path, params = self._params()
        is_put = self.command == "PUT"
        try:
            client_txn = int(params.get("ClientTransactionID", 0) or 0)
        except ValueError:
            client_txn = 0

        def env(value, err=None):
            return device._envelope(value, client_txn, err)

        try:
            if path == "/management/apiversions":
                self._send(env(_API_VERSIONS))
            elif path == "/management/v1/description":
                self._send(env({
                    "ServerName": "ScopePilot Alpaca",
                    "Manufacturer": "ScopePilot",
                    "ManufacturerVersion": "1.0",
                    "Location": "local"}))
            elif path == "/management/v1/configureddevices":
                self._send(env([{
                    "DeviceName": "ScopePilot 6SE",
                    "DeviceType": "Telescope",
                    "DeviceNumber": _DEVICE_NUMBER,
                    "UniqueID": "scopepilot-6se-telescope-0"}]))
            elif path.startswith("/api/v1/telescope/"):
                rest = path[len("/api/v1/telescope/"):].split("/")
                if len(rest) != 2 or rest[0] != str(_DEVICE_NUMBER):
                    self._send(env(
                        None,
                        AlpacaError(ERR_INVALID_VALUE,
                                    "only telescope 0 exists")), 400)
                    return
                try:
                    value = device.handle(rest[1], is_put, params)
                except AlpacaError as exc:
                    # Device errors -> HTTP 200 + envelope (Alpaca spec).
                    self._send(env(None, exc))
                    return
                except (NexStarError, ValueError) as exc:
                    self._send(env(None, AlpacaError(ERR_DRIVER, str(exc))))
                    return
                self._send(env(value))
            else:
                self._send(env(
                    None, AlpacaError(ERR_INVALID_VALUE, "unknown path")), 404)
        except AlpacaError as exc:
            self._send(env(None, exc))

    do_GET = _route
    do_POST = _route
    do_PUT = _route


def _discovery_loop(port: int, stop: threading.Event):
    """Answer Alpaca UDP discovery probes on 32227."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 32227))
    except OSError as exc:
        log.warning("Alpaca discovery disabled: %s", exc)
        return
    s.settimeout(0.5)
    reply = json.dumps({"AlpacaPort": port}).encode()
    while not stop.is_set():
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            break
        if data[:32].lower().startswith(b"alpacadiscovery"):
            try:
                s.sendto(reply, addr)
            except OSError:
                pass
    s.close()


def make_server(controller, host: str = "0.0.0.0", port: int = 11111):
    """Build the Alpaca HTTP server (does not start serving)."""
    device = AlpacaTelescope(controller)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.alpaca_device = device  # type: ignore
    return httpd, device


def serve_alpaca(controller, host: str = "0.0.0.0", port: int = 11111,
                 discovery: bool = True):
    """Serve the Alpaca telescope device until Ctrl-C."""
    controller.connect()
    httpd, _device = make_server(controller, host, port)
    stop = threading.Event()
    dth = None
    if discovery:
        dth = threading.Thread(target=_discovery_loop, args=(port, stop),
                               daemon=True, name="alpaca-discovery")
        dth.start()
    print(f"Alpaca telescope on http://{host}:{port} "
          f"(device 0; discovery {'on' if discovery else 'off'})")
    print("N.I.N.A.: Telescope -> ASCOM Alpaca -> this machine's IP, "
          f"port {port}. Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        controller.track_stop()
        controller.disconnect()
        httpd.server_close()
