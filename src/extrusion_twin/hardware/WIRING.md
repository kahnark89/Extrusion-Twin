# Wiring 18 PXR-9s to a Raspberry Pi in the panel

This is the physical half of the twin. Nothing here writes to a controller: the Pi is a listener on the
bus, and the twin has no control path by design.

Before anything else: this is a 480 V panel. The RS-485 work is low voltage, but it happens inside an
enclosure that is not, and it should be done by whoever is qualified to open that panel, with the line
locked out. Nothing in this document changes that.

---

## 1. The bus

All 18 controllers share one twisted pair, daisy-chained. Not a star, not a home run per controller —
one continuous pair that walks from the Pi to the first controller, to the second, and so on to the last.

```
Pi + isolated RS-485 adapter
        │
      [120 Ω]                          ┌── controller n, in and out on the same terminals
        │                              │
   ─────┴──────●───────●───────●── … ──●────[120 Ω]
   twisted pair, shielded
   shield grounded at the Pi end ONLY
```

**Terminals.** On the PXR5/PXR9 the RS-485 pair lands on terminals ① and ②. Which of the two is the
non-inverting side is printed on the controller's terminal legend — check it rather than trusting a
diagram. If the bus comes up silent, swap the pair at the Pi and try again. It costs nothing, it damages
nothing, and reversed polarity is the most common reason a new RS-485 run says nothing at all.

**Cable.** Shielded twisted pair with roughly 120 Ω characteristic impedance. Belden 9841 or 3106A, or
any equivalent RS-485 cable. One pair is enough; a cable with a pair plus a drain is ideal. Do not use
spare conductors in a multiconductor cable that also carries anything switched.

**Termination.** 120 Ω across the pair at each physical end of the chain — one at the Pi, one at the last
controller. Two resistors total, not eighteen. Most RS-485 HATs have a jumper or switch for the one at
their end; use it rather than adding a loose resistor.

**Biasing.** The line needs to idle at a defined state or the first byte of every reply gets chewed. Good
HATs include fail-safe bias resistors. If yours does not, a 560 Ω pull-up on the non-inverting line and a
560 Ω pull-down on the inverting line, at one point on the bus only.

**Signal common.** With an isolated adapter you can usually run the pair alone. If the bus is marginal,
run a third conductor as signal common between the adapter's ground and the controllers' common. Never
use the panel's safety ground as the signal return.

**Routing.** Keep the pair out of the wireway that carries heater leads and SSR output wiring. If it has
to cross power wiring, cross at ninety degrees. An extruder panel with eighteen SSRs chopping current is
a hostile place for a differential pair, and most flaky RS-485 installations are really routing problems.

**Length and count.** The PXR line supports up to 31 units; eighteen is comfortable. Total run at
9600 bps can be well over a thousand feet, so panel-internal distance is not a constraint.

---

## 2. The controllers

Each PXR needs three settings, entered from its front panel. Set them once, write down what you used, and
change nothing else.

| setting | value | why |
|---|---|---|
| `STno` | 1 through 18, in zone order B1…D7 | The station number. 0 disables communication entirely. |
| `PCoL` | Modbus RTU | The PXR can also speak a Fuji ASCII protocol. The twin expects Modbus. |
| `CoM`  | parity, matched across all 18 | Whatever you pick, every controller and the console must agree. |

Baud rate is fixed at 9600 on the PXR and is not adjustable — that is a property of the instrument, not
a choice. Eight data bits, one stop bit.

Give the stations out in zone order and write the map on the inside of the panel door. When station 12
stops answering at three in the morning, you want to read which zone that is off a label rather than
count terminal blocks.

---

## 3. The Pi

**Which one.** A Pi 4 (2 GB is plenty) if you want the ensemble filter running continuously. A Pi Zero 2 W
handles the fixed-gain estimator and the console fine. Either one is far more computer than an 18-zone
sweep at 9600 bps needs; the reason to size up is the filter, not the bus.

**RS-485 adapter.** Use an isolated one. A Waveshare RS485 CAN HAT (isolated version), an isolated USB
RS-485 dongle, or any DIN-mount isolated converter. Isolation is not optional in a panel with a VFD and
eighteen SSRs — it is what keeps a ground fault on the bus from finding the Pi.

**Power.** A DIN-rail regulated supply feeding 5 V at 3 A, fused. Not a phone charger zip-tied to a
wireway. Brownouts corrupt SD cards, and a Pi that silently stops logging is worse than no Pi.

**Storage.** Boot from a USB SSD if you can. If it has to be an SD card, use an industrial-grade one and
install `log2ram` so the system log is not writing to flash every few seconds. SD corruption is the
single most common way a panel-mounted Pi dies.

**Heat.** Panel interior temperature next to an extruder can pass what a Pi is rated for. Mount it low, in
the coolest part of the enclosure, away from heater terminal blocks — or in its own small enclosure on the
outside of the panel with the RS-485 pair and 24 V brought out through a gland. The second option is
better and costs an hour.

---

## 3b. The crammer drive

The crammer is a second machine with its own VFD, and its load is a different measurement from the
extruder's. Extruder amps say how hard the screw is working the melt — that is where the viscosity index
comes from. Crammer amps say how hard the feeder is working to stuff material into the throat. Feeding
one into the other's channel makes the viscosity index meaningless, so the twin keeps them as separate
inputs and never mixes them.

Worth wiring because it measures the one thing the twin otherwise only infers. The drift detectors can say
*this started at the feed end*; the crammer can say whether anything actually changed at the feed end. And
because the crammer meets new material at the throat before any of it reaches the barrel, it sees a feed
change first. In the simulated run a bulk-density change is caught at the crammer in **11 seconds**; the
viscosity index, working from motor amps, needs **197 seconds** to see the same event arrive in the barrel.

### First choice: read the drive, not the gauges

The crammer VFD almost certainly has an RS-485 port. If it does, read its output current and output
frequency registers directly and leave the panel meters alone. No analog wiring, no loading of an existing
circuit, no calibration to get wrong. Put it on the same drive bus as the extruder VFD with its own
station number:

```json
{"name": "crammer_amps", "port": "COM4", "station": 2, "fc": 3, "register": 0,
 "type": "u16", "scale": 0.1, "unit": "A", "maps_to": "crammer_amps"},
{"name": "crammer_hz",   "port": "COM4", "station": 2, "fc": 3, "register": 1,
 "type": "u16", "scale": 0.01, "unit": "Hz", "maps_to": "crammer_hz"}
```

Register numbers and scaling come from that drive's manual — every manufacturer numbers them differently,
and a wrong scale is the difference between 12 A and 120 A. Give `crammer_rpm_per_hz` and
`crammer_gear_ratio` in the config and the twin converts frequency to feeder rpm itself.

### Second choice: tap the gauge signals

Only if the drive has no comms port. What you tap depends on what drives each meter, and one of the four
cases is genuinely dangerous.

| the gauge is driven by | how to tap it | watch out |
|---|---|---|
| 0–10 V analog output | Parallel a high-impedance ADC input across the meter. Divide 10 V down to the ADC range and use a differential or isolated input. | A single-ended ADC referenced to a different ground reads the ground offset as signal. Isolate. |
| 4–20 mA loop | It is a series loop, so you cannot parallel it. Put a 100 Ω precision resistor in series and read 0.4–2.0 V across it, or use a loop splitter. | Breaking the loop momentarily just blanks the meter. Not dangerous, but do it with the drive stopped. |
| A current transformer on a motor lead | **Do not touch the existing CT.** Clamp a second CT on the same conductor and read that. | An open CT secondary with the motor running develops lethal voltage. This is the one that hurts people. There is no version of this worth doing live. |
| A tachometer or encoder | Read the pulse train on a GPIO with a level shifter, or take the drive's frequency output instead. | Panel noise on a pulse input gives phantom rpm. Use a Schmitt input or an opto. |

For analog, an ADS1115 on the Pi's I²C bus is the usual answer — sixteen bits, four channels, differential
pairs, and a programmable range. Two channels covers crammer amps and rpm with two to spare. Feed it
through an isolated I²C bridge if the signal ground is anywhere near the drive's.

Whichever route you take, calibrate against the gauges once. Run the crammer at two speeds, write down what
each meter reads and what the twin logged, and set the scale from the pair. A signal you cannot check
against a face you already trust is not worth having.

### What it gives you

Three things the twin can say once the crammer is wired, none of which it could say before:

- **Load rising** against its own baseline: denser or coarser material at the throat. This is the early
  warning, minutes ahead of anything the barrel can show you.
- **Load falling**: the throat is starving, or the hopper is running out.
- **Load swinging without settling**: bridging or rat-holing. The level stays put while the load moves
  around, which is a different signature from either of the above and gets its own verdict.

It also tracks crammer load against extruder load as a ratio, which is a fill-consistency number: two
signals that normally move together, so when they separate something has changed between the throat and
the screw.

If nothing is wired to it, the crammer watcher stays quiet and everything else works as before.

---

## 3c. The TCU loops and the driveline

Four temperatures and one speed, and the cooling side stops being a guess.

### What to fit

| signal | where | why |
|---|---|---|
| barrel oil supply | in the line leaving the TCU, before it reaches the barrel | with the return it gives the loop's real duty |
| barrel oil return | in the line coming back, before the TCU | |
| screw oil supply and return | the same two points on the screw TCU | the screw loop has no other instrument at all |
| tower water return | leaving the shell-and-tube exchanger | with tower supply, this is the fouling number |
| motor speed | the drive's own speed feedback, or a tach on the motor | against screw rpm it watches the coupling and gearbox |
| loop flow *(optional)* | either oil line | turns a delta into watts, and see below for what that unlocks |

Use RTDs in thermowells, not clamp-on sensors. A clamp-on reads pipe wall through insulation and lags the
oil by minutes, which destroys exactly the delta you are trying to measure. Pt100 or Pt1000 in a short
thermowell, both ends the same type, both read by the same converter — matched sensors matter more than
absolute accuracy here, because what you want is the difference between two numbers.

### What the twin does with them

The delta gives the loop's duty directly: **Q = ṁ · c_p · (return − supply)**. That is a measurement of
the heat the barrel is actually shedding, where before the twin had a modelled conductance and a guessed
exchanger. Four things separate cleanly:

- **Supply climbing, delta holding** — the cold side has given up. Tower water, tower fan, a fouling
  exchanger. The loop is still moving heat; it has nowhere to put it. In validation this cuts the tower
  event's detection from roughly ten minutes to a few.
- **Delta up, and the twin's own model does not agree that more heat is arriving** — the flow assumption
  is what is wrong. Pump, valve, air lock, blocked strainer. The alarm estimates the flow that would
  explain the reading.
- **Delta up, and the model agrees** — genuinely more heat in the oil. On the barrel loop that means
  shear, not bands.
- **Delta down with the model agreeing** — less heat arriving. Usually the line slowing.

One thing that trips people, and that the code gets right: **losing flow makes the delta go up, not
down.** The same watts carried by less oil. A detector that treats a rising delta as "more heat" will
call a failing pump a process change. That is why the loop watcher cross-checks against the twin's own
predicted cooling instead of reading the delta on its own.

### The flow meter, and why it is worth more than it looks

Without a flow meter, the twin computes watts from the nameplate pump rate. That is fine for a detector,
which compares a reading against its own history. It is not fine for the state estimator: feed a guessed
flow into the filter and a real flow change gets absorbed into a learned parameter, which then quietly
bends every zone that touches the oil. So the rule in the code is that measured quantities go into the
filter and assumed ones stay in the detectors, where a wrong assumption raises an alarm instead of
corrupting a constant.

With a real flow meter the loop's heat flow becomes a proper observation, and the barrel-to-oil
conductance becomes identifiable. In validation the twin recovers it as **0.90** against the simulator's
true 0.90, from an initial guess of 1.00 — a constant that otherwise has to come from a reversal test.
Without a meter the twin holds it at 1.0 and says so.

An honest limit on that: during the injected flow-loss event the estimate degrades even with a meter,
because the oil-loop model has no flow term in it at all. Putting flow into that model is the obvious next
piece of physics, and until it exists, treat the learned conductance as good under steady flow and
suspect during an upset.

### The driveline

Motor speed against screw speed is a fixed mechanical ratio, so it should not move. When it does,
something between the motor and the screw is slipping — coupling, belt, or a gearbox on its way out. It
also catches a drive commanding one speed and delivering another. The twin holds `rpm` as the screw and
`rpm_motor` as the motor, and never confuses them: shear rate is computed from screw speed.

---

## 4. Network: the hotspot-name scheme

The idea is sound: the Pi carries no WiFi of its own and simply waits for a network with a particular name
to appear. Whoever is authorized turns on their phone hotspot, names it that, and the Pi joins them.
Nothing on the plant network, no IT ticket, no static addressing.

**One correction, and it matters.** The network *name* is not the credential. Anyone standing in the
parking lot can name a hotspot `PANEL-LINK`. What actually decides whether the Pi joins is the WPA2
passphrase, and that is the thing to treat as the key. So:

- Generate a long random passphrase once. Not the machine number, not the plant name.
- Everyone authorized configures a hotspot with that exact name **and** that exact passphrase.
- When someone leaves the authorized list, change the passphrase on the Pi and reissue it. There is no
  way to revoke one person without doing that, which is the honest limit of a shared-secret scheme.
- A hotspot with the right name and the wrong passphrase gets nothing. A hotspot with both is
  indistinguishable from yours — that is the residual risk, and it is why the console has its own key.

**The console key.** Because the Pi binds to the network so the phone can reach it, anything else on that
hotspot can reach it too. Start the console with `--token` and it generates a key, prints it, and keeps it
in the data folder. The address becomes `http://…:8000/?token=…`; the browser stores it after the first
visit, so it is pasted once per device. Two independent secrets — the WiFi passphrase and the console
key — is the right shape here, because they fail differently.

**Finding the Pi.** The hotspot hands out an address by DHCP, so it will not be the same every time. Two
ways to not care:

- `avahi-daemon` is installed by the setup script, so the Pi answers to `extrusion-twin.local`. Try
  `http://extrusion-twin.local:8000/` first. Chrome on Android resolves this most of the time.
- If that fails, most phones list connected devices in the hotspot settings, with addresses.

**Fallback so you are never locked out.** The setup script can also configure the Pi to raise its *own*
access point if it sees no known network for three minutes. Then the address is always
`http://192.168.50.1:8000/`, whatever else has happened. Worth doing — the failure mode it prevents is
standing at the panel unable to reach a computer that is eighteen inches away.

---

## 5. Bringing it up

```bash
# on the Pi, as a normal user
sudo bash setup_pi.sh --ssid PANEL-LINK --pass 'the-long-random-one' --ap-fallback
```

The script installs Python and the twin's dependencies, writes the WiFi configuration, installs
`avahi-daemon` so `extrusion-twin.local` works, optionally sets up the access-point fallback, and installs a
systemd service so the console comes up on boot and restarts if it ever dies.

Then, before trusting anything:

1. `python -m extrusion_twin.tests` on the Pi. Seventy checks, no hardware needed. If they pass, the software
   made the trip intact.
2. Open the console, choose **Read the real line**, put in `/dev/ttyUSB0` (or `/dev/ttySC0` for a HAT),
   stations `1-18`, the parity you set on the controllers, and start a short run.
3. Watch the first sweep. Every zone answering is the whole test. A station that does not answer shows up
   immediately as a bus fault naming the station number.
4. Only then do the two reversal tests in the main README, which are what turn the placeholder constants
   into your machine's constants.

---

## 6. What to check when it does not work

| symptom | first thing to try |
|---|---|
| Nothing answers at all | Swap the pair at the Pi. Then confirm `PCoL` is Modbus RTU on at least one controller. |
| One station never answers | Its `STno`, or a loose terminal on that drop. The fault message names the station. |
| Answers, but garbage or intermittent | Termination — exactly two resistors, at the two ends. Then routing away from SSR wiring. |
| Works cold, fails once the line runs | Almost always noise coupling. Check the shield is grounded at one end only, and re-route the pair. |
| Fine for days, then the Pi is gone | Power or SD card. Check the supply first, then move to USB boot. |
| Temperatures read about 1.8× too high | A controller set to °F that the startup read missed. Restart the run so the units are re-read. |
