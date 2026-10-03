"""High-level telescope control: orchestration over a :class:`Backend`.

The controller owns the *workflow* (alignment guards, goto-and-wait,
park-as-a-sequence, clock/site setup); backends own the wire.
"""

from __future__ import annotations

import time

from scopepilot import nexstar
from scopepilot.backends import Backend, MountStatus
from scopepilot.bridge import resolve_target
from scopepilot.nexstar import NexStarError


class AlignmentError(NexStarError):
    """The mount is not aligned -- align it from the hand controller first."""


class NotConnectedError(NexStarError):
    pass


class TelescopeController:
    """Operator-facing telescope control.

    Use as a context manager to guarantee disconnect::

        with TelescopeController(backend) as scope:
            scope.goto_target("M51")
    """

    def __init__(
        self,
        backend: Backend,
        *,
        site_lat: float | None = None,
        site_lon: float | None = None,
        home_az: float = 0.0,
        home_alt: float = 5.0,
    ) -> None:
        self.backend = backend
        self.site_lat = site_lat
        self.site_lon = site_lon
        self.home_az = home_az
        self.home_alt = home_alt
        self.parked = False
        self._connected = False

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> "TelescopeController":
        self.backend.connect()
        self._connected = True
        return self

    def disconnect(self) -> None:
        try:
            self.backend.disconnect()
        finally:
            self._connected = False

    def __enter__(self) -> "TelescopeController":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnectedError("not connected -- call connect() first")

    # -- status / diagnostics ---------------------------------------------
    def status(self) -> MountStatus:
        self._require_connected()
        st = self.backend.status()
        st.parked = self.parked or st.parked
        return st

    def require_aligned(self) -> None:
        """Raise :class:`AlignmentError` unless the mount is aligned."""
        if not self.status().aligned:
            raise AlignmentError(
                "mount reports not-aligned: run a SkyAlign / Auto Two-Star "
                "alignment from the hand controller first (ScopePilot cannot "
                "align the mount for you)"
            )

    def probe(self) -> dict:
        """Connection shakedown: echo/version/model/alignment/tracking/site."""
        self._require_connected()
        st = self.status()
        report = {
            "backend": self.backend.name,
            "connected": st.connected,
            "model": st.model,
            "aligned": st.aligned,
            "tracking_mode": st.tracking_mode,
            "slewing": st.slewing,
            "position": {
                "ra_hours": st.ra_hours,
                "dec_deg": st.dec_deg,
                "az_deg": st.az_deg,
                "alt_deg": st.alt_deg,
            },
        }
        report.update(self.backend.probe_details())
        return report

    # -- slewing -----------------------------------------------------------
    def goto_radec(
        self,
        ra_hours: float,
        dec_deg: float,
        wait: bool = True,
        timeout: float = 300.0,
        settle: float = 2.0,
    ) -> bool:
        """Slew to RA/Dec. Returns True when settled within *timeout*."""
        self._require_connected()
        self.require_aligned()
        self.parked = False
        self.backend.goto_radec(ra_hours, dec_deg)
        if not wait:
            return False
        ok = self.backend.wait_goto(timeout)
        if ok and settle > 0:
            time.sleep(settle)
        return ok

    def goto_altaz(
        self,
        az_deg: float,
        alt_deg: float,
        wait: bool = True,
        timeout: float = 300.0,
        settle: float = 2.0,
    ) -> bool:
        self._require_connected()
        self.parked = False
        self.backend.goto_altaz(az_deg, alt_deg)
        if not wait:
            return False
        ok = self.backend.wait_goto(timeout)
        if ok and settle > 0:
            time.sleep(settle)
        return ok

    def goto_target(
        self, name: str, wait: bool = True, timeout: float = 300.0
    ) -> tuple[float, float, str, bool]:
        """Resolve *name* via the catalog bridge, then slew to it.

        Returns (ra_hours, dec_deg, source, settled).
        """
        ra, dec, source = resolve_target(name)
        settled = self.goto_radec(ra, dec, wait=wait, timeout=timeout)
        return ra, dec, source, settled

    def sync_here(self, ra_hours: float, dec_deg: float) -> None:
        """Sync the mount on the object currently centered in the eyepiece."""
        self._require_connected()
        self.require_aligned()
        self.backend.sync_radec(ra_hours, dec_deg)

    def sync_target(self, name: str) -> tuple[float, float, str]:
        ra, dec, source = resolve_target(name)
        self.sync_here(ra, dec)
        return ra, dec, source

    def abort(self) -> None:
        """Cancel any GOTO and stop all axis motion."""
        self._require_connected()
        self.backend.abort()

    # -- tracking ------------------------------------------------------------
    def set_tracking(self, mode: str | int) -> str:
        """Set tracking; *mode* like ``"off"``, ``"alt-az"``, ``2``..."""
        self._require_connected()
        if isinstance(mode, str):
            key = mode.lower().replace("_", "-")
            if key not in nexstar.TRACKING_MODE_IDS:
                raise ValueError(
                    f"unknown tracking mode {mode!r}; "
                    f"choose from {sorted(nexstar.TRACKING_MODE_IDS)}"
                )
            mode_id = nexstar.TRACKING_MODE_IDS[key]
        else:
            mode_id = int(mode)
        self.backend.set_tracking(mode_id)
        return nexstar.TRACKING_MODES[mode_id]

    # -- manual jog ------------------------------------------------------------
    def jog(
        self,
        direction: str,
        rate: int = 5,
        seconds: float | None = None,
    ) -> None:
        """Nudge the mount: direction in up/down/left/right.

        With *seconds* set, the axis moves for that long then stops;
        otherwise motion continues until :meth:`stop`.
        """
        self._require_connected()
        mapping = {"up": ("alt", 1), "down": ("alt", -1),
                   "left": ("az", -1), "right": ("az", 1)}
        try:
            axis, sign = mapping[direction.lower()]
        except KeyError:
            raise ValueError(
                f"unknown jog direction {direction!r}; "
                "choose up/down/left/right"
            ) from None
        if not 1 <= rate <= 9:
            raise ValueError("jog rate must be 1-9")
        self.parked = False
        self.backend.jog_start(axis, sign, rate)
        if seconds is not None:
            time.sleep(seconds)
            self.backend.jog_stop(axis)

    def stop(self) -> None:
        """Stop jog motion on all axes (does not cancel tracking)."""
        self._require_connected()
        self.backend.jog_stop()

    # -- park ------------------------------------------------------------------
    def park(self, wait: bool = True, timeout: float = 300.0) -> bool:
        """Park: abort, slew to the home position, tracking off.

        NexStar mounts have no park command or home sensor, so parking is a
        controlled slew to the configured home (default az 0 / alt 5) plus
        tracking off. Slew the OTA to a safe stow pose with the HC
        afterwards if you like.
        """
        self._require_connected()
        self.abort()
        ok = self.goto_altaz(self.home_az, self.home_alt,
                             wait=wait, timeout=timeout)
        self.set_tracking("off")
        self.parked = True
        return ok

    def unpark(self) -> None:
        """Clear the parked flag and resume alt-az tracking."""
        self._require_connected()
        self.parked = False
        self.set_tracking("alt-az")

    # -- clock / site ------------------------------------------------------------
    def sync_clock(self) -> dict:
        """Set the HC clock from the computer's local time."""
        self._require_connected()
        now = time.localtime()
        dst = bool(now.tm_isdst and now.tm_isdst > 0)
        utc_offset = -(time.altzone if dst else time.timezone) / 3600.0
        self.backend.set_hc_time(
            now.tm_year, now.tm_mon, now.tm_mday,
            now.tm_hour, now.tm_min, now.tm_sec,
            utc_offset, dst,
        )
        return {
            "year": now.tm_year, "month": now.tm_mon, "day": now.tm_mday,
            "hour": now.tm_hour, "minute": now.tm_min, "second": now.tm_sec,
            "utc_offset_hours": utc_offset, "dst": dst,
        }

    def set_site(self, lat_deg: float, lon_deg: float) -> None:
        """Set the HC observing site (also stored on the controller)."""
        self._require_connected()
        self.backend.set_hc_location(lat_deg, lon_deg)
        self.site_lat, self.site_lon = lat_deg, lon_deg
