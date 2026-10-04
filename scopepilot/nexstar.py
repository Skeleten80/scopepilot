"""Celestron NexStar hand-controller serial protocol.

Implements the documented PC-port command set for talking to a NexStar /
NexStar+ hand controller (e.g. the one on Mathias's Celestron NexStar 6SE):

* 9600 baud, 8 data bits, no parity, 1 stop bit
* every response is terminated with ``#`` (0x23); allow up to 3.5 s
* positions are hexadecimal fractions of a full rotation
  (16-bit "standard", or 24-bit "precise" packed into 32-bit fields whose
  last byte is always ``00``)

Reference: Celestron "NexStar Communication Protocol" (PC-port commands),
cross-checked against the community AUX reference validated on real
hardware in 2026 (open-astro/AlpacaBridge ``nexstar_protocol_reference.md``).

This module has **no third-party dependencies**: the serial transport is
import-guarded so everything else (codecs, driver logic, simulator) works
without ``pyserial`` installed.
"""

from __future__ import annotations

import queue
import threading
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_BAUD = 9600
#: How long the driver waits for a ``#``-terminated response (protocol doc).
RESPONSE_TIMEOUT = 3.5

#: AUX bus device addresses for the two motor controllers.
DEV_AZM = 16  # azimuth (RA on an equatorial mount)
DEV_ALT = 17  # altitude (Dec on an equatorial mount)

#: HC model IDs (``m`` command). 12 == 6/8 SE -- Mathias's scope.
MODEL_NAMES = {
    1: "NexStar GPS Series",
    3: "NexStar i-Series",
    4: "NexStar i-Series SE",
    5: "CGE",
    6: "Advanced GT",
    7: "NexStar SLT",
    9: "NexStar CPC",
    10: "NexStar GT",
    11: "NexStar 4/5 SE",
    12: "NexStar 6/8 SE",
    14: "CGX",
    20: "CGX-L",
    22: "NexStar Evolution",
}

#: Tracking modes (``t`` / ``T`` commands).
TRACKING_MODES = {0: "off", 1: "alt-az", 2: "eq-north", 3: "eq-south"}
TRACKING_MODE_IDS = {v: k for k, v in TRACKING_MODES.items()}

#: Fixed slew rates 1-9 (``MC_MOVE_POS/NEG``) mapped to approximate °/s.
#: Rate 9 is the mount's maximum slew; Celestron documents ~4 deg/s for the
#: SE series. Intermediate steps are approximate -- the HC does not publish
#: exact values.
JOG_RATES_DPS = {
    1: 0.10,
    2: 0.25,
    3: 0.50,
    4: 1.00,
    5: 1.50,
    6: 2.00,
    7: 2.50,
    8: 3.00,
    9: 4.00,
}

#: Mean sidereal rate, deg/s.
SIDEREAL_DPS = 360.0 / 86164.0905


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NexStarError(RuntimeError):
    """Base class for protocol/driver errors."""


class NexStarTimeout(NexStarError):
    """No ``#``-terminated response arrived in time."""


class PassthroughError(NexStarError):
    """An AUX pass-through command failed (device absent / unknown cmd)."""


# ---------------------------------------------------------------------------
# Coordinate codecs (pure functions -- heavily unit tested)
# ---------------------------------------------------------------------------


def ra_hours_to_frac(ra_hours: float) -> float:
    """RA hours -> fraction of a full rotation [0, 1)."""
    return (ra_hours % 24.0) / 24.0


def frac_to_ra_hours(frac: float) -> float:
    return (frac % 1.0) * 24.0


def dec_deg_to_frac(dec_deg: float) -> float:
    """Declination degrees -> fraction of a full rotation [0, 1).

    Negative declinations wrap: -30 deg == 330 deg == 0xD555... on the wire.
    """
    return (dec_deg % 360.0) / 360.0


def frac_to_dec_deg(frac: float) -> float:
    d = (frac % 1.0) * 360.0
    return d - 360.0 if d > 180.0 else d


def az_deg_to_frac(az_deg: float) -> float:
    return (az_deg % 360.0) / 360.0


def alt_deg_to_frac(alt_deg: float) -> float:
    return (alt_deg % 360.0) / 360.0


def frac_to_az_deg(frac: float) -> float:
    return (frac % 1.0) * 360.0


def frac_to_alt_deg(frac: float) -> float:
    return frac_to_dec_deg(frac)


def encode_16(frac: float) -> str:
    """Standard 16-bit encoding: 4 uppercase hex chars (``HHHH``)."""
    return f"{int(round((frac % 1.0) * 0xFFFF)):04X}"


def decode_16(hex4: str) -> float:
    return int(hex4, 16) / 0xFFFF


def encode_24(frac: float) -> str:
    """Precise encoding: 8 hex chars, upper 24 bits used, last byte ``00``."""
    return f"{((int(round((frac % 1.0) * 0xFFFFFF)) << 8) & 0xFFFFFFFF):08X}"


def decode_24(hex8: str) -> float:
    return (int(hex8, 16) >> 8) / 0xFFFFFF


def encode_radec_16(ra_hours: float, dec_deg: float) -> bytes:
    return (
        encode_16(ra_hours_to_frac(ra_hours))
        + ","
        + encode_16(dec_deg_to_frac(dec_deg))
    ).encode("ascii")


def encode_radec_24(ra_hours: float, dec_deg: float) -> bytes:
    return (
        encode_24(ra_hours_to_frac(ra_hours))
        + ","
        + encode_24(dec_deg_to_frac(dec_deg))
    ).encode("ascii")


def decode_radec_16(payload: bytes) -> tuple[float, float]:
    ra_h, dec_h = payload.decode("ascii").split(",")
    return frac_to_ra_hours(decode_16(ra_h)), frac_to_dec_deg(decode_16(dec_h))


def decode_radec_24(payload: bytes) -> tuple[float, float]:
    ra_h, dec_h = payload.decode("ascii").split(",")
    return frac_to_ra_hours(decode_24(ra_h)), frac_to_dec_deg(decode_24(dec_h))


def encode_altaz_16(az_deg: float, alt_deg: float) -> bytes:
    return (
        encode_16(az_deg_to_frac(az_deg)) + "," + encode_16(alt_deg_to_frac(alt_deg))
    ).encode("ascii")


def encode_altaz_24(az_deg: float, alt_deg: float) -> bytes:
    return (
        encode_24(az_deg_to_frac(az_deg)) + "," + encode_24(alt_deg_to_frac(alt_deg))
    ).encode("ascii")


def decode_altaz_16(payload: bytes) -> tuple[float, float]:
    az_h, alt_h = payload.decode("ascii").split(",")
    return frac_to_az_deg(decode_16(az_h)), frac_to_alt_deg(decode_16(alt_h))


def decode_altaz_24(payload: bytes) -> tuple[float, float]:
    az_h, alt_h = payload.decode("ascii").split(",")
    return frac_to_az_deg(decode_24(az_h)), frac_to_alt_deg(decode_24(alt_h))


def _dms_parts(value_deg: float) -> tuple[int, int, int, int]:
    """Split degrees into (deg, min, sec, sign) for the location command."""
    sign = 0 if value_deg >= 0 else 1
    v = abs(value_deg)
    deg = int(v)
    minutes_full = (v - deg) * 60.0
    minutes = int(minutes_full)
    seconds = int(round((minutes_full - minutes) * 60.0))
    if seconds == 60:  # carry
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        deg += 1
    return deg, minutes, seconds, sign


def encode_location(lat_deg: float, lon_deg: float) -> bytes:
    """8-byte site location: lat (deg,min,sec,0=N/1=S), lon (deg,min,sec,0=E/1=W)."""
    la = _dms_parts(lat_deg)
    lo = _dms_parts(lon_deg)
    return bytes([la[0], la[1], la[2], la[3], lo[0], lo[1], lo[2], lo[3]])


def decode_location(data: bytes) -> tuple[float, float]:
    """Inverse of :func:`encode_location`."""
    if len(data) != 8:
        raise NexStarError(f"location payload must be 8 bytes, got {len(data)}")
    lat = data[0] + data[1] / 60.0 + data[2] / 3600.0
    if data[3] == 1:
        lat = -lat
    lon = data[4] + data[5] / 60.0 + data[6] / 3600.0
    if data[7] == 1:
        lon = -lon
    return lat, lon


def encode_time(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    utc_offset_hours: float,
    dst: bool,
) -> bytes:
    """8-byte HC clock: hour,min,sec,month,day,year-2000,UTC offset,DST flag."""
    offset_byte = int(round(utc_offset_hours)) % 256  # negative -> 256-abs
    return bytes(
        [
            hour & 0xFF,
            minute & 0xFF,
            second & 0xFF,
            month & 0xFF,
            day & 0xFF,
            (year - 2000) & 0xFF,
            offset_byte,
            1 if dst else 0,
        ]
    )


def decode_time(data: bytes) -> dict:
    if len(data) != 8:
        raise NexStarError(f"time payload must be 8 bytes, got {len(data)}")
    offset = data[6] if data[6] < 128 else data[6] - 256
    return {
        "hour": data[0],
        "minute": data[1],
        "second": data[2],
        "month": data[3],
        "day": data[4],
        "year": 2000 + data[5],
        "utc_offset_hours": offset,
        "dst": bool(data[7]),
    }


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class Transport:
    """Byte transport to the hand controller."""

    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read_until(self, terminator: bytes, timeout: float) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def read_exact(self, n: int, timeout: float) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class SerialTransport(Transport):
    """Real RS-232/USB-serial link to the hand controller's PC port."""

    def __init__(self, port: str, baud: int = PROTOCOL_BAUD) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise NexStarError(
                "pyserial is required for the serial backend "
                "(pip install pyserial)"
            ) from exc
        self._ser = serial.Serial(
            port, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=0.1
        )

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def read_until(self, terminator: bytes, timeout: float) -> bytes:
        self._ser.timeout = timeout
        data = self._ser.read_until(terminator)
        if not data.endswith(terminator):
            raise NexStarTimeout(
                f"no {terminator!r}-terminated response within {timeout:.1f}s"
            )
        return data

    def read_exact(self, n: int, timeout: float) -> bytes:
        out = bytearray()
        deadline = time.monotonic() + timeout
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NexStarTimeout(
                    f"expected {n} bytes, got {len(out)} within {timeout:.1f}s"
                )
            self._ser.timeout = max(0.05, remaining)
            chunk = self._ser.read(n - len(out))
            if chunk:
                out += chunk
        return bytes(out)

    def close(self) -> None:
        self._ser.close()


class LoopbackPipe:
    """In-memory byte pipe connecting a client transport to a fake HC."""

    def __init__(self) -> None:
        self.client_to_hc: "queue.Queue[bytes]" = queue.Queue()
        self.hc_to_client: "queue.Queue[bytes]" = queue.Queue()


class LoopbackTransport(Transport):
    """Client side of a :class:`LoopbackPipe` (used with the simulator)."""

    def __init__(self, pipe: LoopbackPipe) -> None:
        self._pipe = pipe
        self._buf = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise NexStarError("transport closed")
        self._pipe.client_to_hc.put(bytes(data))

    def read_until(self, terminator: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(terminator)
            if idx >= 0:
                out = bytes(self._buf[: idx + len(terminator)])
                del self._buf[: idx + len(terminator)]
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NexStarTimeout(
                    f"no {terminator!r}-terminated response within {timeout:.1f}s"
                )
            try:
                chunk = self._pipe.hc_to_client.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            self._buf += chunk

    def read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NexStarTimeout(
                    f"expected {n} bytes, got {len(self._buf)} "
                    f"within {timeout:.1f}s"
                )
            try:
                chunk = self._pipe.hc_to_client.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class NexStarDriver:
    """High-level driver speaking the NexStar HC serial protocol.

    All methods block until the ``#``-terminated response arrives (up to
    ``timeout`` seconds, 3.5 s per the protocol document).
    """

    def __init__(self, transport: Transport, timeout: float = RESPONSE_TIMEOUT) -> None:
        self._t = transport
        self._timeout = timeout
        self._lock = threading.Lock()

    # -- low level ---------------------------------------------------------
    def _exchange(self, payload: bytes, expect_len: int | None = None) -> bytes:
        """Send *payload*, return the response body (``#`` stripped).

        ASCII responses are read up to the ``#`` terminator. Binary
        responses (version, time, location, AUX) are read as exactly
        *expect_len* bytes followed by ``#`` -- required because a binary
        payload byte may itself be 0x23 (``#``), e.g. HC fw 5.35.
        """
        with self._lock:
            self._t.write(payload)
            if expect_len is None:
                raw = self._t.read_until(b"#", self._timeout)
                if not raw.endswith(b"#"):  # pragma: no cover - defensive
                    raise NexStarTimeout("response not # terminated")
                return raw[:-1]
            body = self._t.read_exact(expect_len, self._timeout)
            term = self._t.read_exact(1, self._timeout)
            if term != b"#":
                raise NexStarError(
                    f"command {payload[:1]!r}: expected '#' after "
                    f"{expect_len} bytes, got {term!r}"
                )
            return body

    def close(self) -> None:
        self._t.close()

    # -- misc --------------------------------------------------------------
    def echo(self, byte: int = 0x42) -> bool:
        """``K``: round-trip check, True when the HC echoes the byte back."""
        return self._exchange(bytes([0x4B, byte & 0xFF]), 1) == bytes([byte & 0xFF])

    def get_version(self) -> tuple[int, int]:
        """``V``: hand-controller firmware (major, minor)."""
        resp = self._exchange(b"V", 2)
        return resp[0], resp[1]

    def get_model(self) -> int:
        """``m``: numeric model ID (12 == 6/8 SE)."""
        return self._exchange(b"m", 1)[0]

    def model_name(self) -> str:
        return MODEL_NAMES.get(self.get_model(), "unknown")

    def alignment_complete(self) -> bool:
        """``J``: True once the mount has been aligned (via the HC)."""
        return self._exchange(b"J", 1) == b"\x01"

    # -- position ----------------------------------------------------------
    def get_radec(self, precise: bool = True) -> tuple[float, float]:
        """``e``/``E``: current (RA hours, Dec degrees)."""
        resp = self._exchange(b"e" if precise else b"E")
        return (decode_radec_24 if precise else decode_radec_16)(resp)

    def get_altaz(self, precise: bool = True) -> tuple[float, float]:
        """``z``/``Z``: current (azimuth, altitude) degrees."""
        resp = self._exchange(b"z" if precise else b"Z")
        return (decode_altaz_24 if precise else decode_altaz_16)(resp)

    # -- goto / sync -------------------------------------------------------
    def goto_radec(self, ra_hours: float, dec_deg: float, precise: bool = True) -> None:
        """``r``/``R``: slew to RA/Dec (requires alignment). Returns at once."""
        cmd = b"r" if precise else b"R"
        enc = encode_radec_24 if precise else encode_radec_16
        self._exchange(cmd + enc(ra_hours, dec_deg))

    def goto_altaz(self, az_deg: float, alt_deg: float, precise: bool = True) -> None:
        """``b``/``B``: slew to azimuth/altitude. Returns at once."""
        cmd = b"b" if precise else b"B"
        enc = encode_altaz_24 if precise else encode_altaz_16
        self._exchange(cmd + enc(az_deg, alt_deg))

    def sync_radec(self, ra_hours: float, dec_deg: float, precise: bool = True) -> None:
        """``s``/``S``: sync on the given RA/Dec (centered object)."""
        cmd = b"s" if precise else b"S"
        enc = encode_radec_24 if precise else encode_radec_16
        self._exchange(cmd + enc(ra_hours, dec_deg))

    def goto_in_progress(self) -> bool:
        """``L``: True while a GOTO slew is running."""
        return self._exchange(b"L") == b"1"

    def cancel_goto(self) -> None:
        """``M``: cancel a running GOTO."""
        self._exchange(b"M")

    # -- tracking ----------------------------------------------------------
    def get_tracking(self) -> int:
        """``t``: tracking mode 0=off, 1=alt-az, 2=eq-north, 3=eq-south."""
        return self._exchange(b"t", 1)[0]

    def set_tracking(self, mode: int) -> None:
        """``T``: set tracking mode (see :data:`TRACKING_MODES`)."""
        if mode not in TRACKING_MODES:
            raise ValueError(f"unknown tracking mode {mode}")
        self._exchange(bytes([0x54, mode]))

    # -- time / location ---------------------------------------------------
    def get_time(self) -> dict:
        """``h``: HC clock as a dict."""
        return decode_time(self._exchange(b"h", 8))

    def set_time(
        self,
        year: int,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
        utc_offset_hours: float,
        dst: bool,
    ) -> None:
        """``H``: set the HC clock."""
        self._exchange(
            b"H"
            + encode_time(
                year, month, day, hour, minute, second, utc_offset_hours, dst
            )
        )

    def get_location(self) -> tuple[float, float]:
        """``w``: site (latitude, longitude) degrees."""
        return decode_location(self._exchange(b"w", 8))

    def set_location(self, lat_deg: float, lon_deg: float) -> None:
        """``W``: set the observing site."""
        self._exchange(b"W" + encode_location(lat_deg, lon_deg))

    # -- AUX pass-through --------------------------------------------------
    def passthrough(
        self, dest: int, cmd: int, data: bytes = b"", resp_len: int = 0
    ) -> bytes:
        """``P``: relay one AUX-bus command through the HC.

        *data* holds at most 3 bytes. Returns exactly *resp_len* bytes.
        Raises :class:`PassthroughError` when the byte after the expected
        response is not ``#`` -- the documented garbage-byte signal for an
        absent device or unknown command.
        """
        data = bytes(data)
        if not 1 <= 1 + len(data) <= 4:
            raise ValueError("AUX payload must be 1-4 bytes (cmd + data)")
        payload = (
            bytes([0x50, 1 + len(data), dest & 0xFF, cmd & 0xFF])
            + data
            + b"\x00" * (3 - len(data))
            + bytes([resp_len & 0xFF])
        )
        with self._lock:
            self._t.write(payload)
            body = self._t.read_exact(resp_len, self._timeout)
            term = self._t.read_exact(1, self._timeout)
            if term == b"#":
                return body
            # Garbage byte: consume through the terminator, then report.
            self._t.read_until(b"#", self._timeout)
            raise PassthroughError(
                f"AUX cmd 0x{cmd:02X} to device {dest}: "
                f"device absent or command unknown (garbage {term!r} before '#')"
            )

    def jog(self, axis: str, direction: int, rate: int) -> None:
        """Fixed-rate slew: *axis* ``"az"``/``"alt"``, *direction* +/-1,
        *rate* 1-9 (``MC_MOVE_POS``/``MC_MOVE_NEG``)."""
        if axis not in ("az", "alt"):
            raise ValueError("axis must be 'az' or 'alt'")
        if direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if not 0 <= rate <= 9:
            raise ValueError("rate must be 0-9 (0 stops)")
        dev = DEV_AZM if axis == "az" else DEV_ALT
        cmd = 0x24 if direction > 0 else 0x25
        self.passthrough(dev, cmd, bytes([rate]), 0)

    def stop_axis(self, axis: str) -> None:
        """Stop motion on one axis (rate 0)."""
        self.jog(axis, 1, 0)

    def stop_all(self) -> None:
        """Stop both axes."""
        self.stop_axis("az")
        self.stop_axis("alt")

    # -- Utilities-menu equivalents (anti-backlash, cordwrap) --------------

    def set_backlash(self, axis: str, direction: int, value: int) -> None:
        """Anti-backlash: *axis* ``"az"``/``"alt"``, *direction* +/-1,
        *value* 0-99 (``MC_SET_POS/NEG_BACKLASH``). Mirrors the HC
        Utilities -> Anti-backlash menu."""
        if axis not in ("az", "alt"):
            raise ValueError("axis must be 'az' or 'alt'")
        if direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if not 0 <= value <= 99:
            raise ValueError("backlash value must be 0-99")
        dev = DEV_AZM if axis == "az" else DEV_ALT
        cmd = 0x10 if direction > 0 else 0x11
        self.passthrough(dev, cmd, bytes([value]), 0)

    def get_backlash(self, axis: str, direction: int) -> int:
        """Read an anti-backlash value (0-99) for one axis/direction."""
        if axis not in ("az", "alt"):
            raise ValueError("axis must be 'az' or 'alt'")
        if direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        dev = DEV_AZM if axis == "az" else DEV_ALT
        cmd = 0x40 if direction > 0 else 0x41
        return self.passthrough(dev, cmd, b"", 1)[0]

    def set_cordwrap(self, enabled: bool) -> None:
        """Enable/disable cord wrap (``MC_ENABLE/DISABLE_CORDWRAP``)."""
        self.passthrough(DEV_AZM, 0x38 if enabled else 0x39, b"", 0)

    def cordwrap_enabled(self) -> bool:
        """Poll cordwrap state (``MC_POLL_CORDWRAP``)."""
        return self.passthrough(DEV_AZM, 0x3B, b"", 1)[0] != 0

    def variable_rate(self, axis: str, rate_arcsec_s: float) -> None:
        """Variable-rate slew on *axis* at *rate_arcsec_s* (signed).

        Rate 0 stops the axis. Uses ``MC_SET_POS/NEG_GUIDERATE``.
        """
        dev = DEV_AZM if axis == "az" else DEV_ALT
        raw = int(round(abs(rate_arcsec_s) * 4.0))
        cmd = 0x06 if rate_arcsec_s >= 0 else 0x07
        self.passthrough(dev, cmd, bytes([(raw >> 8) & 0xFF, raw & 0xFF]), 0)

    def mc_version(self, axis: str) -> tuple[int, int]:
        """``MC_GET_VER`` for a motor controller."""
        dev = DEV_AZM if axis == "az" else DEV_ALT
        resp = self.passthrough(dev, 0xFE, b"", 2)
        return resp[0], resp[1]

    def slew_done(self, axis: str) -> bool:
        """``MC_SLEW_DONE``: per-axis slew status (finer than ``L``)."""
        dev = DEV_AZM if axis == "az" else DEV_ALT
        return self.passthrough(dev, 0x13, b"", 1) == b"\xff"

    def bus_scan(self, devices: tuple[int, ...] = (16, 17, 176, 178)) -> dict:
        """Probe AUX bus devices with ``MC_GET_VER``; returns {dev: (maj, min)}."""
        found: dict[int, tuple[int, int]] = {}
        for dev in devices:
            try:
                resp = self.passthrough(dev, 0xFE, b"", 2)
            except (PassthroughError, NexStarTimeout):
                continue
            found[dev] = (resp[0], resp[1])
        return found

    def gps_linked(self) -> bool:
        """True when a GPS module (device 176) reports a fix."""
        try:
            return self.passthrough(176, 0x37, b"", 1) == b"\x01"
        except (PassthroughError, NexStarTimeout):
            return False
