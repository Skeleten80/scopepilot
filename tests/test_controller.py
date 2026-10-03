"""Controller orchestration tests on the sim backend."""

import pytest

from scopepilot.backends import SimBackend
from scopepilot.controller import AlignmentError, TelescopeController


@pytest.fixture()
def scope():
    ctl = TelescopeController(SimBackend(slew_rate_dps=720.0))
    ctl.connect()
    yield ctl
    ctl.disconnect()


def test_probe_report(scope):
    rep = scope.probe()
    assert rep["backend"] == "sim"
    assert rep["model"] == "NexStar 6/8 SE"
    assert rep["aligned"] is True
    assert rep["tracking_mode"] == "alt-az"
    assert rep["model_id"] == 12
    assert 16 in rep["bus"]


def test_goto_radec_wait(scope):
    assert scope.goto_radec(13.498, 47.195) is True
    st = scope.status()
    assert st.ra_hours == pytest.approx(13.498, abs=0.02)
    assert st.dec_deg == pytest.approx(47.195, abs=0.02)
    assert st.slewing is False


def test_goto_target_uses_catalog(scope):
    ra, dec, source, settled = scope.goto_target("M51")
    assert ra == pytest.approx(13.498, abs=0.02)
    assert dec == pytest.approx(47.195, abs=0.02)
    assert source in ("astrocapture-catalog", "scopepilot-builtin")
    assert settled is True


def test_goto_requires_alignment():
    ctl = TelescopeController(SimBackend(aligned=False))
    ctl.connect()
    try:
        with pytest.raises(AlignmentError):
            ctl.goto_radec(10.0, 20.0)
    finally:
        ctl.disconnect()


def test_goto_altaz(scope):
    assert scope.goto_altaz(250.0, 60.0) is True
    st = scope.status()
    assert st.az_deg == pytest.approx(250.0, abs=0.05)
    assert st.alt_deg == pytest.approx(60.0, abs=0.05)


def test_park_and_unpark(scope):
    scope.goto_radec(13.5, 47.0)
    assert scope.park() is True
    st = scope.status()
    assert st.parked is True
    assert st.tracking_mode == "off"
    assert st.az_deg == pytest.approx(0.0, abs=0.1)
    scope.unpark()
    st = scope.status()
    assert st.parked is False
    assert st.tracking_mode == "alt-az"


def test_tracking_modes(scope):
    assert scope.set_tracking("off") == "off"
    assert scope.set_tracking("alt-az") == "alt-az"
    assert scope.set_tracking(2) == "eq-north"
    with pytest.raises(ValueError):
        scope.set_tracking("ludicrous")


def test_jog_timed(scope):
    alt0 = scope.status().alt_deg
    scope.jog("up", rate=9, seconds=0.3)
    assert scope.status().alt_deg > alt0 + 0.3


def test_jog_bad_direction(scope):
    with pytest.raises(ValueError):
        scope.jog("sideways")


def test_abort_when_idle(scope):
    scope.abort()  # must not raise


def test_sync_clock(scope):
    t = scope.sync_clock()
    assert t["year"] >= 2026
    assert isinstance(t["utc_offset_hours"], float)


def test_set_site(scope):
    scope.set_site(43.0, -81.0)
    assert (scope.site_lat, scope.site_lon) == (43.0, -81.0)


def test_sync_target(scope):
    ra, dec, source = scope.sync_target("M13")
    assert dec == pytest.approx(36.46, abs=0.05)


def test_status_parked_flag(scope):
    assert scope.status().parked is False
    scope.park()
    assert scope.status().parked is True
