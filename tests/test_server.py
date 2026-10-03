"""HTTP API tests against the sim backend."""

import json
import threading
import urllib.request

import pytest

from scopepilot.backends import SimBackend
from scopepilot.controller import TelescopeController
from scopepilot.server import create_server


@pytest.fixture()
def api():
    ctl = TelescopeController(SimBackend(slew_rate_dps=720.0))
    ctl.connect()
    server = create_server(ctl, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield base
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    ctl.disconnect()


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post(base, path, body):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_state(api):
    code, body = _get(api, "/api/state")
    assert code == 200
    st = json.loads(body)
    assert st["ok"] and st["backend"] == "sim"
    assert st["aligned"] is True
    assert st["manual_override"] == {"claimed": False, "by": None, "at": None}


def test_claim_release(api):
    _code, j = _post(api, "/api/claim", {"by": "tester"})
    assert j["manual_override"]["claimed"] is True
    assert j["manual_override"]["by"] == "tester"
    _code, body = _get(api, "/api/state")
    assert json.loads(body)["manual_override"]["claimed"] is True
    _code, j = _post(api, "/api/release", {})
    assert j["manual_override"]["claimed"] is False


def test_goto_by_name(api):
    _code, j = _post(api, "/api/goto", {"name": "M51"})
    assert j["ok"] is True
    assert j["ra_hours"] == pytest.approx(13.498, abs=0.02)


def test_goto_by_coords(api):
    _code, j = _post(api, "/api/goto", {"ra_hours": 10.0, "dec_deg": 20.0})
    assert j["ok"] is True
    _code, j = _post(api, "/api/goto", {"az_deg": 200.0, "alt_deg": 50.0})
    assert j["ok"] is True


def test_goto_bad_request(api):
    code, j = _post(api, "/api/goto", {})
    assert code == 400
    assert j["ok"] is False


def test_goto_unknown_target(api):
    code, j = _post(api, "/api/goto", {"name": "NoSuchStarXYZ"})
    assert code == 400
    assert j["ok"] is False


def test_sync(api):
    _code, j = _post(api, "/api/sync", {"name": "M13"})
    assert j["ok"] is True


def test_jog_and_stop(api):
    _code, j = _post(api, "/api/jog", {"direction": "up", "rate": 3})
    assert j["ok"] is True
    _code, j = _post(api, "/api/jog_stop", {})
    assert j["ok"] is True
    _code, j = _post(api, "/api/stop", {})
    assert j["ok"] is True


def test_track(api):
    _code, j = _post(api, "/api/track", {"mode": "off"})
    assert j["tracking_mode"] == "off"
    _code, j = _post(api, "/api/track", {"mode": "alt-az"})
    assert j["tracking_mode"] == "alt-az"


def test_park_unpark(api):
    _code, j = _post(api, "/api/park", {})
    assert j["ok"] is True
    _code, j = _post(api, "/api/unpark", {})
    assert j["ok"] is True


def test_targets_search(api):
    code, body = _get(api, "/api/targets?q=M5")
    assert code == 200
    names = {r["name"] for r in json.loads(body)["results"]}
    assert "M51" in names


def test_plan(api):
    code, body = _get(
        api,
        "/api/plan?path=/home/hatch/workspace/astro-capture/examples/night_queue.yaml",
    )
    assert code == 200
    targets = json.loads(body)["targets"]
    assert [t["name"] for t in targets] == ["M81", "M51", "M13"]


def test_dash_page(api):
    code, body = _get(api, "/")
    assert code == 200
    assert "ScopePilot" in body


def test_unknown_route(api):
    code, body = _get(api, "/nope")
    assert code == 404
