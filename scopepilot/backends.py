"""Mount backends: serial (direct NexStar protocol), sim, and INDI.

Every backend implements :class:`Backend` -- small primitives the
:class:`~scopepilot.controller.TelescopeController` orchestrates into
goto-with-wait, park, probe, etc.

Backend choice and the "works alongside AstroCapture" story:

* ``serial`` -- ScopePilot owns the hand-controller serial port and speaks
  the NexStar protocol directly. Use when AstroCapture is idle.
* ``indi``   -- ScopePilot attaches as a *second INDI client* to the same
  ``indiserver`` AstroCapture uses (``indi_celestron_gps`` keeps the serial
  port). Both programs can drive the mount without fighting over the port.
* ``sim``    -- wire-level fake 6SE; exercises the real protocol driver.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from scopepilot import nexstar
from scopepilot.nexstar import NexStarDriver, NexStarError, SerialTransport

try:  # optional: only needed for --backend indi
    from astrocapture.drivers.indi import INDIMount
except ImportError:  # pragma: no cover - optional dependency
    INDIMount = None  # type: ignore[assignment]


@dataclass
class MountStatus:
    connected: bool = False
    backend: str = ""
    model: str = ""
    aligned: bool = False
    tracking_mode: str = "unknown"  # off | alt-az | eq-north | eq-south
    ra_hours: float | None = None
    dec_deg: float | None = None
    az_deg: float | None = None
    alt_deg: float | None = None
    slewing: bool = False
    parked: bool = False
    note: str = ""


class Backend(abc.ABC):
    """Primitive mount operations. Orchestration lives in the controller."""

    name: str = "backend"

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def status(self) -> MountStatus: ...

    @abc.abstractmethod
    def goto_radec(self, ra_hours: float, dec_deg: float) -> None: ...

    @abc.abstractmethod
    def goto_altaz(self, az_deg: float, alt_deg: float) -> None: ...

    @abc.abstractmethod
    def sync_radec(self, ra_hours: float, dec_deg: float) -> None: ...

    @abc.abstractmethod
    def abort(self) -> None: ...

    @abc.abstractmethod
    def set_tracking(self, mode: int) -> None: ...

    @abc.abstractmethod
    def jog_start(self, axis: str, direction: int, rate: int) -> None: ...

    @abc.abstractmethod
    def jog_stop(self, axis: str | None = None) -> None: ...

    @abc.abstractmethod
    def set_hc_time(
        self, year: int, month: int, day: int, hour: int, minute: int, second: int,
        utc_offset_hours: float, dst: bool,
    ) -> None: ...

    @abc.abstractmethod
    def set_hc_location(self, lat_deg: float, lon_deg: float) -> None: ...

    @abc.abstractmethod
    def wait_goto(self, timeout: float) -> bool:
        """Block until the slew finishes; True == settled in time."""

    def probe_details(self) -> dict:
        """Extra backend-specific diagnostics for ``scopepilot probe``."""
        return {}

    # -- optional Utilities-menu features ------------------------------------
    def set_backlash(self, axis: str, direction: int, value: int) -> None:
        """Anti-backlash 0-99 for one axis/direction (optional)."""
        raise NotImplementedError(f"{self.name} backend has no backlash control")

    def get_backlash(self, axis: str, direction: int) -> int:
        raise NotImplementedError(f"{self.name} backend has no backlash control")

    def set_cordwrap(self, enabled: bool) -> None:
        """Enable/disable cord wrap (optional)."""
        raise NotImplementedError(f"{self.name} backend has no cordwrap control")

    def cordwrap_enabled(self) -> bool:
        raise NotImplementedError(f"{self.name} backend has no cordwrap control")


# ---------------------------------------------------------------------------
# Serial: direct NexStar protocol
# ---------------------------------------------------------------------------


class SerialBackend(Backend):
    """Speak the NexStar protocol straight to the hand controller."""

    name = "serial"

    def __init__(self, port: str, baud: int = nexstar.PROTOCOL_BAUD) -> None:
        if not port:
            raise NexStarError(
                "serial backend needs a port, e.g. --port /dev/ttyUSB0 "
                "(post-2016 NexStar+ hand controllers expose mini-USB, "
                "seen as /dev/ttyUSB0 on Linux)"
            )
        self.port = port
        self.baud = baud
        self._driver: NexStarDriver | None = None

    def connect(self) -> None:
        self._driver = NexStarDriver(SerialTransport(self.port, self.baud))
        if not self._driver.echo():
            raise NexStarError(f"no echo from hand controller on {self.port}")

    def disconnect(self) -> None:
        if self._driver is not None:
            try:
                self._driver.stop_all()
            finally:
                self._driver.close()
                self._driver = None

    def _d(self) -> NexStarDriver:
        if self._driver is None:
            raise NexStarError("not connected")
        return self._driver

    def status(self) -> MountStatus:
        d = self._d()
        try:
            ra, dec = d.get_radec()
            az, alt = d.get_altaz()
            mode = d.get_tracking()
            st = MountStatus(
                connected=True,
                backend="serial",
                model=d.model_name(),
                aligned=d.alignment_complete(),
                tracking_mode=nexstar.TRACKING_MODES.get(mode, f"mode-{mode}"),
                ra_hours=ra,
                dec_deg=dec,
                az_deg=az,
                alt_deg=alt,
                slewing=d.goto_in_progress(),
            )
        except NexStarError as exc:
            st = MountStatus(connected=False, backend="serial", note=str(exc))
        return st

    def goto_radec(self, ra_hours: float, dec_deg: float) -> None:
        self._d().goto_radec(ra_hours, dec_deg)

    def goto_altaz(self, az_deg: float, alt_deg: float) -> None:
        self._d().goto_altaz(az_deg, alt_deg)

    def sync_radec(self, ra_hours: float, dec_deg: float) -> None:
        self._d().sync_radec(ra_hours, dec_deg)

    def abort(self) -> None:
        d = self._d()
        d.cancel_goto()
        d.stop_all()

    def set_tracking(self, mode: int) -> None:
        self._d().set_tracking(mode)

    def jog_start(self, axis: str, direction: int, rate: int) -> None:
        self._d().jog(axis, direction, rate)

    def jog_stop(self, axis: str | None = None) -> None:
        d = self._d()
        if axis is None:
            d.stop_all()
        else:
            d.stop_axis(axis)

    def set_backlash(self, axis: str, direction: int, value: int) -> None:
        self._d().set_backlash(axis, direction, value)

    def get_backlash(self, axis: str, direction: int) -> int:
        return self._d().get_backlash(axis, direction)

    def set_cordwrap(self, enabled: bool) -> None:
        self._d().set_cordwrap(enabled)

    def cordwrap_enabled(self) -> bool:
        return self._d().cordwrap_enabled()

    def set_hc_time(self, year, month, day, hour, minute, second,
                    utc_offset_hours, dst) -> None:
        self._d().set_time(
            year, month, day, hour, minute, second, utc_offset_hours, dst)

    def set_hc_location(self, lat_deg: float, lon_deg: float) -> None:
        self._d().set_location(lat_deg, lon_deg)

    def wait_goto(self, timeout: float) -> bool:
        d = self._d()
        deadline = time.monotonic() + timeout
        poll = 0.5
        while time.monotonic() < deadline:
            if not d.goto_in_progress():
                # Double-check via the per-axis AUX status (finer grained).
                try:
                    if d.slew_done("az") and d.slew_done("alt"):
                        return True
                except NexStarError:
                    return True
            time.sleep(poll)
            poll = min(poll * 1.5, 5.0)
        return False

    def probe_details(self) -> dict:
        d = self._d()
        version = d.get_version()
        details = {
            "port": self.port,
            "baud": self.baud,
            "hc_version": f"{version[0]}.{version[1]}",
            "model_id": d.get_model(),
            "model": d.model_name(),
            "bus": {
                dev: f"{v[0]}.{v[1]}"
                for dev, v in d.bus_scan().items()
            },
            "gps_linked": d.gps_linked(),
        }
        try:
            details["hc_time"] = d.get_time()
        except NexStarError:
            pass
        try:
            lat, lon = d.get_location()
            details["hc_location"] = {"lat": lat, "lon": lon}
        except NexStarError:
            pass
        return details


# ---------------------------------------------------------------------------
# Sim: wire-level fake 6SE (exercises the real protocol driver)
# ---------------------------------------------------------------------------


class SimBackend(Backend):
    """Simulated 6SE: real :class:`NexStarDriver`, fake hand controller."""

    name = "sim"

    def __init__(self, slew_rate_dps: float = 360.0, aligned: bool = True) -> None:
        self._slew_rate = slew_rate_dps
        self._aligned = aligned
        self._driver: NexStarDriver | None = None
        self._sim = None  # SimNexStar, imported lazily (thread)

    def connect(self) -> None:
        from scopepilot.sim import make_sim_driver

        self._driver, self._sim = make_sim_driver(
            slew_rate_dps=self._slew_rate, aligned=self._aligned
        )

    def disconnect(self) -> None:
        if self._sim is not None:
            self._sim.stop()
            self._sim = None
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def _d(self) -> NexStarDriver:
        if self._driver is None:
            raise NexStarError("not connected")
        return self._driver

    # The sim speaks the identical protocol, so delegate everything to the
    # same implementation the serial backend uses.
    def _serial_view(self) -> SerialBackend:
        view = SerialBackend.__new__(SerialBackend)
        view.port = "sim"
        view.baud = 9600
        view._driver = self._driver
        return view

    def status(self) -> MountStatus:
        st = self._serial_view().status()
        st.backend = "sim"
        return st

    def goto_radec(self, ra_hours: float, dec_deg: float) -> None:
        self._serial_view().goto_radec(ra_hours, dec_deg)

    def goto_altaz(self, az_deg: float, alt_deg: float) -> None:
        self._serial_view().goto_altaz(az_deg, alt_deg)

    def sync_radec(self, ra_hours: float, dec_deg: float) -> None:
        self._serial_view().sync_radec(ra_hours, dec_deg)

    def abort(self) -> None:
        self._serial_view().abort()

    def set_tracking(self, mode: int) -> None:
        self._serial_view().set_tracking(mode)

    def jog_start(self, axis: str, direction: int, rate: int) -> None:
        self._serial_view().jog_start(axis, direction, rate)

    def jog_stop(self, axis: str | None = None) -> None:
        self._serial_view().jog_stop(axis)

    def set_hc_time(self, year, month, day, hour, minute, second,
                    utc_offset_hours, dst) -> None:
        self._serial_view().set_hc_time(
            year, month, day, hour, minute, second, utc_offset_hours, dst)

    def set_hc_location(self, lat_deg: float, lon_deg: float) -> None:
        self._serial_view().set_hc_location(lat_deg, lon_deg)

    def set_backlash(self, axis: str, direction: int, value: int) -> None:
        self._serial_view().set_backlash(axis, direction, value)

    def get_backlash(self, axis: str, direction: int) -> int:
        return self._serial_view().get_backlash(axis, direction)

    def set_cordwrap(self, enabled: bool) -> None:
        self._serial_view().set_cordwrap(enabled)

    def cordwrap_enabled(self) -> bool:
        return self._serial_view().cordwrap_enabled()

    def wait_goto(self, timeout: float) -> bool:
        return self._serial_view().wait_goto(timeout)

    def probe_details(self) -> dict:
        details = self._serial_view().probe_details()
        details["simulated"] = True
        return details


# ---------------------------------------------------------------------------
# INDI: second client on AstroCapture's indiserver
# ---------------------------------------------------------------------------


class INDIBackend(Backend):
    """Drive the mount through INDI as a second client.

    ``indi_celestron_gps`` keeps the serial port; AstroCapture (imaging)
    and ScopePilot (mount console) coexist as two INDI clients on the same
    ``indiserver``. Needs the ``astrocapture`` package installed.
    """

    name = "indi"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7624,
        device: str = "Celestron GPS",
    ) -> None:
        if INDIMount is None:
            raise NexStarError(
                "the indi backend needs the astrocapture package "
                "(pip install -e ../astro-capture)"
            )
        self._mount = INDIMount(host=host, port=port, device=device)
        self._tracking = True

    def connect(self) -> None:
        self._mount.connect()
        self._mount.unpark()

    def disconnect(self) -> None:
        self._mount.disconnect()

    def status(self) -> MountStatus:
        try:
            ra, dec = self._mount.position
            state = self._mount.state
            return MountStatus(
                connected=True,
                backend="indi",
                model=f"INDI ({self._mount.device_name})",
                aligned=True,  # INDI exposes no alignment flag; driver owns it
                tracking_mode="alt-az" if self._tracking else "off",
                ra_hours=ra,
                dec_deg=dec,
                slewing=state.value == "slewing",
                parked=state.value == "parked",
                note="az/alt unavailable over INDI; alignment flag not exposed",
            )
        except Exception as exc:  # INDIError etc.
            return MountStatus(connected=False, backend="indi", note=str(exc))

    def goto_radec(self, ra_hours: float, dec_deg: float) -> None:
        self._mount.goto(ra_hours, dec_deg)

    def goto_altaz(self, az_deg: float, alt_deg: float) -> None:
        raise NotImplementedError(
            "alt-az goto is not exposed over INDI; use the serial backend"
        )

    def sync_radec(self, ra_hours: float, dec_deg: float) -> None:
        raise NotImplementedError(
            "sync is not wrapped for the INDI backend; use the serial backend"
        )

    def abort(self) -> None:
        client = self._mount._client
        dev = self._mount.device_name
        if client.has_property(dev, "TELESCOPE_ABORT_MOTION"):
            items = client.items_of(dev, "TELESCOPE_ABORT_MOTION")
            client.send_switch(
                dev, "TELESCOPE_ABORT_MOTION",
                {n: ("On" if n == "ABORT" else "Off") for n in items},
            )
        else:
            raise NotImplementedError(
                "this INDI driver exposes no TELESCOPE_ABORT_MOTION"
            )

    def set_tracking(self, mode: int) -> None:
        if mode == 0:
            self._mount.stop_tracking()
            self._tracking = False
        else:
            self._mount.start_tracking()
            self._tracking = True

    def jog_start(self, axis: str, direction: int, rate: int) -> None:
        raise NotImplementedError(
            "jog is only available on the serial backend (AUX pass-through)"
        )

    def jog_stop(self, axis: str | None = None) -> None:
        raise NotImplementedError(
            "jog is only available on the serial backend (AUX pass-through)"
        )

    def set_hc_time(self, year, month, day, hour, minute, second,
                    utc_offset_hours, dst) -> None:
        raise NotImplementedError("HC clock is only settable over serial")

    def set_hc_location(self, lat_deg: float, lon_deg: float) -> None:
        raise NotImplementedError("HC site is only settable over serial")

    def wait_goto(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._mount.slew_complete():
                return True
            time.sleep(0.5)
        return False


_BACKENDS = {"serial": SerialBackend, "sim": SimBackend, "indi": INDIBackend}


def make_backend(kind: str, **kwargs) -> Backend:
    try:
        cls = _BACKENDS[kind]
    except KeyError:
        raise NexStarError(
            f"unknown backend {kind!r}; choose from {sorted(_BACKENDS)}"
        ) from None
    return cls(**kwargs)


def available_backends() -> list[str]:
    return sorted(_BACKENDS)
