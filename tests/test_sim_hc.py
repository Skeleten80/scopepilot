"""Wire-level tests: real NexStarDriver <-> simulated hand controller."""

import time

import pytest

from scopepilot.nexstar import PassthroughError
from scopepilot.sim import make_sim_driver


@pytest.fixture()
def driver():
    drv, sim = make_sim_driver(slew_rate_dps=720.0)
    yield drv, sim
    sim.stop()
    drv.close()


def test_echo(driver):
    drv, _sim = driver
    assert drv.echo()
    assert drv.echo(0x41)


def test_version_and_model(driver):
    drv, _sim = driver
    major, minor = drv.get_version()
    assert (major, minor) == (5, 35)
    assert drv.get_model() == 12
    assert drv.model_name() == "NexStar 6/8 SE"


def test_alignment(driver):
    drv, _sim = driver
    assert drv.alignment_complete() is True


def test_get_position(driver):
    drv, _sim = driver
    ra, dec = drv.get_radec()
    assert ra == pytest.approx(5.5, abs=0.01)
    assert dec == pytest.approx(20.0, abs=0.01)
    az, alt = drv.get_altaz()
    assert az == pytest.approx(180.0, abs=0.01)
    assert alt == pytest.approx(45.0, abs=0.01)


def test_get_position_non_precise(driver):
    drv, _sim = driver
    ra, dec = drv.get_radec(precise=False)
    assert ra == pytest.approx(5.5, abs=0.02)


def test_goto_radec_settles(driver):
    drv, sim = driver
    drv.goto_radec(13.498, 47.195)
    assert drv.goto_in_progress() is True
    deadline = time.monotonic() + 10
    while drv.goto_in_progress() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert drv.goto_in_progress() is False
    ra, dec = drv.get_radec()
    assert ra == pytest.approx(13.498, abs=0.02)
    assert dec == pytest.approx(47.195, abs=0.02)
    assert drv.slew_done("az") and drv.slew_done("alt")


def test_goto_altaz_settles(driver):
    drv, _sim = driver
    drv.goto_altaz(250.0, 60.0)
    deadline = time.monotonic() + 10
    while drv.goto_in_progress() and time.monotonic() < deadline:
        time.sleep(0.05)
    az, alt = drv.get_altaz()
    assert az == pytest.approx(250.0, abs=0.05)
    assert alt == pytest.approx(60.0, abs=0.05)


def test_cancel_goto(driver):
    drv, sim = driver
    # slow the sim down so the goto is still running when we cancel
    drv.goto_radec(20.0, -20.0)
    assert drv.goto_in_progress() is True
    drv.cancel_goto()
    assert drv.goto_in_progress() is False


def test_sync_sets_position_without_moving(driver):
    drv, sim = driver
    before = sim.snapshot()
    drv.sync_radec(10.0, 30.0)
    ra, dec = drv.get_radec()
    assert ra == pytest.approx(10.0, abs=0.02)
    assert dec == pytest.approx(30.0, abs=0.02)
    after = sim.snapshot()
    # sync must not start a slew or move the alt-az axes
    assert after["goto_active"] is False
    assert after["az_deg"] == pytest.approx(before["az_deg"])


def test_tracking_modes(driver):
    drv, _sim = driver
    assert drv.get_tracking() == 1
    drv.set_tracking(0)
    assert drv.get_tracking() == 0
    drv.set_tracking(1)
    assert drv.get_tracking() == 1
    with pytest.raises(ValueError):
        drv.set_tracking(9)


def test_time_roundtrip(driver):
    drv, _sim = driver
    drv.set_time(2026, 10, 3, 21, 30, 0, -4, True)
    t = drv.get_time()
    assert (t["year"], t["month"], t["day"]) == (2026, 10, 3)
    assert (t["hour"], t["minute"]) == (21, 30)
    assert t["utc_offset_hours"] == -4
    assert t["dst"] is True


def test_location_roundtrip(driver):
    drv, _sim = driver
    drv.set_location(43.3767, -80.9809)
    lat, lon = drv.get_location()
    assert lat == pytest.approx(43.3767, abs=0.001)
    assert lon == pytest.approx(-80.9809, abs=0.001)


def test_jog_moves_axis_then_stops(driver):
    drv, sim = driver
    alt0 = drv.get_altaz()[1]
    drv.jog("alt", 1, 9)  # up at max rate
    time.sleep(0.4)
    alt1 = drv.get_altaz()[1]
    assert alt1 > alt0 + 0.5
    assert sim.snapshot()["jog"]["alt"] > 0
    drv.stop_axis("alt")
    time.sleep(0.1)
    assert sim.snapshot()["jog"]["alt"] == 0.0
    drv.stop_all()


def test_jog_bad_args(driver):
    drv, _sim = driver
    with pytest.raises(ValueError):
        drv.jog("sideways", 1, 5)
    with pytest.raises(ValueError):
        drv.jog("az", 2, 5)
    with pytest.raises(ValueError):
        drv.jog("az", 1, 10)


def test_variable_rate(driver):
    drv, sim = driver
    az0 = drv.get_altaz()[0]
    drv.variable_rate("az", 3600.0)  # 1 deg/s positive
    time.sleep(0.4)
    assert drv.get_altaz()[0] > az0 + 0.2
    drv.variable_rate("az", 0.0)
    time.sleep(0.05)
    assert sim.snapshot()["jog"]["az"] == 0.0


def test_mc_version(driver):
    drv, _sim = driver
    assert drv.mc_version("az") == (1, 0)
    assert drv.mc_version("alt") == (1, 0)


def test_bus_scan(driver):
    drv, _sim = driver
    found = drv.bus_scan()
    assert found[16] == (1, 0)
    assert found[17] == (1, 0)
    assert 176 not in found  # no GPS on the sim


def test_passthrough_absent_device_raises(driver):
    drv, _sim = driver
    with pytest.raises(PassthroughError):
        drv.passthrough(0x30, 0xFE, b"", 2)


def test_gps_not_linked(driver):
    drv, _sim = driver
    assert drv.gps_linked() is False


def test_slew_done_while_jogging(driver):
    drv, _sim = driver
    assert drv.slew_done("az") is True
    drv.jog("az", 1, 5)
    assert drv.slew_done("az") is False
    drv.stop_all()
    # settle: jog cleared synchronously by stop_all
    assert drv.slew_done("az") is True


def test_unaligned_sim_reports_not_aligned():
    drv, sim = make_sim_driver(aligned=False)
    try:
        assert drv.alignment_complete() is False
    finally:
        sim.stop()
        drv.close()
