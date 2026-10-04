# ScopePilot

Telescope control for the **Celestron NexStar 6SE** — the operator's console
that sits next to [AstroCapture](https://github.com/Skeleten80/astro-capture).

AstroCapture is the *imaging sequencer*: it drives the camera, runs the
exposure plan, solves plates, guides, stacks. ScopePilot is the *mount
console*: slewing, syncing, jogging, tracking, parking, and a web
hand-controller — plus the glue that lets the two programs share one mount
without fighting over it.

```
                ┌──────────────┐   INDI client   ┌──────────────┐
                │ AstroCapture │◄──────────────►│  indiserver  │
                │ (imaging)    │                 │ celestron_gps│─── serial ──► 6SE
                └──────────────┘                 └──────────────┘        ▲
                        ▲ ▲                          ▲                 │
                        │ │ night plan               │ INDI client       │ direct
                        │ │ manual-override          │                 serial
                        │ │                          │                 │
                ┌───────┴─┴────────────────┐   ┌─────┴────────┐        │
                │ scopepilot queue         │   │ scopepilot   │────────┘
                │ (walk the night's        │   │ --backend    │
                │  targets, slew by slew)  │   │ serial|indi  │
                └──────────────────────────┘   └──────────────┘
```

## Features

- **Real NexStar protocol driver** (`scopepilot/nexstar.py`) — pure Python,
  no compiled dependencies. HC serial commands (goto/sync/position/
  tracking/time/site) plus AUX-bus pass-through (fixed & variable rate
  slew, per-axis slew polling, bus scan, GPS check).
- **Software pointing model** — align with sync stars from the MacBook;
  no hand-controller menus, no HC alignment needed (see below).
- **Closed-loop plate-solve centering** — slew, solve with AstroCapture's
  solver, auto-correct, repeat until the target is centered to ~1 arcmin.
- **Three backends**: `serial` (direct to the hand controller), `indi`
  (second INDI client on AstroCapture's indiserver — no serial-port fight),
  `sim` (wire-level fake 6SE that exercises the real driver code).
- **Web hand-controller** (`scopepilot dash`): live position, press-and-hold
  jog pad, catalog GoTo with search, tracking modes, park, sync, pointing
  model panel, and a **manual-override claim** AstroCapture can poll.
- **Night-queue bridge** (`scopepilot queue --plan …`): reads an AstroCapture
  night plan and slews through its targets one by one.
- **Catalog resolution**: uses AstroCapture's 5,045-object catalog when
  installed, with a built-in bright-object fallback.
- 144 pytest tests, all passing on the simulator.

## Hardware hookup

The 6SE's hand controller has a PC port on its underside:

- **Post-2016 NexStar+ HC**: mini-USB port (Prolific PL2303 inside) —
  shows up as `/dev/ttyUSB0` on Linux, no extra cable needed beyond
  USB-A → mini-USB.
- **Older HC**: RJ11 "phone jack" PC port — needs the Celestron RS-232
  cable (or a homebrew RJ11→DB9, 3 wires: TX/RX/GND) plus a USB-serial
  adapter on modern machines.

Either way the link is **9600 baud, 8N1**. The hand controller stays fully
usable while ScopePilot is connected.

## Quickstart (simulator, no hardware)

```bash
pip install -e .                 # PyYAML only; add pyserial for real serial
scopepilot probe                 # shakedown: echo/version/model/alignment
scopepilot goto "M51"            # slew the sim, wait for settle
scopepilot status --watch        # live position
scopepilot dash                  # web console on http://127.0.0.1:8765
```

## Real hardware

```bash
scopepilot --backend serial --port /dev/ttyUSB0 probe
```

`probe` is the shakedown: echo round-trip, HC firmware, model check
(expect **12 = 6/8 SE**), alignment state, tracking mode, AUX bus scan,
HC clock and site. If `probe` passes, the driver is talking to your mount.

Typical session:

```bash
scopepilot --backend serial --port /dev/ttyUSB0 set-time
scopepilot --backend serial --port /dev/ttyUSB0 set-location --lat 43.3767 --lon -80.9809
# align from the hand controller (SkyAlign / Auto Two-Star) …
scopepilot --backend serial --port /dev/ttyUSB0 goto "M13"
scopepilot --backend serial --port /dev/ttyUSB0 jog up --seconds 2
scopepilot --backend serial --port /dev/ttyUSB0 park
```

Config file (`~/.scopepilot/config.yaml`, see `examples/scopepilot.yaml`)
remembers the backend, port, site and park position so you can drop the
flags.

## Replacing the hand controller (Depth 1)

The HC stays plugged in as a dumb motor driver, but you never touch its
menus: ScopePilot drives the mount in **alt-az** (`GOTO AZM-ALT` needs no
HC alignment, unlike `GOTO RA/DEC`) and carries its own pointing model.

**1. Align from the MacBook** — center 2–3 bright stars, no HC menus:

```bash
scopepilot align                  # guided: suggests well-placed stars,
                                  # slews close, you center, press Enter
# or one star at a time:
scopepilot align --star Vega
scopepilot align --star Altair    # auto-fits once 2+ stars are recorded
scopepilot align --status         # offsets + RMS residual
```

Each sync star pairs the star's *true* alt-az (computed from site + time,
J2000 precessed to date, verified <30" vs astropy) with the mount's
*reported* alt-az. The fit recovers the two zero-point offsets (exact for
an ideal mount); the RMS tells you how honest the model is. The model is
saved to `~/.scopepilot/pointing.json` and **auto-loads on every command**
— from then on every `goto` goes through the model and the HC's alignment
state is irrelevant.

**2. Closed-loop centering** — the model gets the target on the chip;
plate solving centers it to ~1 arcmin:

```bash
scopepilot center "M51"           # slew -> capture -> solve -> correct -> repeat
```

This captures with AstroCapture's camera drivers and solves with its
`PlateSolver`, so it needs the `astrocapture` package importable:

```bash
pip install -e ~/workspace/astro-capture -e ~/workspace/scopepilot
```

The same flow runs from the dashboard (pointing panel → *Center on
target*), with live progress.

**Power-on pose trick:** the model's zero points depend on where the OTA
was pointing at power-on. Always power on with the tube level and pointing
north and the saved model (`~/.scopepilot/pointing.json`) stays valid
between sessions — `scopepilot align --reuse` picks it up.

## Working alongside AstroCapture

**Option A — shared indiserver (recommended while imaging).**
`indi_celestron_gps` owns the serial port; both programs attach as INDI
clients:

```bash
scopepilot --backend indi goto "M51"      # same mount AstroCapture uses
```

**Option B — direct serial (AstroCapture idle).** ScopePilot owns the port
and speaks the NexStar protocol with zero dependencies beyond pyserial.

**Option C — night queue.** Let AstroCapture plan the night, let ScopePilot
drive the slews:

```bash
scopepilot queue --plan ~/astro-capture/examples/night_queue.yaml --dwell 30
```

**Manual-override claim.** Grab the mount in the dashboard (Claim button)
and AstroCapture's sequencer can poll before slewing:

```python
from scopepilot.bridge import check_manual_override
if (check_manual_override("http://127.0.0.1:8765") or {}).get("claimed"):
    pause_sequencer()   # operator is driving
```

## CLI reference

| Command | What it does |
|---|---|
| `probe` | connection shakedown report |
| `status [--watch]` | live position / tracking / slew state |
| `goto "M51"` / `--ra/--dec` / `--az/--alt` | slew (waits for settle) |
| `sync "M51"` / `--ra/--dec` | sync on the centered object |
| `track off\|alt-az\|eq-north\|eq-south` | tracking mode |
| `jog up\|down\|left\|right [--rate 1-9] [--seconds N]` | handpad nudge |
| `stop` | cancel goto + halt axes |
| `park` / `unpark` | slew to home + tracking off / resume |
| `set-time` / `set-location --lat --lon` | HC clock / site from this computer |
| `bus-scan` | enumerate AUX-bus devices |
| `targets [query]` | catalog search |
| `queue --plan night.yaml [--dwell N]` | slew the AstroCapture night plan |
| `align [--star NAME] [--fit] [--status] [--clear] [--reuse]` | software pointing model, no HC menus |
| `center "M51" [--exposure S] [--tolerance A] [--max-iters N]` | closed-loop plate-solve centering |
| `dash` / `server [--port N]` | web console / JSON API |

Global flags: `--backend sim|serial|indi`, `--port`, `--config`, `--slew-rate`
(sim only).

## HTTP API

`GET /api/state` returns the mount snapshot plus `manual_override`:

```json
{"ok": true, "backend": "serial", "model": "NexStar 6/8 SE",
 "aligned": true, "tracking_mode": "alt-az",
 "ra_hours": 13.498, "dec_deg": 47.19, "az_deg": 201.3, "alt_deg": 55.1,
 "slewing": false, "parked": false,
 "manual_override": {"claimed": false, "by": null, "at": null}}
```

`POST` endpoints: `/api/goto`, `/api/sync`, `/api/jog`, `/api/jog_stop`,
`/api/stop`, `/api/track`, `/api/park`, `/api/unpark`, `/api/claim`,
`/api/release`. `GET /api/targets?q=` and `GET /api/plan?path=` round it out.

## Layout

```
scopepilot/
  nexstar.py     # protocol codecs + NexStarDriver (no third-party deps)
  sim.py         # wire-level fake 6SE hand controller (tests, sim backend)
  astro.py       # pure-python RA/Dec <-> alt-az (precession, <30" vs astropy)
  pointing.py    # software pointing model: sync stars, fit, persist
  center.py      # closed-loop plate-solve centering engine
  backends.py    # serial / sim / indi backends behind one interface
  controller.py  # orchestration: goto-and-wait, park, probe, alignment guard
  bridge.py      # AstroCapture glue: catalog, night plans, override polling
  server.py      # web console + JSON API (stdlib http.server)
  dash.html      # dashboard page (no CDN, works offline at the scope)
  config.py      # YAML config
  cli.py         # `scopepilot` command tree
tests/           # 144 tests: codecs, wire protocol vs sim, astro transforms,
                 # pointing model, closed loop, controller, bridge, HTTP API, CLI
```

## Testing

```bash
python3 -m pytest tests/ -q     # 144 passed
```

The simulator speaks the real byte protocol, so the driver, controller,
server and CLI are all tested end-to-end with no telescope attached.

## Honest caveats

- **Not yet tested against a physical 6SE.** The protocol implementation
  follows Celestron's published spec cross-checked with a 2026-validated
  community reference, and every byte is exercised against the simulator —
  but the first real connection is a shakedown. Run `probe` first and
  expect to find quirks (that's what `bus-scan` is for).
- **The pointing model replaces HC alignment, not physics.** It fits
  zero-point offsets (exact for an ideal mount); cone error and axis
  non-perpendicularity show up in the RMS. The closed-loop plate solve is
  the mechanism that actually centers targets — the model just gets them
  on the chip.
- **"Park" is a controlled slew**, not a true park — NexStar mounts have
  no park command or home sensor. It slews to your configured home
  (default az 0° / alt 5°) and switches tracking off.
- **GOTO accuracy** is whatever the HC's alignment + sync model gives you;
  use `sync` on a nearby bright star to tighten pointing, exactly as you
  would from the hand controller.
- Slew-rate table (rates 1–9 → °/s) is approximate; Celestron publishes
  only the ~4°/s maximum for the SE series.
- Time/location commands talk to the **HC**, not a GPS module. The 6SE
  has no onboard GPS; enter View Time/Site on the HC after a GPS fix if
  you add one.

## Protocol references

- Celestron, *NexStar Communication Protocol* (PC-port command set)
- open-astro/AlpacaBridge `nexstar_protocol_reference.md` — AUX bus +
  motor-controller commands, validated on hardware 2026

## License

MIT
