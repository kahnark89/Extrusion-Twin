#!/usr/bin/env python3
"""
extrusion_twin.py -- first slice of the melt-state digital twin for the 80" line
(18 zones: 8 barrel, 2 screen changer, 1 adapter, 7 die; oil-TCU barrel cooling,
screw TCU, feed water jacket, melt T/P probe at the adapter; flexible PVC).

Once per poll sweep:
  1. PREDICT  propagate melt / barrel-metal / oil / screw temperatures one step
              (1-D along z, lumped per zone, explicit Euler)
  2. CORRECT  pull the state toward what was measured (barrel PVs, adapter melt T,
              motor power) -> thermal residuals, viscosity-multiplier drift index
  3. EXPECT   the heater duty each zone SHOULD need given the twin (the efference
              copy) -> heater residual e_i = MV_measured - MV_expected
  4. DETECT   sign split, adjacent-zone lag correlators (direction), drift-mode
              integrators with threshold + refractory, fighting-loop waste,
              heater-element check from CT, send-on-delta event economy
  5. LOG      one CSV row per sweep; alarms to the console

No hardware (self-consistent simulator with injected drifts):
    python extrusion_twin.py --sim 4200

Live on the PXR-9 RS-485 bus (pip install minimalmodbus pyserial):
    python extrusion_twin.py --port COM3 --parity O --rpm 60 --amps 285 --tadapter 172

Every number marked FIT is a placeholder to replace from the machine (nameplates,
screw drawing, two reversal tests). The structure is portable; fitted values are
plant-specific -- keep them in a separate config once you fit them.

PXR-9 Modbus facts used here (Fuji manual INP-TN512642a-E):
  9600 bps fixed, 8 data bits, 1 stop bit, parity selectable (CoM: 0 odd, 1 even, 2 none)
  function 04, engineering-unit table, relative address 1000 = register 31001:
     31001 PV  31002 SV(in use)  31003 DV  31004 MV out1 (x0.01 %)  31005 MV out2
     31007 alarm status  31008 input/unit abnormal (bits 0-3 = TC open / range)
     31010 heater current CT (x0.1 A, only with the heater-break option)
  decimal places for PV/SV/DV come from 41020 (P-dP), relative address 1019, function 03
  leave >= 10 ms of silence between messages; unit replies in 1-30 ms
"""

import argparse
import csv
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field

# ----------------------------------------------------------------- zone layout
N = 18
BARREL = list(range(0, 8))
SCREEN = [8, 9]
ADAPTER = 10
DIE = list(range(11, 18))
ZONE_NAMES = [f"B{i + 1}" for i in range(8)] + ["SC1", "SC2", "AD"] + [f"D{i + 1}" for i in range(7)]


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ------------------------------------------------------------- recipe vector
@dataclass
class Recipe:
    """Everything the melt model reads from the recipe. Flexible PVC placeholders."""
    rho: float = 1350.0        # kg/m3, filled plasticized compound              FIT
    cp: float = 1600.0         # J/kg/K                                          FIT
    K_ref: float = 1.5e4       # power-law consistency at T_ref, Pa.s^n           FIT
    n: float = 0.40            # power-law index (well below 1)                   FIT
    EaR: float = 4500.0        # flow activation energy / R, K                    FIT
    T_ref: float = 170.0       # degC reference for K_ref
    EaR_deg: float = 12000.0   # stabilizer consumption activation energy / R, K  FIT
    T_ref_deg: float = 175.0   # degC reference for the degradation index
    # bookkeeping only for now: plasticizer_phr lowers K_ref steeply, filler_phr raises it
    plasticizer_phr: float = 45.0
    filler_phr: float = 30.0


# -------------------------------------------------------------- plant vector
@dataclass
class Plant:
    """Machine geometry and conductances. Structure is generic; numbers are FIT."""
    D: float = 0.152                                   # screw diameter m (0.203 if 8 in)   FIT
    L_zone: float = 0.50                               # barrel zone length m                FIT
    h_channel: list = field(default_factory=lambda:    # channel depth per barrel zone, m    FIT
                            [0.020, 0.020, 0.016, 0.012, 0.009, 0.007, 0.007, 0.007])
    solids_share: float = 0.35                         # fraction of motor power dissipated in solids conveying/compression (B1-B2)  FIT
    k_pump: float = 0.0055                             # kg/s per RPM (60 RPM ~ 1190 kg/h)   FIT
    kW_per_amp: float = 0.78                           # shaft kW per motor amp above no-load FIT
    I_noload: float = 25.0                             # A                                    FIT
    T_feed_offset: float = 5.0                         # solids enter at jacket temp + this   FIT
    k_lip: float = 0.15                                # lip cools this fraction of the way to air FIT
    V_line: float = 480.0                              # heater line voltage for CT check     FIT
    # per-zone vectors (18 long)
    P_heat: list = field(default_factory=lambda: [8000.0] * 8 + [4000.0] * 2 + [3000.0] + [5000.0] * 7)   # W  FIT
    C_b: list = field(default_factory=lambda: [1.0e5] * 8 + [4.0e4] * 2 + [3.0e4] + [6.0e4] * 7)          # J/K FIT
    hA_w: list = field(default_factory=lambda: [400.0] * 8 + [80.0] * 2 + [60.0] + [120.0] * 7)           # W/K melt<->metal FIT
    hA_s: list = field(default_factory=lambda: [60.0] * 8 + [0.0] * 10)                                    # W/K melt<->screw FIT
    UA_oil: list = field(default_factory=lambda: [600.0] * 8 + [0.0] * 10)                                 # W/K metal<->barrel oil FIT
    UA_amb: list = field(default_factory=lambda: [25.0] * 8 + [20.0] * 10)                                 # W/K metal->air FIT
    V_m: list = field(default_factory=list)                                                                # m3 melt per zone (computed)
    # oil loops and screw
    C_oil_b: float = 8.5e4;   UA_hx_b: float = 600.0;  P_tcu_b: float = 12000.0   # barrel TCU   FIT
    C_oil_s: float = 3.0e4;   UA_hx_s: float = 300.0;  P_tcu_s: float = 6000.0    # screw TCU    FIT
    C_s: float = 7.0e4;       UA_screw_oil: float = 400.0                          # screw core   FIT
    tau_tcu: float = 15.0                                                          # TCU tracking time constant s

    def __post_init__(self):
        if not self.V_m:
            barrel = [math.pi * self.D * h * self.L_zone * 0.9 for h in self.h_channel]
            self.V_m = barrel + [0.0020, 0.0020] + [0.0015] + [0.0015] * 7        # m3  FIT


# --------------------------------------------------------------- state/inputs
@dataclass
class State:
    T_m: list                    # melt degC per zone
    T_b: list                    # barrel/die metal degC per zone
    T_oil_b: float               # barrel oil loop degC
    T_oil_s: float               # screw oil loop degC
    T_s: float                   # screw core degC
    kappa: float = 1.0           # viscosity multiplier (drift state)

    def copy(self):
        return State(self.T_m[:], self.T_b[:], self.T_oil_b, self.T_oil_s, self.T_s, self.kappa)


@dataclass
class Inputs:
    PV: list                     # degC per zone (barrel thermocouple)
    SV: list                     # degC per zone
    MV: list                     # heater duty fraction 0..1 per zone
    rpm: float
    amps: float
    T_adapter: float = None      # melt probe degC (None if not read)
    CT: list = None              # heater current A per zone (None if no CT option)
    tcu_b_sv: float = 140.0      # barrel TCU setpoint degC
    tcu_s_sv: float = 120.0      # screw TCU setpoint degC
    T_tower: float = 30.0        # cooling tower supply degC (measure it!)
    T_air: float = 35.0          # plant air degC (measure it!)
    T_feed_jacket: float = 40.0  # feed-throat water jacket degC


# -------------------------------------------------------------------- physics
def shear_rate(p, i, rpm):
    return math.pi * p.D * (rpm / 60.0) / p.h_channel[i]


def viscosity(rc, T, gdot):
    TK, TrK = T + 273.15, rc.T_ref + 273.15
    return rc.K_ref * math.exp(rc.EaR * (1.0 / TK - 1.0 / TrK)) * max(gdot, 1e-3) ** (rc.n - 1.0)


def viscous_weights(p, rc, st, rpm):
    """eta * gdot^2 * V per barrel zone; B1-B2 are solids conveying (friction), weight 0."""
    w = [0.0] * N
    for i in BARREL:
        if i >= 2:
            g = shear_rate(p, i, rpm)
            w[i] = viscosity(rc, st.T_m[i], g) * g * g * p.V_m[i]
    return w


def mech_power_meas(p, amps):
    return p.kW_per_amp * 1000.0 * max(amps - p.I_noload, 0.0)


def mech_power_pred(p, rc, st, rpm):
    """what the viscosity model says the screw should be drawing at this melt state"""
    return st.kappa * sum(viscous_weights(p, rc, st, rpm)) / (1.0 - p.solids_share)


def tcu_flux(p, T_oil, sv, T_tower, C, UA_hx, P_tcu):
    """well-tuned TCU: drives oil toward sv within its heating/cooling authority. Returns W into oil."""
    want = C * (sv - T_oil) / p.tau_tcu
    return clamp(want, -UA_hx * max(T_oil - T_tower, 0.0), P_tcu)


def step(p, rc, st, u, dt, P_heat=None, T_tower=None):
    """one explicit-Euler step of the whole twin. Returns (new state, diagnostics)."""
    P_heat = P_heat or p.P_heat
    T_tower = u.T_tower if T_tower is None else T_tower
    mdot = p.k_pump * max(u.rpm, 0.0)
    P_mech = mech_power_meas(p, u.amps)
    w = viscous_weights(p, rc, st, u.rpm)
    sw = sum(w) or 1.0
    Q_shear = [0.0] * N
    for i in BARREL:
        Q_shear[i] = P_mech * p.solids_share / 2.0 if i < 2 else P_mech * (1.0 - p.solids_share) * w[i] / sw

    T_in = u.T_feed_jacket + p.T_feed_offset
    new = st.copy()
    for i in range(N):
        # series chain B1..B8 -> SC1 -> SC2 -> AD, then a fan-out: the seven die zones sit
        # side by side across the 80" and each takes 1/7 of the flow from the adapter
        T_prev = T_in if i == 0 else st.T_m[ADAPTER] if i in DIE else st.T_m[i - 1]
        m_i = mdot / len(DIE) if i in DIE else mdot
        C = rc.rho * rc.cp * p.V_m[i]
        q = (m_i * rc.cp * (T_prev - st.T_m[i]) + Q_shear[i]
             + p.hA_w[i] * (st.T_b[i] - st.T_m[i]) + p.hA_s[i] * (st.T_s - st.T_m[i]))
        new.T_m[i] = st.T_m[i] + dt * q / C

    Q_cool = [0.0] * N
    for i in range(N):
        Q_cool[i] = p.UA_oil[i] * (st.T_b[i] - st.T_oil_b)
        q = (u.MV[i] * P_heat[i] - p.hA_w[i] * (st.T_b[i] - st.T_m[i]) - Q_cool[i]
             - p.UA_amb[i] * (st.T_b[i] - u.T_air))
        new.T_b[i] = st.T_b[i] + dt * q / p.C_b[i]

    q_tcu_b = tcu_flux(p, st.T_oil_b, u.tcu_b_sv, T_tower, p.C_oil_b, p.UA_hx_b, p.P_tcu_b)
    new.T_oil_b = st.T_oil_b + dt * (q_tcu_b + sum(Q_cool)) / p.C_oil_b
    q_tcu_s = tcu_flux(p, st.T_oil_s, u.tcu_s_sv, T_tower, p.C_oil_s, p.UA_hx_s, p.P_tcu_s)
    q_screw = sum(p.hA_s[i] * (st.T_m[i] - st.T_s) for i in range(N))
    new.T_oil_s = st.T_oil_s + dt * (q_tcu_s + p.UA_screw_oil * (st.T_s - st.T_oil_s)) / p.C_oil_s
    new.T_s = st.T_s + dt * (q_screw - p.UA_screw_oil * (st.T_s - st.T_oil_s)) / p.C_s

    # indices: fighting loops, degradation exposure, drool propensity
    fight_W = sum(min(u.MV[i] * P_heat[i], max(Q_cool[i], 0.0)) for i in BARREL)
    res = [rc.rho * p.V_m[i] / max(mdot / len(DIE) if i in DIE else mdot, 1e-6) for i in range(N)]   # residence s per zone
    deg = sum(res[i] * math.exp(-rc.EaR_deg * (1.0 / (st.T_m[i] + 273.15) - 1.0 / (rc.T_ref_deg + 273.15)))
              for i in range(2, N))
    T_die_melt = sum(st.T_m[i] for i in DIE) / len(DIE)
    T_die_metal = sum(st.T_b[i] for i in DIE) / len(DIE)
    T_lip = T_die_metal - p.k_lip * (T_die_metal - u.T_air)
    drool = math.exp((T_die_melt - T_lip) / 10.0) - 1.0
    diag = dict(P_mech=P_mech, Q_shear=Q_shear, Q_cool=Q_cool, fight_W=fight_W, residence=res,
                H_index=deg, drool_index=drool, T_lip=T_lip, mdot=mdot)
    return new, diag


def mv_expected(p, st, u, tau_ctrl=120.0):
    """heater duty each zone should need to hold its metal where it is and close the SV error"""
    out, want = [], []
    for i in range(N):
        Q_need = (p.hA_w[i] * (st.T_b[i] - st.T_m[i]) + p.UA_oil[i] * (st.T_b[i] - st.T_oil_b)
                  + p.UA_amb[i] * (st.T_b[i] - u.T_air) + p.C_b[i] * (u.SV[i] - u.PV[i]) / tau_ctrl)
        want.append(Q_need)
        out.append(clamp(Q_need / p.P_heat[i], 0.0, 1.0))
    return out, want


# ------------------------------------------------------------------ estimator
class Estimator:
    """heuristic observer: fixed gains, measurement-by-measurement. Upgrade path: EnKF."""

    def __init__(self, p, rc, g_b=0.05, g_a=0.20, g_k=0.002, calib_steps=180):
        self.p, self.rc = p, rc
        self.g_b, self.g_a, self.g_k = g_b, g_a, g_k
        self.calib_steps = calib_steps
        self.n = 0
        self.kappa_cal = None

    def correct(self, st, u, dt):
        p, rc = self.p, self.rc
        r_b = [u.PV[i] - st.T_b[i] for i in range(N)]
        for i in range(N):
            st.T_b[i] += self.g_b * r_b[i]
        r_a = 0.0
        if u.T_adapter is not None:
            r_a = u.T_adapter - st.T_m[ADAPTER]
            for i in range(N):
                st.T_m[i] += self.g_a * r_a * math.exp(-abs(i - ADAPTER) / 3.0)
        P_meas = mech_power_meas(p, u.amps)
        base = mech_power_pred(p, rc, st, u.rpm) / max(st.kappa, 1e-9)      # prediction at kappa = 1
        r_k = 0.0
        if base > 1.0:
            if self.n < self.calib_steps:                                    # auto-calibrate the multiplier
                ratio = P_meas / base
                st.kappa = ratio if self.n == 0 else 0.98 * st.kappa + 0.02 * ratio
                self.kappa_cal = st.kappa
            else:
                r_k = (P_meas - st.kappa * base) / (st.kappa * base)
                st.kappa *= 1.0 + self.g_k * r_k
        self.n += 1
        drift_index = (st.kappa / self.kappa_cal - 1.0) if self.kappa_cal else 0.0
        # innovation -> heat the model is missing at each metal node, in heater-duty units
        q_unexpl = [p.C_b[i] * self.g_b * r_b[i] / dt for i in range(N)]
        r_q = [q_unexpl[i] / p.P_heat[i] for i in range(N)]
        return dict(r_b=r_b, r_a=r_a, r_k=r_k, drift_index=drift_index, r_q=r_q, q_unexpl=q_unexpl)


# ------------------------------------------------------------------ detectors
class Bank:
    """one detector bank over an 18-vector residual: bias removal, sign split, adjacent-zone
    lag correlators (direction), wide-field templates and local detectors with threshold +
    refractory alarms. Mirrors the fly: T4/T5 pairs -> HS/VS wide-field cells -> spike."""

    def __init__(self, name, dt, theta, tau_bias=7200.0, tau_int=120.0, refractory=600.0, warmup=600.0,
                 lag_factor=3.0):
        self.name, self.dt, self.theta = name, dt, theta
        self.a_bias_slow, self.a_bias_fast = dt / tau_bias, dt / 120.0   # fast adaptation while warming up
        self.a_int = dt / tau_int
        self.refractory, self.warmup = refractory, warmup
        self.lag_factor = lag_factor          # propagation delay / plug-flow residence (solids+melting transit)  FIT
        self.bias = [0.0] * N
        self.hist = [deque(maxlen=600) for _ in range(N)]
        self.templates = {
            "barrel": [1.0 if i in BARREL else 0.0 for i in range(N)],
            "screen_adapter": [1.0 if i in SCREEN + [ADAPTER] else 0.0 for i in range(N)],
            "die": [1.0 if i in DIE else 0.0 for i in range(N)],
            "gradient": [-1.0 if i in BARREL[:4] else 1.0 if i in BARREL[4:] else 0.0 for i in range(N)],
        }
        self.M = {k: 0.0 for k in self.templates}
        self.L = [0.0] * N                                     # local (single-zone) integrators
        self.Dint, self.Pint = [0.0] * (N - 1), [0.0] * (N - 1)
        self.last_alarm = {k: -1e9 for k in self.templates}
        self.last_local = [-1e9] * N

    def direction(self, zones):
        pairs = [i for i in range(N - 1) if i in zones and i + 1 in zones]
        d = sum(self.Dint[i] for i in pairs)
        s = sum(self.Pint[i] for i in pairs)
        return d / s if s > 1e-12 else 0.0

    @staticmethod
    def verdict(direction):
        return ("downstream-propagating (feed/material side)" if direction > 0.3 else
                "upstream-propagating (die/back-pressure side)" if direction < -0.3 else
                "simultaneous (electrical/control/cooling side)")

    def update(self, e, residence, t, global_active=False):
        ep = [0.0] * N
        a_bias = self.a_bias_fast if t < self.warmup else self.a_bias_slow
        for i in range(N):
            self.bias[i] += a_bias * (e[i] - self.bias[i])
            ep[i] = e[i] - self.bias[i]
            self.hist[i].append(ep[i])
        pos = [max(x, 0.0) for x in ep]                       # ON channel
        neg = [max(-x, 0.0) for x in ep]                      # OFF channel
        # elementary drift detectors on every adjacent pair. Like the lamina, we high-pass first
        # (change over one lag), then correlate the upstream zone's delayed change with the
        # downstream zone's current change. Lag = expected propagation delay from zone i to i+1.
        for i in range(N - 1):
            lag = max(1, int(round(self.lag_factor * residence[i] / self.dt)))
            h0, h1 = self.hist[i], self.hist[i + 1]
            if len(h0) > 2 * lag:
                d0_now, d0_then = h0[-1] - h0[-1 - lag], h0[-1 - lag] - h0[-1 - 2 * lag]
                d1_now, d1_then = h1[-1] - h1[-1 - lag], h1[-1 - lag] - h1[-1 - 2 * lag]
                D = d0_then * d1_now - d1_then * d0_now
                P = abs(d0_then * d1_now) + abs(d1_then * d0_now)
                self.Dint[i] += self.a_int * (D - self.Dint[i])
                self.Pint[i] += self.a_int * (P - self.Pint[i])
        alarms = []
        armed = t > self.warmup
        # wide-field templates (weighted means), leaky integration, threshold + refractory
        for k, w in self.templates.items():
            x = sum(w[i] * ep[i] for i in range(N)) / sum(abs(v) for v in w)
            self.M[k] += self.a_int * (x - self.M[k])
            if armed and abs(self.M[k]) > self.theta and t - self.last_alarm[k] > self.refractory:
                self.last_alarm[k] = t
                zones = [i for i in range(N) if w[i] != 0.0]
                d = self.direction(zones)
                alarms.append(f"[{self.name}] {k} {self.M[k]:+.3f} dir {d:+.2f} {self.verdict(d)}")
        # local detectors: one zone moving on its own = band, SSR, thermocouple, or a local cooling fault.
        # A wide-field template or a global drift state that already explains the zone wins (surround suppression).
        for i in range(N):
            self.L[i] += self.a_int * (ep[i] - self.L[i])
            explained = global_active or any(abs(self.M[k]) > 0.5 * self.theta and w[i] != 0.0
                                             for k, w in self.templates.items())
            if armed and not explained and abs(self.L[i]) > 2.0 * self.theta and t - self.last_local[i] > self.refractory:
                self.last_local[i] = t
                alarms.append(f"[{self.name}] local {ZONE_NAMES[i]} {self.L[i]:+.3f}: single-zone (band/SSR/TC/local cooling)")
        return dict(ep=ep, pos=pos, neg=neg, direction=self.direction(BARREL), M=dict(self.M), alarms=alarms)


class CrossModal:
    """barrel heater residual vs motor-power residual, in sigma units. Opposite signs cancel
    (energy rebalancing, benign); same sign reinforces (real drift)."""

    def __init__(self, dt, theta=2.0, tau_int=120.0, refractory=600.0, warmup=600.0):
        self.a = dt / tau_int
        self.theta, self.refractory, self.warmup = theta, refractory, warmup
        self.var_e, self.var_k, self.M, self.last = 1e-4, 1e-4, 0.0, -1e9

    def update(self, ep, r_k, t):
        e_bar = sum(ep[i] for i in BARREL) / len(BARREL)
        self.var_e += 0.002 * (e_bar * e_bar - self.var_e)
        self.var_k += 0.002 * (r_k * r_k - self.var_k)
        x = e_bar / math.sqrt(self.var_e + 1e-9) + r_k / math.sqrt(self.var_k + 1e-9)
        self.M += self.a * (x - self.M)
        if t > self.warmup and abs(self.M) > self.theta and t - self.last > self.refractory:
            self.last = t
            return f"[cross] heater and motor residuals reinforcing ({self.M:+.2f} sigma): real thermal-mechanical drift"
        return None


class EventEmitter:
    """send-on-delta: a channel only produces an event when it moves out of its band"""

    def __init__(self, bands):
        self.bands, self.last = bands, {}

    def update(self, channels):
        events = []
        for name, val in channels.items():
            band = self.bands.get(name.rstrip("0123456789_"), None)
            if band is None or val is None:
                continue
            if name not in self.last or abs(val - self.last[name]) > band:
                self.last[name] = val
                events.append((name, val))
        return events


def heater_check(p, u, hold, latched):
    """CT versus duty-weighted expectation: a lost element shows as a step in the ratio.
    Only visible while the zone is actually calling for heat (expected > 1 A)."""
    flags = []
    if u.CT is None:
        return flags
    for i in range(N):
        expected = u.MV[i] * p.P_heat[i] / p.V_line
        if expected > 1.0:
            hold[i] = 0.95 * hold[i] + 0.05 * (u.CT[i] / expected)
            if hold[i] < 0.8 and not latched[i]:
                latched[i] = True
                flags.append(f"[heater] {ZONE_NAMES[i]} current {hold[i]:.0%} of duty-expected: element or SSR")
            elif hold[i] > 0.9 and latched[i]:
                latched[i] = False
                flags.append(f"[heater] {ZONE_NAMES[i]} current back to {hold[i]:.0%} of expected")
    return flags


# --------------------------------------------------------------------- poller
def open_pxr(port, station, parity):
    import minimalmodbus
    import serial
    inst = minimalmodbus.Instrument(port, station)
    inst.serial.baudrate = 9600                     # fixed on the PXR
    inst.serial.bytesize = 8
    inst.serial.parity = {"O": serial.PARITY_ODD, "E": serial.PARITY_EVEN, "N": serial.PARITY_NONE}[parity]
    inst.serial.stopbits = 1
    inst.serial.timeout = 0.3
    inst.mode = minimalmodbus.MODE_RTU
    inst.clear_buffers_before_each_transaction = True
    return inst


def read_pxr(inst, dp):
    """31001..31010 in one message: PV SV DV MV1 MV2 STno ALM FAULT RAMP CT"""
    r = inst.read_registers(1000, 10, functioncode=4)
    s16 = lambda v: v - 65536 if v > 32767 else v
    sc = 10.0 ** dp
    return dict(PV=s16(r[0]) / sc, SV=s16(r[1]) / sc, DV=s16(r[2]) / sc, MV=s16(r[3]) / 10000.0,
                MV2=s16(r[4]) / 10000.0, alarm=r[6], fault=r[7], CT=r[9] / 10.0)


class LiveBus:
    def __init__(self, port, parity, stations, args):
        self.insts = [open_pxr(port, s, parity) for s in stations]
        self.dp, self.degf = [], []
        for inst in self.insts:
            self.degf.append(bool(inst.read_register(1016, functioncode=3)))   # 41017 P-F: 0 = degC, 1 = degF
            time.sleep(0.02)
            self.dp.append(inst.read_register(1019, functioncode=3))          # 41020 P-dP
            time.sleep(0.02)
        self.args = args
        self.any_ct = False

    def poll(self):
        PV, SV, MV, CT, faults = [], [], [], [], []
        for inst, dp, degf in zip(self.insts, self.dp, self.degf):
            d = read_pxr(inst, dp)
            time.sleep(0.02)                                              # >= 10 ms silence required
            if degf:                                                      # twin works in degC internally
                d["PV"], d["SV"] = (d["PV"] - 32.0) / 1.8, (d["SV"] - 32.0) / 1.8
            PV.append(d["PV"]); SV.append(d["SV"]); MV.append(clamp(d["MV"], 0.0, 1.0)); CT.append(d["CT"])
            if d["fault"] & 0x0F:
                faults.append(f"[fault] {inst.address} input abnormal bits {d['fault'] & 0x0F:04b}")
            if d["CT"] > 0:
                self.any_ct = True
        a = self.args
        # TODO: replace the manual values with VFD / adapter-controller / TCU / thermistor reads
        return Inputs(PV, SV, MV, rpm=a.rpm, amps=a.amps, T_adapter=a.tadapter,
                      CT=CT if self.any_ct else None, tcu_b_sv=a.tcu_b, tcu_s_sv=a.tcu_s,
                      T_tower=a.tower, T_air=a.air, T_feed_jacket=a.jacket), faults


# ------------------------------------------------------------------ simulator
class SimPlant:
    """a 'true' plant with the same equations, different constants, its own PXR PI loops,
    and injected drifts: a viscosity front from the feed, warm tower water, a lost heater element."""

    def __init__(self, p, rc, dt):
        self.p, self.rc, self.dt = p, rc, dt
        self.truth = Plant(hA_w=[x * 1.2 for x in p.hA_w], UA_oil=[x * 0.9 for x in p.UA_oil], k_pump=p.k_pump * 1.05,
                           UA_hx_b=250.0)                                 # a fouled exchanger: less cooling authority than the twin assumes
        self.SV = [130, 140, 150, 155, 160, 160, 160, 160, 175, 175, 178, 182, 182, 182, 182, 182, 182, 182]
        self.st = State(T_m=[60 + 115 * min(i, 7) / 7 for i in range(N)], T_b=[float(s) for s in self.SV],
                        T_oil_b=140.0, T_oil_s=120.0, T_s=130.0)
        self.I = [0.0] * N
        self.MV = [0.3] * N
        self.kappa_base, self.kappa_true = 1.8, [1.8] * N
        self.P_heat_true = list(p.P_heat)
        self.T_tower = 30.0
        self.rpm, self.t = 60.0, 0.0
        for _ in range(int(1800 / dt)):                                  # let the plant settle before the twin starts
            self._plant_step()

    def controllers(self):
        Kp, Ti = 0.08, 200.0
        for i in range(N):
            err = self.SV[i] - self.st.T_b[i]
            self.I[i] = clamp(self.I[i] + err * self.dt / Ti, -1.0 / Kp, 1.0 / Kp)
            self.MV[i] = clamp(Kp * (err + self.I[i]), 0.0, 1.0)

    def disturbances(self, residence):
        t = self.t
        # 1500 s: viscosity +15 % entering with the feed and advancing zone by zone
        cum = 0.0
        for i in BARREL:
            cum += residence[i] * 3.0                                   # solids/melting transit is slower than plug flow
            self.kappa_true[i] = self.kappa_base * (1.15 if t > 1500 + cum else 1.0)
        # 3000 s: tower water climbs 30 -> 55 over ten minutes (hot afternoon, tower fan down) and the TCU saturates
        self.T_tower = 30.0 + 25.0 * clamp((t - 3000) / 600.0, 0.0, 1.0)
        # 3600 s: die zone D3 (a zone that is actually heating) loses 40 % of its heater
        self.P_heat_true[13] = self.p.P_heat[13] * (0.6 if t > 3600 else 1.0)

    def measure(self):
        """motor power comes from the melt, not the other way round"""
        w = viscous_weights(self.truth, self.rc, self.st, self.rpm)
        P = sum(self.kappa_true[i] * w[i] for i in BARREL) / (1.0 - self.truth.solids_share)
        amps = self.truth.I_noload + P / (self.truth.kW_per_amp * 1000.0)
        return amps

    def _plant_step(self):
        import random
        self.controllers()
        amps = self.measure()
        u = Inputs(PV=[x + random.gauss(0, 0.1) for x in self.st.T_b], SV=[float(s) for s in self.SV],
                   MV=list(self.MV), rpm=self.rpm, amps=amps + random.gauss(0, 1.0),
                   T_adapter=self.st.T_m[ADAPTER] + random.gauss(0, 0.2),
                   CT=[self.MV[i] * self.P_heat_true[i] / self.truth.V_line for i in range(N)],
                   T_tower=self.T_tower, T_air=35.0, T_feed_jacket=40.0)
        self.st, diag = step(self.truth, self.rc, self.st, u, self.dt, P_heat=self.P_heat_true, T_tower=self.T_tower)
        return u, diag

    def advance(self):
        u, diag = self._plant_step()
        self.disturbances(diag["residence"])
        self.t += self.dt
        # the twin does NOT get to see tower water in this demo -> it surfaces as unexplained thermal drift
        u.T_tower = 30.0
        return u, []


# ----------------------------------------------------------------------- main
def run(source, dt, duration, csv_path, quiet=False):
    p, rc = Plant(), Recipe()
    u, faults = source.poll() if hasattr(source, "poll") else source.advance()
    st = State(T_m=[u.SV[i] + (15.0 if i >= 4 else -20.0 + 8.0 * i) for i in range(N)],
               T_b=list(u.PV), T_oil_b=u.tcu_b_sv, T_oil_s=u.tcu_s_sv, T_s=u.tcu_s_sv + 10.0)
    est = Estimator(p, rc)
    heat_bank = Bank("heater", dt, theta=0.06)
    therm_bank = Bank("thermal", dt, theta=0.06)
    cross = CrossModal(dt)
    emitter = EventEmitter(bands=dict(PV=0.5, MV=0.02, amps=2.0, rpm=0.5, Tad=0.5))
    hold, latched = [1.0] * N, [False] * N
    fight_kWh, n_events_total, last_kappa_alarm, kappa_at_alarm = 0.0, 0, -1e9, 0.0
    drift_hist = deque(maxlen=int(600 / dt) + 1)

    cols = (["t", "rpm", "amps", "T_adapter", "kappa", "drift_index", "mdot_kg_s", "P_mech_kW",
             "fight_kW", "fight_kWh", "H_index", "drool_index", "T_lip", "T_oil_b", "T_oil_s", "T_screw",
             "direction_heater", "direction_thermal", "n_events", "r_adapter", "unexplained_kW"]
            + [f"Tm_{z}" for z in ZONE_NAMES] + [f"Tb_{z}" for z in ZONE_NAMES]
            + [f"e_{z}" for z in ZONE_NAMES] + [f"rq_{z}" for z in ZONE_NAMES]
            + [f"Mh_{k}" for k in heat_bank.templates] + [f"Mt_{k}" for k in therm_bank.templates]
            + ["M_cross", "alarms"])
    f = open(csv_path, "w", newline="")
    wr = csv.writer(f)
    wr.writerow(cols)

    t, next_print = 0.0, 0.0
    while t < duration:
        # 1. predict
        st, diag = step(p, rc, st, u, dt)
        # 2. correct
        corr = est.correct(st, u, dt)
        # 3. expect (efference copy) -> heater residual
        mv_exp, _ = mv_expected(p, st, u)
        e = [u.MV[i] - mv_exp[i] for i in range(N)]
        # 4. detect
        drift_hist.append(corr["drift_index"])
        global_active = abs(drift_hist[-1] - drift_hist[0]) > 0.02          # kappa moving right now, not merely offset
        h = heat_bank.update(e, diag["residence"], t, global_active)
        th = therm_bank.update(corr["r_q"], diag["residence"], t, global_active)
        x = cross.update(h["ep"], corr["r_k"], t)
        alarms = h["alarms"] + th["alarms"] + ([x] if x else []) + heater_check(p, u, hold, latched) + faults
        if t > 600 and abs(corr["drift_index"] - kappa_at_alarm) > 0.05 and t - last_kappa_alarm > 600:
            last_kappa_alarm, kappa_at_alarm = t, corr["drift_index"]
            alarms.append(f"[kappa] viscosity multiplier {corr['drift_index']:+.1%} vs calibration: recipe/plasticizer/wear side")
        events = emitter.update({**{f"PV{i}": u.PV[i] for i in range(N)}, **{f"MV{i}": u.MV[i] for i in range(N)},
                                 "amps": u.amps, "rpm": u.rpm, "Tad": u.T_adapter})
        n_events_total += len(events)
        fight_kWh += diag["fight_W"] * dt / 3.6e6
        # 5. log
        wr.writerow([f"{t:.0f}", f"{u.rpm:.1f}", f"{u.amps:.1f}", "" if u.T_adapter is None else f"{u.T_adapter:.1f}",
                     f"{st.kappa:.4f}", f"{corr['drift_index']:+.4f}", f"{diag['mdot']:.3f}", f"{diag['P_mech'] / 1e3:.1f}",
                     f"{diag['fight_W'] / 1e3:.2f}", f"{fight_kWh:.3f}", f"{diag['H_index']:.1f}", f"{diag['drool_index']:.3f}",
                     f"{diag['T_lip']:.1f}", f"{st.T_oil_b:.1f}", f"{st.T_oil_s:.1f}", f"{st.T_s:.1f}",
                     f"{h['direction']:+.2f}", f"{th['direction']:+.2f}", len(events),
                     f"{corr['r_a']:+.2f}", f"{sum(corr['q_unexpl']) / 1e3:+.2f}"]
                    + [f"{v:.1f}" for v in st.T_m] + [f"{v:.1f}" for v in st.T_b]
                    + [f"{v:+.3f}" for v in e] + [f"{v:+.3f}" for v in corr["r_q"]]
                    + [f"{v:+.4f}" for v in h["M"].values()] + [f"{v:+.3f}" for v in th["M"].values()]
                    + [f"{cross.M:+.2f}", " | ".join(alarms)])
        for a in alarms:
            print(f"t={t:6.0f}s  {a}")
        if not quiet and t >= next_print:
            print(f"t={t:6.0f}s  amps={u.amps:5.0f}  adapter={u.T_adapter or 0:6.1f}  twin_AD={st.T_m[ADAPTER]:6.1f}  "
                  f"B8 melt={st.T_m[7]:6.1f}  oil={st.T_oil_b:5.1f}  kappa={st.kappa:5.2f} ({corr['drift_index']:+.1%})  "
                  f"fight={diag['fight_W'] / 1e3:4.1f}kW  Mh_barrel={h['M']['barrel']:+.3f}  Mt_barrel={th['M']['barrel']:+.2f}  "
                  f"dir={h['direction']:+.2f}  events={len(events)}")
            next_print += 300.0
        # next sample
        t += dt
        if hasattr(source, "poll"):
            t0 = time.time()
            u, faults = source.poll()
            dt = max(time.time() - t0, 0.5)                                # real sweep time becomes the step
        else:
            u, faults = source.advance()
    f.close()
    print(f"done: {csv_path}  fighting-loop waste {fight_kWh:.2f} kWh  events emitted {n_events_total} "
          f"(vs {int(duration / dt) * (2 * N + 3)} raw samples)")


def main():
    ap = argparse.ArgumentParser(description="extrusion line melt-state twin, first slice")
    ap.add_argument("--sim", type=float, default=0.0, help="run the simulator for this many seconds")
    ap.add_argument("--dt", type=float, default=1.0, help="simulator step, s")
    ap.add_argument("--port", help="serial port of the RS-485 adapter, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--parity", default="O", choices=["O", "E", "N"], help="PXR CoM: 0=O 1=E 2=N")
    ap.add_argument("--stations", default=",".join(str(i) for i in range(1, 19)),
                    help="PXR station numbers for zones B1..D7 in order")
    ap.add_argument("--rpm", type=float, default=60.0)
    ap.add_argument("--amps", type=float, default=285.0)
    ap.add_argument("--tadapter", type=float, default=None, help="adapter melt temperature if not automated")
    ap.add_argument("--tcu-b", dest="tcu_b", type=float, default=140.0)
    ap.add_argument("--tcu-s", dest="tcu_s", type=float, default=120.0)
    ap.add_argument("--tower", type=float, default=30.0)
    ap.add_argument("--air", type=float, default=35.0)
    ap.add_argument("--jacket", type=float, default=40.0)
    ap.add_argument("--hours", type=float, default=8.0, help="live run length")
    ap.add_argument("--csv", default="twin_log.csv")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.sim > 0:
        run(SimPlant(Plant(), Recipe(), a.dt), a.dt, a.sim, a.csv, a.quiet)
    elif a.port:
        stations = [int(s) for s in a.stations.split(",")]
        if len(stations) != N:
            sys.exit(f"need {N} station numbers, got {len(stations)}")
        run(LiveBus(a.port, a.parity, stations, a), 1.5, a.hours * 3600.0, a.csv, a.quiet)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
