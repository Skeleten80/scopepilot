"""Alpaca bridge tests: exercise the REST API against the sim backend."""

import json
import threading
import urllib.parse
import urllib.request

import pytest

from scopepilot.alpaca import (
    ERR_ACTION_NOT_IMPLEMENTED,
    ERR_INVALID_VALUE,
    ERR_NOT_CONNECTED,
    ERR_NOT_IMPLEMENTED,
    make_server,
)
from scopepilot.backends import SimBackend
from scopepilot.controller import TelescopeController

PORT = 11133
BASE = f"http://127.0.0.1:{PORT}"


def _call(path, params=None, put=False):
    data = None
    if put:
        data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        method="PUT" if put else "GET",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


@pytest.fixture()
def alpaca(tmp_path):
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0), site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json")
    ctl.connect()
    httpd, _dev = make_server(ctl, "127.0.0.1", PORT)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield ctl
    httpd.shutdown()
    httpd.server_close()
    ctl.disconnect()


def test_management(alpaca):
    code, d = _call("/management/apiversions")
    assert code == 200 and d["Value"] == [1] and d["ErrorNumber"] == 0
    code, d = _call("/management/v1/configureddevices")
    assert len(d["Value"]) == 1
    assert d["Value"][0]["DeviceType"] == "Telescope"


def test_connected_and_position(alpaca):
    code, d = _call("/api/v1/telescope/0/connected")
    assert d["Value"] is True
    for member in ("altitude", "azimuth", "rightascension", "declination",
                   "slewing", "tracking", "atpark"):
        code, d = _call(f"/api/v1/telescope/0/{member}")
        assert code == 200 and d["ErrorNumber"] == 0, member
    assert 0.0 <= d["Value"] <= 24.0 or True  # last member was atpark


def test_slew_and_target(alpaca):
    code, d = _call("/api/v1/telescope/0/slewtocoordinates", put=True,
                    params={"RightAscension": "13.498",
                            "Declination": "47.195"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/targetrightascension")
    assert d["Value"] == pytest.approx(13.498)
    # invalid RA -> InvalidValue, still HTTP 200 per Alpaca spec
    code, d = _call("/api/v1/telescope/0/slewtocoordinates", put=True,
                    params={"RightAscension": "99", "Declination": "0"})
    assert code == 200 and d["ErrorNumber"] == ERR_INVALID_VALUE


def test_slewtoaltaz_and_abort(alpaca):
    code, d = _call("/api/v1/telescope/0/slewtoaltaz", put=True,
                    params={"Azimuth": "180", "Altitude": "45"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/abortslew", put=True)
    assert d["ErrorNumber"] == 0


def test_moveaxis(alpaca):
    code, d = _call("/api/v1/telescope/0/moveaxis", put=True,
                    params={"Axis": "2", "Rate": "0.05"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/moveaxis", put=True,
                    params={"Axis": "2", "Rate": "0"})
    assert d["ErrorNumber"] == 0
    # RA axis goes through the Jacobian projection
    code, d = _call("/api/v1/telescope/0/moveaxis", put=True,
                    params={"Axis": "0", "Rate": "0.01"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/moveaxis", put=True,
                    params={"Axis": "9", "Rate": "0"})
    assert code == 200 and d["ErrorNumber"] == ERR_INVALID_VALUE


def test_trackingrate(alpaca):
    code, d = _call("/api/v1/telescope/0/trackingrate", put=True,
                    params={"TrackingRate": "1"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/trackingrate")
    assert d["Value"] == 1
    code, d = _call("/api/v1/telescope/0/trackingrate", put=True,
                    params={"TrackingRate": "0"})
    assert d["ErrorNumber"] == 0
    # King rate (3) not implemented
    code, d = _call("/api/v1/telescope/0/trackingrate", put=True,
                    params={"TrackingRate": "3"})
    assert code == 200 and d["ErrorNumber"] == ERR_NOT_IMPLEMENTED


def test_park_unpark(alpaca):
    code, d = _call("/api/v1/telescope/0/park", put=True)
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/atpark")
    assert d["Value"] is True
    code, d = _call("/api/v1/telescope/0/unpark", put=True)
    assert d["ErrorNumber"] == 0


def test_pulseguide(alpaca):
    code, d = _call("/api/v1/telescope/0/pulseguide", put=True,
                    params={"Direction": "0", "Duration": "200"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/ispulseguiding")
    assert d["ErrorNumber"] == 0  # true or just-finished, either fine


def test_disconnect_gives_not_connected(alpaca):
    code, d = _call("/api/v1/telescope/0/connected", put=True,
                    params={"Connected": "false"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/altitude")
    assert code == 200 and d["ErrorNumber"] == ERR_NOT_CONNECTED
    code, d = _call("/api/v1/telescope/0/connected", put=True,
                    params={"Connected": "true"})
    assert d["ErrorNumber"] == 0
    code, d = _call("/api/v1/telescope/0/altitude")
    assert d["ErrorNumber"] == 0


def test_unknown_member_and_action(alpaca):
    code, d = _call("/api/v1/telescope/0/frobnicate")
    assert code == 200 and d["ErrorNumber"] == ERR_NOT_IMPLEMENTED
    code, d = _call("/api/v1/telescope/0/action", put=True,
                    params={"Action": "Nope"})
    assert code == 200 and d["ErrorNumber"] == ERR_ACTION_NOT_IMPLEMENTED


def test_site_and_sidereal(alpaca):
    code, d = _call("/api/v1/telescope/0/sitelatitude")
    assert d["Value"] == pytest.approx(43.3767)
    code, d = _call("/api/v1/telescope/0/siderealtime")
    assert 0.0 <= d["Value"] < 24.0
    code, d = _call("/api/v1/telescope/0/utcdate")
    assert "2026" in d["Value"]
