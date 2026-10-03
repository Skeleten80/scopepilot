"""Unit tests for the NexStar protocol codecs (pure functions)."""

import math

import pytest

from scopepilot.nexstar import (
    MODEL_NAMES,
    TRACKING_MODES,
    alt_deg_to_frac,
    az_deg_to_frac,
    dec_deg_to_frac,
    decode_altaz_16,
    decode_altaz_24,
    decode_location,
    decode_radec_16,
    decode_radec_24,
    decode_time,
    encode_16,
    encode_24,
    encode_altaz_24,
    encode_location,
    encode_radec_16,
    encode_radec_24,
    encode_time,
    frac_to_alt_deg,
    frac_to_az_deg,
    frac_to_dec_deg,
    frac_to_ra_hours,
    ra_hours_to_frac,
)


def test_known_vector_16bit():
    # Documented example: "34AB,12CE" -> DEC = 0x12CE/0xFFFF*360 = 26.44 deg
    ra, dec = decode_radec_16(b"34AB,12CE")
    assert ra == pytest.approx(0x34AB / 0xFFFF * 24, abs=1e-9)
    assert dec == pytest.approx(0x12CE / 0xFFFF * 360, abs=1e-9)
    assert ra == pytest.approx(4.9374, abs=1e-3)
    assert dec == pytest.approx(26.4449, abs=1e-3)


def test_known_vector_precise():
    # "34AB0500,12CE0500": only the upper 24 bits count, last byte 00.
    ra, dec = decode_radec_24(b"34AB0500,12CE0500")
    assert ra == pytest.approx(0x34AB05 / 0xFFFFFF * 24, abs=1e-9)
    assert dec == pytest.approx(0x12CE05 / 0xFFFFFF * 360, abs=1e-9)


def test_encode_24_last_byte_zero():
    assert encode_24(0.123456).endswith("00")
    assert len(encode_24(0.5)) == 8


def test_encode_16_width():
    assert len(encode_16(0.5)) == 4
    assert encode_16(0.0) == "0000"
    assert encode_16(1.0) == "0000"  # wraps


@pytest.mark.parametrize(
    "ra,dec",
    [(0.0, 0.0), (13.498, 47.195), (23.999, -89.9), (5.588, -5.391),
     (18.0, 90.0), (2.32, 57.13)],
)
def test_radec_roundtrip_16(ra, dec):
    payload = encode_radec_16(ra, dec)
    ra2, dec2 = decode_radec_16(payload)
    # 16-bit resolution is ~20 arcsec
    assert abs((ra2 - ra + 12) % 24 - 12) < 0.002
    assert abs(dec2 - dec) < 0.01


@pytest.mark.parametrize(
    "ra,dec",
    [(0.0, 0.0), (13.498, 47.195), (23.999, -89.9), (5.588, -5.391)],
)
def test_radec_roundtrip_24(ra, dec):
    payload = encode_radec_24(ra, dec)
    ra2, dec2 = decode_radec_24(payload)
    assert abs((ra2 - ra + 12) % 24 - 12) < 1e-4
    assert abs(dec2 - dec) < 1e-3


def test_negative_dec_wraps():
    # -30 deg encodes as the 330-deg fraction and decodes back to -30.
    ra, dec = decode_radec_24(encode_radec_24(10.0, -30.0))
    assert dec == pytest.approx(-30.0, abs=1e-3)


def test_altaz_roundtrip():
    payload = encode_altaz_24(200.5, 45.25)
    az, alt = decode_altaz_24(payload)
    assert az == pytest.approx(200.5, abs=1e-3)
    assert alt == pytest.approx(45.25, abs=1e-3)


def test_frac_helpers():
    assert ra_hours_to_frac(24.0) == pytest.approx(0.0)
    assert frac_to_ra_hours(0.5) == pytest.approx(12.0)
    assert frac_to_dec_deg(dec_deg_to_frac(-45.0)) == pytest.approx(-45.0)
    assert frac_to_az_deg(az_deg_to_frac(359.9)) == pytest.approx(359.9)
    assert frac_to_alt_deg(alt_deg_to_frac(90.0)) == pytest.approx(90.0)


def test_location_stratford():
    blob = encode_location(43.3767, -80.9809)
    assert len(blob) == 8
    # lat 43 22 36 N, lon 80 58 51 W
    assert blob[0] == 43 and blob[3] == 0
    assert blob[4] == 80 and blob[7] == 1
    lat, lon = decode_location(blob)
    assert lat == pytest.approx(43.3767, abs=1 / 3600 + 1e-9)
    assert lon == pytest.approx(-80.9809, abs=1 / 3600 + 1e-9)


def test_location_southern_eastern():
    lat, lon = decode_location(encode_location(-33.86, 151.20))
    assert lat == pytest.approx(-33.86, abs=1 / 3600 + 1e-9)
    assert lon == pytest.approx(151.20, abs=1 / 3600 + 1e-9)


def test_time_codec():
    blob = encode_time(2026, 10, 3, 21, 5, 7, -4, True)
    assert blob == bytes([21, 5, 7, 10, 3, 26, 252, 1])
    t = decode_time(blob)
    assert t == {"hour": 21, "minute": 5, "second": 7, "month": 10,
                 "day": 3, "year": 2026, "utc_offset_hours": -4, "dst": True}


def test_time_codec_positive_offset():
    t = decode_time(encode_time(2026, 1, 15, 12, 0, 0, 5.5 - 0.5, False))
    # encode_time rounds the offset to whole hours
    assert t["utc_offset_hours"] == 5


def test_time_bad_length():
    with pytest.raises(Exception):
        decode_time(b"\x00" * 7)


def test_model_table_has_6se():
    assert MODEL_NAMES[12] == "NexStar 6/8 SE"


def test_tracking_modes():
    assert TRACKING_MODES == {0: "off", 1: "alt-az", 2: "eq-north", 3: "eq-south"}


def test_dec_fold_symmetry():
    for d in (-90, -45, -0.5, 0, 0.5, 45, 89.9):
        assert frac_to_dec_deg(dec_deg_to_frac(d)) == pytest.approx(d, abs=1e-9)
