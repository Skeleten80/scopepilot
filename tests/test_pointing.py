"""Tests for the software pointing model."""

import time

import pytest

from scopepilot.astro import utcnow
from scopepilot.pointing import (
    ALIGN_STARS,
    PointingModel,
    SyncStar,
    default_pointing_path,
    suggest_alignment_stars,
)


def _star(name, t_az, t_alt, r_az, r_alt):
    return SyncStar(name=name, true_az=t_az, true_alt=t_alt,
                    reported_az=r_az, reported_alt=r_alt, at=time.time())


def test_fit_recovers_offsets():
    # True offsets: az +30 (reported = true - 30), alt -12.
    stars = [_star("a", 100.0, 40.0, 70.0, 52.0),
             _star("b", 200.0, 50.0, 170.0, 62.0),
             _star("c", 300.0, 30.0, 270.0, 42.0)]
    m = PointingModel.fit(stars)
    assert m.az_offset == pytest.approx(30.0)
    assert m.alt_offset == pytest.approx(-12.0)
    assert m.rms_arcmin == pytest.approx(0.0, abs=1e-6)


def test_fit_single_star_exact():
    m = PointingModel.fit([_star("a", 100.0, 40.0, 70.0, 52.0)])
    assert m.az_offset == pytest.approx(30.0)
    assert m.rms_arcmin == 0.0


def test_fit_needs_a_star():
    with pytest.raises(ValueError):
        PointingModel.fit([])


def test_fit_handles_az_wrap():
    stars = [_star("a", 5.0, 40.0, 355.0, 40.0),
             _star("b", 10.0, 45.0, 0.0, 45.0)]
    m = PointingModel.fit(stars)
    assert m.az_offset == pytest.approx(10.0)


def test_rms_detects_bad_star():
    stars = [_star("a", 100.0, 40.0, 70.0, 52.0),
             _star("b", 200.0, 50.0, 170.0, 62.0),
             _star("c", 300.0, 30.0, 270.0, 60.0)]  # 18 deg off in alt
    m = PointingModel.fit(stars)
    assert m.rms_arcmin > 100.0


def test_to_true_to_reported_roundtrip():
    m = PointingModel.fit([_star("a", 100.0, 40.0, 70.0, 52.0)])
    az, alt = m.to_true(70.0, 52.0)
    assert az == pytest.approx(100.0)
    assert alt == pytest.approx(52.0 - 12.0)
    r_az, r_alt = m.to_reported(100.0, 40.0)
    assert r_az == pytest.approx(70.0)
    assert r_alt == pytest.approx(52.0)


def test_save_load_roundtrip(tmp_path):
    m = PointingModel.fit([_star("a", 100.0, 40.0, 70.0, 52.0)],
                          site_lat=43.3, site_lon=-80.9)
    p = m.save(tmp_path / "pointing.json")
    m2 = PointingModel.load(p)
    assert m2 is not None
    assert m2.az_offset == pytest.approx(m.az_offset)
    assert m2.rms_arcmin == pytest.approx(m.rms_arcmin)
    assert len(m2.stars) == 1
    assert m2.site_lat == pytest.approx(43.3)


def test_load_missing_returns_none(tmp_path):
    assert PointingModel.load(tmp_path / "nope.json") is None


def test_load_corrupt_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert PointingModel.load(p) is None


def test_default_pointing_path():
    assert str(default_pointing_path()).endswith(".scopepilot/pointing.json")


def test_align_stars_table_sane():
    assert len(ALIGN_STARS) >= 10
    for name, (ra, dec) in ALIGN_STARS.items():
        assert 0 <= ra < 24 and -90 <= dec <= 90, name


def test_suggest_alignment_stars():
    # A winter evening in Stratford: several bright stars should qualify.
    from datetime import datetime, timezone

    dt = datetime(2026, 1, 15, 2, 0, 0, tzinfo=timezone.utc)
    picked = suggest_alignment_stars(43.3767, -80.9809, dt, count=3)
    assert 1 <= len(picked) <= 3
    for name, az, alt in picked:
        assert 25.0 <= alt <= 75.0, (name, alt)
