"""
estimators.py -- predict/correct engines. Both expose

    st_new, diag, corr = est.update(st, u, dt, boundary=None)

`corr` always carries: r_b (PV - model metal, per zone), r_a (adapter innovation), r_k (motor
power innovation, relative), drift_index (kappa vs calibration), q_unexpl / r_q (heat the
model was missing at each metal node, W and in heater-duty units), spread (1-sigma melt
uncertainty per zone; zeros for the fixed-gain corrector).

FixedGain  -- v1 corrector: fixed gains, measurement by measurement. Cheap, local, no numpy.
EnKF       -- stochastic ensemble Kalman filter over the full state incl. ln kappa and ln hw.
              Learns the two "connectome weights" as augmented states, carries a real
              uncertainty for the melt between probes. Needs numpy.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from .model import (ADAPTER, BARREL, N, Inputs, Plant, Recipe, State, loop_watts, mech_power_meas,
                    mech_power_pred, step, viscous_weights)


class FixedGain:
    """heuristic observer: fixed gains, measurement-by-measurement (v1)."""

    def __init__(self, p: Plant, rc: Recipe, zones: Optional[List[int]] = None, learn_kappa: bool = True,
                 parts=("zones", "oil", "screw"), g_b=0.05, g_a=0.20, g_k=0.002, calib_steps=180):
        self.p, self.rc = p, rc
        self.zones = list(range(N)) if zones is None else list(zones)
        self.learn_kappa, self.parts = learn_kappa, parts
        self.g_b, self.g_a, self.g_k = g_b, g_a, g_k
        self.calib_steps, self.n, self.kappa_cal = calib_steps, 0, None

    def update(self, st: State, u: Inputs, dt: float, boundary: Optional[Dict[int, float]] = None):
        st, diag = step(self.p, self.rc, st, u, dt, zones=self.zones, boundary=boundary, parts=self.parts)
        p, rc = self.p, self.rc
        r_b = [0.0] * N
        for i in self.zones:
            r_b[i] = u.PV[i] - st.T_b[i]
            st.T_b[i] += self.g_b * r_b[i]
        r_a = 0.0
        if u.T_adapter is not None and ADAPTER in self.zones:
            r_a = u.T_adapter - st.T_m[ADAPTER]
            for i in self.zones:
                st.T_m[i] += self.g_a * r_a * math.exp(-abs(i - ADAPTER) / 3.0)
        if u.T_oil_b_supply is not None and "oil" in self.parts:
            st.T_oil_b += 0.30 * (u.T_oil_b_supply - st.T_oil_b)
        if u.T_oil_s_supply is not None and "screw" in self.parts:
            st.T_oil_s += 0.30 * (u.T_oil_s_supply - st.T_oil_s)
        r_k = 0.0
        if self.learn_kappa:
            P_meas = mech_power_meas(p, u.amps)
            base = mech_power_pred(p, rc, st.T_m, u.rpm, 1.0)
            if base > 1.0 and P_meas > 0.0:
                if self.n < self.calib_steps:                                    # auto-calibrate the multiplier
                    ratio = P_meas / base
                    st.kappa = ratio if self.n == 0 else 0.98 * st.kappa + 0.02 * ratio
                    self.kappa_cal = st.kappa
                else:
                    r_k = (P_meas - st.kappa * base) / (st.kappa * base)
                    st.kappa *= 1.0 + self.g_k * r_k
            self.n += 1
        drift_index = (st.kappa / self.kappa_cal - 1.0) if self.kappa_cal else 0.0
        q_unexpl = [p.C_b[i] * self.g_b * r_b[i] / dt for i in range(N)]
        r_q = [q_unexpl[i] / p.P_heat[i] for i in range(N)]
        corr = dict(r_b=r_b, r_a=r_a, r_k=r_k, drift_index=drift_index, r_q=r_q, q_unexpl=q_unexpl,
                    spread=[0.0] * N, kappa=st.kappa, hw=st.hw, ua=st.ua)
        return st, diag, corr


class EnKF:
    """Stochastic (perturbed-observation) ensemble Kalman filter over the full state vector
    [T_m(18), T_b(18), T_oil_b, T_oil_s, T_s, ln kappa, ln hw]. Observations: every PV in the
    core's zones, the adapter melt probe when present, and motor mechanical power (nonlinear
    in kappa and T_m; handled by sampling)."""

    def __init__(self, p: Plant, rc: Recipe, zones: Optional[List[int]] = None, learn_kappa: bool = True,
                 parts=("zones", "oil", "screw"), ne: int = 32, seed: int = 0, calib_steps: int = 180,
                 inflation: float = 1.01):
        try:
            import numpy as np
        except ImportError as exc:                      # the one dependency, and only on this path
            raise RuntimeError(
                "The ensemble filter needs numpy: pip install numpy  "
                "(under Termux use pkg install python-numpy). "
                "The fixed-gain estimator needs nothing but the standard library.") from exc
        self.np = np
        self.p, self.rc = p, rc
        self.zones = list(range(N)) if zones is None else list(zones)
        self.learn_kappa, self.parts = learn_kappa, parts
        self.ne, self.calib_steps, self.infl = ne, calib_steps, inflation
        self.rng = np.random.default_rng(seed)
        self.X = None
        self.n, self.kappa_cal = 0, None
        # process noise (1-sigma per sqrt-second), masked to what this filter owns
        q = np.array([0.08] * N + [0.03] * N + [0.05, 0.05, 0.05, 0.003, 0.001, 0.002])
        mask = np.zeros(State.DIM)
        for i in self.zones:
            mask[i] = mask[N + i] = 1.0
        if "oil" in parts:
            mask[2 * N] = 1.0
        if "screw" in parts:
            mask[2 * N + 1] = mask[2 * N + 2] = 1.0
        if learn_kappa:
            mask[2 * N + 3] = 1.0
        mask[2 * N + 4] = 1.0 if self.zones else 0.0
        # the barrel-to-oil conductance is only identifiable once the oil loop is measured; without a
        # supply probe it is indistinguishable from the oil temperature itself, so leave it alone
        # ...and it is only learnable when the loop's heat flow is actually measured, which needs a
        # flow meter. Without one this stays at 1.0 and the oil conductance is whatever the config says.
        self.can_learn_ua = "oil" in parts
        mask[2 * N + 5] = 1.0 if self.can_learn_ua else 0.0
        self.q, self.mask = q * mask, mask
        # observation noise (1-sigma)
        self.r_pv, self.r_ad, self.r_p_rel, self.r_p_min = 0.3, 0.4, 0.02, 2000.0
        self.r_oil = 0.4
        self.r_q_rel_meter, self.r_q_rel_nominal, self.r_q_min = 0.10, 0.25, 800.0

    def init(self, st: State):
        np = self.np
        x0 = np.array(st.to_vector())
        spread = np.array([1.5] * N + [0.5] * N + [1.0, 1.0, 1.0, 0.25, 0.10, 0.15]) * self.mask
        self.X = x0[:, None] + spread[:, None] * self.rng.standard_normal((State.DIM, self.ne))

    def update(self, st: State, u: Inputs, dt: float, boundary: Optional[Dict[int, float]] = None):
        np = self.np
        p, rc = self.p, self.rc
        if self.X is None:
            self.init(st)
        # ---- forecast every member through the physics
        for j in range(self.ne):
            s = State.from_vector(self.X[:, j])
            s2, _ = step(p, rc, s, u, dt, zones=self.zones, boundary=boundary, parts=self.parts)
            v = np.array(s2.to_vector()) + self.q * math.sqrt(dt) * self.rng.standard_normal(State.DIM)
            if not (self.can_learn_ua and u.flow_oil_b):
                v[2 * N + 5] = 0.0                      # ln(ua) = 0: hold it at 1.0 with nothing to fit it
            self.X[:, j] = v
        xf = self.X.mean(axis=1)
        st_f = State.from_vector(xf)
        _, diag = step(p, rc, st_f, u, dt, zones=self.zones, boundary=boundary, parts=self.parts)
        # ---- observations available this sweep
        y, rows, R = [], [], []
        for i in self.zones:
            y.append(u.PV[i]); rows.append(N + i); R.append(self.r_pv ** 2)
        if u.T_adapter is not None and ADAPTER in self.zones:
            y.append(u.T_adapter); rows.append(ADAPTER); R.append(self.r_ad ** 2)
        if u.T_oil_b_supply is not None and "oil" in self.parts:     # the TCU supply IS the loop state
            y.append(u.T_oil_b_supply); rows.append(2 * N); R.append(self.r_oil ** 2)
        if u.T_oil_s_supply is not None and "screw" in self.parts:
            y.append(u.T_oil_s_supply); rows.append(2 * N + 1); R.append(self.r_oil ** 2)
        # The loop delta measures the barrel-to-oil heat flow directly, and that is the only thing
        # that identifies the oil conductance: the supply temperature alone cannot, because the TCU
        # holds it at setpoint whatever the load.
        #
        # It is used ONLY when a flow meter exists. With flow assumed from the nameplate, a real flow
        # change reads as a heat change and gets baked into the conductance, which then corrupts every
        # zone that touches the oil. Measured quantities go in the filter; assumed ones stay in the
        # detectors, where a wrong assumption shows up as an alarm instead of a silently bent parameter.
        Q_meas = None
        if ("oil" in self.parts and u.flow_oil_b and u.T_oil_b_supply is not None
                and u.T_oil_b_return is not None and set(BARREL) <= set(self.zones)):
            Q_meas = loop_watts(p, u.T_oil_b_return - u.T_oil_b_supply, u.flow_oil_b, p.flow_b_nom)
            if Q_meas and Q_meas > 500.0:
                y.append(Q_meas); rows.append(-2)
                R.append(max(self.r_q_min, self.r_q_rel_meter * Q_meas) ** 2)
            else:
                Q_meas = None
        P_meas = mech_power_meas(p, u.amps) if self.learn_kappa else 0.0
        use_p = self.learn_kappa and P_meas > 0.0
        if use_p:
            y.append(P_meas); rows.append(-1); R.append(max(self.r_p_min, self.r_p_rel * P_meas) ** 2)
        m = len(y)
        r_k, r_a = 0.0, 0.0
        if m:
            Y = np.zeros((m, self.ne))
            for k, row in enumerate(rows):
                if row >= 0:
                    Y[k, :] = self.X[row, :]
                elif row == -1:                                         # motor power: nonlinear in the state
                    for j in range(self.ne):
                        s = State.from_vector(self.X[:, j])
                        Y[k, j] = mech_power_pred(p, rc, s.T_m, u.rpm, s.kappa)
                else:                                                   # barrel-to-oil heat flow
                    for j in range(self.ne):
                        s = State.from_vector(self.X[:, j])
                        Y[k, j] = sum(s.ua * p.UA_oil[i] * (s.T_b[i] - s.T_oil_b) for i in BARREL)
            ym = Y.mean(axis=1)
            A = self.X - xf[:, None]
            B = Y - ym[:, None]
            Pxy = A @ B.T / (self.ne - 1)
            Pyy = B @ B.T / (self.ne - 1) + np.diag(R)
            K = np.linalg.solve(Pyy.T, Pxy.T).T
            yv = np.array(y)
            Yp = yv[:, None] + np.sqrt(np.array(R))[:, None] * self.rng.standard_normal((m, self.ne))
            self.X = self.X + K @ (Yp - Y)
            xa = self.X.mean(axis=1)
            self.X = xa[:, None] + self.infl * (self.X - xa[:, None])
            if use_p:
                r_k = (P_meas - ym[-1]) / max(ym[-1], 1.0)
            if u.T_adapter is not None and ADAPTER in self.zones:
                r_a = u.T_adapter - xf[ADAPTER]
        xa = self.X.mean(axis=1)
        st_a = State.from_vector(xa)
        # ---- residual bookkeeping in the same currency as FixedGain
        r_b = [0.0] * N
        for i in self.zones:
            r_b[i] = u.PV[i] - xf[N + i]
        q_unexpl = [p.C_b[i] * (xa[N + i] - xf[N + i]) / dt if i in self.zones else 0.0 for i in range(N)]
        r_q = [q_unexpl[i] / p.P_heat[i] for i in range(N)]
        self.n += 1
        if self.learn_kappa and self.n == self.calib_steps:
            self.kappa_cal = st_a.kappa
        drift_index = (st_a.kappa / self.kappa_cal - 1.0) if self.kappa_cal else 0.0
        spread = list(self.X[:N, :].std(axis=1))
        corr = dict(r_b=r_b, r_a=r_a, r_k=r_k, drift_index=drift_index, r_q=r_q, q_unexpl=q_unexpl,
                    spread=spread, kappa=st_a.kappa, hw=st_a.hw, ua=st_a.ua)
        return st_a, diag, corr


def make_estimator(kind: str, p: Plant, rc: Recipe, **kw):
    if kind == "enkf":
        return EnKF(p, rc, **kw)
    if kind == "fixed":
        return FixedGain(p, rc, **{k: v for k, v in kw.items() if k in ("zones", "learn_kappa", "parts")})
    raise ValueError(f"unknown estimator {kind!r}")
