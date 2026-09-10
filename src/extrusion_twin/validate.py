"""
validate.py -- score the twin against the simulator's hidden truth.

For each configuration it reports
  * melt-state accuracy: RMSE and bias of the estimated melt temperature against the true
    melt temperature, split into probed zones (near the adapter) and unprobed zones (the
    barrel, where nothing is measured and the twin is on its own)
  * learning: kappa relative to its calibration vs the true relative viscosity, and hw
    against the true wall-conductance ratio (the simulator runs hA_w 1.2x the twin's guess)
  * detection: latency from each injected event to the first alarm of a matching kind
  * noise: alarms that fired in a window with no injected event
  * economy: scalars put on the wire vs raw samples

    python -m extrusion_twin.validate                 # the four standard configurations
    python -m extrusion_twin.validate --sweep-delta   # message economy vs detection latency
"""
from __future__ import annotations

import argparse
import math
from typing import List, Optional

from .cores import CoreTwin
from .detectors import Detectors
from .estimators import make_estimator
from .forecast import HorizonWatch, Reconciler
from .io_bus import SimPlant
from .model import ADAPTER, BARREL, DIE, N, SCREEN, Plant, Recipe, State, mv_expected
from .run import initial_state

PROBED = SCREEN + [ADAPTER]                       # zones near the melt probe
UNPROBED = BARREL + DIE                           # nothing measures the melt here


def score(sim: SimPlant, arch: str, estimator: str, duration: float, dt: float, delta: float = 0.0,
          delta_res: float = 0.0, ne: int = 24, settle: float = 900.0, quiet: bool = True,
          horizon: float = 0.0, fc_every: float = 60.0) -> dict:
    p, rc = sim.p, sim.rc
    u, _ = sim.advance()
    st = initial_state(u)
    if arch == "cores":
        twin = CoreTwin(p, rc, dt, estimator=estimator, delta=delta, delta_res=delta_res,
                        **(dict(ne=ne) if estimator == "enkf" else {}))
        twin.init(u, st)
        est = det = None
    else:
        twin = None
        est = make_estimator(estimator, p, rc, **(dict(ne=ne) if estimator == "enkf" else {}))
        det = Detectors(p, dt)

    fc = Reconciler(p, rc, dt, horizon=horizon, every=fc_every) if horizon > 0 else None
    hw_watch = HorizonWatch(dt) if horizon > 0 else None

    sq_p = sq_u = bias_p = bias_u = 0.0
    n_p = n_u = 0
    fired: List[tuple] = []                       # (t, kind, text)
    kappa_err = hw_err = ua_err = 0.0
    n_learn = 0
    msgs = raw = n_events = 0
    t = 0.0
    while t < duration:
        truth = sim.ground_truth()
        if arch == "cores":
            o = twin.sweep(u, t)
            st, diag, corr, alarms = o["st"], o["diag"], o["corr"], o["alarms"]
            msgs, raw = o["msgs"], o["raw"]
            n_events += len(o["events"])
        else:
            st, diag, corr = est.update(st, u, dt)
            mv_exp, _ = mv_expected(p, st, u)
            e = [u.MV[i] - mv_exp[i] for i in range(N)]
            d = det.update(u, corr, e, diag["residence"], t)
            alarms = d["alarms"]
            raw += 2 * N + 3
            n_events += len(d["events"])
            msgs = n_events
        if t > settle:
            for i in PROBED:
                r = st.T_m[i] - truth["T_m"][i]
                sq_p += r * r; bias_p += r; n_p += 1
            for i in UNPROBED:
                r = st.T_m[i] - truth["T_m"][i]
                sq_u += r * r; bias_u += r; n_u += 1
            kappa_err += abs((1.0 + corr["drift_index"]) - truth["kappa_rel"])
            hw_err += abs(st.hw - 1.2)             # the simulator's true hA_w ratio
            ua_err += abs(st.ua - 0.9)             # the simulator's true UA_oil ratio
            n_learn += 1
        if fc is not None:
            settled = fc.settle(t, st, u, diag)
            alarms = list(alarms) + hw_watch.update(t, settled, fc.bar)
            fc.maybe_issue(t, st, u)
        for a in alarms:
            fired.append((t, a["kind"], a.get("template"), a.get("zone"), a["text"]))
        t += dt
        u, _ = sim.advance()

    # detection latency and false alarms
    detections, used = [], set()
    for t_ev, name, kinds in sim.events():
        if t_ev >= duration:
            continue
        hit = None
        for idx, (ta, kind, tmpl, zone, text) in enumerate(fired):
            if ta < t_ev or idx in used:
                continue
            if kind in kinds or (tmpl in kinds if tmpl else False):
                hit = (idx, ta - t_ev)
                break
        if hit:
            used.add(hit[0])
            detections.append(dict(event=name, at=t_ev, latency=hit[1], text=fired[hit[0]][4]))
        else:
            detections.append(dict(event=name, at=t_ev, latency=None, text=None))
    quiet_alarms = [f for idx, f in enumerate(fired)
                    if idx not in used and f[0] > settle
                    and not any(t_ev <= f[0] <= t_ev + 900 for t_ev, _, _ in sim.events() if t_ev < duration)]

    return dict(arch=arch, estimator=estimator, delta=delta, delta_res=delta_res,
                rmse_probed=math.sqrt(sq_p / max(n_p, 1)), bias_probed=bias_p / max(n_p, 1),
                rmse_unprobed=math.sqrt(sq_u / max(n_u, 1)), bias_unprobed=bias_u / max(n_u, 1),
                kappa_mae=kappa_err / max(n_learn, 1), hw_mae=hw_err / max(n_learn, 1),
                ua_mae=ua_err / max(n_learn, 1), ua_final=round(st.ua, 3),
                detections=detections, n_alarms=len(fired), n_quiet_alarms=len(quiet_alarms),
                msgs=msgs, raw=raw, n_events=n_events, horizon=horizon,
                forecast=(fc.state() if fc else None),
                n_horizon_alarms=len([f for f in fired if f[1] == "horizon"]))


def show(r: dict):
    print(f"\n=== {r['arch']} / {r['estimator']}" + (f" / delta {r['delta']} res {r['delta_res']}" if r["arch"] == "cores" else "") + " ===")
    print(f"  melt RMSE  probed {r['rmse_probed']:5.2f} degC (bias {r['bias_probed']:+.2f})   "
          f"unprobed {r['rmse_unprobed']:5.2f} degC (bias {r['bias_unprobed']:+.2f})")
    print(f"  learning   kappa MAE {r['kappa_mae']:.3f} (rel. to truth)   hw MAE {r['hw_mae']:.3f} (truth 1.20)"
          f"   ua {r['ua_final']:.2f} (truth 0.90)")
    for d in r["detections"]:
        lat = f"{d['latency']:6.0f} s" if d["latency"] is not None else "  MISSED"
        print(f"  detect     {d['event']:42s} {lat}")
    print(f"  alarms {r['n_alarms']} total, {r['n_quiet_alarms']} outside any event window")
    if r.get("forecast"):
        f = r["forecast"]
        b, w = f["bias"]["T_m_AD"], f["band"]["T_m_AD"]
        print(f"  ahead      +{f['horizon']:.0f} s: {f['scored']} scored, {f['retired']} retired, "
              f"{r['n_horizon_alarms']} horizon alarms")
        if b is not None:
            print(f"             adapter melt: bias {b:+.2f} degC, centred band {w:.2f} degC "
                  f"(measured, not corrected)")
    print(f"  economy    off-box events {r['n_events']} vs {r['raw']} raw samples "
          f"({100.0 * r['n_events'] / max(r['raw'], 1):.1f} %)"
          + (f";  inter-core scalars {r['msgs']} ({100.0 * r['msgs'] / max(r['raw'], 1):.0f} %)" if r["arch"] == "cores" else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="extrusion_twin.validate")
    ap.add_argument("--seconds", type=float, default=4200.0)
    ap.add_argument("--dt", type=float, default=1.0)
    ap.add_argument("--ne", type=int, default=24)
    ap.add_argument("--sweep-delta", action="store_true", help="message economy vs detection, over delta")
    ap.add_argument("--horizon", type=float, default=0.0,
                    help="also run the twin this many seconds ahead and score the reconciliation")
    ap.add_argument("--flow-meters", action="store_true",
                    help="give the simulator loop flow meters, which is what makes the oil conductance learnable")
    a = ap.parse_args(argv)

    p, rc = Plant(), Recipe()
    results = []
    if a.sweep_delta:
        for delta, dres in [(0.0, 0.0), (0.1, 0.005), (0.3, 0.02), (1.0, 0.05), (3.0, 0.15)]:
            r = score(SimPlant(p, rc, a.dt, flow_meters=a.flow_meters), "cores", "fixed", a.seconds, a.dt, delta, dres, a.ne,
                      horizon=a.horizon)
            results.append(r); show(r)
        print("\n  delta  res  inter-core  %raw   detections   melt RMSE (unprobed)")
        for r in results:
            got = sum(1 for d in r["detections"] if d["latency"] is not None)
            print(f"  {r['delta']:5.1f} {r['delta_res']:5.3f} {r['msgs']:9d} {100.0 * r['msgs'] / r['raw']:6.0f}   "
                  f"{got}/{len(r['detections'])}          {r['rmse_unprobed']:.2f}")
        return results
    try:
        import numpy                                    # noqa: F401
        pairs = [("mono", "fixed"), ("mono", "enkf"), ("cores", "fixed"), ("cores", "enkf")]
    except ImportError:
        print("numpy is not installed, so only the fixed-gain configurations are scored.\n")
        pairs = [("mono", "fixed"), ("cores", "fixed")]
    for arch, est in pairs:
        r = score(SimPlant(p, rc, a.dt, flow_meters=a.flow_meters), arch, est, a.seconds, a.dt, 0.3, 0.02, a.ne, horizon=a.horizon)
        results.append(r); show(r)
    print("\n  configuration        melt RMSE unprobed   kappa MAE   ua (truth 0.90)   detections   alarms outside events")
    for r in results:
        got = sum(1 for d in r["detections"] if d["latency"] is not None)
        print(f"  {r['arch']:5s} / {r['estimator']:5s}        {r['rmse_unprobed']:8.2f}       {r['kappa_mae']:7.3f}"
              f"        {r['ua_final']:8.2f}        {got}/{len(r['detections'])}            {r['n_quiet_alarms']}")
    return results


if __name__ == "__main__":
    main()
