"""Software pointing model -- alignment without the hand-controller menus.

Unaligned, the mount's AZM-ALT readout is relative to the power-on pose::

    reported_az  = true_az  - az_offset
    reported_alt = true_alt - alt_offset

(up to small mechanical errors). Centering N known stars and recording
(true, reported) pairs fits the two zero-point offsets by least squares;
every later GOTO is then issued in *reported* coordinates via GOTO AZM-ALT,
which -- unlike GOTO RA/DEC -- needs no hand-controller alignment at all.

The offsets depend on the power-on pose, so the model is rebuilt each
session -- unless the OTA is always powered on in the same pose (level,
pointing north), in which case the saved model stays valid and
``--reuse`` picks it up.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scopepilot.astro import utcnow, wrap180

#: Bright alignment stars (J2000 RA hours, Dec degrees) -- easy eyepiece
#: targets for the guided alignment flow.
ALIGN_STARS: dict[str, tuple[float, float]] = {
    "Sirius": (6.752, -16.716),
    "Canopus": (6.399, -52.696),
    "Arcturus": (14.261, 19.182),
    "Vega": (18.616, 38.784),
    "Capella": (5.278, 45.998),
    "Rigel": (5.242, -8.202),
    "Procyon": (7.655, 5.225),
    "Betelgeuse": (5.919, 7.407),
    "Altair": (19.846, 8.868),
    "Aldebaran": (4.599, 16.509),
    "Antares": (16.490, -26.432),
    "Spica": (13.420, -11.161),
    "Pollux": (7.755, 28.026),
    "Deneb": (20.690, 45.280),
    "Regulus": (10.139, 11.967),
}


@dataclass
class SyncStar:
    name: str
    true_az: float
    true_alt: float
    reported_az: float
    reported_alt: float
    at: float  # unix seconds


@dataclass
class PointingModel:
    """Zero-point pointing model: true = reported + offset."""

    az_offset: float = 0.0
    alt_offset: float = 0.0
    stars: list[SyncStar] = field(default_factory=list)
    rms_arcmin: float = 0.0
    created: float = field(default_factory=time.time)
    site_lat: float | None = None
    site_lon: float | None = None

    # -- fitting ---------------------------------------------------------
    @classmethod
    def fit(cls, stars: list[SyncStar],
            site_lat: float | None = None,
            site_lon: float | None = None) -> "PointingModel":
        """Least-squares fit of the two zero-point offsets."""
        if not stars:
            raise ValueError("need at least one sync star to fit")
        daz = [wrap180(s.true_az - s.reported_az) for s in stars]
        dalt = [wrap180(s.true_alt - s.reported_alt) for s in stars]
        az_off = sum(daz) / len(daz)
        alt_off = sum(dalt) / len(dalt)
        model = cls(az_offset=az_off, alt_offset=alt_off, stars=list(stars),
                    site_lat=site_lat, site_lon=site_lon)
        model.rms_arcmin = model._rms()
        return model

    def _rms(self) -> float:
        if len(self.stars) < 2:
            return 0.0
        sq = 0.0
        for s in self.stars:
            paz, palt = self.to_true(s.reported_az, s.reported_alt)
            # small-angle sky separation of predicted vs true
            d_az = wrap180(paz - s.true_az) * math.cos(math.radians(s.true_alt))
            d_alt = palt - s.true_alt
            sq += d_az * d_az + d_alt * d_alt
        return math.sqrt(sq / len(self.stars)) * 60.0

    # -- use ---------------------------------------------------------------
    def to_true(self, rep_az: float, rep_alt: float) -> tuple[float, float]:
        """Mount-reported -> true sky coordinates."""
        return (rep_az + self.az_offset) % 360.0, rep_alt + self.alt_offset

    def to_reported(self, az: float, alt: float) -> tuple[float, float]:
        """True sky coordinates -> what to send the mount."""
        return (az - self.az_offset) % 360.0, alt - self.alt_offset

    # -- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PointingModel":
        stars = [SyncStar(**s) for s in d.get("stars", [])]
        return cls(
            az_offset=d.get("az_offset", 0.0),
            alt_offset=d.get("alt_offset", 0.0),
            stars=stars,
            rms_arcmin=d.get("rms_arcmin", 0.0),
            created=d.get("created", time.time()),
            site_lat=d.get("site_lat"),
            site_lon=d.get("site_lon"),
        )

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
        return p

    @classmethod
    def load(cls, path: str | Path) -> "PointingModel | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text()))
        except (json.JSONDecodeError, TypeError, KeyError):
            return None


def default_pointing_path() -> Path:
    return Path.home() / ".scopepilot" / "pointing.json"


def suggest_alignment_stars(
    lat_deg: float,
    lon_deg: float,
    dt=None,
    count: int = 3,
) -> list[tuple[str, float, float]]:
    """Pick *count* bright stars, well-placed and spread in azimuth.

    Returns [(name, az_deg, alt_deg)] sorted by altitude, highest first.
    """
    from scopepilot.astro import radec_to_altaz

    dt = dt or utcnow()
    cands = []
    for name, (ra, dec) in ALIGN_STARS.items():
        az, alt = radec_to_altaz(ra, dec, lat_deg, lon_deg, dt)
        if 25.0 <= alt <= 75.0:
            cands.append((name, az, alt))
    cands.sort(key=lambda c: -c[2])
    picked: list[tuple[str, float, float]] = []
    for cand in cands:
        if len(picked) >= count:
            break
        if all(abs(wrap180(cand[1] - p[1])) >= 60.0 for p in picked):
            picked.append(cand)
    # relax the spread requirement rather than returning too few
    for cand in cands:
        if len(picked) >= count:
            break
        if cand not in picked:
            picked.append(cand)
    return picked
