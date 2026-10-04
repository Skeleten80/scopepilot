"""Controller orchestration tests on the sim backend."""

import pytest

from scopepilot.backends import SimBackend
from scopepilot.controller import AlignmentError, TelescopeController


@pytest.fixture()
def scope(tmp_path):
    ctl = TelescopeController(SimBackend(slew_rate_dps=720.0),
                              site_lat=43.3767, site_lon=-80.9809,
                              pending_path=tmp_path / "sync_stars.json")
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


# -- software pointing model ------------------------------------------------


def test_goto_radec_routes_through_model(tmp_path):
    from scopepilot.astro import radec_to_altaz, utcnow

    backend = SimBackend(slew_rate_dps=720.0)
    ctl = TelescopeController(backend, site_lat=43.3767, site_lon=-80.9809,
                              pending_path=tmp_path / "s.json")
    ctl.connect()
    try:
        calls = []
        orig = backend.goto_altaz
        backend.goto_altaz = lambda az, alt: (calls.append((az, alt)),
                                              orig(az, alt))
        ctl.record_sync_star("Vega")
        ctl.record_sync_star("Altair")
        model = ctl.fit_pointing()
        assert len(model.stars) == 2
        ctl.goto_radec(13.498, 47.195)
        assert calls, "expected an alt-az slew through the model"
        rep_az, rep_alt = calls[-1]
        az, alt = radec_to_altaz(13.498, 47.195, 43.3767, -80.9809,
                                 utcnow())
        exp_az, exp_alt = model.to_reported(az, alt)
        assert abs((rep_az - exp_az + 180) % 360 - 180) < 0.5
        assert abs(rep_alt - exp_alt) < 0.5
    finally:
        ctl.disconnect()


def test_record_sync_star_needs_site(tmp_path):
    backend = SimBackend()
    ctl = TelescopeController(backend, pending_path=tmp_path / "s.json")
    ctl.connect()
    try:
        with pytest.raises(Exception):
            ctl.record_sync_star("Vega")
    finally:
        ctl.disconnect()


def test_pending_stars_survive_reconnect(tmp_path):
    p = tmp_path / "s.json"
    ctl = TelescopeController(SimBackend(), site_lat=43.3767,
                              site_lon=-80.9809, pending_path=p)
    ctl.connect()
    ctl.record_sync_star("Vega")
    ctl.disconnect()
    assert p.exists()
    ctl2 = TelescopeController(SimBackend(), site_lat=43.3767,
                               site_lon=-80.9809, pending_path=p)
    assert len(ctl2._sync_stars) == 1
    assert ctl2._sync_stars[0].name == "Vega"


def test_fit_clears_pending(tmp_path):
    p = tmp_path / "s.json"
    ctl = TelescopeController(SimBackend(), site_lat=43.3767,
                              site_lon=-80.9809, pending_path=p)
    ctl.connect()
    try:
        ctl.record_sync_star("Vega")
        ctl.record_sync_star("Altair")
        ctl.fit_pointing()
        assert not p.exists()
        assert ctl._sync_stars == []
    finally:
        ctl.disconnect()


def test_pointing_status_lifecycle(tmp_path):
    p = tmp_path / "s.json"
    ctl = TelescopeController(SimBackend(), site_lat=43.3767,
                              site_lon=-80.9809, pending_path=p)
    ctl.connect()
    try:
        assert ctl.pointing_status()["active"] is False
        ctl.record_sync_star("Vega")
        ctl.record_sync_star("Altair")
        ctl.fit_pointing()
        st = ctl.pointing_status()
        assert st["active"] is True and st["stars"] == 2
        ctl.clear_pointing()
        assert ctl.pointing_status()["active"] is False
    finally:
        ctl.disconnect()


def test_save_load_pointing(tmp_path):
    p = tmp_path / "s.json"
    ctl = TelescopeController(SimBackend(), site_lat=43.3767,
                              site_lon=-80.9809, pending_path=p)
    ctl.connect()
    try:
        ctl.record_sync_star("Vega")
        ctl.record_sync_star("Altair")
        model = ctl.fit_pointing()
        saved = ctl.save_pointing(tmp_path / "pointing.json")
        ctl.clear_pointing()
        loaded = ctl.load_pointing(saved)
        assert loaded.az_offset == pytest.approx(model.az_offset)
    finally:
        ctl.disconnect()


def test_center_target_uses_solver(tmp_path):
    from scopepilot.center import closed_loop_center  # noqa - wired

    p = tmp_path / "s.json"
    ctl = TelescopeController(SimBackend(slew_rate_dps=720.0),
                              site_lat=43.3767, site_lon=-80.9809,
                              pending_path=p)
    ctl.connect()
    try:
        calls = []

        def fake_solve(exposure_s):
            calls.append(exposure_s)
            return 13.498, 47.195  # already on target

        report = ctl.center_target("M51", fake_solve)
        assert report["converged"] is True
        assert calls == [5.0]
    finally:
        ctl.disconnect()
