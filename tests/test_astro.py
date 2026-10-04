"""Tests for the pure-python astro transforms.

Reference vectors were generated with astropy (independent implementation);
tolerances (60") comfortably cover the omitted nutation/aberration (<30").
"""

from datetime import datetime, timezone

import pytest

from scopepilot.astro import (
    altaz_to_radec,
    angular_sep_deg,
    datetime_to_jd,
    radec_to_altaz,
    utcnow,
    wrap180,
)

LAT, LON = 43.3767, -80.9809

# (ra_deg, dec_deg, iso_utc, astropy_az, astropy_alt)
VECTORS = [
    (202.4696, 47.1952, "2026-10-03T21:00:00+00:00", 292.415466, 59.300812),
    (83.8221, -5.3911, "2026-01-15T05:30:00+00:00", 219.214995, 33.296743),
    (280.0, 89.9, "2026-10-03T21:00:00+00:00", 359.884299, 43.517665),
]


def _dt(iso):
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize("ra_d,dec_d,iso,az_ref,alt_ref", VECTORS)
def test_radec_to_altaz_vs_astropy(ra_d, dec_d, iso, az_ref, alt_ref):
    az, alt = radec_to_altaz(ra_d / 15.0, dec_d, LAT, LON, _dt(iso))
    assert abs(wrap180(az - az_ref)) * 3600 < 60.0
    assert abs(alt - alt_ref) * 3600 < 60.0


@pytest.mark.parametrize("ra_d,dec_d,iso,az_ref,alt_ref", VECTORS)
def test_roundtrip(ra_d, dec_d, iso, az_ref, alt_ref):
    dt = _dt(iso)
    az, alt = radec_to_altaz(ra_d / 15.0, dec_d, LAT, LON, dt)
    ra2, dec2 = altaz_to_radec(az, alt, LAT, LON, dt)
    assert abs(wrap180((ra2 - ra_d / 15.0) * 15.0)) * 3600 < 1.0
    assert abs(dec2 - dec_d) * 3600 < 1.0


def test_zenith_star():
    # A star with dec == lat transits near the zenith (precession shifts
    # the J2000 position by ~0.17 deg, so allow slack).
    dt = datetime(2026, 10, 3, 21, 0, 0, tzinfo=timezone.utc)
    from scopepilot.astro import lst_hours

    lst = lst_hours(datetime_to_jd(dt), LON)
    az, alt = radec_to_altaz(lst, LAT, LAT, LON, dt)
    assert alt == pytest.approx(90.0, abs=0.5)


def test_datetime_to_jd_known():
    # 2000-01-01 12:00 UTC == JD 2451545.0
    jd = datetime_to_jd(datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert jd == pytest.approx(2451545.0, abs=1e-6)


def test_angular_sep():
    assert angular_sep_deg(0, 0, 1 / 15.0, 0) == pytest.approx(1.0, abs=1e-9)
    assert angular_sep_deg(10, 20, 10, 20) == pytest.approx(0.0, abs=1e-9)
    assert angular_sep_deg(0, 0, 12, 0) == pytest.approx(180.0, abs=1e-9)


def test_wrap180():
    assert wrap180(370.0) == pytest.approx(10.0)
    assert wrap180(-190.0) == pytest.approx(170.0)
    assert wrap180(180.0) == pytest.approx(-180.0)


def test_utcnow_is_utc():
    assert utcnow().tzinfo is not None
