"""Integration bridge between ScopePilot and AstroCapture.

Three ways the two programs work alongside each other:

1. **Shared INDI server** -- ``astrocapture`` images through INDI while
   ScopePilot runs ``--backend indi`` as a second INDI client on the same
   ``indiserver``. No serial-port fight.
2. **Night-queue bridge** -- :func:`read_night_plan` reads an AstroCapture
   ``night:`` plan YAML; ``scopepilot queue`` slews the mount through each
   target in turn so the operator (or AstroCapture) can image them.
3. **Manual-override claim** -- while the operator drives the mount by hand
   (dashboard / handpad), ScopePilot's server publishes
   ``manual_override.claimed``; AstroCapture's sequencer can poll
   :func:`check_manual_override` and pause instead of fighting the slew.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml


class TargetNotFound(LookupError):
    pass


#: Fallback bright-object table (J2000 RA hours, Dec degrees) used when the
#: astrocapture catalog is not installed.
BRIGHT_OBJECTS: dict[str, tuple[float, float]] = {
    "M31": (0.712, 41.269),
    "M33": (1.564, 30.660),
    "M51": (13.498, 47.195),
    "M63": (13.263, 42.029),
    "M64": (12.945, 21.683),
    "M81": (9.926, 69.065),
    "M82": (9.931, 69.680),
    "M97": (11.246, 55.019),
    "M101": (14.054, 54.349),
    "M104": (12.666, -11.623),
    "M108": (11.193, 55.674),
    "M1": (5.575, 22.017),
    "M3": (13.703, 28.377),
    "M5": (15.309, 2.084),
    "M8": (18.061, -24.384),
    "M13": (16.695, 36.460),
    "M16": (18.313, -13.806),
    "M17": (18.347, -16.172),
    "M20": (18.045, -23.030),
    "M27": (19.993, 22.721),
    "M35": (6.152, 24.333),
    "M37": (5.873, 32.553),
    "M42": (5.588, -5.391),
    "M45": (3.783, 24.117),
    "M57": (18.893, 33.029),
    "M92": (17.285, 43.136),
    "NGC869": (2.320, 57.133),  # Double Cluster (h Persei)
    "NGC884": (2.373, 57.130),  # Double Cluster (chi Persei)
    "ALBIREO": (19.513, 27.960),
    "MIZAR": (13.399, 54.925),
}

_ALIASES = {
    "WHIRLPOOL": "M51",
    "BODES": "M81",
    "CIGAR": "M82",
    "ANDROMEDA": "M31",
    "TRIANGULUM": "M33",
    "ORION NEBULA": "M42",
    "PLEIADES": "M45",
    "HERCULES CLUSTER": "M13",
    "RING NEBULA": "M57",
    "DUMBBELL": "M27",
    "DOUBLE CLUSTER": "NGC869",
    "CRAB NEBULA": "M1",
    "OWL NEBULA": "M97",
}


def _from_astrocapture_catalog(name: str) -> tuple[float, float, str] | None:
    """Try astrocapture's 5,045-object catalog; None when unavailable."""
    try:
        from astrocapture.catalog import lookup  # type: ignore
    except ImportError:
        return None
    try:
        hit = lookup(name)
    except Exception:
        return None
    if not hit:
        return None
    try:
        # astrocapture stores RA in degrees, Dec in degrees.
        return float(hit["ra"]) / 15.0, float(hit["dec"]), "astrocapture-catalog"
    except (KeyError, TypeError, ValueError):
        return None


def resolve_target(name: str) -> tuple[float, float, str]:
    """Resolve *name* -> (ra_hours, dec_deg, source)."""
    key = name.strip().upper()
    hit = _from_astrocapture_catalog(name)
    if hit is not None:
        return hit
    key = _ALIASES.get(key, key)
    if key in BRIGHT_OBJECTS:
        ra, dec = BRIGHT_OBJECTS[key]
        return ra, dec, "scopepilot-builtin"
    raise TargetNotFound(
        f"unknown target {name!r}; try one of: "
        + ", ".join(sorted(BRIGHT_OBJECTS)[:12])
        + ", ..."
    )


def search_targets(query: str, limit: int = 12) -> list[dict]:
    """Search built-in table (+ astrocapture catalog when installed)."""
    q = query.strip().upper()
    out: list[dict] = []
    for key in sorted(_ALIASES):
        if q in key or q in _ALIASES[key]:
            key2 = _ALIASES[key]
            ra, dec = BRIGHT_OBJECTS[key2]
            out.append({"name": key2, "label": key.title(),
                        "ra_hours": ra, "dec_deg": dec,
                        "source": "scopepilot-builtin"})
    for key in sorted(BRIGHT_OBJECTS):
        if q in key and all(r["name"] != key for r in out):
            ra, dec = BRIGHT_OBJECTS[key]
            out.append({"name": key, "label": key, "ra_hours": ra,
                        "dec_deg": dec, "source": "scopepilot-builtin"})
    # astrocapture catalog matches first when installed
    try:
        from astrocapture.catalog import search  # type: ignore
        for hit in search(query, limit=limit) or []:
            try:
                out.insert(0, {
                    "name": hit.get("name") or hit.get("id") or query,
                    "label": hit.get("name") or query,
                    "ra_hours": float(hit["ra"]) / 15.0,
                    "dec_deg": float(hit["dec"]),
                    "source": "astrocapture-catalog",
                })
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:
        pass
    seen, deduped = set(), []
    for r in out:
        if r["name"] not in seen:
            seen.add(r["name"])
            deduped.append(r)
    return deduped[:limit]


def tonight_list(
    lat: float, lon: float, limit: int = 12, min_alt_deg: float = 20.0
) -> tuple[list[dict], str]:
    """Tonight's best-placed targets for a site (HC Sky Tour idea).

    Prefers astrocapture's astropy-powered ``tonight_best``; falls back
    to the builtin bright-object table ranked by current altitude.
    Returns (items, source).
    """
    from scopepilot.astro import radec_to_altaz, utcnow

    try:
        from astrocapture.catalog import tonight_best  # type: ignore
        rows = tonight_best(lat, lon, min_alt_deg=min_alt_deg, limit=limit)
        items = [{
            "name": r["name"],
            "type": r.get("type"),
            "mag": r.get("mag"),
            "constellation": r.get("constellation"),
            "peak_alt_deg": (round(float(r["peak_alt_deg"]), 1)
                             if r.get("peak_alt_deg") is not None else None),
            "hours_above": (round(float(r["hours_above"]), 1)
                            if r.get("hours_above") is not None else None),
        } for r in rows]
        return items, "astrocapture-catalog"
    except Exception:
        pass
    now = utcnow()
    rows = []
    for name, (ra, dec) in sorted(BRIGHT_OBJECTS.items()):
        try:
            _az, alt = radec_to_altaz(ra, dec, lat, lon, now)
        except Exception:
            continue
        if alt >= min_alt_deg:
            rows.append({
                "name": name, "type": None, "mag": None,
                "constellation": None, "alt_now_deg": round(alt, 1),
                "peak_alt_deg": None, "hours_above": None,
            })
    rows.sort(key=lambda r: -r["alt_now_deg"])
    return rows[:limit], "scopepilot-builtin"


def identify(
    ra_hours: float, dec_deg: float, max_sep_deg: float = 2.0
) -> dict | None:
    """HC Identify: nearest known object to an RA/Dec position.

    Returns ``{"name", "type", "sep_deg", ...}`` or None when nothing is
    within *max_sep_deg*.
    """
    from scopepilot.astro import angular_sep_deg

    candidates: list[tuple[str, float, float, str | None, str]] = []
    try:
        from astrocapture.catalog import load_catalog  # type: ignore
        for obj in load_catalog():
            try:
                candidates.append((
                    str(obj.get("name") or obj.get("id")),
                    float(obj["ra"]) / 15.0, float(obj["dec"]),
                    obj.get("type"), "astrocapture-catalog"))
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:
        candidates = [(name, ra, dec, None, "scopepilot-builtin")
                      for name, (ra, dec) in BRIGHT_OBJECTS.items()]
    best: dict | None = None
    for name, ra, dec, typ, src in candidates:
        sep = angular_sep_deg(ra_hours, dec_deg, ra, dec)
        if sep <= max_sep_deg and (best is None or sep < best["sep_deg"]):
            best = {"name": name, "type": typ, "sep_deg": round(sep, 2),
                    "ra_hours": ra, "dec_deg": dec, "source": src}
    return best


def read_night_plan(path: str | Path) -> list[dict]:
    """Read an AstroCapture night plan -> [{name, priority}]."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    targets = data.get("targets") or []
    plan = []
    for t in targets:
        if isinstance(t, str):
            plan.append({"name": t, "priority": 1.0})
        elif isinstance(t, dict) and t.get("name"):
            plan.append({"name": t["name"],
                         "priority": float(t.get("priority", 1.0))})
    # legacy single-target session block
    if not plan:
        sess = data.get("session") or {}
        tgt = sess.get("target")
        if isinstance(tgt, dict) and tgt.get("name"):
            plan.append({"name": tgt["name"], "priority": 1.0})
    # highest priority first, like the AstroCapture scheduler
    plan.sort(key=lambda t: -t["priority"])
    return plan


def check_manual_override(
    server_url: str, timeout: float = 2.0
) -> dict | None:
    """Poll a ScopePilot server; returns the manual_override dict.

    Returns None when the server is unreachable. AstroCapture's sequencer
    can call this each loop: ``if (ov or {}).get("claimed"): pause()``.
    """
    url = server_url.rstrip("/") + "/api/state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            state = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return state.get("manual_override")
