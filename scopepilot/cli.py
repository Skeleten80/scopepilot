"""ScopePilot command line interface.

Quick start (no hardware)::

    scopepilot probe                      # shakedown against the simulator
    scopepilot goto "M51"                  # slew the sim, wait for settle
    scopepilot dash                        # web hand-controller on :8765

Real hardware (NexStar+ hand controller over USB)::

    scopepilot --backend serial --port /dev/ttyUSB0 probe
    scopepilot --backend serial --port /dev/ttyUSB0 goto "M13"

Shared with AstroCapture's indiserver (no serial fight)::

    scopepilot --backend indi goto "M51"
"""

from __future__ import annotations

import argparse
import sys
import time

from scopepilot import __version__
from scopepilot import bridge
from scopepilot.backends import available_backends
from scopepilot.config import ScopeConfig, load_config, make_backend_from_config
from scopepilot.controller import AlignmentError, TelescopeController
from scopepilot.nexstar import MODEL_NAMES, NexStarError


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scopepilot",
        description="Telescope control for the Celestron NexStar 6SE.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--backend", choices=available_backends(), default=None,
                   help="mount backend (default: from config, else sim)")
    p.add_argument("--port", default=None,
                   help="serial device for --backend serial (e.g. /dev/ttyUSB0)")
    p.add_argument("--config", default=None, help="config YAML path")
    p.add_argument("--slew-rate", type=float, default=None,
                   help="sim slew rate, deg/s (default 360)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="connection shakedown: echo/version/model/alignment")

    s = sub.add_parser("status", help="print mount status")
    s.add_argument("--watch", action="store_true", help="refresh continuously")
    s.add_argument("--interval", type=float, default=2.0)

    g = sub.add_parser("goto", help="slew to a target")
    g.add_argument("name", nargs="?", help="target name (catalog lookup)")
    g.add_argument("--ra", type=float, help="RA hours (with --dec)")
    g.add_argument("--dec", type=float, help="Dec degrees (with --ra)")
    g.add_argument("--az", type=float, help="azimuth deg (with --alt)")
    g.add_argument("--alt", type=float, help="altitude deg (with --az)")
    g.add_argument("--no-wait", action="store_true", help="return immediately")
    g.add_argument("--timeout", type=float, default=300.0)

    s2 = sub.add_parser("sync", help="sync on the centered object")
    s2.add_argument("name", nargs="?", help="target name (catalog lookup)")
    s2.add_argument("--ra", type=float)
    s2.add_argument("--dec", type=float)

    t = sub.add_parser("track", help="set tracking mode")
    t.add_argument("mode", help="off | alt-az | eq-north | eq-south")

    j = sub.add_parser("jog", help="nudge the mount")
    j.add_argument("direction", help="up | down | left | right")
    j.add_argument("--rate", type=int, default=5, help="slew rate 1-9")
    j.add_argument("--seconds", type=float, default=None,
                   help="jog duration; omit to jog until 'stop'")

    sub.add_parser("stop", help="stop jog motion on all axes")
    pk = sub.add_parser("park", help="slew to home, tracking off")
    pk.add_argument("--no-wait", action="store_true")
    sub.add_parser("unpark", help="clear parked flag, resume tracking")

    sub.add_parser("set-time", help="set the HC clock from this computer")
    sl = sub.add_parser("set-location", help="set the HC observing site")
    sl.add_argument("--lat", type=float, required=True)
    sl.add_argument("--lon", type=float, required=True)

    sub.add_parser("bus-scan", help="enumerate AUX bus devices")
    bl = sub.add_parser("backlash",
                        help="get/set anti-backlash (stored in the mount)")
    bl.add_argument("--axis", choices=["az", "alt"], required=True)
    bl.add_argument("--dir", choices=["+", "-"], required=True,
                    help="motor direction")
    bl.add_argument("--value", type=int, default=None,
                    help="0-99 to set; omit to read the current value")
    cw = sub.add_parser("cordwrap", help="get/set cord wrap")
    cw.add_argument("state", nargs="?", choices=["on", "off"],
                    help="omit to read the current state")
    sub.add_parser("targets", help="list / search known targets").add_argument(
        "query", nargs="?", default="")

    d = sub.add_parser("dash", help="web hand-controller dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", dest="http_port", type=int, default=None)

    s3 = sub.add_parser("server", help="JSON API server (AstroCapture polls this)")
    s3.add_argument("--host", default="127.0.0.1")
    s3.add_argument("--port", dest="http_port", type=int, default=None)

    q = sub.add_parser("queue",
                       help="slew through an AstroCapture night plan, target by target")
    q.add_argument("--plan", required=True, help="night_queue.yaml path")
    q.add_argument("--dwell", type=float, default=0.0,
                   help="seconds to sit on each target after settle")
    q.add_argument("--timeout", type=float, default=300.0)

    a = sub.add_parser("align",
                       help="software pointing model: align without HC menus")
    a.add_argument("--star", default=None,
                   help="record one sync star (center it first)")
    a.add_argument("--fit", action="store_true",
                   help="fit the model from recorded stars and save it")
    a.add_argument("--status", action="store_true", help="show model status")
    a.add_argument("--clear", action="store_true", help="clear the model")
    a.add_argument("--reuse", action="store_true",
                   help="load the saved model (same power-on pose only)")
    a.add_argument("--count", type=int, default=3,
                   help="stars for the guided flow")

    c = sub.add_parser("center",
                       help="closed-loop plate-solve centering on a target")
    c.add_argument("name", help="target name")
    c.add_argument("--exposure", type=float, default=None)
    c.add_argument("--tolerance", type=float, default=None,
                   help="arcmin")
    c.add_argument("--max-iters", type=int, default=None)
    c.add_argument("--camera-driver", default=None)
    return p


def _controller(args: argparse.Namespace, cfg: ScopeConfig) -> TelescopeController:
    overrides = {k: v for k, v in vars(args).items()
                 if k in ("backend", "port", "slew_rate") and v is not None}
    backend = make_backend_from_config(cfg, **overrides)
    scope = TelescopeController(
        backend,
        site_lat=cfg.site_lat,
        site_lon=cfg.site_lon,
        home_az=cfg.home_az,
        home_alt=cfg.home_alt,
    )
    # Pick up the saved pointing model automatically: every goto then
    # routes through it with no HC alignment. Only valid if the power-on
    # pose matches the alignment session (see `align --help`).
    scope.load_pointing()
    return scope


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _fmt_deg(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}°"


def _fmt_ra(h: float | None) -> str:
    if h is None:
        return "—"
    h %= 24.0
    hh, rem = divmod(h, 1)
    mm, rem = divmod(rem * 60, 1)
    ss = rem * 60
    return f"{int(hh):02d}:{int(mm):02d}:{int(ss):02d}"


def cmd_probe(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            rep = scope.probe()
        except NexStarError as exc:
            print(f"PROBE FAILED: {exc}", file=sys.stderr)
            return 1
    print(f"backend : {rep['backend']}")
    print(f"model   : {rep['model']} (id {rep.get('model_id', '?')})")
    if rep.get("hc_version"):
        print(f"HC fw   : {rep['hc_version']}")
    print(f"aligned : {'yes' if rep['aligned'] else 'NO -- align from the hand controller'}")
    print(f"tracking: {rep['tracking_mode']}")
    pos = rep["position"]
    print(f"RA/Dec  : {_fmt_ra(pos['ra_hours'])}  {_fmt_deg(pos['dec_deg'])}")
    print(f"Az/Alt  : {_fmt_deg(pos['az_deg'])}  {_fmt_deg(pos['alt_deg'])}")
    if rep.get("bus"):
        names = {16: "AZM", 17: "ALT", 176: "GPS", 178: "RTC"}
        bus = ", ".join(f"{names.get(d, d)} fw {v}" for d, v in rep["bus"].items())
        print(f"AUX bus : {bus}")
    if rep.get("hc_time"):
        t = rep["hc_time"]
        print(f"HC clock: {t['year']}-{t['month']:02d}-{t['day']:02d} "
              f"{t['hour']:02d}:{t['minute']:02d}:{t['second']:02d} "
              f"UTC{t['utc_offset_hours']:+.0f}{' DST' if t['dst'] else ''}")
    if rep.get("hc_location"):
        loc = rep["hc_location"]
        print(f"HC site : {loc['lat']:.4f}, {loc['lon']:.4f}")
    if rep.get("simulated"):
        print("(simulated hand controller)")
    return 0


def cmd_status(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        def show():
            st = scope.status()
            pm = scope.pointing_status()
            model = (f" | model {pm['stars']}★ RMS {pm['rms_arcmin']:.1f}'"
                     if pm["active"] else "")
            print(f"[{time.strftime('%H:%M:%S')}] "
                  f"RA {_fmt_ra(st.ra_hours)} Dec {_fmt_deg(st.dec_deg)} | "
                  f"Az {_fmt_deg(st.az_deg)} Alt {_fmt_deg(st.alt_deg)} | "
                  f"{st.tracking_mode}"
                  f"{' SLEWING' if st.slewing else ''}"
                  f"{' PARKED' if st.parked else ''}"
                  f"{'' if st.aligned else ' NOT-ALIGNED'}"
                  f"{model}")
        show()
        if args.watch:
            try:
                while True:
                    time.sleep(args.interval)
                    show()
            except KeyboardInterrupt:
                pass
    return 0


def _goto_args(args) -> tuple[str, tuple]:
    if args.name:
        return "name", (args.name,)
    if args.ra is not None and args.dec is not None:
        return "radec", (args.ra, args.dec)
    if args.az is not None and args.alt is not None:
        return "altaz", (args.az, args.alt)
    raise ValueError("goto needs a target name or --ra/--dec or --az/--alt")


def cmd_goto(args, cfg) -> int:
    kind, vals = _goto_args(args)
    with _controller(args, cfg) as scope:
        try:
            if kind == "name":
                ra, dec, src, ok = scope.goto_target(
                    vals[0], wait=not args.no_wait, timeout=args.timeout)
                print(f"slewing to {vals[0]}: RA {_fmt_ra(ra)} Dec {_fmt_deg(dec)} [{src}]")
            elif kind == "radec":
                ok = scope.goto_radec(*vals, wait=not args.no_wait, timeout=args.timeout)
                print(f"slewing to RA {_fmt_ra(vals[0])} Dec {_fmt_deg(vals[1])}")
            else:
                ok = scope.goto_altaz(*vals, wait=not args.no_wait, timeout=args.timeout)
                print(f"slewing to Az {_fmt_deg(vals[0])} Alt {_fmt_deg(vals[1])}")
            if not args.no_wait:
                print("settled ✓" if ok else "TIMEOUT waiting for slew")
                return 0 if ok else 1
        except AlignmentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_sync(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            if args.name:
                ra, dec, src = scope.sync_target(args.name)
                print(f"synced on {args.name} [{src}]")
            elif args.ra is not None and args.dec is not None:
                scope.sync_here(args.ra, args.dec)
                print(f"synced on RA {_fmt_ra(args.ra)} Dec {_fmt_deg(args.dec)}")
            else:
                print("sync needs a target name or --ra/--dec", file=sys.stderr)
                return 2
        except AlignmentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_track(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            mode = scope.set_tracking(args.mode)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    print(f"tracking: {mode}")
    return 0


def cmd_jog(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            scope.jog(args.direction, rate=args.rate, seconds=args.seconds)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if args.seconds:
        print(f"jogged {args.direction} at rate {args.rate} for {args.seconds}s")
    else:
        print(f"jogging {args.direction} at rate {args.rate} -- run 'scopepilot stop' to halt")
    return 0


def cmd_stop(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        scope.abort()
    print("stopped: goto cancelled, axes halted")
    return 0


def cmd_park(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        ok = scope.park(wait=not args.no_wait)
    print(f"parked at az {cfg.home_az:.1f} / alt {cfg.home_alt:.1f}, tracking off"
          + ("" if ok or args.no_wait else " (slew timed out)"))
    return 0


def cmd_unpark(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        scope.unpark()
    print("unparked, alt-az tracking on")
    return 0


def cmd_set_time(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            t = scope.sync_clock()
        except NotImplementedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    print(f"HC clock set: {t['year']}-{t['month']:02d}-{t['day']:02d} "
          f"{t['hour']:02d}:{t['minute']:02d} UTC{t['utc_offset_hours']:+.0f}")
    return 0


def cmd_set_location(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            scope.set_site(args.lat, args.lon)
        except NotImplementedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    print(f"HC site set: {args.lat:.4f}, {args.lon:.4f}")
    return 0


def cmd_bus_scan(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        details = scope.backend.probe_details()
    names = {16: "AZM motor", 17: "ALT motor", 176: "GPS", 178: "RTC"}
    bus = details.get("bus", {})
    if not bus:
        print("no AUX devices answered")
        return 1
    for dev, ver in sorted(bus.items()):
        print(f"dev {dev:3d} ({names.get(dev, 'unknown'):9s}): fw {ver}")
    return 0


def cmd_backlash(args, cfg) -> int:
    """Get/set anti-backlash. The value is stored in the motor controller,
    so a one-time set over direct serial persists for INDI sessions too."""
    direction = 1 if args.dir == "+" else -1
    with _controller(args, cfg) as scope:
        try:
            if args.value is None:
                value = scope.get_backlash(args.axis, direction)
                print(f"{args.axis} {args.dir}: {value}")
            else:
                scope.set_backlash(args.axis, direction, args.value)
                print(f"{args.axis} {args.dir} -> {args.value} "
                      f"(stored in the mount)")
        except (ValueError, NotImplementedError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_cordwrap(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        try:
            if args.state is None:
                on = scope.cordwrap_enabled()
                print(f"cordwrap: {'on' if on else 'off'}")
            else:
                scope.set_cordwrap(args.state == "on")
                print(f"cordwrap {args.state}")
        except NotImplementedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return 0


def cmd_targets(args, cfg) -> int:
    for r in bridge.search_targets(args.query, limit=30):
        print(f"{r['name']:10s} RA {_fmt_ra(r['ra_hours'])} "
              f"Dec {_fmt_deg(r['dec_deg'])}  [{r['source']}]")
    return 0


def cmd_dash(args, cfg) -> int:
    from scopepilot.server import serve

    def make_rig():
        from scopepilot.center import AstroCaptureRig
        return AstroCaptureRig(camera_driver=cfg.camera_driver)

    with _controller(args, cfg) as scope:
        serve(scope, host=args.host,
              port=args.http_port or cfg.server_port,
              make_rig=make_rig)
    return 0


cmd_server = cmd_dash


def cmd_queue(args, cfg) -> int:
    try:
        plan = bridge.read_night_plan(args.plan)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read plan {args.plan}: {exc}", file=sys.stderr)
        return 2
    if not plan:
        print(f"no targets found in {args.plan}", file=sys.stderr)
        return 2
    print(f"queue: {len(plan)} target(s) from {args.plan}\n")
    with _controller(args, cfg) as scope:
        try:
            for i, tgt in enumerate(plan, 1):
                name = tgt["name"]
                try:
                    ra, dec, src = bridge.resolve_target(name)
                except bridge.TargetNotFound as exc:
                    print(f"[{i}/{len(plan)}] {name}: SKIP ({exc})")
                    continue
                print(f"[{i}/{len(plan)}] {name}: slewing "
                      f"(RA {_fmt_ra(ra)} Dec {_fmt_deg(dec)} [{src}])…")
                try:
                    ok = scope.goto_radec(ra, dec, timeout=args.timeout)
                except AlignmentError as exc:
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    return 2
                print("  settled ✓" if ok else "  TIMEOUT -- continuing")
                if args.dwell > 0:
                    print(f"  dwelling {args.dwell:.0f}s (image now, Ctrl-C to stop queue)…")
                    time.sleep(args.dwell)
        except KeyboardInterrupt:
            print("\nqueue interrupted -- stopping mount")
            scope.abort()
            return 130
    print("\nqueue complete")
    return 0


def _fit_and_save(scope) -> int:
    try:
        model = scope.fit_pointing()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    path = scope.save_pointing()
    print(f"model: {len(model.stars)} star(s), "
          f"az_offset {model.az_offset:+.3f}°, "
          f"alt_offset {model.alt_offset:+.3f}°, "
          f"RMS {model.rms_arcmin:.2f}'  ->  {path}")
    if model.rms_arcmin > 15:
        print("WARNING: large RMS -- recenter the stars carefully "
              "or add more of them", file=sys.stderr)
    return 0


def _interactive_align(args, cfg, scope) -> int:
    from scopepilot.pointing import suggest_alignment_stars

    stars = suggest_alignment_stars(cfg.site_lat, cfg.site_lon,
                                    count=args.count)
    if not stars:
        print("no suitable alignment stars above the horizon right now",
              file=sys.stderr)
        return 1
    print("Guided alignment: center each star in the eyepiece, then press "
          "Enter.\n"
          "Tip: always power the mount on with the OTA level and pointing\n"
          "north -- then the saved model stays valid between sessions.\n")
    try:
        for i, (name, az, alt) in enumerate(stars, 1):
            print(f"[{i}/{len(stars)}] {name}: true az {az:.1f}°, "
                  f"alt {alt:.1f}°")
            if scope.pointing is not None and scope._sync_stars:
                rep_az, rep_alt = scope.pointing.to_reported(az, alt)
                print(f"  slewing close "
                      f"(reported az {rep_az:.1f}° alt {rep_alt:.1f}°)…")
                scope.goto_altaz(rep_az, rep_alt)
            input("  center it with the jog pad, then press Enter… ")
            star = scope.record_sync_star(name)
            print(f"  recorded (reported az {star.reported_az:.2f}° "
                  f"alt {star.reported_alt:.2f}°)")
            if len(scope._sync_stars) >= 2:
                model = scope.fit_pointing()
                print(f"  model RMS now {model.rms_arcmin:.2f}' "
                      f"({len(model.stars)} stars)")
    except KeyboardInterrupt:
        print("\nalignment interrupted")
    if scope._sync_stars:
        return _fit_and_save(scope)
    print("no stars recorded", file=sys.stderr)
    return 1


def cmd_align(args, cfg) -> int:
    with _controller(args, cfg) as scope:
        if args.clear:
            scope.clear_pointing()
            print("pointing model cleared (memory, pending stars, saved file)")
            return 0
        if args.reuse:
            model = scope.load_pointing()
            if model is None:
                print("no saved pointing model found", file=sys.stderr)
                return 1
            print(f"loaded model: {len(model.stars)} star(s), "
                  f"RMS {model.rms_arcmin:.2f}' "
                  f"(only valid if the power-on pose matches)")
            return 0
        if args.status:
            st = scope.pointing_status()
            if not st["active"]:
                print(f"no active model ({st['stars']} star(s) recorded)")
                return 0
            print(f"model: {st['stars']} star(s), "
                  f"az_offset {st['az_offset_deg']:+.3f}°, "
                  f"alt_offset {st['alt_offset_deg']:+.3f}°, "
                  f"RMS {st['rms_arcmin']:.2f}'")
            return 0
        if args.star:
            try:
                star = scope.record_sync_star(args.star)
            except bridge.TargetNotFound as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            print(f"recorded {star.name}: true az {star.true_az:.2f}° "
                  f"alt {star.true_alt:.2f}° | reported az "
                  f"{star.reported_az:.2f}° alt {star.reported_alt:.2f}°")
            if len(scope._sync_stars) >= 2 or args.fit:
                return _fit_and_save(scope)
            print(f"({len(scope._sync_stars)} star(s) recorded; "
                  f"add more, then `scopepilot align --fit`)")
            return 0
        if args.fit:
            return _fit_and_save(scope)
        return _interactive_align(args, cfg, scope)


def cmd_center(args, cfg) -> int:
    from scopepilot.center import AstroCaptureRig

    exposure = args.exposure or cfg.center_exposure_s
    tolerance = args.tolerance or cfg.center_tolerance_arcmin
    max_iters = args.max_iters or cfg.center_max_iters
    with _controller(args, cfg) as scope:
        try:
            rig = AstroCaptureRig(
                camera_driver=args.camera_driver or cfg.camera_driver)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        try:
            hint = None
            try:
                ra, dec, _src = scope._resolve_with_stars(args.name)
                hint = (ra, dec)
            except bridge.TargetNotFound:
                pass
            rig.hint_radec = hint
            rig.connect()
            try:
                report = scope.center_target(
                    args.name, rig,
                    tolerance_arcmin=tolerance, max_iters=max_iters,
                    exposure_s=exposure)
            finally:
                rig.disconnect()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    for event in report["events"]:
        print(event)
    return 0 if report["converged"] else 1


_COMMANDS = {
    "probe": cmd_probe,
    "status": cmd_status,
    "goto": cmd_goto,
    "sync": cmd_sync,
    "track": cmd_track,
    "jog": cmd_jog,
    "stop": cmd_stop,
    "park": cmd_park,
    "unpark": cmd_unpark,
    "set-time": cmd_set_time,
    "set-location": cmd_set_location,
    "bus-scan": cmd_bus_scan,
    "backlash": cmd_backlash,
    "cordwrap": cmd_cordwrap,
    "targets": cmd_targets,
    "dash": cmd_dash,
    "server": cmd_server,
    "queue": cmd_queue,
    "align": cmd_align,
    "center": cmd_center,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    # CLI flags override the config file.
    if args.backend:
        cfg.backend = args.backend
    if args.port:
        cfg.port = args.port
    try:
        return _COMMANDS[args.cmd](args, cfg)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except NexStarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
