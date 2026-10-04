"""Pure-python astronomical transforms (no astropy needed at runtime).

ScopePilot drives the mount in alt-az: ``GOTO AZM-ALT`` needs no hand-
controller alignment, unlike ``GOTO RA/DEC``. Every equatorial target is
therefore converted here (UTC -> Julian date -> GMST -> LST -> hour angle
-> alt-az) before it reaches the mount.

Conventions: azimuth measured from north, eastward (0=N, 90=E);
longitude east-positive (Stratford, Ontario = -80.98).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def datetime_to_jd(dt: datetime) -> float:
    """UTC datetime -> Julian date."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    a = (14 - m) // 12
    y2 = y + 4800 - a
    m2 = m + 12 * a - 3
    jdn = (
        dt.day
        + (153 * m2 + 2) // 5
        + 365 * y2
        + y2 // 4
        - y2 // 100
        + y2 // 400
        - 32045
    )
    frac = (dt.hour - 12) / 24.0 + dt.minute / 1440.0 + dt.second / 86400.0
    frac += dt.microsecond / 86400.0 / 1e6
    return jdn + frac


def gmst_hours(jd: float) -> float:
    """Greenwich Mean Sidereal Time, hours (IAU low-precision series)."""
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return (gmst_deg / 15.0) % 24.0


def lst_hours(jd: float, lon_deg: float) -> float:
    """Local Sidereal Time, hours (longitude east-positive)."""
    return (gmst_hours(jd) + lon_deg / 15.0) % 24.0


def _matmul(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _matvec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _precession_matrix(jd):
    """IAU 1976 precession matrix J2000 -> mean-of-date (Meeus Ch. 21)."""
    t = (jd - 2451545.0) / 36525.0
    zeta = math.radians(
        (2306.2181 * t + 0.30188 * t * t + 0.017998 * t**3) / 3600.0)
    z = math.radians(
        (2306.2181 * t + 1.09468 * t * t + 0.018203 * t**3) / 3600.0)
    theta = math.radians(
        (2004.3109 * t - 0.42665 * t * t - 0.041833 * t**3) / 3600.0)

    def r3(a):  # Meeus R3
        c, s = math.cos(a), math.sin(a)
        return ((c, s, 0.0), (-s, c, 0.0), (0.0, 0.0, 1.0))

    def r2(a):  # Meeus R2
        c, s = math.cos(a), math.sin(a)
        return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))

    return _matmul(r3(-z), _matmul(r2(theta), r3(-zeta)))


def _rect(ra_hours, dec_deg):
    ra, dec = math.radians(ra_hours * 15.0), math.radians(dec_deg)
    return (math.cos(dec) * math.cos(ra),
            math.cos(dec) * math.sin(ra),
            math.sin(dec))


def _radec(v):
    x, y, z = v
    ra = math.degrees(math.atan2(y, x)) / 15.0 % 24.0
    dec = math.degrees(math.asin(min(1.0, max(-1.0, z))))
    return ra, dec


def _precess_j2000_to_date(ra_hours: float, dec_deg: float, jd: float
                          ) -> tuple[float, float]:
    """Precess J2000 mean coordinates to mean-of-date (IAU 1976).

    Nutation/aberration (<30") are omitted.
    """
    return _radec(_matvec(_precession_matrix(jd), _rect(ra_hours, dec_deg)))


def _deprecess_to_j2000(ra_hours: float, dec_deg: float, jd: float
                       ) -> tuple[float, float]:
    """Inverse: mean-of-date -> J2000 (transpose of the precession matrix)."""
    return _radec(
        _matvec(_transpose(_precession_matrix(jd)), _rect(ra_hours, dec_deg)))


def radec_to_altaz(
    ra_hours: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
    dt: datetime,
) -> tuple[float, float]:
    """J2000 RA/Dec -> (az, alt): precesses to mean-of-date, then converts.

    Returns azimuth degrees from north eastward, altitude degrees.
    Nutation/aberration (<30") are not modeled.
    """
    jd = datetime_to_jd(dt)
    ra_hours, dec_deg = _precess_j2000_to_date(ra_hours, dec_deg, jd)
    ha_deg = ((lst_hours(jd, lon_deg) - ra_hours) % 24.0) * 15.0
    if ha_deg > 180.0:
        ha_deg -= 360.0
    ha, dec, lat = math.radians(ha_deg), math.radians(dec_deg), math.radians(lat_deg)

    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    sin_alt = min(1.0, max(-1.0, sin_alt))
    alt = math.degrees(math.asin(sin_alt))

    # Azimuth from south, westward (Meeus); shift to from-north eastward.
    y = math.sin(ha)
    x = math.cos(ha) * math.sin(lat) - math.tan(dec) * math.cos(lat)
    az_south = math.degrees(math.atan2(y, x))
    az = (az_south + 180.0) % 360.0
    return az, alt


def altaz_to_radec(
    az_deg: float,
    alt_deg: float,
    lat_deg: float,
    lon_deg: float,
    dt: datetime,
) -> tuple[float, float]:
    """Inverse of :func:`radec_to_altaz` -> J2000 (ra_hours, dec_deg).

    Inverts the precession as well, so the round trip is exact.
    """
    jd = datetime_to_jd(dt)
    lat = math.radians(lat_deg)
    alt = math.radians(alt_deg)
    az_south = math.radians((az_deg + 180.0) % 360.0)  # back to from-south

    # Meeus inverse: note the MINUS sign.
    sin_dec = (math.sin(lat) * math.sin(alt)
               - math.cos(lat) * math.cos(alt) * math.cos(az_south))
    sin_dec = min(1.0, max(-1.0, sin_dec))
    dec_date = math.degrees(math.asin(sin_dec))

    y = math.sin(az_south)
    x = math.cos(az_south) * math.sin(lat) + math.tan(alt) * math.cos(lat)
    ha_deg = math.degrees(math.atan2(y, x))
    ra_date = (lst_hours(jd, lon_deg) - ha_deg / 15.0) % 24.0
    # Precession is a rotation: invert it by precessing with negated T.
    return _deprecess_to_j2000(ra_date, dec_date, jd)


def angular_sep_deg(
    ra1_h: float, dec1_d: float, ra2_h: float, dec2_d: float
) -> float:
    """Great-circle separation of two RA/Dec points, degrees (haversine)."""
    r1, d1 = math.radians(ra1_h * 15.0), math.radians(dec1_d)
    r2, d2 = math.radians(ra2_h * 15.0), math.radians(dec2_d)
    s = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(s))))


def wrap180(deg: float) -> float:
    """Wrap degrees to [-180, 180)."""
    return (deg + 180.0) % 360.0 - 180.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
