"""Tests for the eight-feature build: mosaic, ephemeris, satellites,
lunar/solar rates, moon filter, drift correction plumbing."""

import math

import pytest

from scopepilot import ephem, mosaic
from scopepilot.astro import angular_sep_deg


# -- mosaic -----------------------------------------------------------------

def test_mosaic_panel_count_and_center():
    panels = mosaic.plan_mosaic(0.712, 41.269, 0.85, 0.57,
                                overlap=0.15, width_deg=3.0, height_deg=2.0)
    # step 0.7225 x 0.4845 -> ceil(3/0.7225)=5 cols, ceil(2/0.4845)=5 rows
    assert len(panels) == 25
    # odd grid: middle panel is the target center
    mid = panels[12]
    assert mid["ra_hours"] == pytest.approx(0.712, abs=1e-9)
    assert mid["dec_deg"] == pytest.approx(41.269, abs=1e-9)


def test_mosaic_snake_order():
    panels = mosaic.plan_mosaic(10.0, 20.0, 1.0, 1.0, overlap=0.0,
                                nx=3, ny=2)
    assert len(panels) == 6
    ras = [p["ra_hours"] for p in panels]
    # row 0 left->right, row 1 right->left
    assert ras[0] < ras[1] < ras[2]
    assert ras[3] > ras[4] > ras[5]
    assert ras[2] == pytest.approx(ras[3])


def test_mosaic_explicit_counts():
    panels = mosaic.plan_mosaic(5.0, -5.0, 0.5, 0.5, nx=2, ny=2)
    assert len(panels) == 4
    assert panels[0]["panel"] == 1


def test_mosaic_to_plan_entries():
    panels = mosaic.plan_mosaic(5.0, -5.0, 0.5, 0.5, nx=2, ny=1)
    plan = mosaic.mosaic_to_plan(panels, "M42")
    assert len(plan) == 2
    assert plan[0]["name"] == "M42 P1/2"
    assert plan[0]["ra_hours"] == pytest.approx(panels[0]["ra_hours"])


# -- ephemeris math (no network) ---------------------------------------------

def test_kepler_solver_residual():
    for M_deg, e in [(10, 0.1), (200, 0.5), (350, 0.9), (90, 0.0167)]:
        M = math.radians(M_deg)
        E = ephem.kepler_E(M, e)
        assert abs(E - e * math.sin(E) - M) < 1e-10


def test_moon_illumination_range_and_new_moon():
    from datetime import datetime, timezone
    # Oct 11 2026: just after new moon -> dark
    dt = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    ill = ephem.moon_illumination(dt)
    assert 0.0 <= ill <= 0.15
    # Oct 26 2026: near full
    dt2 = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    assert ephem.moon_illumination(dt2) > 0.9


def test_moon_moves_eastward():
    from datetime import datetime, timedelta, timezone
    dt = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    ra1, _ = ephem.moon_radec(dt)
    ra2, _ = ephem.moon_radec(dt + timedelta(days=1))
    dra = (ra2 - ra1) % 24.0
    assert 0.7 < dra < 1.1  # ~13 deg/day


def test_propagate_circular_orbit():
    from datetime import datetime, timezone
    # e=0 body at 2.5 AU, M=0 at epoch -> geocentric ~ its heliocentric dir
    el = {"epoch_jd": 2451545.0, "a_au": 2.5, "e": 0.0, "i_deg": 5.0,
          "node_deg": 80.0, "argp_deg": 70.0, "M0_deg": 0.0}
    ra, dec = ephem.propagate_elements(
        el, datetime(2000, 1, 1, 12, tzinfo=timezone.utc))
    assert 0.0 <= ra < 24.0
    assert -90.0 <= dec <= 90.0
    # sanity: not NaN, plausible ecliptic latitude for i=5
    assert abs(dec) < 30.0


# -- moon filter on tonight_list ----------------------------------------------

def test_tonight_moon_filter_drops_close_targets(monkeypatch):
    from scopepilot import bridge
    # Freeze the moon right on top of M42
    monkeypatch.setattr(ephem, "moon_radec", lambda dt=None: (5.588, -5.391))
    monkeypatch.setattr(ephem, "moon_illumination", lambda dt=None: 1.0)
    items, src = bridge.tonight_list(43.38, -80.98, limit=40,
                                     min_alt_deg=-90, moon_avoid=True)
    names = [i["name"] for i in items]
    assert "M42" not in names  # 0 deg from a full moon -> dropped
    # without the filter it survives
    items2, _ = bridge.tonight_list(43.38, -80.98, limit=40,
                                    min_alt_deg=-90, moon_avoid=False)
    assert "M42" in [i["name"] for i in items2]


# -- variable guide rates on the sim backend -----------------------------------

def test_variable_rate_sim(tmp_path):
    from scopepilot.backends import SimBackend
    from scopepilot.controller import TelescopeController
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0), site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json")
    ctl.connect()
    try:
        ctl.backend.variable_rate("az", 1.5)   # must not raise on sim
        ctl.backend.variable_rate("alt", 0.0)
    finally:
        ctl.disconnect()


def test_track_rate_lifecycle(tmp_path):
    from scopepilot.backends import SimBackend
    from scopepilot.controller import TelescopeController
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0), site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json")
    ctl.connect()
    try:
        assert ctl.rate_tracking is None
        assert ctl.track_rate("lunar") == "lunar"
        assert ctl.rate_tracking == "lunar"
        assert ctl.track_rate("sidereal") == "sidereal"
        assert ctl.rate_tracking is None
    finally:
        ctl.track_stop()
        ctl.disconnect()


def test_sat_tracking_requires_sgp4_or_runs(tmp_path):
    pytest.importorskip("sgp4")
    from scopepilot.backends import SimBackend
    from scopepilot.controller import TelescopeController
    ctl = TelescopeController(
        SimBackend(slew_rate_dps=720.0), site_lat=43.3767, site_lon=-80.9809,
        pending_path=tmp_path / "sync_stars.json")
    ctl.connect()
    try:
        # monkeypatch sat_altaz to avoid network in CI
        import scopepilot.ephem as em
        orig = em.sat_altaz
        em.sat_altaz = lambda norad, lat, lon, dt=None: (180.0, 45.0)
        try:
            rep = ctl.track_satellite(25544, update_hz=5.0)
            assert rep["tracking"] == "sat-25544"
            assert ctl.rate_tracking == "sat-25544"
        finally:
            em.sat_altaz = orig
            ctl.track_stop()
        assert ctl.rate_tracking is None
    finally:
        ctl.disconnect()
