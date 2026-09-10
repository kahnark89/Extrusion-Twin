# Extrusion line melt-state digital twin

Machine: 80" flexible-PVC sheet line. 18 Fuji PXR-9 zones (B1–B8 barrel, SC1–SC2 screen changer, AD adapter, D1–D7 die body), no lip heaters. Barrel cooled by a heat-transfer-oil TCU (tower water through a shell-and-tube exchanger, own controller). Dedicated screw TCU. Water-jacketed feed throat. Dual T/P probe at the adapter: temperature on a read-only digital controller, pressure on a needle gauge.

**Two ways to run it, one implementation.** Install the package and run the console on the
machine wired to the bus, or deploy the same console as a static site — Netlify, GitHub Pages,
anything that serves files — where it runs in the browser under Pyodide. The hosted build is
not a demo or a reimplementation: it loads this package and answers the same API. What it
cannot do is open a serial port, so the live bus stays with the local install.

`reference_v1_single_file.py` is kept at the repository root for reference; the package
supersedes it.

## What it is

A state estimator with a physics model inside it. The melt's thermodynamic state along the screw is never measured, only inferred. The twin propagates melt state forward from the energy inputs and corrects itself wherever a real measurement exists (barrel PVs, adapter melt temperature, motor amps). The fly-circuit detectors sit on the residuals — what the model could not explain — which is also why steady state costs almost nothing to run.

Once per poll sweep:

1. **Predict** — advance melt, barrel-metal, oil-loop and screw temperatures one step (1-D along z, lumped per zone, explicit Euler).
2. **Correct** — pull the state toward the measurements. Produces thermal residuals, a viscosity multiplier κ whose drift is the material-side drift index, and a wall-conductance multiplier hw.
3. **Expect** — the heater duty each zone *should* need given the twin (efference copy). Heater residual e_i = MV_measured − MV_expected.
4. **Detect** — sign split, adjacent-zone lag correlators (direction), wide-field templates, single-zone detectors with surround suppression, cross-modal heater-vs-motor check, heater-element check from CT, contactor chatter, send-on-delta events.
5. **Record** — one CSV row per sweep; each alarm becomes a corpus signature with three confidence registers and the nearest labeled cases attached.

## Install and run

Python 3.9+. The simulator, the console, the detectors and the corpus are standard library
only. `numpy` is needed for one thing — the ensemble Kalman filter. `minimalmodbus` and
`pyserial` are needed for one thing — a serial adapter plugged into this machine.

```
git clone https://github.com/kahnark89/Extrusion-Twin
cd Extrusion-Twin
pip install -e .                        # or: pip install -e ".[all]"
pip install -e ".[enkf]"                # just the ensemble filter
pip install -e ".[serial]"              # just the cabled RS-485 path
```

Installed, the command is `extrusion-twin`. Without installing, `PYTHONPATH=src python -m extrusion_twin`
does the same thing.

```
extrusion-twin serve                                      # the console, in your browser
extrusion-twin run --sim 4200                             # or straight from the command line
extrusion-twin run --sim 4200 --estimator enkf            # ensemble Kalman filter
extrusion-twin run --sim 4200 --arch cores --delta 3.0 --delta-res 0.15
extrusion-twin run --sim 4200 --horizon 600                 # run 10 minutes ahead of the machine
extrusion-twin write-config config.json                   # example config to edit
extrusion-twin run --config config.json --hours 8 --corpus corpus --status status.json
extrusion-twin label --corpus corpus                      # Socratic labeling of alarms
extrusion-twin match --corpus corpus                      # nearest labeled cases
extrusion-twin export --corpus corpus --out ciaer.jsonl

PYTHONPATH=src python -m extrusion_twin.tests             # 128 checks
PYTHONPATH=src python -m extrusion_twin.validate          # score against simulator truth
PYTHONPATH=src python -m extrusion_twin.validate --sweep-delta
PYTHONPATH=src python -m extrusion_twin.validate --horizon 600   # score the phase-ahead layer
```

The tests run with or without numpy; the ensemble-filter checks report as skipped when it is
not installed rather than failing.

## The console

`python -m extrusion_twin serve` starts a small local console and opens it in your browser. It runs on your
own machine only; nothing leaves the computer. Four screens:

**Set up** — choose the source (simulate, or read the real line), the architecture and estimator, and every
machine and compound value, each with its unit and a line saying where the number should come from. Settings
can be saved by name and reused.

**Watch** — the run as it happens, with a stop button. When it finishes it stays on screen.

**Runs** — every finished run with the settings it used, so two of them can be compared honestly. Open one
and the same screens replay with a scrubber.

**Cases** — the alarms waiting for an answer. Write what it turned out to be and the next alarm with a similar
residual pattern arrives carrying your answer. That is the corpus loop, and it now runs entirely in the browser.

What Watch shows, in the order it answers questions:

1. **One line at the top** — holding steady, or drifting and which side to look at. That is the direction verdict from the lag correlators, written out.
2. **The melt along the flow** — B1 through the adapter, then the die and the lip. Solid line is the melt, which nothing measures. Dashed is the barrel metal, which is what the controllers see. The shaded band is the twin's own uncertainty, so a wide band is the panel telling you not to trust that number yet.
3. **Across the sheet** — the seven die zones left to right against the die average. A skew shows here before it shows in the sheet.
4. **Duty gap by zone** — how far each heater sits from what the melt should need. One red cell in a field of grey is a band, an SSR, or a thermocouple. A whole block moving together is something else.
5. **Four readings** — material drift, kilowatts spent fighting the oil, stabilizer consumed, drool risk at the lip.
6. **Three confidence bars** — how much is measured rather than typed in, how sure the model is, how hard the detector is calling it. A short first bar means the panel is running on assumptions and should be read as an opinion.
7. **What tripped** — every alarm in plain language, with the raw detector string underneath because that is what matches the corpus.

The page also works with no console at all: drop a `twin_log.csv` on it and it replays that run.

## Seeing it work, without installing anything

```
python3 tools/build_demo.py            # writes demo.html -- one file, open it from disk
```

A real run recorded into a single self-contained page: seventy minutes of line time, the six
injected faults, the alarms it raised, and the phase-ahead panel. It is `dashboard.html`
unmodified with a stub answering the console's own API routes out of the recording, so the
plots, the scrubber, the plain-language alarms and the Cases screen are the real ones. Labels
typed on it are kept in the browser. The one thing it cannot do is start a new run, and it
says so when asked.

Useful for showing somebody the twin before asking them to install Python, and for attaching
to an email.

## Hosting it

`python -m extrusion_twin serve` is the local console. The same console also deploys as a static
site, which is what `netlify.toml` and `tools/build_web.py` are for.

```
python3 tools/build_web.py                     # dist/, pyodide from a CDN
python3 tools/build_web.py --vendor-pyodide    # dist/, pyodide inside the deploy (~16 MB)
```

On Netlify: point it at this repository and it reads `netlify.toml` — build command
`python3 tools/build_web.py --vendor-pyodide`, publish directory `dist`. There is nothing else
to configure, no environment variables and no node step. Any static host works the same way;
upload `dist/`.

**How it works, because "the twin runs in the browser" is the sort of claim worth checking.**
`tools/build_web.py` writes every `.py` file of the package into `extrusion_twin_bundle.json`.
`twin-worker.js` starts Pyodide in a module worker, writes those files into its filesystem,
imports `extrusion_twin.api`, and serves the console's routes out of `TwinAPI` — the same class
the local HTTP server puts a socket in front of. `browser-api.js` intercepts the page's
`fetch("/api/…")` calls and routes them to that worker. `dashboard.html` is byte-identical
between the two builds. So the physics, the estimators, the detector bank, the CSV and the
corpus are the package, not a port of it, and there is nothing to keep in step.

The one structural difference is who owns the loop. A worker may not block, so the hosted
build drives the run through `StepRunManager.pump(n)` in slices of about a frame's work and
yields between them, which is what keeps the panel updating while a run is going. The test
suite checks that pumping the stepper produces a byte-identical log to running it in one batch.

**What it can and cannot do when hosted:**

| | local install | hosted static site |
|---|---|---|
| Simulated runs, all four configurations | yes | yes |
| Ensemble Kalman filter | yes, with numpy | yes, numpy fetched on first use |
| Replay a `twin_log.csv` | yes | yes, including by dropping the file on the page |
| Runs, saved settings, the corpus and labelling | on disk | in the browser's own storage, per device |
| Read the real line over RS-485 | yes | **no — a browser tab cannot open a serial port** |

The setup screen disables live mode with that explanation rather than offering a button that
cannot work. For live data, run the local console on the machine wired to the bus — or on a Pi
with `--host 0.0.0.0 --token` — and use the phone or the hosted page as the screen.

**Nothing leaves the device.** The hosted build has no backend to send anything to: runs, the
CSV, signatures and labels are written to browser storage and stay there. Clearing site data
clears them, and a different device starts empty. If a run matters, open it from **Runs** and
keep the CSV.

**Vendoring, and why the flag exists.** Without `--vendor-pyodide` the page fetches the Python
runtime from `cdn.jsdelivr.net` on first visit. With it, the runtime and the numpy wheel are
copied into the deploy and the page needs no third party at all — which is the version to
deploy on a plant network, and the version to deploy if a CDN dependency is something you would
rather not have. It costs about 16 MB of deploy and a slower first build. Either way the
browser caches it, so only the first visit pays.

## Running it on a phone

It works under Termux, with one hard limit: Android will not give Termux access to a USB serial adapter
or to a Bluetooth serial port. Not a missing driver, a permission model. So the twin runs fine on the phone,
but the RS-485 bus has to reach it over the network rather than over a cable or a pairing.

```
pkg install python git
pkg install python-numpy        # only if you want the ensemble filter; pip install numpy will not build
git clone https://github.com/kahnark89/Extrusion-Twin && cd Extrusion-Twin
pip install -e .
extrusion-twin serve --no-browser
```

Or skip all of that and open the hosted build, which needs nothing installed. Same console,
same simulator, same corpus — no live bus.

Then open `http://localhost:8000` in the phone's browser. Simulated runs, replay, the corpus and the
labelling loop all work with no extra packages at all — the console and the network transport are standard
library only.

For live data, put a wireless serial gateway on the RS-485 pair and give the console an address instead of
a port name:

| what you type | what it means |
|---|---|
| `COM3`, `/dev/ttyUSB0` | a serial adapter on this machine |
| `rtu://192.168.4.1:8899` | a transparent gateway: RTU frames, CRC and all, over a socket |
| `tcp://192.168.4.1:502` | a gateway that speaks proper Modbus TCP |

Both framings are implemented from scratch in `io_bus.py`, so the wireless path needs neither
`minimalmodbus` nor `pyserial` — which matters on a phone, where compiling anything is a fight. A gateway
joined to the phone's own hotspot needs no plant network and no IT involvement.

Two practical notes. A whole 18-zone sweep over a gateway takes well under a second, so the twin runs at
the same rate it would over a cable. And an eight-hour ensemble-filter run will flatten a phone battery;
for a long shift, run the twin on a laptop or a Pi wired to the bus with `--host 0.0.0.0`, and use the
phone as the screen.

## The crammer

The crammer drive is a separate input pair, `crammer_rpm` and `crammer_amps`, and the twin never mixes it
with the extruder drive — extruder amps are where the viscosity index comes from, crammer amps are a
measurement of the feed. Wiring it turns the feed-side verdict from an inference into a corroborated call,
and buys warning time: in validation a bulk-density change is caught at the crammer in 11 s, against 197 s
for the same event to become visible in the viscosity index.

Three verdicts: load rising against baseline (denser or coarser feed), load falling (starving throat), and
load swinging without settling (bridging or rat-holing). A relative floor sits under the sigma test, so a
quarter-amp wander on a 12 A feeder never reaches the console. With nothing wired the watcher stays silent.

`hardware/WIRING.md` covers reading the crammer VFD over Modbus, which is the right way, and tapping the
gauge signals, which is the fallback — including the one case, an existing current transformer, that is
genuinely dangerous to touch with the motor running.

## The TCU loops and the driveline

Supply and return on both oil loops turn the cooling side from something the twin models into something it
measures: Q = ṁ·c_p·(return − supply), the heat the barrel is actually shedding. Four verdicts — cold side
losing ground, flow loss, load up, load down — and the discrimination matters, because losing flow makes
the delta go **up**, not down. The loop watcher cross-checks the delta against the twin's own predicted
cooling rather than reading it alone, which is what separates a failing pump from a process change.

Motor speed alongside screw speed watches the mechanical ratio, which should never move: when it does, the
coupling, belt or gearbox is slipping. The twin keeps `rpm` (screw) and `rpm_motor` distinct and computes
shear from the screw.

A design rule the numbers forced, worth stating: **measured quantities go into the state estimator,
assumed ones stay in the detectors.** Feeding a nameplate flow rate into the filter let a real flow change
get absorbed into a learned conductance, which then bent every zone touching the oil. With a genuine flow
meter the loop heat flow becomes a proper observation and the twin recovers the barrel-to-oil conductance
as 0.90 against the simulator's true 0.90, from a starting guess of 1.00. Without one it holds the
constant at 1.0 and says so.

## Panel hardware

`hardware/WIRING.md` covers the RS-485 daisy chain, the three settings each controller needs, what to
buy for the Pi, and the network scheme. `hardware/setup_pi.sh` provisions the Pi in one command.

One correction worth carrying over from that document: a hotspot **name** is not a credential. The WPA2
passphrase is. And because the console binds to a network address so a phone can reach it, it now takes
`--token` — a key in the address, stored in the data folder and remembered by the browser after the
first visit. Two independent secrets, because they fail differently.

## Modules

| file | what it holds |
|---|---|
| `model.py` | topology, `Plant`, `Recipe`, `State`, `Inputs`, the energy balances, the efference copy |
| `estimators.py` | `FixedGain` (v1 corrector) and `EnKF` (ensemble Kalman filter), one interface |
| `detectors.py` | `Bank`, `CrossModal`, `HeaterCheck`, `KappaAlarm`, `ContactWatch`, `EventEmitter`, `Detectors` |
| `cores.py` | `Core`, `Council`, `CoreTwin` — the predictive-coding partition |
| `io_bus.py` | `PXRBus`, `ModbusDevice`, `AnalogSerial`, `OpsFile`, `LiveBus`, `SimPlant`, config loader |
| `corpus.py` | alarm signatures, Socratic labeling, nearest-case `Matcher`, CIAER+ export |
| `run.py` | `TwinRun` (one sweep at a time), `run()` (the batch form), and the command line |
| `forecast.py` | `roll`, `Reconciler`, `ErrorBar`, `HorizonWatch` — the twin a phase ahead |
| `api.py` | the console's routes and the run manager, with no transport attached |
| `validate.py` | scoring against the simulator's hidden truth |
| `server.py` | HTTP in front of `api.py`: serves the page and the JSON API |
| `hardware/` | panel wiring guide and the Raspberry Pi provisioning script |
| `io_bus.py` transports | serial through minimalmodbus, or Modbus TCP and RTU-over-TCP with no dependencies |
| `dashboard.html` | the four screens: set up, watch, runs, cases |
| `tests.py` | 128 checks, no external runner |
| `../../web/` | `browser-api.js` (the fetch shim) and `twin-worker.js` (Pyodide in a module worker) |
| `../../tools/build_web.py` | assembles `dist/` for a static host; optionally vendors Pyodide |
| `../../tools/build_demo.py` | records a run into one self-contained `demo.html` |

## Repository layout

```
src/extrusion_twin/     the package: physics, estimators, detectors, io, corpus, api, console
  dashboard.html     the four screens -- shared byte-for-byte by both builds
  hardware/          panel wiring guide and the Raspberry Pi provisioning script
web/                 browser-api.js (fetch shim) + twin-worker.js (Pyodide worker)
tools/build_web.py   assembles dist/ for a static host
netlify.toml         build command and headers, so Netlify needs no configuring
.github/workflows/   tests on 3.9 and 3.12, with and without numpy, plus the static build
reference_v1_single_file.py   the earlier single file, kept for reference
```

Not in the repository, and kept out by `.gitignore`: `config.json` (the plant-specific half)
and anything a run produces (the operational half). See **IP separation** below.

## Node graph

```
feed jacket ─► B1 ► B2 ► B3 ► B4 ► B5 ► B6 ► B7 ► B8 ► SC1 ► SC2 ► AD ─┬► D1 ─┐
                │    │    │    │    │    │    │    │                    ├► D2  │
              barrel oil loop (TCU, tower water) ── screw core (own TCU) ├► …   ├► lip (inferred)
                                                                        └► D7 ─┘
```

Series chain through the adapter; the seven die zones are lateral branches across the 80", each carrying 1/7 of the flow. Every zone has a melt node and a metal node. Barrel zones also exchange with the screw core and the barrel oil. About 40 thermal states.

## Equations

Melt, per zone i:

    ρ·c_p·V_i·dT_m,i/dt = ṁ_i·c_p·(T_up − T_m,i) + Q_shear,i
                          + hw·hA_w,i·(T_b,i − T_m,i) + hA_s,i·(T_s − T_m,i)

Metal:

    C_b,i·dT_b,i/dt = MV_i·P_i − hw·hA_w,i·(T_b,i − T_m,i)
                      − UA_oil,i·(T_b,i − T_oil_b) − UA_amb,i·(T_b,i − T_air)

Oil loop: `C_oil·dT_oil/dt = Q_TCU + Σ UA_oil,i·(T_b,i − T_oil)`, with Q_TCU bounded by the TCU heater on one side and `UA_hx·(T_oil − T_tower)` on the other — the tower-water ceiling.

Shear: `P_mech = kW_per_amp·(amps − I_noload)`, apportioned into B3–B8 by local η·γ̇²·V with γ̇ = πDN/h, and a fixed share for solids conveying in B1–B2. The split always sums back to P_mech (checked in the tests).

Viscosity: `η = K·exp[(E_a/R)(1/T − 1/T_ref)]·γ̇^(n−1)`. κ scales K and is learned from the amps residual.

Degradation index: `H = Σ τ_i·exp[−(E_a,deg/R)(1/T_m,i − 1/T_ref,deg)]` over the melt path — relative stabilizer consumption; drives HCl release, screen-changer corrosion, drool propensity.

Fighting loops: `P_fight = Σ_barrel min(MV_i·P_i, UA_oil,i·(T_b,i − T_oil_b)⁺)`. Waste in watts, integrated to kWh. Also a diagnostic: when the two loops fight, PV reads on setpoint while the melt goes wherever the imbalance sends it.

## Slice 1 — the ensemble Kalman filter

`--estimator enkf`. A 41-element state vector `[T_m(18), T_b(18), T_oil_b, T_oil_s, T_s, ln κ, ln hw]` carried as an ensemble (default 32 members). Each member is pushed through the same physics, then corrected against every PV in the core, the adapter probe when present, and motor mechanical power. Motor power is nonlinear in κ and T_m, so it is handled by sampling rather than by a Jacobian.

Two things this buys over fixed gains:

- κ and hw become augmented states with proper uncertainty instead of hand-tuned learning rates. In validation the EnKF drives hw from 1.00 toward the simulator's true 1.20 wall-conductance ratio; the fixed-gain corrector never moves it.
- The melt between probes carries a real ±. `spread_AD` and `spread_barrel` are logged every sweep, and the corpus signature's *model* confidence register is derived from it. The three-register model now has a number under it rather than a placeholder.

The residual bookkeeping is identical to the fixed-gain path, so every detector works unchanged against either.

## Slice 2 — the wired I/O layer

`--config config.json`. Four input paths, merged per channel with priority bus > analog > ops > manual, and a `live` flag recorded per channel so the twin knows what it is running on:

- **PXRBus** — the 18 controllers. One function-04 read of registers 31001–31010 per zone covers PV, SV, DV, MV1, MV2, alarm bits, input-abnormal bits and heater current. Decimal places from 41020 (P-dP) and the °C/°F setting from 41017 (P-F) are read once at startup, so a Fahrenheit-configured zone is converted rather than fed to the model as Celsius. A dead station returns NaN and raises a bus fault instead of stopping the sweep.
- **ModbusDevice** — any other register, described in the config, not in code: VFD amps and Hz, the adapter melt-T readout, the two TCU supply temperatures. Each entry gives port, station, function code, register, type, scale and unit.
- **AnalogSerial** — a microcontroller on USB sending `key=value,key=value` lines: tower and plant-air thermistors, the feed-jacket thermistor, a 4–20 mA melt-pressure loop, and contact states for fans, TCU valves and the drive contactor. Goes to a `stale` flag if the line stops.
- **OpsFile** — a JSON file the PWA or a person writes: adapter temperature read off the display, needle-gauge pressure, recipe id, notes. Marked stale after 30 minutes.

PXR MODBUS facts used (Fuji manual INP-TN512642d-E): 9600 bps fixed, 8 data bits, 1 stop bit, parity per CoM; station numbers 1–255 with 0 disabling comms; up to 31 units per line; PXR5/PXR9 RS-485 on terminals ① and ②; relative address = lower four digits − 1 (31001 → 1000); ≥ 10 ms silence between frames, unit replies in 1–30 ms. Eighteen controllers at one 10-word read each is roughly a 1–1.5 s sweep, and the loop uses the real sweep time as its step.

The register decoding is unit-tested against a fake instrument, including the signed-value and Fahrenheit paths.

## Slice 3 — the CIAER+ corpus bridge

`--corpus DIR`. Every alarm is written to `signatures.jsonl` with the residual vectors, the twin state, the inputs, and three confidence registers computed at that moment:

- *measurement* — the fraction of channels that were live rather than manual or stale.
- *model* — from the estimator's melt spread (1/(1+mean σ)).
- *detector* — how far past threshold the detector was.

The nearest labeled cases are attached to the alarm line as it prints, so a new alarm arrives already saying "closest cases: …". `label` walks the Socratic chain — Cause, Intuition (what you noticed before you knew), Action, Effect, Result, Shadow actions, confidence, tags — one signature at a time, showing the nearest cases first so recall is prompted rather than invented. `export` merges signature and label in CIAER+ field order (`cause, intuition, action, effect, result, shadow_actions, iar`) for the Pydantic module; `iar` is left null for your scorer.

The matcher is cosine similarity on normalized residual vectors plus verdict and template one-hots plus direction. An identical residual signature matches at 1.00; a different zone and verdict drops to 0.60. So the corpus does what the elicitation sessions did — it turns a residual pattern into a cause — but at machine speed and with the pattern attached to the words.

## Slice 4 — the predictive-coding core partition

`--arch cores`. The same physics, split by anatomy:

| core | zones | owns |
|---|---|---|
| barrel | B1–B8 | the drive, the barrel oil loop, the screw core, κ, the contact watch |
| screen | SC1, SC2, AD | the adapter probe |
| die | D1–D7 | the lip estimate, the lateral skew template |

Each core predicts its own zones from its own measurements and corrects locally. Cores exchange only three things, all send-on-delta: a boundary melt temperature to the core downstream when it has moved more than `--delta`; the components of their residual vectors that moved more than `--delta-res`; and alarms. A council runs the cross-core templates (whole barrel, gradient, die, die skew) and the cross-modal check on whatever it last received, and stays quiet about anything a core already reported.

The point is the traffic, and the traffic is measured:

| delta / delta-res | inter-core scalars | % of raw samples | events detected | melt RMSE, unprobed |
|---|---|---|---|---|
| 0.0 / 0.000 | 153,670 | 94 % | 4/4 | 1.93 °C |
| 0.1 / 0.005 | 122,248 | 75 % | 4/4 | 1.93 °C |
| 0.3 / 0.020 | 72,006 | 44 % | 4/4 | 1.93 °C |
| 1.0 / 0.050 | 42,590 | 26 % | 4/4 | 1.93 °C |
| 3.0 / 0.150 | 6,207 | 4 % | 4/4 | 1.98 °C |

Ninety-four percent of the wire down to four percent, all four injected events still caught, and 0.05 °C of accuracy given up. That is the sparse-coding argument with a number on it. Off-box events run 3.2 % of raw samples independently of the partition.

## Running a phase ahead

`--horizon 600`. Every minute the twin takes its corrected state and rolls it forward ten
minutes with no corrections at all, holding the exogenous inputs where they are. When real
time catches up, the projection is settled against what actually happened.

The rollout is possible because `mv_expected` already exists. There is no measured heater
duty in the future — MV is what the controllers are going to decide — so the efference copy
becomes the controller model and closes on the projected metal temperature instead of the
measured one.

**A projection is conditional, and the condition is recorded with it.** It says where the melt
goes *if nothing changes*: same screw speed, same setpoints, same product. When it settles it
is either

- **retired** — somebody moved rpm or a setpoint, so the projection is invalid rather than
  wrong. Retired immediately, and never scored. Without this the twin would be graded on its
  ability to predict operator behaviour, which it cannot do and should not try to.
- **scored** — the condition held, so the gap is the model's own mistake, integrated over the
  horizon.

### One rule, and it is the whole design

> **A filter residual is an observation of the state. A forecast error is a score of the
> model.** They go to different places.

Feeding forecast error back into the estimator would put two loops with different time
constants on the same κ and hw, and they would fight. This is the same rule the oil-flow work
arrived at — measured quantities go into the estimator, assumed ones stay in the detectors.

So **nothing here is tuned back into anything.** Bias is measured and reported; the plant
constants and the estimator are untouched. The test suite enforces it: every column the log
carried before the phase-ahead layer is byte-identical with it switched on.

### The error bar is centred on the bias

The band is the spread of past errors **about their own mean**, not about zero. Taking the
percentile of `|error|` instead would fold a constant bias into the width of the band, so a
twin reliably 2 °C cold would carry a 2 °C band and never trip anything — two mistakes
cancelling to make both invisible. Centred, the same case reads as bias −2.0 with a band of
0.2: precise, and wrong by a fixed amount. That is a commissioning finding, not a shift alarm.

It is also the only uncertainty estimate in the twin that can see **model-form error**. The
ensemble spread cannot, by construction: it only knows about uncertainty the model already
represents. On the simulator the adapter melt comes out at bias −1.6 to −1.8 °C with a centred
band of 0.3–0.6 °C at horizons of 300–600 s.

### What it actually did

Measured, not asserted. `--horizon` on the standard 4,200 s harness:

| | existing detectors | phase-ahead |
|---|---|---|
| denser feed at the crammer | 6 s | — |
| viscosity front | **192 s** (`cross`) | 270 s |
| contactor chatter | 31 s | — |
| tower water 30 → 55 °C | 1,111 s | — |
| D3 loses its heater | 15 s | — |
| barrel loop flow loss | 35 s | — |
| alarms outside any event window | 0 | **0** |

**No latency improvement on any of the six, and no false alarms.** On the viscosity front it
fires later than the cross-modal check and earlier than κ — corroboration, not speed.

The reason is visible in the arithmetic: the first settlement lands at warm-up plus horizon,
and the band needs a dozen of them. At a 600 s horizon that is t ≈ 1,500 s, which is when the
events start. **The layer is calibrating during the events it is meant to be measured against.**

Given a quiet run-in first (`SimPlant(..., event_offset=5400)`), the picture changes on exactly
the event predicted:

| | existing detectors | phase-ahead |
|---|---|---|
| tower water 30 → 55 °C | **missed** | **540 s**, on the oil loop |
| viscosity front | 179 s (`cross`) | 300 s |
| alarms outside any event window | 0 | **0** |

A slow barrel-wide drift is what this channel is for, and it is the one thing the existing
banks are worst at: over a long quiet stretch their adaptive baselines absorb the drift and
stop seeing it, while a forecast error integrates the same mismatch over the horizon.

**Three caveats on those numbers, in order of how much they should bother you.**

1. The simulator shares the twin's equation structure, so this measures parameter and
   disturbance robustness — *not* model-form error, which is precisely what the empirical band
   exists to capture. These numbers are optimistic about the thing the layer is best at.
2. The tower-water result is not apples-to-apples with the row above it: the existing detector
   misses it in the shifted run partly *because* of the long quiet period, so the comparison
   flatters the new channel as well as indicting the old one.
3. The layer needs a quiet stretch to learn its own error before it can flag anything. On a
   shift that is free; in a short test it is most of the test.

### What it is actually for

Not knowing a temperature early for its own sake:

- **`H_index` is cumulative and monotone**, so projecting it forward turns it into a clock —
  *hold at this condition and the stabilizer budget crosses `[PLANT: threshold]` in 40 minutes.*
  That is a computed answer to the "how long may it sit hot after an emergency stop" question
  instead of a guessed one.
- **Startup** — when soak is reached, and which zone is still short when it is.
- **Drool at the lip**, the same shape.

### Cost, and the honest ceiling

A rollout is `horizon/dt` extra physics steps. The fixed-gain path does about 3,100 steps/s, so
a ten-minute projection is roughly 0.2 s — affordable every minute against a 1.5 s bus sweep.
Rolling the whole ensemble instead of the mean is ~32× that and buys less than the empirical
band does, for the reason above.

Two limits worth knowing before trusting a long horizon. The useful horizon is capped by the
slowest *exogenous* input, not by the physics: if rpm moves every three minutes, most ten-minute
projections retire rather than score. And past one or two thermal time constants the rollout
converges to the model's own steady state, at which point it is describing the model rather than
the machine.

Error growth here is roughly **linear in bias, not exponential** — thermal diffusion is
dissipative, so this is not a chaos problem. That is exactly why forecast error is a good
estimator of bias, and exactly why it must not be fed back into the filter that is already
estimating some of it.

## Validation

`python -m extrusion_twin.validate` runs each configuration against the simulator's hidden truth. The simulator is a second plant with different constants (hA_w 1.2×, UA_oil 0.9×, a fouled exchanger at UA_hx 250 vs the twin's 600), its own PI loops, and four injected events. RMSE is split into probed zones (near the adapter) and unprobed zones — the barrel, where nothing measures the melt and the twin is entirely on its own. Over 4,200 s at dt = 1 s:

| configuration | melt RMSE, unprobed | κ MAE | detections | alarms outside any event window |
|---|---|---|---|---|
| mono / fixed | 1.88 °C | 0.068 | 6/6 | 0 |
| mono / enkf | 0.88 °C | 0.092 | 6/6 | 0 |
| cores / fixed | 1.94 °C | 0.069 | 6/6 | 0 |
| cores / enkf | **0.87 °C** | **0.025** | 6/6 | 1 |

Detection latencies (cores / enkf): denser feed at the crammer 6 s, viscosity front 204 s, contactor
chatter 31 s, tower water 517 s, lost heater element 15 s, barrel loop flow loss 35 s. The first two are
the same physical event seen at two places — the crammer meets the new material three minutes before the
barrel does. The bias in the unprobed zones is what separates the two estimators: +0.91 °C for fixed gains, +0.03 °C for the EnKF.

Two honest limits on those numbers. They are the twin scored against a plant that shares its equation structure, so they measure parameter and disturbance robustness, not model-form error — the real line will have physics this model does not contain. And the 591 s latency on tower water is a genuine weakness: with no tower thermistor the only evidence is a slow barrel-wide drift, which is exactly why that sensor is second on the list below.

## Commissioning path

1. **Nameplates first.** Screw diameter and channel depths from the screw drawing, gearbox ratio, heater watts per zone, line voltage, motor no-load amps. These are not fits.
2. **Read-only bus run.** Set each PXR's STno, CoM and PCoL, run `--config` with everything else manual, and watch the residuals for a shift. No writes to the controllers — the manual warns the EEPROM is good for 100,000 writes and this twin never writes to it.
3. **Two reversal tests.** An RPM step of ±5 rpm held for three residence times fits `kW_per_amp`, `k_pump` and κ. A single barrel-zone setpoint step of +5 °C held until the adapter probe settles fits `hA_w` and `UA_oil` for that zone and the propagation lag. Repeat the second on one die zone.
4. **Instrumentation, in order of value.** A melt-pressure transducer with a 4–20 mA output at the adapter closes the η × flow check; a second upstream of the screens gives ΔP across the screen changer, which is a screen-change predictor and therefore a downtime lever. Then the tower-supply and plant-air thermistors, which turn afternoon drift from "recipe" into "weather". Then confirm whether the adapter readout and the two TCU controllers are Modbus-capable — if PXR-class they drop onto the same bus with their own station numbers. Then fan, valve and contactor aux contacts for the binary layer.
5. **Corpus from day one.** Run with `--corpus` from the first shift. Signatures accumulate whether or not anyone labels them; labeling can happen later, and the nearest-case matcher improves with every label.

## IP separation

Three objects, three files, from day one:

- **portable** — the physics structure, the ingredient property model, the detector bank, the corpus schema. This is `src/extrusion_twin/` itself, and it is generalizable expertise.
- **plant-specific** — `config.json`: fitted constants, station map, register addresses, nameplate values. the plant owner's.
- **operational** — `twin_log.csv`, `signatures.jsonl`, `labels.jsonl`. the plant owner's process data, and the contested zone.

Nothing in the package hardcodes a fitted constant that belongs in the config, so the split is enforceable rather than merely intended.

**The licence is deliberately not chosen yet** — see `LICENSE`. The portable half is worth
licensing on purpose rather than by default, and the repository is private until that is
decided.

## What is still open

- Model-form error is untested. The simulator shares the twin's equations. First contact with the real line will show what the 1-D lumped model omits — most likely die flow distribution, which is currently seven lumped zones with no lateral coupling except a skew template.
- The phase-ahead layer measures its own bias and does not act on it. Turning that into
  parameter trimming is the obvious next step and deliberately not taken yet: the loop should
  be watched on a real line for a while before it is given authority over anything.
- Melt pressure is in the state vector and read by the I/O layer but is not yet an EnKF observation. Once a transducer exists, adding it is one entry in the observation list and it will constrain κ far better than amps alone.
- No control action. The twin advises and does not write. That is the right place to stop until the corpus has enough labeled cases to justify a closed loop, and the PXR EEPROM write limit means any future control path should go through the drive or the TCUs, not the zone setpoints.
