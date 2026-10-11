"""Solar-system ephemerides: Moon, comets/asteroids (MPC/JPL), satellites (TLE).

All pure-python except satellite propagation, which needs the ``sgp4``
package (``pip install sgp4``) -- imported lazily so everything else works
without it.

Accuracy notes (honest): the lunar series is Meeus' low-precision
truncated set (~10' worst case); Kepler propagation is two-body only
(good to arcminutes over days, fine for a goto that plate-solving then
centers); TLE+SGP4 is the standard public propagator (~km-level).
Network fetches (JPL SBDB, CelesTrak) are cached under
``~/.scopepilot/ephem/`` and ``~/.scopepilot/tle/``.
"""

from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from scopepilot.astro import angular_sep_deg, datetime_to_jd, utcnow

_CACHE = Path.home() / ".scopepilot" / "ephem"
_TLE_CACHE = Path.home() / ".scopepilot" / "tle"

_GAUSS_K = 0.01720209895  # Gaussian gravitational constant (AU^1.5/day)


# ---------------------------------------------------------------------------
# Moon (Meeus low-precision, truncated series)
# ---------------------------------------------------------------------------

def moon_radec(dt: datetime | None = None) -> tuple[float, float]:
    """Geocentric J2000 RA (hours) / Dec (deg) of the Moon, ~10' accuracy."""
    if dt is None:
        dt = utcnow()
    d = datetime_to_jd(dt) - 2451543.5  # days since 2000 Jan 1.5
    # Mean elements (degrees)
    L = (218.3164477 + 13.17639648 * d) % 360.0   # mean longitude
    Dm = (297.8501921 + 12.19074912 * d) % 360.0  # mean elongation
    Mm = (134.9633964 + 13.06499295 * d) % 360.0  # Moon mean anomaly
    Ms = (357.5291092 + 0.98560028 * d) % 360.0   # Sun mean anomaly
    F = (93.2720950 + 13.22935024 * d) % 360.0    # argument of latitude

    def s(x):
        return math.sin(math.radians(x))

    # Major perturbation terms only (Meeus Table 47.A, largest)
    lon = (L + 6.289 * s(Mm) + 1.274 * s(2 * Dm - Mm) + 0.658 * s(2 * Dm)
           + 0.214 * s(2 * Mm) - 0.186 * s(Ms) - 0.114 * s(2 * F))
    lat = (5.128 * s(F) + 0.281 * s(Mm + F) + 0.277 * s(Mm - F)
           + 0.173 * s(2 * Dm - F))
    # Ecliptic -> equatorial (J2000 obliquity)
    eps = math.radians(23.4392911)
    lo, la = math.radians(lon), math.radians(lat)
    x = math.cos(lo) * math.cos(la)
    y = math.sin(lo) * math.cos(la) * math.cos(eps) - math.sin(la) * math.sin(eps)
    z = math.sin(lo) * math.cos(la) * math.sin(eps) + math.sin(la) * math.cos(eps)
    ra = math.degrees(math.atan2(y, x)) / 15.0 % 24.0
    dec = math.degrees(math.asin(min(1.0, max(-1.0, z))))
    return ra, dec


def _sun_radec(dt: datetime) -> tuple[float, float]:
    d = datetime_to_jd(dt) - 2451543.5
    Ms = (357.5291092 + 0.98560028 * d) % 360.0
    L = (280.4606184 + 0.98564736 * d) % 360.0

    def s(x):
        return math.sin(math.radians(x))

    lon = L + 1.915 * s(Ms) + 0.020 * s(2 * Ms)
    eps = math.radians(23.4392911)
    lo = math.radians(lon)
    ra = math.degrees(math.atan2(math.sin(lo) * math.cos(eps), math.cos(lo))) / 15.0 % 24.0
    dec = math.degrees(math.asin(math.sin(lo) * math.sin(eps)))
    return ra, dec


def moon_illumination(dt: datetime | None = None) -> float:
    """Fraction of the Moon's disc illuminated, 0 (new) .. 1 (full)."""
    if dt is None:
        dt = utcnow()
    mra, mdec = moon_radec(dt)
    sra, sdec = _sun_radec(dt)
    elong = angular_sep_deg(mra, mdec, sra, sdec)
    return (1.0 - math.cos(math.radians(elong))) / 2.0


def moon_sep_deg(ra_h: float, dec_d: float,
                 dt: datetime | None = None) -> float:
    """Angular separation of an RA/Dec target from the Moon, degrees."""
    mra, mdec = moon_radec(dt)
    return angular_sep_deg(ra_h, dec_d, mra, mdec)


# ---------------------------------------------------------------------------
# Kepler propagation (comets / asteroids from orbital elements)
# ---------------------------------------------------------------------------

def kepler_E(M_rad: float, e: float) -> float:
    """Solve Kepler's equation M = E - e*sin(E) (elliptical, Newton)."""
    E = M_rad if e < 0.8 else math.pi
    for _ in range(50):
        f = E - e * math.sin(E) - M_rad
        fp = 1.0 - e * math.cos(E)
        dE = f / fp
        E -= dE
        if abs(dE) < 1e-12:
            break
    return E


def kepler_H(M_rad: float, e: float) -> float:
    """Hyperbolic Kepler: M = e*sinh(H) - H."""
    H = math.asinh(M_rad / e)
    for _ in range(50):
        f = e * math.sinh(H) - H - M_rad
        fp = e * math.cosh(H) - 1.0
        dH = f / fp
        H -= dH
        if abs(dH) < 1e-12:
            break
    return H


def _earth_heliocentric(jd: float) -> tuple[float, float, float]:
    """Earth heliocentric ecliptic rectangular coords (AU, J2000-ish)."""
    d = jd - 2451543.5
    Ms = math.radians((357.5291092 + 0.98560028 * d) % 360.0)
    L = math.radians((280.4606184 + 0.98564736 * d) % 360.0)
    # Low-precision Earth orbit
    e = 0.01671123
    a = 1.00000261
    E = kepler_E(Ms, e)
    x = a * (math.cos(E) - e)
    y = a * math.sqrt(1 - e * e) * math.sin(E)
    lon = L - math.radians(180.0)  # heliocentric = geocentric + 180
    # rotate by (lon - Ms) to align perihelion correctly; good to ~arcmin
    ang = lon - Ms
    ca, sa = math.cos(ang), math.sin(ang)
    return (x * ca - y * sa, x * sa + y * ca, 0.0)


def propagate_elements(el: dict, dt: datetime) -> tuple[float, float]:
    """Orbital elements -> geocentric J2000 (ra_hours, dec_deg) at *dt*.

    *el* keys: ``epoch_jd``, ``a_au``, ``e``, ``i_deg``, ``node_deg``,
    ``argp_deg``, ``M0_deg`` (mean anomaly at epoch).
    """
    jd = datetime_to_jd(dt)
    a, e = float(el["a_au"]), float(el["e"])
    i = math.radians(float(el["i_deg"]))
    node = math.radians(float(el["node_deg"]))
    argp = math.radians(float(el["argp_deg"]))
    M0 = math.radians(float(el["M0_deg"]))
    n_deg_day = math.degrees(_GAUSS_K) / (abs(a) ** 1.5)  # mean motion
    M = math.radians((math.degrees(M0) + n_deg_day * (jd - float(el["epoch_jd"]))) % 360.0)

    if e < 1.0:
        E = kepler_E(M, e)
        xp = a * (math.cos(E) - e)
        yp = a * math.sqrt(1 - e * e) * math.sin(E)
    else:  # hyperbolic (some comets)
        H = kepler_H(M, e)
        xp = a * (e - math.cosh(H))  # a negative for hyperbola
        yp = -a * math.sqrt(e * e - 1) * math.sinh(H)

    # Orbital plane -> ecliptic J2000
    cw, sw = math.cos(argp), math.sin(argp)
    cn, sn = math.cos(node), math.sin(node)
    ci, si = math.cos(i), math.sin(i)
    x = (cn * cw - sn * sw * ci) * xp + (-cn * sw - sn * cw * ci) * yp
    y = (sn * cw + cn * sw * ci) * xp + (-sn * sw + cn * cw * ci) * yp
    z = (sw * si) * xp + (cw * si) * yp

    # Geocentric: subtract Earth's heliocentric position
    ex, ey, ez = _earth_heliocentric(jd)
    gx, gy, gz = x - ex, y - ey, z - ez

    # Ecliptic -> equatorial
    eps = math.radians(23.4392911)
    xq, yq, zq = gx, gy * math.cos(eps) - gz * math.sin(eps), gy * math.sin(eps) + gz * math.cos(eps)
    ra = math.degrees(math.atan2(yq, xq)) / 15.0 % 24.0
    dec = math.degrees(math.asin(min(1.0, max(-1.0, zq / math.sqrt(xq * xq + yq * yq + zq * zq)))))
    return ra, dec


def _sbdb_elements(name: str) -> dict:
    """Fetch orbital elements from JPL's Small-Body Database API (cached)."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in name.lower())[:48]
    cache = _CACHE / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = ("https://ssd-api.jpl.nasa.gov/sbdb.api?sstr="
           + urllib.request.quote(name))
    req = urllib.request.Request(url, headers={"User-Agent": "ScopePilot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "orbit" not in data or "elements" not in data["orbit"]:
        raise LookupError(f"JPL SBDB has no orbit for {name!r}")
    els = {e["name"]: float(e["value"]) for e in data["orbit"]["elements"]}
    epoch_jd = float(data["orbit"]["epoch"])
    # SBDB gives time-of-perihelion tp; convert to mean anomaly at epoch.
    a, e = els["a"], els["e"]
    n_deg_day = math.degrees(_GAUSS_K) / (abs(a) ** 1.5)
    M0 = (n_deg_day * (epoch_jd - els["tp"])) % 360.0
    out = {
        "name": data.get("object", {}).get("fullname", name),
        "epoch_jd": epoch_jd, "a_au": a, "e": e,
        "i_deg": els["i"], "node_deg": els["om"], "argp_deg": els["w"],
        "M0_deg": M0,
    }
    cache.write_text(json.dumps(out))
    return out


def ephem_radec(name: str, kind: str = "auto",
                dt: datetime | None = None) -> tuple[float, float, dict]:
    """Current J2000 (ra_hours, dec_deg) for a comet/asteroid *name*.

    Resolves elements via JPL SBDB (cached). Returns (ra, dec, elements).
    """
    el = _sbdb_elements(name)
    ra, dec = propagate_elements(el, dt or utcnow())
    return ra, dec, el


# ---------------------------------------------------------------------------
# Satellites (TLE + SGP4)
# ---------------------------------------------------------------------------

def _require_sgp4():
    try:
        from sgp4.api import Satrec, jday  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "satellite tracking needs the 'sgp4' package: "
            "pip install sgp4") from exc
    return Satrec, jday


def fetch_tle(norad_id: int) -> tuple[str, str]:
    """Fetch TLE lines from CelesTrak (cached 24 h)."""
    _TLE_CACHE.mkdir(parents=True, exist_ok=True)
    cache = _TLE_CACHE / f"{norad_id}.tle"
    if cache.exists():
        import time
        if time.time() - cache.stat().st_mtime < 86400:
            l1, l2 = cache.read_text().splitlines()[:2]
            return l1.strip(), l2.strip()
    url = (f"https://celestrak.org/NORAD/elements/gp.php?"
           f"CATNR={norad_id}&FORMAT=TLE")
    req = urllib.request.Request(url, headers={"User-Agent": "ScopePilot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        lines = [ln.strip() for ln in resp.read().decode("utf-8").splitlines()
                 if ln.strip().startswith(("1 ", "2 "))]
    if len(lines) < 2:
        raise LookupError(f"no TLE for NORAD {norad_id}")
    cache.write_text("\n".join(lines[:2]) + "\n")
    return lines[0], lines[1]


def sat_altaz(norad_id: int, lat_deg: float, lon_deg: float,
              dt: datetime | None = None) -> tuple[float, float]:
    """Topocentric (az_deg, alt_deg) for a satellite at *dt*.

    Simplified TEME->topocentric chain (drops the equation-of-the-
    equinoxes term); good to ~0.1°, plenty for tracking updates at Hz.
    """
    Satrec, jday = _require_sgp4()
    if dt is None:
        dt = utcnow()
    l1, l2 = fetch_tle(norad_id)
    sat = Satrec.twoline2rv(l1, l2)
    jd, fr = jday(dt.year, dt.month, dt.day,
                  dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    err, r_teme, _v = sat.sgp4(jd, fr)
    if err:
        raise RuntimeError(f"SGP4 error {err} for NORAD {norad_id}")
    # TEME -> pseudo-Earth-fixed by GMST rotation
    from scopepilot.astro import gmst_hours
    theta = math.radians(gmst_hours(jd + fr) * 15.0)
    x = r_teme[0] * math.cos(theta) + r_teme[1] * math.sin(theta)
    y = -r_teme[0] * math.sin(theta) + r_teme[1] * math.cos(theta)
    z = r_teme[2]
    # ECEF -> topocentric ENU
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    # observer ECEF (spherical Earth, km)
    R = 6371.0
    ox, oy, oz = (R * math.cos(lat) * math.cos(lon),
                  R * math.cos(lat) * math.sin(lon),
                  R * math.sin(lat))
    dx, dy, dz = x - ox, y - oy, z - oz
    e = -math.sin(lon) * dx + math.cos(lon) * dy
    n = (-math.sin(lat) * math.cos(lon) * dx - math.sin(lat) * math.sin(lon) * dy
         + math.cos(lat) * dz)
    u = (math.cos(lat) * math.cos(lon) * dx + math.cos(lat) * math.sin(lon) * dy
         + math.sin(lat) * dz)
    rng = math.sqrt(e * e + n * n + u * u)
    alt = math.degrees(math.asin(min(1.0, max(-1.0, u / rng))))
    az = math.degrees(math.atan2(e, n)) % 360.0
    return az, alt


def sat_passes(norad_id: int, lat_deg: float, lon_deg: float,
               hours: float = 24.0, min_alt_deg: float = 20.0,
               step_s: int = 20) -> list[dict]:
    """Upcoming passes above *min_alt_deg*: [{rise_utc, set_utc, max_alt}]."""
    from datetime import timedelta
    _require_sgp4()  # fail fast with the pip-install hint, not empty passes
    now = utcnow().replace(microsecond=0)
    n = int(hours * 3600 / step_s)
    passes, cur = [], None
    for i in range(n + 1):
        t = now + timedelta(seconds=i * step_s)
        try:
            _az, alt = sat_altaz(norad_id, lat_deg, lon_deg, t)
        except Exception:
            continue
        up = alt >= min_alt_deg
        if up and cur is None:
            cur = {"rise": t, "max_alt": alt}
        elif up and cur is not None:
            cur["max_alt"] = max(cur["max_alt"], alt)
        elif not up and cur is not None:
            cur["set"] = t
            passes.append({
                "rise_utc": cur["rise"].isoformat(timespec="minutes"),
                "set_utc": cur["set"].isoformat(timespec="minutes"),
                "max_alt_deg": round(cur["max_alt"], 1)})
            cur = None
    return passes
