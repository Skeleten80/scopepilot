"""Closed-loop centering tests with a fake mount + scripted solver."""

from types import SimpleNamespace

import pytest

from scopepilot.astro import altaz_to_radec, radec_to_altaz, utcnow
from scopepilot.center import closed_loop_center

LAT, LON = 43.3767, -80.9809


class FakeMount:
    """Alt-az mount with a fixed pointing error.

    ``az``/``alt`` are the encoder (reported) position; the true sky
    pointing is reported + the fixed error, simulating an unmodeled mount.
    """

    def __init__(self, az_err=30.0, alt_err=-12.0):
        self.site_lat, self.site_lon = LAT, LON
        self.pointing = None
        self.az_err, self.alt_err = az_err, alt_err
        self.az, self.alt = 180.0, 45.0  # reported position
        self.slews = []

    def goto_radec(self, ra, dec, wait=True, **kw):
        # No model: the true alt-az is commanded as if it were reported,
        # so the true pointing lands off by the mount error.
        az, alt = radec_to_altaz(ra, dec, LAT, LON, utcnow())
        self.slews.append(("radec", ra, dec))
        return self.goto_altaz(az % 360.0, alt, wait=wait)

    def goto_altaz(self, az, alt, wait=True, **kw):
        self.slews.append(("altaz", az, alt))
        self.az, self.alt = az % 360.0, alt
        return True

    def status(self):
        return SimpleNamespace(az_deg=self.az, alt_deg=self.alt)


def make_solver(mount):
    """A 'plate solver' that returns the mount's true pointing."""

    def solve(exposure_s):
        t_az = (mount.az + mount.az_err) % 360.0
        t_alt = mount.alt + mount.alt_err
        return altaz_to_radec(t_az, t_alt, LAT, LON, utcnow())

    return solve


def test_closed_loop_converges():
    mount = FakeMount()
    report = closed_loop_center(mount, 13.498, 47.195, make_solver(mount),
                                tolerance_arcmin=1.0, max_iters=4)
    assert report["converged"] is True
    assert report["final_sep_arcmin"] <= 1.0
    assert report["iters"] >= 2  # first slew was 30+ deg off
    assert any("converged" in e for e in report["events"])


def test_converges_immediately_when_on_target():
    mount = FakeMount(az_err=0.0, alt_err=0.0)

    def perfect(exposure_s):
        return 13.498, 47.195

    report = closed_loop_center(mount, 13.498, 47.195, perfect)
    assert report["converged"] is True
    assert report["iters"] == 1


def test_solver_failure_aborts_cleanly():
    mount = FakeMount()
    report = closed_loop_center(mount, 13.498, 47.195,
                                lambda exp: None, max_iters=4)
    assert report["converged"] is False
    assert any("plate solve failed" in e for e in report["events"])


def test_gives_up_after_max_iters():
    mount = FakeMount()

    def stuck(exposure_s):
        return 0.0, 0.0  # always far away

    report = closed_loop_center(mount, 13.498, 47.195, stuck,
                                max_iters=2, tolerance_arcmin=0.01)
    assert report["converged"] is False
    assert report["iters"] == 2


def test_no_site_is_an_error():
    mount = FakeMount()
    mount.site_lat = None
    report = closed_loop_center(mount, 13.498, 47.195, make_solver(mount))
    assert report["converged"] is False
    assert "site" in report["events"][0]


def test_events_stream():
    mount = FakeMount()
    seen = []
    closed_loop_center(mount, 13.498, 47.195, make_solver(mount),
                       on_event=seen.append)
    assert len(seen) >= 3
