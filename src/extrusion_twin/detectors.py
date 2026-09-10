"""
detectors.py -- the fly circuit on the residuals.

Bank          one detector bank over a residual vector: bias removal, ON/OFF sign split,
              adjacent-zone lag correlators (direction), wide-field templates and single-zone
              detectors with threshold + refractory. T4/T5 pairs -> HS/VS cells -> spike.
CrossModal    barrel heater residual vs motor-power residual: opposite signs cancel (benign
              rebalancing), same sign reinforces (real thermal-mechanical drift).
HeaterCheck   CT current vs duty-weighted expectation: a lost element is a step in the ratio.
KappaAlarm    viscosity multiplier drifting away from its calibration.
EventEmitter  send-on-delta: a channel only speaks when it leaves its band.
Detectors     facade wiring all of the above for one twin (mono) or one core.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Dict, List, Optional

from .model import ADAPTER, BARREL, DIE, N, SCREEN, ZONE_NAMES, Plant, loop_watts


def default_templates(zones: List[int]) -> Dict[str, List[float]]:
    """wide-field templates restricted to the zones this bank owns"""
    zs = set(zones)
    t = {}
    if zs & set(BARREL):
        t["barrel"] = [1.0 if i in zs and i in BARREL else 0.0 for i in range(N)]
    if zs & set(SCREEN + [ADAPTER]):
        t["screen_adapter"] = [1.0 if i in zs and i in SCREEN + [ADAPTER] else 0.0 for i in range(N)]
    if zs & set(DIE):
        t["die"] = [1.0 if i in zs and i in DIE else 0.0 for i in range(N)]
    if len(zs & set(BARREL)) >= 6:
        t["gradient"] = [(-1.0 if i in BARREL[:4] else 1.0 if i in BARREL[4:] else 0.0) if i in zs else 0.0
                         for i in range(N)]
    if len(zs & set(DIE)) >= 6:                                   # lateral: left half vs right half of the die
        t["die_skew"] = [(-1.0 if i in DIE[:3] else 1.0 if i in DIE[4:] else 0.0) if i in zs else 0.0
                         for i in range(N)]
    return t


class Bank:
    def __init__(self, name: str, dt: float, theta: float, zones: Optional[List[int]] = None,
                 templates: Optional[Dict[str, List[float]]] = None, tau_bias=7200.0, tau_int=120.0,
                 refractory=600.0, warmup=600.0, lag_factor=3.0):
        self.name, self.dt, self.theta = name, dt, theta
        self.zones = list(range(N)) if zones is None else list(zones)
        self.a_bias_slow, self.a_bias_fast = dt / tau_bias, dt / 120.0
        self.a_int = dt / tau_int
        self.refractory, self.warmup, self.lag_factor = refractory, warmup, lag_factor
        self.bias = [0.0] * N
        self.hist = [deque(maxlen=600) for _ in range(N)]
        self.templates = templates if templates is not None else default_templates(self.zones)
        self.M = {k: 0.0 for k in self.templates}
        self.L = [0.0] * N
        self.Dint, self.Pint = [0.0] * (N - 1), [0.0] * (N - 1)
        self.last_alarm = {k: -1e9 for k in self.templates}
        self.last_local = [-1e9] * N
        self.pairs = [i for i in range(N - 1) if i in self.zones and i + 1 in self.zones]

    def direction(self, zones) -> float:
        pairs = [i for i in self.pairs if i in zones and i + 1 in zones]
        d = sum(self.Dint[i] for i in pairs)
        s = sum(self.Pint[i] for i in pairs)
        return d / s if s > 1e-12 else 0.0

    @staticmethod
    def verdict(direction: float) -> str:
        return ("downstream-propagating (feed/material side)" if direction > 0.3 else
                "upstream-propagating (die/back-pressure side)" if direction < -0.3 else
                "simultaneous (electrical/control/cooling side)")

    def update(self, e: List[float], residence: List[float], t: float, global_active: bool = False) -> dict:
        ep = [0.0] * N
        a_bias = self.a_bias_fast if t < self.warmup else self.a_bias_slow
        for i in self.zones:
            self.bias[i] += a_bias * (e[i] - self.bias[i])
            ep[i] = e[i] - self.bias[i]
            self.hist[i].append(ep[i])
        pos = [max(x, 0.0) for x in ep]                       # ON channel
        neg = [max(-x, 0.0) for x in ep]                      # OFF channel
        # elementary drift detectors on adjacent pairs: high-pass over one lag, then correlate the
        # upstream zone's delayed change with the downstream zone's current change.
        for i in self.pairs:
            lag = max(1, int(round(self.lag_factor * residence[i] / self.dt)))
            h0, h1 = self.hist[i], self.hist[i + 1]
            if len(h0) > 2 * lag:
                d0_now, d0_then = h0[-1] - h0[-1 - lag], h0[-1 - lag] - h0[-1 - 2 * lag]
                d1_now, d1_then = h1[-1] - h1[-1 - lag], h1[-1 - lag] - h1[-1 - 2 * lag]
                D = d0_then * d1_now - d1_then * d0_now
                P = abs(d0_then * d1_now) + abs(d1_then * d0_now)
                self.Dint[i] += self.a_int * (D - self.Dint[i])
                self.Pint[i] += self.a_int * (P - self.Pint[i])
        alarms, armed = [], t > self.warmup
        for k, w in self.templates.items():
            x = sum(w[i] * ep[i] for i in range(N)) / (sum(abs(v) for v in w) or 1.0)
            self.M[k] += self.a_int * (x - self.M[k])
            if armed and abs(self.M[k]) > self.theta and t - self.last_alarm[k] > self.refractory:
                self.last_alarm[k] = t
                zones = [i for i in range(N) if w[i] != 0.0]
                d = self.direction(zones)
                alarms.append(dict(kind="template", bank=self.name, template=k, value=self.M[k], direction=d,
                                   verdict=self.verdict(d), zones=zones,
                                   text=f"[{self.name}] {k} {self.M[k]:+.3f} dir {d:+.2f} {self.verdict(d)}"))
        # single zone moving on its own: band, SSR, thermocouple, or local cooling. A template or an
        # active global drift that already explains the zone wins (surround suppression).
        for i in self.zones:
            self.L[i] += self.a_int * (ep[i] - self.L[i])
            explained = global_active or any(abs(self.M[k]) > 0.5 * self.theta and w[i] != 0.0
                                             for k, w in self.templates.items())
            if armed and not explained and abs(self.L[i]) > 2.0 * self.theta and t - self.last_local[i] > self.refractory:
                self.last_local[i] = t
                alarms.append(dict(kind="local", bank=self.name, template="local", zone=ZONE_NAMES[i], value=self.L[i],
                                   direction=0.0, verdict="single-zone (band/SSR/TC/local cooling)", zones=[i],
                                   text=f"[{self.name}] local {ZONE_NAMES[i]} {self.L[i]:+.3f}: single-zone (band/SSR/TC/local cooling)"))
        return dict(ep=ep, pos=pos, neg=neg, direction=self.direction(self.zones), M=dict(self.M), alarms=alarms)


class CrossModal:
    def __init__(self, dt: float, theta=2.0, tau_int=120.0, refractory=600.0, warmup=600.0):
        self.a = dt / tau_int
        self.theta, self.refractory, self.warmup = theta, refractory, warmup
        self.var_e, self.var_k, self.M, self.last = 1e-4, 1e-4, 0.0, -1e9

    def update(self, ep: List[float], r_k: float, t: float) -> Optional[dict]:
        e_bar = sum(ep[i] for i in BARREL) / len(BARREL)
        self.var_e += 0.002 * (e_bar * e_bar - self.var_e)
        self.var_k += 0.002 * (r_k * r_k - self.var_k)
        x = e_bar / math.sqrt(self.var_e + 1e-9) + r_k / math.sqrt(self.var_k + 1e-9)
        self.M += self.a * (x - self.M)
        if t > self.warmup and abs(self.M) > self.theta and t - self.last > self.refractory:
            self.last = t
            return dict(kind="cross", bank="cross", template="heater_vs_motor", value=self.M, direction=0.0,
                        verdict="reinforcing: real thermal-mechanical drift", zones=BARREL,
                        text=f"[cross] heater and motor residuals reinforcing ({self.M:+.2f} sigma): real thermal-mechanical drift")
        return None


class HeaterCheck:
    """CT versus duty-weighted expectation. Only visible while the zone calls for heat (> 1 A)."""

    def __init__(self, p: Plant, zones: Optional[List[int]] = None):
        self.p = p
        self.zones = list(range(N)) if zones is None else list(zones)
        self.hold, self.latched = [1.0] * N, [False] * N

    def update(self, u) -> List[dict]:
        flags = []
        if u.CT is None:
            return flags
        for i in self.zones:
            expected = u.MV[i] * self.p.P_heat[i] / self.p.V_line
            if expected > 1.0:
                self.hold[i] = 0.95 * self.hold[i] + 0.05 * (u.CT[i] / expected)
                if self.hold[i] < 0.8 and not self.latched[i]:
                    self.latched[i] = True
                    flags.append(dict(kind="heater", bank="electrical", template="ct", zone=ZONE_NAMES[i], value=self.hold[i],
                                      direction=0.0, verdict="element or SSR", zones=[i],
                                      text=f"[heater] {ZONE_NAMES[i]} current {self.hold[i]:.0%} of duty-expected: element or SSR"))
                elif self.hold[i] > 0.9 and self.latched[i]:
                    self.latched[i] = False
                    flags.append(dict(kind="heater", bank="electrical", template="ct", zone=ZONE_NAMES[i], value=self.hold[i],
                                      direction=0.0, verdict="recovered", zones=[i],
                                      text=f"[heater] {ZONE_NAMES[i]} current back to {self.hold[i]:.0%} of expected"))
        return flags


class KappaAlarm:
    def __init__(self, warmup=600.0, refractory=600.0, theta=0.05):
        self.warmup, self.refractory, self.theta = warmup, refractory, theta
        self.last, self.at_alarm = -1e9, 0.0

    def update(self, drift_index: float, t: float) -> Optional[dict]:
        if t > self.warmup and abs(drift_index - self.at_alarm) > self.theta and t - self.last > self.refractory:
            self.last, self.at_alarm = t, drift_index
            return dict(kind="kappa", bank="barrel", template="kappa", value=drift_index, direction=0.0,
                        verdict="recipe/plasticizer/wear side", zones=BARREL,
                        text=f"[kappa] viscosity multiplier {drift_index:+.1%} vs calibration: recipe/plasticizer/wear side")
        return None


class ContactWatch:
    """binary layer: a contact only speaks when it changes state; chatter = event rate."""

    def __init__(self, dt: float, chatter_rate=0.05, window=120.0):
        self.dt, self.chatter_rate = dt, chatter_rate
        self.last, self.rate = {}, {}
        self.a = dt / window
        self.flagged = set()

    def update(self, contacts: dict, t: float):
        events, alarms = [], []
        for name, v in contacts.items():
            changed = name in self.last and v != self.last[name]
            self.last[name] = v
            self.rate[name] = self.rate.get(name, 0.0) + self.a * ((1.0 if changed else 0.0) / self.dt - self.rate.get(name, 0.0))
            if changed:
                events.append((f"contact_{name}", v))
            if self.rate[name] > self.chatter_rate and name not in self.flagged:
                self.flagged.add(name)
                alarms.append(dict(kind="contact", bank="electrical", template="chatter", zone=name, value=self.rate[name],
                                   direction=0.0, verdict="contactor/valve chatter", zones=[],
                                   text=f"[contact] {name} changing {self.rate[name]:.2f}/s: chatter"))
            elif self.rate[name] < 0.5 * self.chatter_rate and name in self.flagged:
                self.flagged.discard(name)
        return events, alarms


class FeedWatch:
    """The crammer, watched on its own terms.

    Crammer load is the force needed to stuff the fluff into the throat. It is NOT the extruder
    drive and must never be fed into the viscosity index -- but it measures the one thing the twin
    otherwise only infers. The lag correlators can say "this drift started at the feed end"; the
    crammer can say whether anything actually changed at the feed end, which turns an inference
    into a corroborated call.

    Three things it can say:
      load up      denser or coarser material, or a wetter blend: more work per unit volume
      load down    the throat is starving -- the crammer is turning in a partial void
      erratic      bridging or rat-holing: the load swings instead of sitting

    It also carries `corroborated`, which the feed-side verdict can lean on. The crammer sees new
    material before the barrel does, so this normally fires first.
    """

    def __init__(self, dt: float, warmup=600.0, refractory=600.0, z_load=2.5, z_swing=3.0,
                 tau_base=1800.0, tau_fast=60.0, min_frac=0.02):
        self.dt, self.warmup, self.refractory = dt, warmup, refractory
        self.z_load, self.z_swing, self.min_frac = z_load, z_swing, min_frac
        self.a_base, self.a_fast = dt / tau_base, dt / tau_fast
        self.base = None
        self.var = 1e-6
        self.fast = None
        self.swing, self.swing_base = 0.0, None
        self.last = -1e9
        self.state = "normal"
        self.corroborated = 0.0                       # decays; nonzero means the feed really moved
        self.ratio = None                             # crammer load against extruder load

    def update(self, u, t: float) -> Optional[dict]:
        self.corroborated = max(0.0, self.corroborated - self.dt / 900.0)
        a = u.crammer_amps
        if a is None:
            return None
        if self.base is None:
            self.base = self.fast = a
        self.fast += self.a_fast * (a - self.fast)
        dev = self.fast - self.base
        self.base += self.a_base * (a - self.base)
        self.var += self.a_base * (dev * dev - self.var)
        sd = math.sqrt(max(self.var, 1e-9))
        z = dev / sd
        # swing: how much the fast average is moving around, against its own history
        inst = abs(a - self.fast)
        self.swing += self.a_fast * (inst - self.swing)
        if self.swing_base is None:
            self.swing_base = self.swing
        sb = self.swing_base
        self.swing_base += self.a_base * (self.swing - self.swing_base)
        swing_z = (self.swing - sb) / max(sb, 1e-6)
        if u.amps and u.amps > 0:
            r = a / max(u.amps, 1e-6)
            self.ratio = r if self.ratio is None else self.ratio + self.a_fast * (r - self.ratio)
        if t < self.warmup or t - self.last < self.refractory:
            return None
        # A step in level and a swing in level both raise the swing estimate for a while, so the
        # level test has to be settled first. Bridging is the case where the load moves about
        # without going anywhere: swing high, level still near its baseline.
        # A sixty-second average of a noisy signal has a small standard deviation, so pure noise
        # will reach three sigma sooner or later. Sigma decides whether a move is real; this floor
        # decides whether it is worth saying out loud. Two percent of baseline on a 12 A crammer is
        # a quarter of an amp -- below that, nobody would act on it either way.
        big = abs(dev) > self.min_frac * max(abs(self.base), 1e-6)
        kind, text = None, None
        if z > self.z_load and big:
            kind = "denser"
            text = (f"[feed] crammer load rising: {self.fast:.1f} A against a {self.base:.1f} A baseline "
                    f"({z:+.1f} sigma): denser or coarser feed reaching the throat")
        elif z < -self.z_load and big:
            kind = "starving"
            text = (f"[feed] crammer load falling: {self.fast:.1f} A against a {self.base:.1f} A baseline "
                    f"({z:+.1f} sigma): throat starving or the hopper running out")
        elif swing_z > self.z_swing and abs(z) < 1.0:
            kind = "erratic"
            text = (f"[feed] crammer load swinging about {self.swing:.1f} A around {self.base:.1f} A "
                    f"without settling: bridging or rat-holing in the throat")
        if not kind:
            return None
        self.last, self.state = t, kind
        self.corroborated = 1.0
        return dict(kind="feed", bank="crammer", template=kind, zone="crammer", value=round(z, 2),
                    direction=1.0, verdict="feed side, measured at the crammer", zones=list(BARREL),
                    text=text)


class LoopWatch:
    """One TCU loop, watched through its supply and return.

    Supply and return give the loop's actual duty: Q = mdot * cp * (return - supply). That turns the
    cooling side from something the twin models into something it measures.

    The delta alone cannot tell you why it moved, because Q = mdot * cp * dT has two ways to change.
    Lose flow at constant heat and the delta goes UP, not down -- the same watts carried by less oil.
    That trips people up, and it is why this watcher needs the twin's own predicted cooling load to
    separate the two cases:

      delta up, and the measured watts now disagree with what the model says the barrel is shedding
          -> the flow assumption is wrong. Pump, valve, air lock, blocked strainer.
      delta up, and the measured watts still agree with the model
          -> genuinely more heat arriving in the oil. On the barrel loop that means shear, not bands.
      delta down with the model agreeing
          -> less heat arriving. Usually the line slowing, sometimes a zone that stopped fighting.
      supply drifting up with the delta holding
          -> the cold side is losing ground. Tower water, tower fan, a fouling exchanger. The loop is
             still moving heat; it has nowhere to put it.

    Watts are reported when a flow meter exists and inferred from the nominal pump rate when not. The
    nominal version is right until the flow itself is what changed, which is exactly the case above.
    """

    def __init__(self, name: str, p: Plant, dt: float, nominal: float, warmup=900.0, refractory=600.0,
                 tau_base=1800.0, tau_fast=120.0, z=3.0, min_frac=0.10, model_gap=0.08):
        self.name, self.p, self.dt, self.nominal = name, p, dt, nominal
        self.warmup, self.refractory, self.z = warmup, refractory, z
        self.min_frac, self.model_gap = min_frac, model_gap
        self.a_base, self.a_fast = dt / tau_base, dt / tau_fast
        self.dT = self.dT_base = self.sup = self.sup_base = None
        self.var_d = self.var_s = 1e-6
        self.last, self.state = {}, "normal"      # refractory per kind: a flow loss should not be
        self.watts = None                         # held back by a load change reported ten minutes ago
        self.q_model = None
        # measured watts over modelled watts. Its absolute value carries every standing model error,
        # so what matters is movement away from its own settled value, and in which direction.
        self.mismatch = self.mismatch_base = None
        self.approach = None                    # oil supply above tower supply; a fouling number

    def update(self, supply: Optional[float], ret: Optional[float], flow: Optional[float],
               T_tower: Optional[float], t: float, q_model: Optional[float] = None) -> Optional[dict]:
        if supply is None or ret is None:
            return None
        d = ret - supply
        if self.dT is None:
            self.dT = self.dT_base = d
            self.sup = self.sup_base = supply
        self.dT += self.a_fast * (d - self.dT)
        self.sup += self.a_fast * (supply - self.sup)
        dev_d, dev_s = self.dT - self.dT_base, self.sup - self.sup_base
        self.dT_base += self.a_base * (d - self.dT_base)
        self.sup_base += self.a_base * (supply - self.sup_base)
        self.var_d += self.a_base * (dev_d * dev_d - self.var_d)
        self.var_s += self.a_base * (dev_s * dev_s - self.var_s)
        zd = dev_d / math.sqrt(max(self.var_d, 1e-9))
        zs = dev_s / math.sqrt(max(self.var_s, 1e-9))
        self.watts = loop_watts(self.p, self.dT, flow, self.nominal)
        dev_m = 0.0
        if q_model is not None and q_model > 500.0 and self.watts:
            m = self.watts / q_model
            self.q_model = q_model
            # `m` is already built from the smoothed delta, so smoothing it again would just double
            # the lag and let a flow loss be misread as a load change while the evidence catches up.
            if self.mismatch_base is None:
                self.mismatch_base = m
            self.mismatch = m
            dev_m = self.mismatch - self.mismatch_base
            self.mismatch_base += self.a_base * (m - self.mismatch_base)
        if T_tower is not None:
            self.approach = self.sup - T_tower
        if t < self.warmup:
            return None
        big_d = abs(dev_d) > self.min_frac * max(abs(self.dT_base), 1.0)
        big_s = abs(dev_s) > 1.5
        # More heat measured than the model says is arriving means the watts are being computed from a
        # flow that is no longer true. Less heat measured, or the two moving together, is a real load
        # change. The sign is the whole discriminator.
        over_reads = self.mismatch is not None and dev_m > self.model_gap
        kind = text = None
        if zd > self.z and big_d and over_reads and (flow is None):
            kind = "flow_loss"
            implied = self.nominal * self.mismatch_base / max(self.mismatch, 1e-3)
            text = (f"[loop:{self.name}] delta up to {self.dT:.1f} K from {self.dT_base:.1f} K, but the barrel "
                    f"is not shedding more heat: at {self.nominal:.0f} L/min that reads "
                    f"{self.mismatch / max(self.mismatch_base, 1e-3):.2f}x what it used to against the model, so "
                    f"the oil is moving at roughly {implied:.0f} L/min. Pump, valve, air lock or strainer")
        elif zd > self.z and big_d:
            kind = "load_up"
            text = (f"[loop:{self.name}] delta up to {self.dT:.1f} K from {self.dT_base:.1f} K"
                    + (f" ({self.watts / 1e3:.1f} kW)" if self.watts else "")
                    + ": more heat arriving in the oil, and the model agrees it is really there")
        elif zd < -self.z and big_d:
            kind = "load_down"
            text = (f"[loop:{self.name}] delta down to {self.dT:.1f} K from {self.dT_base:.1f} K"
                    + (f" ({self.watts / 1e3:.1f} kW)" if self.watts else "")
                    + ": less heat reaching the oil than before")
        elif zs > self.z and big_s and abs(zd) < self.z:
            kind = "cold_side"
            text = (f"[loop:{self.name}] supply drifted up to {self.sup:.1f} degC ({dev_s:+.1f} K) with the delta "
                    f"holding at {self.dT:.1f} K: the cold side is losing ground, not the loop")
        if not kind or t - self.last.get(kind, -1e9) < self.refractory:
            return None
        self.last[kind], self.state = t, kind
        return dict(kind="loop", bank=f"tcu_{self.name}", template=kind, zone=f"{self.name} loop",
                    value=round(zs if kind == "cold_side" else zd, 2), direction=0.0,
                    verdict="cooling side", zones=list(BARREL) if self.name == "barrel" else [],
                    text=text)


class DriveWatch:
    """Motor speed against screw speed. The ratio is mechanical, so it should not move. When it does,
    something between the motor and the screw is slipping -- coupling, belt, or a gearbox on its way
    out. Also catches a drive that is commanding one speed and delivering another."""

    def __init__(self, p: Plant, dt: float, warmup=600.0, refractory=1800.0, tol=0.04, tau=120.0):
        self.p, self.dt, self.warmup, self.refractory, self.tol = p, dt, warmup, refractory, tol
        self.a = dt / tau
        self.ratio = None
        self.base = None
        self.last = -1e9

    def update(self, rpm_motor: Optional[float], rpm_screw: Optional[float], t: float) -> Optional[dict]:
        if not rpm_motor or not rpm_screw or rpm_screw < 1.0:
            return None
        r = rpm_motor / rpm_screw
        self.ratio = r if self.ratio is None else self.ratio + self.a * (r - self.ratio)
        if self.base is None and t > self.warmup:
            self.base = self.ratio
        if self.base is None or t - self.last < self.refractory:
            return None
        off = self.ratio / self.base - 1.0
        if abs(off) > self.tol:
            self.last = t
            return dict(kind="drive", bank="drive", template="ratio", zone="drive", value=round(off, 4),
                        direction=0.0, verdict="mechanical, between motor and screw", zones=[],
                        text=(f"[drive] motor turning {self.ratio:.1f} rev per screw rev against {self.base:.1f} "
                              f"({off:+.1%}): slipping coupling, belt, or gearbox"))
        return None


class EventEmitter:
    def __init__(self, bands: dict):
        self.bands, self.last = bands, {}

    def update(self, channels: dict) -> list:
        events = []
        for name, val in channels.items():
            band = self.bands.get(name.rstrip("0123456789_"), None)
            if band is None or val is None:
                continue
            if name not in self.last or abs(val - self.last[name]) > band:
                self.last[name] = val
                events.append((name, val))
        return events


class Detectors:
    """everything above, wired for one residual stream (whole line or one core)."""

    def __init__(self, p: Plant, dt: float, zones: Optional[List[int]] = None, name: str = "", theta: float = 0.06,
                 with_cross: bool = True, with_kappa: bool = True, with_contacts: bool = True):
        self.zones = list(range(N)) if zones is None else list(zones)
        tag = f"{name}:" if name else ""
        self.heat_bank = Bank(f"{tag}heater", dt, theta, self.zones)
        self.therm_bank = Bank(f"{tag}thermal", dt, theta, self.zones)
        self.cross = CrossModal(dt) if with_cross and set(BARREL) <= set(self.zones) else None
        self.kappa = KappaAlarm() if with_kappa else None
        self.heater = HeaterCheck(p, self.zones)
        owns_loops = set(BARREL) <= set(self.zones)
        self.feed = FeedWatch(dt) if owns_loops else None
        self.loop_b = LoopWatch("barrel", p, dt, p.flow_b_nom) if owns_loops else None
        self.loop_s = LoopWatch("screw", p, dt, p.flow_s_nom) if owns_loops else None
        self.drive = DriveWatch(p, dt) if owns_loops else None
        self.contacts = ContactWatch(dt) if with_contacts else None
        self.emitter = EventEmitter(bands=dict(PV=0.5, MV=0.02, amps=2.0, rpm=0.5, Tad=0.5, Pad=2.0,
                                              Camps=0.3, Crpm=0.5, Obs=0.3, Obr=0.3, Oss=0.3, Osr=0.3))
        self.drift_hist = deque(maxlen=int(600 / dt) + 1)

    def update(self, u, corr: dict, e: List[float], residence: List[float], t: float,
               diag: Optional[dict] = None) -> dict:
        self.drift_hist.append(corr["drift_index"])
        global_active = abs(self.drift_hist[-1] - self.drift_hist[0]) > 0.02
        h = self.heat_bank.update(e, residence, t, global_active)
        th = self.therm_bank.update(corr["r_q"], residence, t, global_active)
        alarms = h["alarms"] + th["alarms"]
        if self.cross:
            x = self.cross.update(h["ep"], corr["r_k"], t)
            if x:
                alarms.append(x)
        if self.kappa:
            k = self.kappa.update(corr["drift_index"], t)
            if k:
                alarms.append(k)
        alarms += self.heater.update(u)
        if self.feed:
            fa = self.feed.update(u, t)
            if fa:
                alarms.append(fa)
        if self.loop_b:
            q_b = sum((diag or {}).get("Q_cool", [])) or None
            for a in (self.loop_b.update(u.T_oil_b_supply, u.T_oil_b_return, u.flow_oil_b, u.T_tower, t, q_b),
                      self.loop_s.update(u.T_oil_s_supply, u.T_oil_s_return, u.flow_oil_s, None, t),
                      self.drive.update(u.rpm_motor, u.rpm, t)):
                if a:
                    alarms.append(a)
        c_events, c_alarms = self.contacts.update(u.contacts, t) if self.contacts else ([], [])
        alarms += c_alarms
        events = self.emitter.update({**{f"PV{i}": u.PV[i] for i in self.zones}, **{f"MV{i}": u.MV[i] for i in self.zones},
                                      "amps": u.amps, "rpm": u.rpm, "Tad": u.T_adapter, "Pad": u.P_adapter,
                                      "Camps": u.crammer_amps, "Crpm": u.crammer_rpm,
                                      "Obs": u.T_oil_b_supply, "Obr": u.T_oil_b_return,
                                      "Oss": u.T_oil_s_supply, "Osr": u.T_oil_s_return}) + c_events
        return dict(h=h, th=th, alarms=alarms, events=events, cross_M=self.cross.M if self.cross else 0.0,
                    feed=self.feed, loop_b=self.loop_b, loop_s=self.loop_s, drive=self.drive)
