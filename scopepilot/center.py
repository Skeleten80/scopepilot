"""Closed-loop centering: slew -> plate-solve -> correct -> repeat.

The pointing model gets the target onto the chip; this loop centers it to
arcminute precision no matter how much residual model error remains. Every
correction is purely differential (solved field center vs. target, both in
alt-az at the same instant), so the loop converges even with a crude -- or
absent -- pointing model.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Callable

from scopepilot.astro import (
    angular_sep_deg,
    radec_to_altaz,
    utcnow,
    wrap180,
)

#: capture_and_solve(exposure_s) -> (ra_hours, dec_deg) of the field center,
#: or None when the frame could not be solved.
CaptureAndSolve = Callable[[float], tuple[float, float] | None]


def closed_loop_center(
    controller,
    ra_h: float,
    dec_d: float,
    capture_and_solve: CaptureAndSolve,
    *,
    tolerance_arcmin: float = 1.0,
    max_iters: int = 4,
    exposure_s: float = 5.0,
    on_event=None,
) -> dict:
    """Center (ra_h, dec_d) by closed-loop plate solving.

    Returns a report dict: {converged, iters, final_sep_arcmin, events[]}.
    Never raises for a failed solve -- it reports instead.
    """
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event:
            on_event(msg)

    lat, lon = controller.site_lat, controller.site_lon
    if lat is None or lon is None:
        return {"converged": False, "iters": 0, "final_sep_arcmin": None,
                "events": ["no site configured; set site_lat/site_lon first"]}

    emit(f"initial slew to RA {ra_h:.4f}h Dec {dec_d:+.3f}°")
    settled = controller.goto_radec(ra_h, dec_d, wait=True)
    if not settled:
        emit("WARNING: initial slew timed out; continuing anyway")

    for it in range(1, max_iters + 1):
        field = capture_and_solve(exposure_s)
        if field is None:
            emit(f"iter {it}: plate solve failed -- aborting loop")
            return {"converged": False, "iters": it,
                    "final_sep_arcmin": None, "events": events}
        fra_h, fdec_d = field
        sep = angular_sep_deg(fra_h, fdec_d, ra_h, dec_d) * 60.0
        emit(f"iter {it}: field center RA {fra_h:.4f}h Dec {fdec_d:+.3f}° "
             f"({sep:.2f}' from target)")
        if sep <= tolerance_arcmin:
            emit(f"converged: {sep:.2f}' <= {tolerance_arcmin:.2f}'")
            return {"converged": True, "iters": it,
                    "final_sep_arcmin": sep, "events": events}

        now = utcnow()
        az_f, alt_f = radec_to_altaz(fra_h, fdec_d, lat, lon, now)
        az_t, alt_t = radec_to_altaz(ra_h, dec_d, lat, lon, now)
        d_az = wrap180(az_t - az_f)
        d_alt = alt_t - alt_f
        st = controller.status()
        if st.az_deg is None or st.alt_deg is None:
            # INDI backend: no alt-az readout -- correct through the model.
            if controller.pointing is None:
                emit("no alt-az readout and no pointing model -- cannot correct")
                return {"converged": False, "iters": it,
                        "final_sep_arcmin": sep, "events": events}
            new_az, new_alt = controller.pointing.to_reported(az_t, alt_t)
        else:
            new_az = (st.az_deg + d_az) % 360.0
            new_alt = st.alt_deg + d_alt
        emit(f"iter {it}: correcting by {d_az*60:.1f}' az, {d_alt*60:.1f}' alt")
        controller.goto_altaz(new_az, new_alt, wait=True)

    emit(f"not converged after {max_iters} iterations")
    return {"converged": False, "iters": max_iters,
            "final_sep_arcmin": sep, "events": events}


class AstroCaptureRig:
    """capture_and_solve via AstroCapture's camera drivers + plate solver.

    Captures a frame with the same driver AstroCapture itself uses
    (``sim`` for testing, ``indi`` for the T7i through ``indi_gphoto_cc``),
    writes it to FITS, and solves it with ``astrocapture.platesolve``.
    Needs the ``astrocapture`` package and ``astropy`` installed.
    """

    def __init__(
        self,
        camera_driver: str = "sim",
        hint_radec: tuple[float, float] | None = None,
        workdir: str | Path | None = None,
        **camera_kwargs,
    ) -> None:
        try:
            from astrocapture.drivers import make_camera  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "closed-loop capture needs the astrocapture package"
            ) from exc
        self._camera = make_camera(camera_driver, **camera_kwargs)
        self.hint_radec = hint_radec
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(
            prefix="scopepilot-center-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._frame_no = 0

    def connect(self) -> None:
        self._camera.connect()

    def disconnect(self) -> None:
        self._camera.disconnect()

    def __call__(self, exposure_s: float) -> tuple[float, float] | None:
        from astrocapture.platesolve import PlateSolver  # type: ignore

        try:
            from astropy.io import fits  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "closed-loop capture needs astropy (pip install astropy)"
            ) from exc

        self._frame_no += 1
        path = self.workdir / f"center-{self._frame_no:03d}.fits"
        self._camera.set_exposure_settings(exposure_s)
        self._camera.start_exposure()
        deadline = time.monotonic() + exposure_s + 60.0
        while not self._camera.exposure_complete():
            if time.monotonic() > deadline:
                raise RuntimeError("exposure timed out")
            time.sleep(0.2)
        img = self._camera.download_image()
        fits.writeto(path, img, overwrite=True)
        hint = {}
        if self.hint_radec:
            hint = {"ra_hint_deg": self.hint_radec[0] * 15.0,
                    "dec_hint_deg": self.hint_radec[1]}
        solved = PlateSolver().solve(path, **hint)
        if solved is None:
            return None
        return solved[0] / 15.0, solved[1]
