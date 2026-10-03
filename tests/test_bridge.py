"""Bridge tests: night-plan parsing, catalog resolution, override polling."""

import pytest

from scopepilot import bridge

PLAN = "/home/hatch/workspace/astro-capture/examples/night_queue.yaml"


def test_read_night_plan():
    plan = bridge.read_night_plan(PLAN)
    assert [t["name"] for t in plan] == ["M81", "M51", "M13"]
    assert plan[0]["priority"] == pytest.approx(2.0)


def test_read_night_plan_missing_file():
    with pytest.raises(Exception):
        bridge.read_night_plan("/no/such/plan.yaml")


def test_resolve_m51():
    ra, dec, source = bridge.resolve_target("M51")
    assert ra == pytest.approx(13.498, abs=0.02)
    assert dec == pytest.approx(47.195, abs=0.02)
    assert source in ("astrocapture-catalog", "scopepilot-builtin")


def test_resolve_alias():
    ra, dec, _src = bridge.resolve_target("whirlpool")
    assert ra == pytest.approx(13.498, abs=0.05)


def test_resolve_unknown():
    with pytest.raises(bridge.TargetNotFound):
        bridge.resolve_target("NoSuchStarXYZ")


def test_search_targets():
    results = bridge.search_targets("M5")
    names = {r["name"] for r in results}
    assert "M51" in names and "M57" in names
    for r in results:
        assert {"name", "ra_hours", "dec_deg", "source"} <= set(r)


def test_check_manual_override_unreachable():
    assert bridge.check_manual_override("http://127.0.0.1:1", timeout=0.5) is None


def test_check_manual_override_live():
    import threading

    from scopepilot.backends import SimBackend
    from scopepilot.controller import TelescopeController
    from scopepilot.server import create_server

    ctl = TelescopeController(SimBackend())
    ctl.connect()
    server = create_server(ctl, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ov = bridge.check_manual_override(f"http://127.0.0.1:{port}")
        assert ov == {"claimed": False, "by": None, "at": None}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        ctl.disconnect()
