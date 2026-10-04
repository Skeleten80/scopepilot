"""High-level telescope control: orchestration over a :class:`Backend`.

The controller owns the *workflow* (alignment guards, goto-and-wait,
park-as-a-sequence, clock/site setup); backends own the wire.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scopepilot import nexstar
from scopepilot.astro import radec_to_altaz, utcnow
from scopepilot.backends import Backend, MountStatus
from scopepilot.bridge import TargetNotFound, resolve_target
from scopepilot.nexstar import NexStarError
from scopepilot.pointing import (
    ALIGN_STARS,
    PointingModel,
    SyncStar,
    default_pointing_path,
)


def default_pending_path() -> Path:
    return Path.home() / ".scopepilot" / "sync_stars.json"


def default_user_objects_path() -> Path:
    return Path.home() / ".scopepilot" / "user_objects.json"


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
        pending_path=None,
        user_objects_path=None,
    ) -> None:
        self.backend = backend
        self.site_lat = site_lat
        self.site_lon = site_lon
        self.home_az = home_az
        self.home_alt = home_alt
        self.parked = False
        self._connected = False
        # Software pointing model (Depth 1 HC replacement): when active,
        # goto_radec converts to alt-az through the model and needs no
        # hand-controller alignment at all.
        self.pointing: PointingModel | None = None
        self._pending_path = (Path(pending_path) if pending_path
                              else default_pending_path())
        self._sync_stars: list[SyncStar] = self._read_pending()
        # Undo GoTo: az/alt before the most recent goto.
        self._pre_goto: tuple[float, float] | None = None
        # User Objects (HC "User Objects" menu): named RA/Dec targets.
        self._user_objects_path = (Path(user_objects_path)
                                   if user_objects_path
                                   else default_user_objects_path())

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
        """Slew to RA/Dec. Returns True when settled within *timeout*.

        With a pointing model loaded, the target is converted to alt-az
        through the model and sent as GOTO AZM-ALT -- no hand-controller
        alignment required. Without a model it falls back to the HC's
        GOTO RA/DEC (which does require alignment).
        """
        self._require_connected()
        if self.pointing is not None:
            if self.site_lat is None or self.site_lon is None:
                raise NexStarError("pointing model needs site_lat/site_lon")
            az, alt = radec_to_altaz(
                ra_hours, dec_deg, self.site_lat, self.site_lon, utcnow())
            rep_az, rep_alt = self.pointing.to_reported(az, alt)
            return self.goto_altaz(rep_az, rep_alt, wait=wait,
                                   timeout=timeout, settle=settle)
        self.require_aligned()
        self.parked = False
        self._record_pre_goto()
        self.backend.goto_radec(ra_hours, dec_deg)
        if not wait:
            return False
        ok = self.backend.wait_goto(timeout)
        if ok and settle > 0:
            time.sleep(settle)
        return ok

    def _record_pre_goto(self) -> None:
        """Remember the current az/alt for Undo GoTo."""
        try:
            st = self.backend.status()
            self._pre_goto = (st.az_deg, st.alt_deg)
        except NexStarError:
            self._pre_goto = None

    def undo_goto(
        self, wait: bool = True, timeout: float = 300.0
    ) -> bool:
        """Slew back to the position before the last goto (HC Undo GoTo).

        Calling it twice returns to the goto target (a toggle).
        """
        self._require_connected()
        if self._pre_goto is None:
            raise NexStarError("no previous goto to undo")
        return self.goto_altaz(*self._pre_goto, wait=wait, timeout=timeout)

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
        self._record_pre_goto()
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

    # -- hand-controller info / utilities --------------------------------------
    def hc_info(self) -> dict:
        """Hand-controller info panel: model, firmware, clock, GPS, bus."""
        self._require_connected()
        st = self.status()
        details = self.backend.probe_details()
        return {
            "model": st.model or details.get("model"),
            "hc_version": details.get("hc_version"),
            "hc_time": details.get("hc_time"),
            "bus": details.get("bus"),
            "gps_linked": details.get("gps_linked"),
            "tracking": st.tracking_mode,
            "aligned": st.aligned,
        }

    def set_backlash(self, axis: str, direction: int, value: int) -> None:
        """Anti-backlash 0-99 for one axis/direction (HC Utilities menu)."""
        self._require_connected()
        self.backend.set_backlash(axis, direction, value)

    def get_backlash(self, axis: str, direction: int) -> int:
        self._require_connected()
        return self.backend.get_backlash(axis, direction)

    def set_cordwrap(self, enabled: bool) -> None:
        self._require_connected()
        self.backend.set_cordwrap(enabled)

    def cordwrap_enabled(self) -> bool:
        self._require_connected()
        return self.backend.cordwrap_enabled()

    # -- user objects (HC "User Objects" menu) ----------------------------------
    def _read_user_objects(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._user_objects_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        out: dict[str, dict] = {}
        for item in raw if isinstance(raw, list) else []:
            name = str(item.get("name", "")).strip()
            try:
                ra = float(item["ra_hours"]) % 24.0
                dec = float(item["dec_deg"])
            except (KeyError, TypeError, ValueError):
                continue
            if name and -90.0 <= dec <= 90.0:
                out[name.lower()] = {"name": name, "ra_hours": ra,
                                     "dec_deg": dec}
        return out

    def _write_user_objects(self, objs: dict[str, dict]) -> None:
        self._user_objects_path.parent.mkdir(parents=True, exist_ok=True)
        self._user_objects_path.write_text(
            json.dumps(sorted(objs.values(), key=lambda o: o["name"].lower()),
                       indent=2))

    def list_user_objects(self) -> list[dict]:
        """All saved user objects, sorted by name."""
        return sorted(self._read_user_objects().values(),
                      key=lambda o: o["name"].lower())

    def add_user_object(
        self, name: str, ra_hours: float, dec_deg: float
    ) -> dict:
        """Save a named RA/Dec target (HC "User Objects" -> Save)."""
        clean = name.strip()
        if not clean:
            raise ValueError("user object name must not be empty")
        if not -90.0 <= float(dec_deg) <= 90.0:
            raise ValueError("dec_deg must be between -90 and +90")
        objs = self._read_user_objects()
        objs[clean.lower()] = {"name": clean,
                               "ra_hours": float(ra_hours) % 24.0,
                               "dec_deg": float(dec_deg)}
        self._write_user_objects(objs)
        return objs[clean.lower()]

    def save_current_as(self, name: str) -> dict:
        """Save the current pointing as a user object (HC "Save Sky Object")."""
        self._require_connected()
        st = self.status()
        return self.add_user_object(name, st.ra_hours, st.dec_deg)

    def delete_user_object(self, name: str) -> None:
        """Delete a user object by name."""
        objs = self._read_user_objects()
        key = name.strip().lower()
        if key not in objs:
            raise ValueError(f"no user object named {name!r}")
        del objs[key]
        self._write_user_objects(objs)

    def goto_user_object(
        self, name: str, wait: bool = True, timeout: float = 300.0
    ) -> tuple[float, float, bool]:
        """Slew to a saved user object; returns (ra_hours, dec_deg, settled)."""
        objs = self._read_user_objects()
        key = name.strip().lower()
        if key not in objs:
            raise ValueError(f"no user object named {name!r}")
        obj = objs[key]
        settled = self.goto_radec(obj["ra_hours"], obj["dec_deg"],
                                  wait=wait, timeout=timeout)
        return obj["ra_hours"], obj["dec_deg"], settled

    # -- software pointing model (Depth 1: no HC menus) ----------------------
    def _read_pending(self) -> list[SyncStar]:
        try:
            data = json.loads(self._pending_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        stars = []
        for d in data:
            try:
                stars.append(SyncStar(**d))
            except TypeError:
                continue
        return stars

    def _write_pending(self) -> None:
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_path.write_text(
            json.dumps([s.__dict__ for s in self._sync_stars], indent=2))
    def _resolve_with_stars(self, name: str) -> tuple[float, float, str]:
        try:
            return resolve_target(name)
        except TargetNotFound:
            key = name.strip().upper()
            for star_name, (ra, dec) in ALIGN_STARS.items():
                if star_name.upper() == key:
                    return ra, dec, "scopepilot-align-stars"
            raise

    def record_sync_star(self, name: str) -> SyncStar:
        """Record the currently-centered object as a sync star.

        Center *name* in the eyepiece (jog pad / dashboard), then call this.
        The star's true alt-az (from site + time) is paired with the mount's
        reported alt-az; :meth:`fit_pointing` turns the pairs into a model.
        """
        self._require_connected()
        if self.site_lat is None or self.site_lon is None:
            raise NexStarError("need site_lat/site_lon to record a sync star")
        ra, dec, _src = self._resolve_with_stars(name)
        now = utcnow()
        az, alt = radec_to_altaz(ra, dec, self.site_lat, self.site_lon, now)
        st = self.status()
        if st.az_deg is None or st.alt_deg is None:
            raise NexStarError(
                "sync stars need the mount's alt-az readout "
                "(serial/sim backend)")
        star = SyncStar(name=name, true_az=az, true_alt=alt,
                        reported_az=st.az_deg, reported_alt=st.alt_deg,
                        at=now.timestamp())
        self._sync_stars.append(star)
        self._write_pending()
        return star

    def fit_pointing(self) -> PointingModel:
        """Fit the pointing model from recorded sync stars and activate it."""
        self._require_connected()
        self.pointing = PointingModel.fit(
            self._sync_stars, site_lat=self.site_lat, site_lon=self.site_lon)
        # Stars are consumed into the model; drop the pending file.
        self._sync_stars = []
        try:
            self._pending_path.unlink(missing_ok=True)
        except OSError:
            pass
        return self.pointing

    def load_pointing(self, path=None) -> PointingModel | None:
        """Load a saved model (same power-on pose required to stay valid)."""
        self.pointing = PointingModel.load(path or default_pointing_path())
        return self.pointing

    def save_pointing(self, path=None):
        if self.pointing is None:
            raise NexStarError("no pointing model to save")
        return self.pointing.save(path or default_pointing_path())

    def clear_pointing(self, pointing_path=None) -> None:
        self.pointing = None
        self._sync_stars = []
        for p in (self._pending_path,
                  Path(pointing_path) if pointing_path
                  else default_pointing_path()):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def pointing_status(self) -> dict:
        if self.pointing is None:
            return {"active": False, "stars": len(self._sync_stars)}
        p = self.pointing
        return {
            "active": True,
            "stars": len(p.stars),
            "pending": len(self._sync_stars),
            "az_offset_deg": p.az_offset,
            "alt_offset_deg": p.alt_offset,
            "rms_arcmin": p.rms_arcmin,
            "created": p.created,
        }

    def center_target(
        self,
        name: str,
        capture_and_solve,
        *,
        tolerance_arcmin: float = 1.0,
        max_iters: int = 4,
        exposure_s: float = 5.0,
        on_event=None,
    ) -> dict:
        """Slew to *name*, then closed-loop plate-solve centering."""
        from scopepilot.center import closed_loop_center

        ra, dec, _src = self._resolve_with_stars(name)
        return closed_loop_center(
            self, ra, dec, capture_and_solve,
            tolerance_arcmin=tolerance_arcmin, max_iters=max_iters,
            exposure_s=exposure_s, on_event=on_event)
