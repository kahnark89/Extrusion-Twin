"""
cores.py -- the same physics partitioned by anatomy, predictive-coding style.

    barrel core   B1..B8 + drive + barrel oil loop + screw core      (learns kappa from amps)
    screen core   SC1, SC2, AD + the adapter probe
    die core      D1..D7 + lip
    electrical    CT heater checks, contacts (lives inside each core's detectors; the council
                  sees only its alarms)

Each core predicts its own zones from its own measurements and corrects locally (its own
gains, its own learning). Cores exchange only:
  * a boundary melt temperature to the core downstream, when it has moved more than `delta`
  * their residual vectors to the council, when a component has moved more than `delta_res`
  * alarms
The council runs the cross-core templates (whole barrel, gradient, die, die skew) and the
cross-modal check on what it last received. Message counts are logged against raw samples.
With delta = delta_res = 0 every value is passed every sweep (dense); the partition is then
just a different estimator topology (local corrections instead of a global spillover).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .detectors import Bank, CrossModal, Detectors
from .estimators import make_estimator
from .model import ADAPTER, BARREL, CORES, DIE, N, SCREEN, Inputs, Plant, Recipe, State, indices, mv_expected


class Core:
    def __init__(self, name: str, zones: List[int], p: Plant, rc: Recipe, dt: float, estimator: str,
                 delta: float, delta_res: float, downstream: Optional[str], boundary_zones: List[int], **est_kw):
        self.name, self.zones, self.p, self.rc, self.dt = name, list(zones), p, rc, dt
        self.delta, self.delta_res, self.downstream, self.boundary_zones = delta, delta_res, downstream, boundary_zones
        parts = ("zones", "oil", "screw") if name == "barrel" else ("zones",)
        self.est = make_estimator(estimator, p, rc, zones=self.zones, learn_kappa=(name == "barrel"), parts=parts, **est_kw)
        self.det = Detectors(p, dt, self.zones, name=name, with_cross=False, with_kappa=(name == "barrel"),
                             with_contacts=(name == "barrel"))
        self.st: Optional[State] = None
        self.inbox: Dict[int, float] = {}
        self.sent_boundary: Optional[float] = None
        self.sent_e, self.sent_rq = [0.0] * N, [0.0] * N
        self.msgs, self.scalars = 0, 0
        self.last_corr: dict = {}

    def init(self, u: Inputs, st0: State):
        self.st = st0.copy()

    def out_zone(self) -> int:
        return self.zones[-1] if self.name != "screen" else ADAPTER

    def sweep(self, u: Inputs, t: float) -> dict:
        self.st, diag, corr = self.est.update(self.st, u, self.dt, boundary=self.inbox)
        self.last_corr = corr
        mv_exp, _ = mv_expected(self.p, self.st, u, zones=self.zones)
        e = [u.MV[i] - mv_exp[i] if i in self.zones else 0.0 for i in range(N)]
        d = self.det.update(u, corr, e, diag["residence"], t, diag)
        msgs = []
        # boundary message to the downstream core (send-on-delta)
        T_out = self.st.T_m[self.out_zone()]
        if self.downstream and (self.sent_boundary is None or abs(T_out - self.sent_boundary) > self.delta):
            self.sent_boundary = T_out
            msgs.append(("boundary", self.downstream, T_out))
        # residual vectors to the council (send-on-delta, per component: only the components
        # that moved are put on the wire, so the cost is the number of scalars, not vectors)
        moved_e = [i for i in self.zones if abs(e[i] - self.sent_e[i]) > self.delta_res]
        if moved_e:
            for i in moved_e:
                self.sent_e[i] = e[i]
            msgs.append(("e", "council", {i: e[i] for i in moved_e}))
        moved_q = [i for i in self.zones if abs(corr["r_q"][i] - self.sent_rq[i]) > self.delta_res]
        if moved_q:
            for i in moved_q:
                self.sent_rq[i] = corr["r_q"][i]
            msgs.append(("r_q", "council", {i: corr["r_q"][i] for i in moved_q}))
        for a in d["alarms"]:
            msgs.append(("alarm", "council", a))
        self.msgs += len(msgs)
        scalars = sum((len(pl) if isinstance(pl, dict) else 1) for kind, _, pl in msgs if kind != "alarm")
        self.scalars += scalars
        return dict(diag=diag, corr=corr, e=e, det=d, msgs=msgs, scalars=scalars)


class Council:
    """cross-core wide-field templates and the cross-modal check on the last received vectors"""

    def __init__(self, dt: float, theta: float = 0.06):
        self.heat = Bank("council:heater", dt, theta, list(range(N)))
        self.therm = Bank("council:thermal", dt, theta, list(range(N)))
        self.cross = CrossModal(dt)
        self.e, self.r_q = [0.0] * N, [0.0] * N
        self.received = 0

    def deliver(self, kind: str, payload: dict):
        """payload is {zone index: value} -- only the components that moved"""
        self.received += len(payload)
        target = self.e if kind == "e" else self.r_q if kind == "r_q" else None
        if target is not None:
            for i, v in payload.items():
                target[i] = v

    def update(self, residence: List[float], r_k: float, t: float, global_active: bool) -> dict:
        h = self.heat.update(self.e, residence, t, global_active)
        th = self.therm.update(self.r_q, residence, t, global_active)
        alarms = h["alarms"] + th["alarms"]
        x = self.cross.update(h["ep"], r_k, t)
        if x:
            alarms.append(x)
        return dict(h=h, th=th, alarms=alarms, cross_M=self.cross.M)


class CoreTwin:
    """drop-in for the monolithic loop: sweep(u, t) returns the same kind of record"""

    def __init__(self, p: Plant, rc: Recipe, dt: float, estimator: str = "fixed", delta: float = 0.0,
                 delta_res: float = 0.0, **est_kw):
        self.p, self.rc, self.dt = p, rc, dt
        self.cores = {
            "barrel": Core("barrel", CORES["barrel"], p, rc, dt, estimator, delta, delta_res, "screen", [], **est_kw),
            "screen": Core("screen", CORES["screen"], p, rc, dt, estimator, delta, delta_res, "die", [SCREEN[0]], **est_kw),
            "die": Core("die", CORES["die"], p, rc, dt, estimator, delta, delta_res, None, DIE, **est_kw),
        }
        self.order = ["barrel", "screen", "die"]
        self.council = Council(dt)
        self.dedup_window = 60.0
        self._recent: Dict[tuple, float] = {}
        self.msgs_total, self.scalars_total, self.raw_total = 0, 0, 0
        self.drift_hist = []

    def init(self, u: Inputs, st0: State):
        for c in self.cores.values():
            c.init(u, st0)
        # prime the boundaries so the first sweep has an upstream value
        self.cores["screen"].inbox = {SCREEN[0]: st0.T_m[BARREL[-1]]}
        self.cores["die"].inbox = {i: st0.T_m[ADAPTER] for i in DIE}

    def sweep(self, u: Inputs, t: float) -> dict:
        outs, alarms, events = {}, [], []
        for name in self.order:
            c = self.cores[name]
            o = c.sweep(u, t)
            outs[name] = o
            events += o["det"]["events"]
            for kind, to, payload in o["msgs"]:
                if kind == "boundary":
                    tgt = self.cores[to]
                    for z in tgt.boundary_zones:
                        tgt.inbox[z] = payload
                elif kind == "alarm":
                    alarms.append(payload)
                    self.council.received += 1
                else:
                    self.council.deliver(kind, payload)
            self.msgs_total += len(o["msgs"])
            self.scalars_total += o["scalars"]
        self.raw_total += 2 * N + 3
        # stitched state for logging and indices
        st = State(T_m=[0.0] * N, T_b=[0.0] * N, T_oil_b=self.cores["barrel"].st.T_oil_b,
                   T_oil_s=self.cores["barrel"].st.T_oil_s, T_s=self.cores["barrel"].st.T_s,
                   kappa=self.cores["barrel"].st.kappa, hw=self.cores["barrel"].st.hw,
                   ua=self.cores["barrel"].st.ua)
        spread = [0.0] * N
        for name, c in self.cores.items():
            for i in c.zones:
                st.T_m[i], st.T_b[i] = c.st.T_m[i], c.st.T_b[i]
                spread[i] = outs[name]["corr"]["spread"][i]
        bd = outs["barrel"]["diag"]
        residence = bd["residence"]
        full = indices(self.p, self.rc, st, u, bd["mdot"], bd["Q_cool"], self.p.P_heat)
        corr_b = outs["barrel"]["corr"]
        self.drift_hist.append(corr_b["drift_index"])
        if len(self.drift_hist) > int(600 / self.dt) + 1:
            self.drift_hist.pop(0)
        global_active = abs(self.drift_hist[-1] - self.drift_hist[0]) > 0.02
        for a in alarms:                                        # core alarms claim their signature first
            self._recent[(a.get("template"), a.get("zone"))] = t
        cn = self.council.update(residence, corr_b["r_k"], t, global_active)
        for a in cn["alarms"]:                                  # the council only speaks about what no core already reported
            key = (a.get("template"), a.get("zone"))
            if t - self._recent.get(key, -1e9) > self.dedup_window:
                self._recent[key] = t
                alarms.append(a)
        e = [0.0] * N
        r_q = [0.0] * N
        r_b = [0.0] * N
        for name, c in self.cores.items():
            for i in c.zones:
                e[i] = outs[name]["e"][i]
                r_q[i] = outs[name]["corr"]["r_q"][i]
                r_b[i] = outs[name]["corr"]["r_b"][i]
        diag = dict(bd)
        diag.update(full)
        corr = dict(r_b=r_b, r_a=outs["screen"]["corr"]["r_a"], r_k=corr_b["r_k"], drift_index=corr_b["drift_index"],
                    r_q=r_q, q_unexpl=[self.p.P_heat[i] * r_q[i] for i in range(N)], spread=spread,
                    kappa=st.kappa, hw=st.hw, ua=st.ua)
        return dict(st=st, diag=diag, corr=corr, e=e, alarms=alarms, events=events,
                    h=cn["h"], th=cn["th"], cross_M=cn["cross_M"],
                    msgs=self.scalars_total, n_msgs=self.msgs_total, raw=self.raw_total,
                    core_dirs={n: outs[n]["det"]["h"]["direction"] for n in self.order})
