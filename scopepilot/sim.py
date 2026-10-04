"""Wire-level simulator: a fake NexStar hand controller.

:class:`SimNexStar` runs in a background thread and answers the exact byte
protocol implemented by :class:`scopepilot.nexstar.NexStarDriver` over a
:class:`~scopepilot.nexstar.LoopbackPipe`.  The sim backend therefore
exercises the *real* driver code path -- command framing, codecs,
timeouts -- with no telescope attached.

The simulated mount behaves like a 6SE:

* GOTOs slew both axes toward the target at a configurable rate and report
  ``L`` == 1 while moving (``MC_SLEW_DONE`` per axis too).
* Fixed-rate jog (``MC_MOVE_POS/NEG``) and variable guide rates move the
  axes until stopped; any jog cancels a running GOTO (like the HC arrows).
* With tracking off, the sky drifts: RA advances at the sidereal rate.
* ``J`` reports the configured alignment state; GOTOs are still accepted
  (the *controller* layer enforces the alignment guard, like a real
  workflow where the HC refuses to mean anything before alignment).
"""

from __future__ import annotations

import queue
import threading
import time

from scopepilot.nexstar import (
    JOG_RATES_DPS,
    SIDEREAL_DPS,
    LoopbackPipe,
    LoopbackTransport,
    NexStarDriver,
    alt_deg_to_frac,
    az_deg_to_frac,
    dec_deg_to_frac,
    decode_altaz_16,
    decode_altaz_24,
    decode_location,
    decode_radec_16,
    decode_radec_24,
    decode_time,
    encode_altaz_16,
    encode_altaz_24,
    encode_location,
    encode_radec_16,
    encode_radec_24,
    encode_time,
    ra_hours_to_frac,
)

#: First-byte -> total command length for the ASCII command set.
_CMD_LEN = {
    0x45: 1,  # E  get RA/Dec
    0x65: 1,  # e  get precise RA/Dec
    0x5A: 1,  # Z  get Az/Alt
    0x7A: 1,  # z  get precise Az/Alt
    0x74: 1,  # t  get tracking mode
    0x56: 1,  # V  get version
    0x6D: 1,  # m  get model
    0x4A: 1,  # J  alignment complete?
    0x4C: 1,  # L  goto in progress?
    0x4D: 1,  # M  cancel goto
    0x68: 1,  # h  get time
    0x77: 1,  # w  get location
    0x54: 2,  # T  set tracking mode
    0x4B: 2,  # K  echo
    0x52: 10,  # R  goto RA/Dec
    0x42: 10,  # B  goto Az/Alt
    0x53: 10,  # S  sync RA/Dec
    0x72: 18,  # r  goto precise RA/Dec
    0x62: 18,  # b  goto precise Az/Alt
    0x73: 18,  # s  sync precise RA/Dec
    0x48: 9,  # H  set time
    0x57: 9,  # W  set location
    0x50: 8,  # P  AUX pass-through
}


def _shortest_step(current: float, target: float) -> float:
    """Signed shortest angular distance target-current in degrees."""
    return (target - current + 540.0) % 360.0 - 180.0


class SimNexStar(threading.Thread):
    """Fake 6SE hand controller.

    Parameters mirror a plausible scope: model 12 (6/8 SE), aligned by
    default, alt-az tracking on, parked at az 0 / alt 5.
    """

    def __init__(
        self,
        pipe: LoopbackPipe,
        *,
        slew_rate_dps: float = 360.0,
        aligned: bool = True,
        model: int = 12,
        version: tuple[int, int] = (5, 35),
        ra_hours: float = 5.5,
        dec_deg: float = 20.0,
        az_deg: float = 180.0,
        alt_deg: float = 45.0,
        lat_deg: float = 43.3767,
        lon_deg: float = -80.9809,
        tick: float = 0.02,
    ) -> None:
        super().__init__(daemon=True, name="sim-nexstar")
        self._pipe = pipe
        self._slew_rate = slew_rate_dps
        self._tick = tick
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._buf = bytearray()

        self.model = model
        self.version = version
        self.aligned = aligned
        self.tracking = 1  # alt-az, like a powered-on 6SE
        self.ra_hours = ra_hours
        self.dec_deg = dec_deg
        self.az_deg = az_deg
        self.alt_deg = alt_deg
        self.goto_active = False
        self._goto_kind: str | None = None  # "radec" | "altaz"
        self._goto_target = (0.0, 0.0)
        self._jog = {"az": 0.0, "alt": 0.0}  # signed deg/s
        self._backlash = {"az": [0, 0], "alt": [0, 0]}  # [pos, neg] 0-99
        self._cordwrap = False
        # Start with the computer's local time, like a real HC would show.
        _now = time.localtime()
        _off = -((time.altzone if _now.tm_isdst else time.timezone) / 3600.0)
        self._time = encode_time(
            _now.tm_year, _now.tm_mon, _now.tm_mday,
            _now.tm_hour, _now.tm_min, _now.tm_sec,
            _off, bool(_now.tm_isdst))
        self._location = encode_location(lat_deg, lon_deg)
        self._last = time.monotonic()

    # -- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)

    # -- main loop --------------------------------------------------------
    def run(self) -> None:  # noqa: C901 - protocol dispatch is long by nature
        while not self._stop_event.is_set():
            self._drain()
            cmd = self._take_command()
            if cmd is not None:
                try:
                    resp = self._dispatch(cmd)
                except Exception:  # never kill the sim thread on bad input
                    resp = b""
                self._pipe.hc_to_client.put(resp + b"#")
            self._motion()
            time.sleep(0.001)

    def _drain(self) -> None:
        try:
            while True:
                self._buf += self._pipe.client_to_hc.get_nowait()
        except queue.Empty:
            pass

    def _take_command(self) -> bytes | None:
        if not self._buf:
            return None
        want = _CMD_LEN.get(self._buf[0])
        if want is None:  # unknown byte: consume it, ignore
            del self._buf[0]
            return None
        if len(self._buf) < want:
            return None
        cmd = bytes(self._buf[:want])
        del self._buf[:want]
        return cmd

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, cmd: bytes) -> bytes:  # noqa: C901
        c = cmd[0]
        if c == 0x4B:  # K echo
            return cmd[1:2]
        if c == 0x56:  # V version
            return bytes([self.version[0], self.version[1]])
        if c == 0x6D:  # m model
            return bytes([self.model])
        if c == 0x4A:  # J alignment
            return bytes([1 if self.aligned else 0])
        if c == 0x4C:  # L goto in progress
            return b"1" if self.goto_active else b"0"
        if c == 0x4D:  # M cancel goto
            with self._lock:
                self.goto_active = False
                self._jog = {"az": 0.0, "alt": 0.0}
            return b""
        if c == 0x65:  # e precise RA/Dec
            return encode_radec_24(self.ra_hours, self.dec_deg)
        if c == 0x45:  # E RA/Dec
            return encode_radec_16(self.ra_hours, self.dec_deg)
        if c == 0x7A:  # z precise Az/Alt
            return encode_altaz_24(self.az_deg, self.alt_deg)
        if c == 0x5A:  # Z Az/Alt
            return encode_altaz_16(self.az_deg, self.alt_deg)
        if c == 0x72:  # r goto precise RA/Dec
            ra, dec = decode_radec_24(cmd[1:])
            self._start_goto("radec", ra, dec)
            return b""
        if c == 0x52:  # R goto RA/Dec
            ra, dec = decode_radec_16(cmd[1:])
            self._start_goto("radec", ra, dec)
            return b""
        if c == 0x62:  # b goto precise Az/Alt
            az, alt = decode_altaz_24(cmd[1:])
            self._start_goto("altaz", az, alt)
            return b""
        if c == 0x42:  # B goto Az/Alt
            az, alt = decode_altaz_16(cmd[1:])
            self._start_goto("altaz", az, alt)
            return b""
        if c == 0x73:  # s sync precise
            ra, dec = decode_radec_24(cmd[1:])
            with self._lock:
                self.ra_hours, self.dec_deg = ra, dec
            return b""
        if c == 0x53:  # S sync
            ra, dec = decode_radec_16(cmd[1:])
            with self._lock:
                self.ra_hours, self.dec_deg = ra, dec
            return b""
        if c == 0x74:  # t get tracking
            return bytes([self.tracking])
        if c == 0x54:  # T set tracking
            with self._lock:
                self.tracking = cmd[1]
            return b""
        if c == 0x68:  # h get time
            return self._time
        if c == 0x48:  # H set time
            with self._lock:
                self._time = bytes(cmd[1:])
            return b""
        if c == 0x77:  # w get location
            return self._location
        if c == 0x57:  # W set location
            with self._lock:
                self._location = bytes(cmd[1:])
            return b""
        if c == 0x50:  # P pass-through
            return self._passthrough(cmd)
        return b""

    def _passthrough(self, cmd: bytes) -> bytes:
        _p, _length, dest, aux, d1, d2, d3, resp_len = cmd
        # Error signal: resp_len filler bytes + one garbage byte before '#',
        # exactly what the protocol doc describes for an absent device or
        # unknown command (the client checks for '#' at the expected spot).
        garbage = b"\x00" * resp_len + b"\xff"
        if aux == 0xFE:  # GET_VER
            if dest in (16, 17):
                return bytes([1, 0])  # motor controller fw 1.0 (sim)
            return garbage
        if dest not in (16, 17):
            if dest == 176 and aux == 0x37:  # GPS linked?
                return b"\x00"  # no GPS on this sim
            return garbage
        axis = "az" if dest == 16 else "alt"
        if aux in (0x24, 0x25):  # MC_MOVE_POS / MC_MOVE_NEG
            rate = d1
            direction = 1 if aux == 0x24 else -1
            with self._lock:
                self.goto_active = False  # HC arrows cancel a goto
                self._jog[axis] = direction * JOG_RATES_DPS.get(rate, 0.0)
            return b""
        if aux in (0x06, 0x07):  # variable guide rate
            dps = ((d1 << 8) | d2) / 4.0 / 3600.0
            direction = 1 if aux == 0x06 else -1
            with self._lock:
                self.goto_active = False
                self._jog[axis] = direction * dps
            return b""
        if aux == 0x13:  # MC_SLEW_DONE
            slewing = self.goto_active or any(
                abs(v) > 1e-9 for v in self._jog.values()
            )
            return b"\x00" if slewing else b"\xff"
        if aux in (0x10, 0x11):  # MC_SET_POS/NEG_BACKLASH
            with self._lock:
                self._backlash[axis][0 if aux == 0x10 else 1] = min(99, d1)
            return b""
        if aux in (0x40, 0x41):  # MC_GET_POS/NEG_BACKLASH
            with self._lock:
                return bytes([self._backlash[axis][0 if aux == 0x40 else 1]])
        if aux == 0x38:  # MC_ENABLE_CORDWRAP
            with self._lock:
                self._cordwrap = True
            return b""
        if aux == 0x39:  # MC_DISABLE_CORDWRAP
            with self._lock:
                self._cordwrap = False
            return b""
        if aux == 0x3B:  # MC_POLL_CORDWRAP
            with self._lock:
                return b"\x01" if self._cordwrap else b"\x00"
        if aux == 0x01:  # MC_GET_POSITION -> 24-bit fraction
            frac = (
                az_deg_to_frac(self.az_deg)
                if axis == "az"
                else alt_deg_to_frac(self.alt_deg)
            )
            raw = (int(round(frac * 0xFFFFFF)) << 8) & 0xFFFFFFFF
            return bytes([(raw >> 24) & 0xFF, (raw >> 16) & 0xFF, (raw >> 8) & 0xFF])
        return garbage

    # -- motion ------------------------------------------------------------
    def _start_goto(self, kind: str, a: float, b: float) -> None:
        with self._lock:
            self._goto_kind = kind
            self._goto_target = (a, b)
            self.goto_active = True
            self._jog = {"az": 0.0, "alt": 0.0}

    def _motion(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if dt > 1.0:  # thread was stalled; don't teleport
            dt = self._tick
        with self._lock:
            # jog velocities
            if self._jog["az"]:
                self.az_deg = (self.az_deg + self._jog["az"] * dt) % 360.0
            if self._jog["alt"]:
                self.alt_deg = min(90.0, max(-90.0, self.alt_deg + self._jog["alt"] * dt))
            # goto slew
            if self.goto_active and self._goto_kind:
                ta, tb = self._goto_target
                if self._goto_kind == "radec":
                    step_a = _shortest_step(self.ra_hours * 15.0, ta * 15.0) / 15.0
                    step_b = _shortest_step(self.dec_deg, tb)
                    max_step = self._slew_rate * dt / 15.0
                    na = abs(step_a)
                    nb = abs(step_b * 15.0) / 15.0
                    move_a = min(na, max_step) * (1 if step_a >= 0 else -1)
                    move_b = min(abs(step_b), self._slew_rate * dt) * (
                        1 if step_b >= 0 else -1
                    )
                    self.ra_hours = (self.ra_hours + move_a) % 24.0
                    self.dec_deg = min(90.0, max(-90.0, self.dec_deg + move_b))
                    done = na < 0.0005 and abs(step_b) < 0.005
                else:
                    step_a = _shortest_step(self.az_deg, ta)
                    step_b = tb - self.alt_deg
                    max_step = self._slew_rate * dt
                    self.az_deg = (self.az_deg + min(abs(step_a), max_step)
                                   * (1 if step_a >= 0 else -1)) % 360.0
                    self.alt_deg += min(abs(step_b), max_step) * (
                        1 if step_b >= 0 else -1
                    )
                    done = abs(step_a) < 0.005 and abs(step_b) < 0.005
                if done:
                    self.goto_active = False
            # sidereal drift when tracking is off
            if self.tracking == 0 and not self.goto_active:
                self.ra_hours = (self.ra_hours + (SIDEREAL_DPS / 15.0) * dt) % 24.0

    # -- introspection (tests) ----------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ra_hours": self.ra_hours,
                "dec_deg": self.dec_deg,
                "az_deg": self.az_deg,
                "alt_deg": self.alt_deg,
                "tracking": self.tracking,
                "aligned": self.aligned,
                "goto_active": self.goto_active,
                "jog": dict(self._jog),
                "time": decode_time(self._time),
                "location": decode_location(self._location),
            }


def make_sim_driver(**kwargs) -> tuple[NexStarDriver, SimNexStar]:
    """Build a driver wired to a running :class:`SimNexStar`.

    Returns ``(driver, sim)``; the caller owns ``sim.stop()``.
    """
    pipe = LoopbackPipe()
    sim = SimNexStar(pipe, **kwargs)
    sim.start()
    return NexStarDriver(LoopbackTransport(pipe)), sim
