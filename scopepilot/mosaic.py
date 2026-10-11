"""Mosaic planner: tile big targets into panels for a small-FOV scope.

A 6SE at f/10 gives ~0.85° x 0.57° on a Canon T7i -- M31 (3° wide) needs
a mosaic. :func:`plan_mosaic` lays out a snake-ordered panel grid around
the target center; panels are exported as a night-plan YAML that
``scopepilot queue --plan`` already walks (slew -> dwell/image ->
next panel).
"""

from __future__ import annotations

import math


def plan_mosaic(
    ra_hours: float,
    dec_deg: float,
    fov_w_deg: float,
    fov_h_deg: float,
    *,
    overlap: float = 0.15,
    width_deg: float | None = None,
    height_deg: float | None = None,
    nx: int | None = None,
    ny: int | None = None,
) -> list[dict]:
    """Compute mosaic panel centers around (ra_hours, dec_deg).

    Give either the target *width/height* (panels are derived) or explicit
    *nx*/*ny* panel counts. Panels step by ``fov * (1 - overlap)`` on a
    local tangent plane; RA steps are widened by 1/cos(dec). Snake order
    minimizes slew distance. Returns [{panel, ra_hours, dec_deg}, ...].
    """
    if not 0.0 <= overlap < 0.9:
        raise ValueError("overlap must be in [0, 0.9)")
    step_w = fov_w_deg * (1.0 - overlap)
    step_h = fov_h_deg * (1.0 - overlap)
    if width_deg is not None and height_deg is not None:
        nx = max(1, math.ceil(width_deg / step_w))
        ny = max(1, math.ceil(height_deg / step_h))
    if nx is None or ny is None:
        raise ValueError("give width_deg+height_deg or nx+ny")
    nx, ny = max(1, int(nx)), max(1, int(ny))

    cosd = max(0.2, math.cos(math.radians(dec_deg)))
    panels: list[dict] = []
    for row in range(ny):
        dy = (row - (ny - 1) / 2.0) * step_h
        cols = range(nx) if row % 2 == 0 else range(nx - 1, -1, -1)
        for col in cols:
            dx = (col - (nx - 1) / 2.0) * step_w
            panels.append({
                "panel": len(panels) + 1,
                "ra_hours": (ra_hours + (dx / cosd) / 15.0) % 24.0,
                "dec_deg": dec_deg + dy,
            })
    return panels


def mosaic_to_plan(panels: list[dict], name: str) -> list[dict]:
    """Panels -> night-plan entries ``queue --plan`` understands."""
    return [{"name": f"{name} P{p['panel']}/{len(panels)}",
             "ra_hours": p["ra_hours"], "dec_deg": p["dec_deg"],
             "priority": 1.0} for p in panels]


def write_mosaic_yaml(path, panels: list[dict], name: str) -> None:
    """Write panels as a night-plan YAML (targets with explicit coords)."""
    from pathlib import Path

    import yaml
    doc = {"targets": [
        {"name": f"{name} P{p['panel']}/{len(panels)}",
         "ra_hours": round(p["ra_hours"], 5),
         "dec_deg": round(p["dec_deg"], 4)}
        for p in panels]}
    Path(path).write_text(yaml.safe_dump(doc))
