# ScopePilot First-Light Shakedown

First hardware night with the NexStar 6SE. Validate in order; **each phase
gates the next — stop at the first red step.** Nothing here needs clear
skies except Phases 2–5; Phase 0 is a daylight indoor job.

## Phase 0 — Indoors, daylight (do this before the scope comes out)

1. Fresh pull, `pip install -e .`, run `pytest` — all green before hardware.
2. Sim-backend rehearsal (zero stakes, learn the CLI):
   `scopepilot --backend sim probe` → `goto "M13"` → `jog` → `park` →
   `status` → `log`.
3. Dashboard: `scopepilot dash`, open it on your phone, try the joystick
   and shift+arrow precision nudge.
4. Plate-solve check (only needed for Phase 4): does
   `astrocapture.platesolve` solve a test frame on the control computer?
5. Cable check: which hand controller do you have?
   - **Post-2016 NexStar+**: mini-USB port on the HC underside → USB-A to
     mini-USB cable. Shows up as `/dev/ttyUSB0` (Linux) or
     `/dev/tty.usbserial-*` (macOS).
   - **Older HC**: RJ11 "phone jack" PC port → needs the Celestron RS-232
     cable (or homebrew RJ11→DB9: TX/RX/GND) plus a USB-serial adapter.

## Phase 1 — At the scope, dusk (mechanical + link)

1. Level the tripod, mount the OTA, power on **level and pointing north**.
   (Required later for the software pointing model; harmless otherwise.)
2. Plug in USB, confirm the port appears: `ls /dev/tty.*` (macOS) and
   look for the PL2303 / usbserial device.
3. `scopepilot --backend serial --port <dev> probe` — expect: connected,
   a model string, echo OK.
   - **Red:** probe fails → check cable, HC version, and that the HC is
     powered (the PC port is dead when the HC is off). This is a
     hardware problem, not a software one.
4. `scopepilot set-time` and `set-location` (or `site use <home>`) — the
   mount needs a sane clock and observing site.
5. `scopepilot status` — confirm idle, tracking mode as expected.
6. `scopepilot limits` — set altitude slew limits **before any goto**.
   The 6SE can strike the base at low altitudes.

## Phase 2 — First slew (HC alignment, the known-good path)

Deliberately using the mount's own proven alignment first: this validates
the serial slew path before we test our software model.

1. Run SkyAlign / Auto Two-Star from the hand controller, the familiar way.
2. `scopepilot goto "Vega"` (or any bright star up) — watch it slew,
   verify in the eyepiece.
3. `scopepilot jog` to nudge, then `scopepilot sync` on the centered star.
4. `scopepilot park` — confirm it returns home. `scopepilot log` to
   review the session.
   - **Red:** slews but goes the wrong way / mirrored → stop, note the
     exact behavior, it's a backend config issue.

## Phase 3 — Software pointing model (the Depth-1 feature)

1. Power-cycle the mount: **level + pointing north** (model requirement —
   the saved model is only valid for this power-on pose).
2. Skip HC alignment entirely. Guided flow, 3 stars:
   `scopepilot align` → center each star in the eyepiece →
   `scopepilot align --star` (×3) → `scopepilot align --fit`.
3. `scopepilot align --status` — check the residuals.
   - **Red:** residuals worse than ~0.5° → the power-on pose wasn't truly
     level/north. Redo from step 1.
4. `scopepilot goto` a target on the **opposite side of the sky** from
   the align stars — the real test of the model.
5. Next session: `scopepilot align --reuse` loads the saved model, but
   only if you power on in the same level+north pose.

## Phase 4 — Closed-loop centering (stretch; needs the camera)

1. T7i on the scope, AstroCapture running, plate solver confirmed working
   (Phase 0, step 4).
2. `scopepilot center "M13"` — slew → solve → correct loop. Watch the
   iterations converge inside the tolerance.
   - **Red:** solves fail repeatedly → check exposure/focus first; a
     defocused frame never solves.

## Phase 5 — Night queue (stretch)

1. Write a short 2-target AstroCapture YAML plan.
2. `scopepilot queue --plan <file>` — walks the plan target by target.

## Safety notes

- Never walk away mid-slew on night one.
- First gotos: pick high-altitude targets, well clear of the base.
- Alt-az + long sessions = cable winding: check the `cordwrap` setting.
- `backlash` is a set-once calibration over direct serial; it persists,
  including for INDI sessions.
