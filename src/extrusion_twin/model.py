"""
model.py -- physics and parameter objects for the extrusion line twin.

The STRUCTURE here is portable (any single-screw line with zone heaters, an oil-jacketed
barrel, a screw TCU and a wide sheet die). Every number tagged FIT / GUESS is a placeholder
for the machine and belongs in the plant-constants file, not in this module.

Melt path:  jacket -> B1..B8 -> SC1 -> SC2 -> AD -> (D1..D7 side by side) -> lip
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

N = 18
BARREL = list(range(0, 8))
SCREEN = [8, 9]
ADAPTER = 10
DIE = list(range(11, 18))
ZONE_NAMES = [f"B{i + 1}" for i in range(8)] + ["SC1", "SC2", "AD"] + [f"D{i + 1}" for i in range(7)]
CORES: Dict[str, List[int]] = {"barrel": BARREL, "screen": SCREEN + [ADAPTER], "die": DIE}
CORE_OF = {i: name for name, zs in CORES.items() for i in zs}


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def upstream_of(i: int) -> Optional[int]:
    """Series chain through the adapter; every die zone is fed from the adapter (fan-out)."""
    if i == 0:
        return None
    if i in DIE:
        return ADAPTER
    return i - 1


def mass_flow(i: int, mdot: float) -> float:
    return mdot / len(DIE) if i in DIE else mdot


# ------------------------------------------------------------------ parameter objects
@dataclass
class Recipe:
    """Everything the melt model reads from the recipe. Flexible-PVC placeholders."""
    name: str = "flexible PVC (placeholder)"
    rho: float = 1350.0        # kg/m3, filled plasticized compound              FIT
    cp: float = 1600.0         # J/kg/K                                          FIT
    K_ref: float = 1.5e4       # power-law consistency at T_ref, Pa.s^n           FIT
    n: float = 0.40            # power-law index (well below 1)                   FIT
    EaR: float = 4500.0        # flow activation energy / R, K                    FIT
    T_ref: float = 170.0       # degC reference for K_ref
    EaR_deg: float = 12000.0   # stabilizer consumption activation energy / R, K  FIT
    T_ref_deg: float = 175.0   # degC reference for the degradation index
    plasticizer_phr: float = 45.0   # bookkeeping for now: lowers K_ref steeply
    filler_phr: float = 30.0        # raises K_ref, rho, conductivity

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plant:
    """Machine geometry and conductances. Structure generic; numbers FIT."""
    D: float = 0.152                                   # screw diameter m                    NAMEPLATE
    L_zone: float = 0.50                               # barrel zone length m                NAMEPLATE
    h_channel: list = field(default_factory=lambda:    # channel depth per barrel zone, m    NAMEPLATE
                            [0.020, 0.020, 0.016, 0.012, 0.009, 0.007, 0.007, 0.007])
    solids_share: float = 0.35                         # motor power dissipated in solids conveying (B1-B2)  FIT
    k_pump: float = 0.0055                             # kg/s per RPM (60 RPM ~ 1190 kg/h)   FIT
    kW_per_amp: float = 0.78                           # shaft kW per motor amp above no-load FIT
    I_noload: float = 25.0                             # A                                    NAMEPLATE
    T_feed_offset: float = 5.0                         # solids enter at jacket temp + this   FIT
    k_lip: float = 0.15                                # lip cools this fraction toward air   FIT
    V_line: float = 480.0                              # heater line voltage                  NAMEPLATE
    P_heat: list = field(default_factory=lambda: [8000.0] * 8 + [4000.0] * 2 + [3000.0] + [5000.0] * 7)   # W  NAMEPLATE
    C_b: list = field(default_factory=lambda: [1.0e5] * 8 + [4.0e4] * 2 + [3.0e4] + [6.0e4] * 7)          # J/K GUESS
    hA_w: list = field(default_factory=lambda: [400.0] * 8 + [80.0] * 2 + [60.0] + [120.0] * 7)           # W/K FIT
    hA_s: list = field(default_factory=lambda: [60.0] * 8 + [0.0] * 10)                                    # W/K FIT
    UA_oil: list = field(default_factory=lambda: [600.0] * 8 + [0.0] * 10)                                 # W/K FIT
    UA_amb: list = field(default_factory=lambda: [25.0] * 8 + [20.0] * 10)                                 # W/K FIT
    V_m: list = field(default_factory=list)                                                                # m3 (computed)
    rho_oil: float = 850.0                                                         # heat transfer oil kg/m3
    cp_oil: float = 2100.0                                                         # J/kg/K
    flow_b_nom: float = 40.0                                                       # barrel loop L/min  NAMEPLATE
    flow_s_nom: float = 20.0                                                       # screw loop L/min   NAMEPLATE
    gear_ratio: float = 20.0                                                       # motor rev per screw rev NAMEPLATE
    C_oil_b: float = 8.5e4;   UA_hx_b: float = 600.0;  P_tcu_b: float = 12000.0   # barrel TCU   GUESS/FIT
    C_oil_s: float = 3.0e4;   UA_hx_s: float = 300.0;  P_tcu_s: float = 6000.0    # screw TCU    GUESS/FIT
    C_s: float = 7.0e4;       UA_screw_oil: float = 400.0                          # screw core   GUESS/FIT
    tau_tcu: float = 15.0                                                          # TCU tracking time constant s

    def __post_init__(self):
        if not self.V_m:
            barrel = [math.pi * self.D * h * self.L_zone * 0.9 for h in self.h_channel]
            self.V_m = barrel + [0.0020, 0.0020] + [0.0015] + [0.0015] * 7        # m3  GUESS

    @classmethod
    def from_dict(cls, d: dict) -> "Plant":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ state and inputs
@dataclass
class State:
    T_m: list                    # melt degC per zone
    T_b: list                    # barrel/die metal degC per zone
    T_oil_b: float               # barrel oil loop degC
    T_oil_s: float               # screw oil loop degC
    T_s: float                   # screw core degC
    kappa: float = 1.0           # viscosity multiplier (learned; its drift = material-side drift index)
    hw: float = 1.0              # wall-conductance multiplier (learned)
    ua: float = 1.0              # barrel-to-oil conductance multiplier (learned once the oil is measured)

    def copy(self) -> "State":
        return State(self.T_m[:], self.T_b[:], self.T_oil_b, self.T_oil_s, self.T_s,
                     self.kappa, self.hw, self.ua)

    # vector form for the ensemble filter:
    # [T_m(18), T_b(18), T_oil_b, T_oil_s, T_s, ln kappa, ln hw, ln ua]
    DIM = 2 * N + 6

    def to_vector(self) -> list:
        return self.T_m + self.T_b + [self.T_oil_b, self.T_oil_s, self.T_s,
                                      math.log(max(self.kappa, 1e-6)), math.log(max(self.hw, 1e-6)),
                                      math.log(max(self.ua, 1e-6))]

    @classmethod
    def from_vector(cls, v) -> "State":
        v = list(v)
        return cls(v[:N], v[N:2 * N], v[2 * N], v[2 * N + 1], v[2 * N + 2],
                   math.exp(v[2 * N + 3]), math.exp(v[2 * N + 4]), math.exp(v[2 * N + 5]))


@dataclass
class Inputs:
    PV: list                     # degC per zone (metal thermocouple)
    SV: list                     # degC per zone
    MV: list                     # heater duty fraction 0..1 per zone
    rpm: float
    amps: float
    T_adapter: Optional[float] = None      # melt probe degC
    CT: Optional[list] = None              # heater current A per zone (PXR CT option)
    tcu_b_sv: float = 140.0                # barrel TCU setpoint degC (or measured supply temp)
    tcu_s_sv: float = 120.0
    T_tower: float = 30.0                  # cooling tower supply degC
    T_air: float = 35.0                    # plant air degC
    T_feed_jacket: float = 40.0            # feed-throat water jacket degC
    P_adapter: Optional[float] = None      # melt pressure, bar
    rpm_motor: Optional[float] = None      # drive motor speed, ahead of the gearbox. `rpm` is the screw.
    T_oil_b_supply: Optional[float] = None   # barrel loop: oil leaving the TCU, degC
    T_oil_b_return: Optional[float] = None   # barrel loop: oil coming back, degC
    T_oil_s_supply: Optional[float] = None   # screw loop
    T_oil_s_return: Optional[float] = None
    T_tower_return: Optional[float] = None   # tower water leaving the barrel exchanger, degC
    flow_oil_b: Optional[float] = None       # barrel loop flow, L/min -- turns a delta into watts
    flow_oil_s: Optional[float] = None
    crammer_rpm: Optional[float] = None    # crammer (force feeder) screw speed
    crammer_amps: Optional[float] = None   # crammer motor current -- a feed-side measurement,
                                           # NOT the extruder drive. Never mix the two.
    contacts: dict = field(default_factory=dict)   # name -> 0/1 (fans, valves, contactors)
    live: dict = field(default_factory=dict)       # channel -> 'bus'|'analog'|'ops'|'manual'|'stale'|'sim'


# ------------------------------------------------------------------ physics primitives
def shear_rate(p: Plant, i: int, rpm: float) -> float:
    return math.pi * p.D * (rpm / 60.0) / p.h_channel[i]


def viscosity(rc: Recipe, T: float, gdot: float) -> float:
    TK, TrK = T + 273.15, rc.T_ref + 273.15
    return rc.K_ref * math.exp(rc.EaR * (1.0 / TK - 1.0 / TrK)) * max(gdot, 1e-3) ** (rc.n - 1.0)


def viscous_weights(p: Plant, rc: Recipe, T_m: list, rpm: float) -> list:
    """eta * gdot^2 * V per barrel zone; B1-B2 carry solids (friction), weight 0."""
    w = [0.0] * N
    for i in BARREL:
        if i >= 2:
            g = shear_rate(p, i, rpm)
            w[i] = viscosity(rc, T_m[i], g) * g * g * p.V_m[i]
    return w


def mech_power_meas(p: Plant, amps: float) -> float:
    return p.kW_per_amp * 1000.0 * max(amps - p.I_noload, 0.0)


def mech_power_pred(p: Plant, rc: Recipe, T_m: list, rpm: float, kappa: float) -> float:
    """what the viscosity model says the screw should draw at this melt state"""
    return kappa * sum(viscous_weights(p, rc, T_m, rpm)) / (1.0 - p.solids_share)


def shear_split(p: Plant, rc: Recipe, T_m: list, rpm: float, P_mech: float) -> list:
    w = viscous_weights(p, rc, T_m, rpm)
    sw = sum(w) or 1.0
    Q = [0.0] * N
    for i in BARREL:
        Q[i] = P_mech * p.solids_share / 2.0 if i < 2 else P_mech * (1.0 - p.solids_share) * w[i] / sw
    return Q


def loop_watts(p: Plant, dT: Optional[float], flow_lpm: Optional[float], nominal: float) -> Optional[float]:
    """Heat carried by a TCU loop: Q = mdot * cp * (return - supply). With no flow meter the nominal
    pump rate is used, which is right until the flow itself is the thing that changed -- and that case
    shows up as a delta moving with no load change, which the loop watcher flags on its own."""
    if dT is None:
        return None
    lpm = nominal if flow_lpm is None else flow_lpm
    mdot = lpm / 60000.0 * p.rho_oil
    return mdot * p.cp_oil * dT


def tcu_flux(p: Plant, T_oil: float, sv: float, T_tower: float, C: float, UA_hx: float, P_tcu: float) -> float:
    """well-tuned TCU: drives oil toward sv within heating / tower-limited cooling authority. W into oil."""
    want = C * (sv - T_oil) / p.tau_tcu
    return clamp(want, -UA_hx * max(T_oil - T_tower, 0.0), P_tcu)


# ------------------------------------------------------------------ the step
def step(p: Plant, rc: Recipe, st: State, u: Inputs, dt: float, P_heat: Optional[list] = None,
         T_tower: Optional[float] = None, zones: Optional[List[int]] = None,
         boundary: Optional[Dict[int, float]] = None, parts=("zones", "oil", "screw")):
    """One explicit-Euler step. `zones` restricts the update to a subset (a core); `boundary`
    supplies the upstream melt temperature for zones whose upstream node lives in another core.
    Returns (new state, diagnostics)."""
    P_heat = P_heat or p.P_heat
    T_tower = u.T_tower if T_tower is None else T_tower
    zones = list(range(N)) if zones is None else zones
    boundary = boundary or {}
    mdot = p.k_pump * max(u.rpm, 0.0)
    P_mech = mech_power_meas(p, u.amps)
    Q_shear = shear_split(p, rc, st.T_m, u.rpm, P_mech)
    T_in = u.T_feed_jacket + p.T_feed_offset
    new = st.copy()
    Q_cool = [0.0] * N

    if "zones" in parts:
        for i in zones:
            up = upstream_of(i)
            T_prev = boundary[i] if i in boundary else (T_in if up is None else st.T_m[up])
            C = rc.rho * rc.cp * p.V_m[i]
            q = (mass_flow(i, mdot) * rc.cp * (T_prev - st.T_m[i]) + Q_shear[i]
                 + st.hw * p.hA_w[i] * (st.T_b[i] - st.T_m[i]) + p.hA_s[i] * (st.T_s - st.T_m[i]))
            new.T_m[i] = st.T_m[i] + dt * q / C
        for i in zones:
            Q_cool[i] = st.ua * p.UA_oil[i] * (st.T_b[i] - st.T_oil_b)
            q = (u.MV[i] * P_heat[i] - st.hw * p.hA_w[i] * (st.T_b[i] - st.T_m[i]) - Q_cool[i]
                 - p.UA_amb[i] * (st.T_b[i] - u.T_air))
            new.T_b[i] = st.T_b[i] + dt * q / p.C_b[i]
    else:
        for i in zones:
            Q_cool[i] = st.ua * p.UA_oil[i] * (st.T_b[i] - st.T_oil_b)

    if "oil" in parts:
        q_tcu_b = tcu_flux(p, st.T_oil_b, u.tcu_b_sv, T_tower, p.C_oil_b, p.UA_hx_b, p.P_tcu_b)
        new.T_oil_b = st.T_oil_b + dt * (q_tcu_b + sum(Q_cool)) / p.C_oil_b
    if "screw" in parts:
        q_tcu_s = tcu_flux(p, st.T_oil_s, u.tcu_s_sv, T_tower, p.C_oil_s, p.UA_hx_s, p.P_tcu_s)
        q_screw = sum(p.hA_s[i] * (st.T_m[i] - st.T_s) for i in BARREL)
        new.T_oil_s = st.T_oil_s + dt * (q_tcu_s + p.UA_screw_oil * (st.T_s - st.T_oil_s)) / p.C_oil_s
        new.T_s = st.T_s + dt * (q_screw - p.UA_screw_oil * (st.T_s - st.T_oil_s)) / p.C_s

    diag = indices(p, rc, st, u, mdot, Q_cool, P_heat)
    diag.update(P_mech=P_mech, Q_shear=Q_shear, Q_cool=Q_cool, mdot=mdot)
    return new, diag


def indices(p: Plant, rc: Recipe, st: State, u: Inputs, mdot: float, Q_cool: list, P_heat: list) -> dict:
    """fighting loops, degradation exposure, drool propensity, residence per zone"""
    fight_W = sum(min(u.MV[i] * P_heat[i], max(Q_cool[i], 0.0)) for i in BARREL)
    res = [rc.rho * p.V_m[i] / max(mass_flow(i, mdot), 1e-6) for i in range(N)]
    deg = sum(res[i] * math.exp(-rc.EaR_deg * (1.0 / (st.T_m[i] + 273.15) - 1.0 / (rc.T_ref_deg + 273.15)))
              for i in range(2, N))
    T_die_melt = sum(st.T_m[i] for i in DIE) / len(DIE)
    T_die_metal = sum(st.T_b[i] for i in DIE) / len(DIE)
    T_lip = T_die_metal - p.k_lip * (T_die_metal - u.T_air)
    drool = math.exp((T_die_melt - T_lip) / 10.0) - 1.0
    return dict(fight_W=fight_W, residence=res, H_index=deg, drool_index=drool, T_lip=T_lip)


def mv_expected(p: Plant, st: State, u: Inputs, zones: Optional[List[int]] = None, tau_ctrl: float = 120.0):
    """the efference copy: heater duty each zone SHOULD need to hold its metal where it is and
    close the SV error over tau_ctrl. Residual = MV_measured - this."""
    zones = list(range(N)) if zones is None else zones
    out, want = [0.0] * N, [0.0] * N
    for i in zones:
        Q_need = (st.hw * p.hA_w[i] * (st.T_b[i] - st.T_m[i]) + st.ua * p.UA_oil[i] * (st.T_b[i] - st.T_oil_b)
                  + p.UA_amb[i] * (st.T_b[i] - u.T_air) + p.C_b[i] * (u.SV[i] - u.PV[i]) / tau_ctrl)
        want[i] = Q_need
        out[i] = clamp(Q_need / p.P_heat[i], 0.0, 1.0)
    return out, want
