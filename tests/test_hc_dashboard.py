"""Tests for the full hand-controller feature set:

driver/sim backlash + cordwrap, undo-goto, user objects, hc_info,
tonight list, identify, and the new dashboard API routes.
"""

import json
import threading
import urllib.request

import pytest

from scopepilot import bridge
from scopepilot.backends import Backend, SimBackend
from scopepilot.controller import TelescopeController
from scopepilot.nexstar import NexStarError
from scopepilot.server import create_server


@pytest.fixture()
def scope(tmp_path):
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0),
        site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json",
        user_objects_path=tmp_path / "user_objects.json",
    )
    ctl.connect()
    yield ctl
    ctl.disconnect()


# -- driver / sim: backlash + cordwrap -----------------------------------

def test_backlash_roundtrip(scope):
    for axis in ("az", "alt"):
        for direction in (1, -1):
            scope.set_backlash(axis, direction, 42)
            assert scope.get_backlash(axis, direction) == 42
    # axes/directions are independent
    scope.set_backlash("az", 1, 7)
    assert scope.get_backlash("az", -1) == 42
    assert scope.get_backlash("alt", 1) == 42


def test_backlash_validation(scope):
    with pytest.raises(ValueError):
        scope.set_backlash("el", 1, 10)
    with pytest.raises(ValueError):
        scope.set_backlash("az", 0, 10)
    with pytest.raises(ValueError):
        scope.set_backlash("az", 1, 100)
    with pytest.raises(ValueError):
        scope.set_backlash("az", 1, -1)


def test_cordwrap_roundtrip(scope):
    assert scope.cordwrap_enabled() is False
    scope.set_cordwrap(True)
    assert scope.cordwrap_enabled() is True
    scope.set_cordwrap(False)
    assert scope.cordwrap_enabled() is False


def test_backend_default_raises():
    class Dummy(Backend):
        name = "dummy"
        def connect(self): ...
        def disconnect(self): ...
        def status(self): ...
        def goto_radec(self, ra_hours, dec_deg): ...
        def goto_altaz(self, az_deg, alt_deg): ...
        def sync_radec(self, ra_hours, dec_deg): ...
        def abort(self): ...
        def set_tracking(self, mode): ...
        def jog_start(self, axis, direction, rate): ...
        def jog_stop(self, axis=None): ...
        def set_hc_time(self, *a): ...
        def set_hc_location(self, lat_deg, lon_deg): ...
        def wait_goto(self, timeout): ...

    d = Dummy()
    with pytest.raises(NotImplementedError):
        d.set_backlash("az", 1, 5)
    with pytest.raises(NotImplementedError):
        d.cordwrap_enabled()


# -- undo goto ------------------------------------------------------------

def test_undo_goto_returns_to_previous_position(scope):
    before = scope.status()
    assert scope.goto_altaz(120.0, 55.0) is True
    assert scope.undo_goto() is True
    after = scope.status()
    assert after.az_deg == pytest.approx(before.az_deg, abs=0.1)
    assert after.alt_deg == pytest.approx(before.alt_deg, abs=0.1)


def test_undo_goto_toggle(scope):
    assert scope.goto_altaz(120.0, 55.0) is True
    mid = scope.status()
    assert scope.undo_goto() is True          # back to start
    assert scope.undo_goto() is True          # back to the goto target
    back = scope.status()
    assert back.az_deg == pytest.approx(mid.az_deg, abs=0.1)


def test_undo_goto_without_goto_raises(scope):
    with pytest.raises(NexStarError):
        scope.undo_goto()


def test_undo_goto_tracks_radec_goto(scope):
    before = scope.status()
    assert scope.goto_radec(13.498, 47.195) is True
    assert scope.undo_goto() is True
    after = scope.status()
    assert after.az_deg == pytest.approx(before.az_deg, abs=0.2)
    assert after.alt_deg == pytest.approx(before.alt_deg, abs=0.2)


# -- user objects ----------------------------------------------------------

def test_user_objects_crud(scope):
    assert scope.list_user_objects() == []
    obj = scope.add_user_object("Comet Spot", 10.5, -20.25)
    assert obj == {"name": "Comet Spot", "ra_hours": 10.5,
                   "dec_deg": -20.25}
    assert [o["name"] for o in scope.list_user_objects()] == ["Comet Spot"]
    # overwrite same name (case-insensitive)
    scope.add_user_object("comet spot", 11.0, 21.0)
    assert len(scope.list_user_objects()) == 1
    scope.delete_user_object("COMET SPOT")
    assert scope.list_user_objects() == []


def test_user_objects_validation(scope):
    with pytest.raises(ValueError):
        scope.add_user_object("   ", 10.0, 20.0)
    with pytest.raises(ValueError):
        scope.add_user_object("Bad", 10.0, 95.0)
    with pytest.raises(ValueError):
        scope.delete_user_object("Nope")
    with pytest.raises(ValueError):
        scope.goto_user_object("Nope")


def test_user_objects_persist(tmp_path):
    path = tmp_path / "user_objects.json"
    ctl = TelescopeController(
        SimBackend(), pending_path=tmp_path / "s.json",
        user_objects_path=path)
    ctl.connect()
    try:
        ctl.add_user_object("Mine", 5.0, 10.0)
    finally:
        ctl.disconnect()
    ctl2 = TelescopeController(
        SimBackend(), pending_path=tmp_path / "s.json",
        user_objects_path=path)
    ctl2.connect()
    try:
        assert [o["name"] for o in ctl2.list_user_objects()] == ["Mine"]
    finally:
        ctl2.disconnect()
    assert json.loads(path.read_text())[0]["name"] == "Mine"


def test_save_current_as_and_goto(scope):
    scope.goto_altaz(200.0, 50.0)
    st = scope.status()
    scope.save_current_as("Here")
    objs = scope.list_user_objects()
    assert len(objs) == 1
    assert objs[0]["ra_hours"] == pytest.approx(st.ra_hours, abs=1e-9)
    ra, dec, settled = scope.goto_user_object("here")
    assert settled is True
    assert ra == pytest.approx(st.ra_hours, abs=1e-9)


# -- hc info ----------------------------------------------------------------

def test_hc_info(scope):
    info = scope.hc_info()
    assert info["model"] == "NexStar 6/8 SE"
    assert info["hc_version"] == "5.35"
    assert info["gps_linked"] is False
    assert 16 in info["bus"]


# -- bridge: tonight + identify ----------------------------------------------

def test_tonight_list_fallback_shape():
    items, source = bridge.tonight_list(43.3767, -80.9809, limit=8)
    assert source in ("astrocapture-catalog", "scopepilot-builtin")
    assert 0 < len(items) <= 8
    assert all("name" in t for t in items)


def test_identify_finds_m31():
    hit = bridge.identify(0.712, 41.269)  # exact M31 coords
    assert hit is not None
    assert hit["name"] == "M31"
    assert hit["sep_deg"] == pytest.approx(0.0, abs=0.05)


def test_identify_empty_sky_returns_none():
    assert bridge.identify(12.0, -80.0, max_sep_deg=1.0) is None


# -- server routes ------------------------------------------------------------

@pytest.fixture()
def api(tmp_path):
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0),
        site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json",
        user_objects_path=tmp_path / "user_objects.json",
    )
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
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _delete(base, path):
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_state_has_undo_available(api):
    _code, st = _get(api, "/api/state")
    assert st["undo_available"] is False
    _post(api, "/api/goto", {"az_deg": 120.0, "alt_deg": 55.0})
    _code, st = _get(api, "/api/state")
    assert st["undo_available"] is True


def test_undo_goto_route(api):
    _post(api, "/api/goto", {"az_deg": 120.0, "alt_deg": 55.0})
    code, j = _post(api, "/api/undo-goto", {})
    assert code == 200 and j["ok"] is True
    code, j = _post(api, "/api/undo-goto", {})
    assert code == 200  # toggles back; still fine


def test_align_stars_route(api):
    code, j = _get(api, "/api/align-stars")
    assert code == 200 and j["ok"] is True
    assert 1 <= len(j["stars"]) <= 3
    for s in j["stars"]:
        assert 25.0 <= s["alt_deg"] <= 75.0


def test_tonight_route(api):
    code, j = _get(api, "/api/tonight")
    assert code == 200 and j["ok"] is True
    assert len(j["targets"]) > 0


def _wait_settled(base, timeout=15.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _code, st = _get(base, "/api/state")
        if not st.get("slewing"):
            return
        time.sleep(0.2)
    raise AssertionError("slew never settled")


def test_identify_route(api):
    _post(api, "/api/goto", {"ra_hours": 0.712, "dec_deg": 41.269})
    _wait_settled(api)
    code, j = _get(api, "/api/identify")
    assert code == 200 and j["ok"] is True
    assert j["result"]["name"] == "M31"


def test_user_objects_routes(api):
    code, j = _post(api, "/api/user-objects",
                    {"name": "Test Spot", "use_current": True})
    assert code == 200 and j["object"]["name"] == "Test Spot"
    code, j = _get(api, "/api/user-objects")
    assert [o["name"] for o in j["objects"]] == ["Test Spot"]
    code, j = _post(api, "/api/goto-user", {"name": "test spot"})
    assert code == 200 and j["ok"] is True
    code, j = _delete(api, "/api/user-objects?name=Test%20Spot")
    assert code == 200 and j["ok"] is True
    code, j = _get(api, "/api/user-objects")
    assert j["objects"] == []


def test_hc_routes(api):
    code, j = _get(api, "/api/hc")
    assert code == 200 and j["hc"]["model"] == "NexStar 6/8 SE"
    code, j = _post(api, "/api/hc-sync", {})
    assert code == 200 and j["ok"] is True
    assert "year" in j["clock"]


def test_backlash_routes(api):
    code, j = _get(api, "/api/backlash?axis=az&direction=1")
    assert code == 200 and j["value"] == 0
    code, j = _post(api, "/api/backlash",
                    {"axis": "alt", "direction": -1, "value": 33})
    assert code == 200 and j["ok"] is True
    code, j = _get(api, "/api/backlash?axis=alt&direction=-1")
    assert j["value"] == 33
    code, j = _post(api, "/api/backlash",
                    {"axis": "az", "direction": 1, "value": 150})
    assert code == 400


def test_cordwrap_routes(api):
    code, j = _get(api, "/api/cordwrap")
    assert code == 200 and j["enabled"] is False
    code, j = _post(api, "/api/cordwrap", {"enabled": True})
    assert code == 200 and j["enabled"] is True
    code, j = _get(api, "/api/cordwrap")
    assert j["enabled"] is True
