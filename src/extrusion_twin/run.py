"""
run.py -- the loop and the command line.

    python -m extrusion_twin run --sim 4200                          # simulator, mono, fixed gains
    python -m extrusion_twin run --sim 4200 --estimator enkf         # ensemble Kalman filter
    python -m extrusion_twin run --sim 4200 --arch cores --delta 0.3 # predictive-coding partition
    python -m extrusion_twin run --config config.json --hours 8      # live on the line
    python -m extrusion_twin label --corpus corpus                   # Socratic labeling of alarms
    python -m extrusion_twin match --corpus corpus                   # nearest labeled cases
    python -m extrusion_twin export --corpus corpus --out ciaer.jsonl
    python -m extrusion_twin write-config config.json                # example config to edit
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Optional

from .cores import CoreTwin
from .corpus import Corpus, label_cli, match_cli
from .detectors import Detectors
from .estimators import make_estimator
from .forecast import CHANNELS, HorizonWatch, Reconciler
from .io_bus import EXAMPLE_CONFIG, LiveBus, SimPlant, build_plant_recipe, load_config
from .model import ADAPTER, BARREL, N, ZONE_NAMES, State, mv_expected


def initial_state(u) -> State:
    return State(T_m=[u.SV[i] + (15.0 if i >= 4 else -20.0 + 8.0 * i) for i in range(N)],
                 T_b=list(u.PV), T_oil_b=u.tcu_b_sv, T_oil_s=u.tcu_s_sv, T_s=u.tcu_s_sv + 10.0)


def snapshot(t: float, u, st, diag, corr, e, h, th, alarm_feed, live_frac, fight_kWh,
             arch: str, estimator: str, feed=None, loops=(None, None), drive=None,
             forecast=None) -> dict:
    """the current picture, nothing historical -- shared by the status file and the server"""
    from .model import ZONE_NAMES
    spread = corr.get("spread") or [0.0] * N
    feed_state = getattr(feed, "state", None)
    feed_ratio = round(feed.ratio, 4) if getattr(feed, "ratio", None) else None
    lb, ls = loops

    def loopinfo(lw, sup, ret):
        if sup is None or ret is None:
            return None
        return dict(supply=round(sup, 1), ret=round(ret, 1), dT=round(getattr(lw, "dT", ret - sup) or 0.0, 2),
                    kW=(round(lw.watts / 1e3, 2) if getattr(lw, "watts", None) else None),
                    state=getattr(lw, "state", None),
                    approach=(round(lw.approach, 1) if getattr(lw, "approach", None) is not None else None))
    direction = h["direction"]
    verdict = ("holding steady" if abs(h["M"].get("barrel", 0.0)) < 0.03 and abs(corr["drift_index"]) < 0.03
               else Detectors_verdict(direction))
    payload = dict(
        t=t, iso=time.strftime("%Y-%m-%dT%H:%M:%S"), arch=arch, estimator=estimator,
        verdict=verdict, direction=direction,
        zones=ZONE_NAMES, T_m=[round(x, 1) for x in st.T_m], T_b=[round(x, 1) for x in st.T_b],
        SV=[round(x, 1) for x in u.SV], MV=[round(x, 3) for x in u.MV],
        spread=[round(x, 2) for x in spread], e=[round(x, 3) for x in e],
        r_q=[round(x, 3) for x in corr["r_q"]],
        drift_index=round(corr["drift_index"], 4), kappa=round(st.kappa, 3), hw=round(st.hw, 3),
        fight_kW=round(diag["fight_W"] / 1e3, 2), fight_kWh=round(fight_kWh, 2),
        H_index=round(diag["H_index"], 1), drool_index=round(diag["drool_index"], 3),
        T_lip=round(diag["T_lip"], 1), T_oil_b=round(st.T_oil_b, 1), T_oil_s=round(st.T_oil_s, 1),
        T_screw=round(st.T_s, 1), rpm=u.rpm, amps=round(u.amps, 1),
        crammer_rpm=(None if u.crammer_rpm is None else round(u.crammer_rpm, 1)),
        crammer_amps=(None if u.crammer_amps is None else round(u.crammer_amps, 2)),
        feed_state=feed_state, feed_ratio=feed_ratio,
        rpm_motor=u.rpm_motor, gear_ratio=(round(drive.ratio, 2) if getattr(drive, "ratio", None) else None),
        loop_barrel=loopinfo(lb, u.T_oil_b_supply, u.T_oil_b_return),
        loop_screw=loopinfo(ls, u.T_oil_s_supply, u.T_oil_s_return),
        T_adapter=u.T_adapter, P_adapter=u.P_adapter,
        confidence=dict(measurement=round(live_frac, 2),
                        model=round(1.0 / (1.0 + sum(spread) / N), 2),
                        detector=round(min(abs(h["M"].get("barrel", 0.0)) / 0.06, 1.0), 2)),
        live=dict(u.live or {}), alarms=alarm_feed[-12:], forecast=forecast)
    return payload


def write_status(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)                                   # atomic, so nothing ever reads half a file


def Detectors_verdict(direction: float) -> str:
    from .detectors import Bank
    return "drifting, " + Bank.verdict(direction)


class TwinRun:
    """One sweep at a time, so the loop can be driven from somewhere that owns its own event loop.

    ``run()`` below is the batch form and is what the command line and the threaded console use.
    A browser worker, which cannot block, pumps ``step()`` in slices and yields between them --
    same physics, same detectors, same CSV, no fork of the loop."""

    def __init__(self, source, p, rc, dt: float, duration: float, csv_path: str, arch: str = "mono",
                 estimator: str = "fixed", delta: float = 0.0, delta_res: float = 0.0, ne: int = 32,
                 corpus_dir: Optional[str] = None, quiet: bool = False, run_id: Optional[str] = None,
                 status_path: Optional[str] = None, on_status=None, should_stop=None,
                 status_every: int = 1, pace: float = 0.0, on_alarm=None,
                 horizon: float = 0.0, fc_every: float = 60.0):
        self.source, self.p, self.rc = source, p, rc
        self.dt, self.duration, self.csv_path = dt, duration, csv_path
        self.arch, self.estimator = arch, estimator
        self.quiet, self.run_id_hint = quiet, run_id
        self.status_path, self.on_status, self.should_stop = status_path, on_status, should_stop
        self.status_every, self.pace, self.on_alarm = max(1, int(status_every)), pace, on_alarm

        self.u, self.faults = source.poll() if hasattr(source, "poll") else source.advance()
        self.st = initial_state(self.u)
        self.corpus = Corpus(corpus_dir, run_id) if corpus_dir else None
        est_kw = dict(ne=ne) if estimator == "enkf" else {}
        if arch == "cores":
            self.twin = CoreTwin(p, rc, dt, estimator=estimator, delta=delta, delta_res=delta_res, **est_kw)
            self.twin.init(self.u, self.st)
            self.est = self.det = None
        else:
            self.twin = None
            self.est = make_estimator(estimator, p, rc, **est_kw)
            self.det = Detectors(p, dt)

        # the twin run a phase ahead, off by default: it costs horizon/dt extra physics steps
        # per issue, and it is worth nothing until there is a machine to be wrong about
        self.fc = Reconciler(p, rc, dt, horizon=horizon, every=fc_every) if horizon > 0 else None
        self.hw_watch = HorizonWatch(dt) if horizon > 0 else None
        self.fc_alarms = 0

        self.cols = (["t", "rpm", "rpm_motor", "gear_ratio", "amps", "crammer_rpm", "crammer_amps", "feed_ratio", "feed_state",
                      "oil_b_supply", "oil_b_return", "oil_b_dT", "oil_b_kW", "oil_b_state", "oil_b_approach",
                      "oil_s_supply", "oil_s_return", "oil_s_dT", "oil_s_kW", "oil_s_state",
                      "T_adapter", "P_adapter", "kappa", "hw", "drift_index", "mdot_kg_s", "P_mech_kW",
                      "fight_kW", "fight_kWh", "H_index", "drool_index", "T_lip", "T_oil_b", "T_oil_s", "T_screw",
                      "direction_heater", "direction_thermal", "n_events", "r_adapter", "unexplained_kW",
                      "spread_AD", "spread_barrel", "meas_live_frac", "msgs", "raw",
                      "fc_horizon", "fc_ahead_AD", "fc_err_AD", "fc_err_barrel", "fc_err_H",
                      "fc_band_AD", "fc_bias_AD", "fc_scored", "fc_retired", "fc_retired_why"]
                     + [f"Tm_{z}" for z in ZONE_NAMES] + [f"Tb_{z}" for z in ZONE_NAMES]
                     + [f"e_{z}" for z in ZONE_NAMES] + [f"rq_{z}" for z in ZONE_NAMES] + [f"sd_{z}" for z in ZONE_NAMES]
                     + ["M_cross", "alarms"])
        self.f = open(csv_path, "w", newline="")
        self.wr = csv.writer(self.f)
        self.wr.writerow(self.cols)
        self.fight_kWh = self.t = self.next_print = 0.0
        self.n_events_total = self.msgs = self.raw = self.step_i = 0
        self.stopped = self.done = False
        self.alarm_feed = []
        self.corr = dict(drift_index=0.0)
        self.snap = None

    # ------------------------------------------------------------------ one sweep
    def step(self) -> bool:
        """Advance one sweep. False once the run is over -- call finish() then."""
        if self.done:
            return False
        if self.t >= self.duration:
            return self._end(False)
        p, dt, u, st = self.p, self.dt, self.u, self.st
        t = self.t

        if self.arch == "cores":
            o = self.twin.sweep(u, t)
            st, diag, corr, e = o["st"], o["diag"], o["corr"], o["e"]
            alarms, events, h, th, M_cross = o["alarms"], o["events"], o["h"], o["th"], o["cross_M"]
            self.msgs, self.raw = o["msgs"], o["raw"]
        else:
            st, diag, corr = self.est.update(st, u, dt)
            mv_exp, _ = mv_expected(p, st, u)
            e = [u.MV[i] - mv_exp[i] for i in range(N)]
            d = self.det.update(u, corr, e, diag["residence"], t, diag)
            alarms, events, h, th, M_cross = d["alarms"], d["events"], d["h"], d["th"], d["cross_M"]
            self.raw += 2 * N + 3
            self.msgs += len(events) + len(alarms)
        self.st, self.corr = st, corr
        # a phase ahead: settle whatever came due against what actually happened, then issue
        # the next projection. Scored errors never touch the estimator -- see forecast.py.
        settled, fc_alarms = [], []
        if self.fc is not None:
            settled = self.fc.settle(t, st, u, diag)
            fc_alarms = self.hw_watch.update(t, settled, self.fc.bar)
            self.fc_alarms += len(fc_alarms)
            self.fc.maybe_issue(t, st, u)

        alarms = alarms + fc_alarms + [
            dict(kind="fault", bank="bus", template="fault", value=0.0, direction=0.0,
                 verdict="bus/input fault", zones=[], text=x) for x in self.faults]
        self.n_events_total += len(events)
        self.fight_kWh += diag["fight_W"] * dt / 3.6e6
        live = u.live or {}
        live_frac = sum(1 for v in live.values() if v in ("bus", "analog", "sim")) / max(len(live), 1)
        spread = corr.get("spread") or [0.0] * N

        for a in alarms:
            line = f"t={t:6.0f}s  {a['text']}"
            if self.corpus and a["kind"] != "fault":
                sig = self.corpus.record(a, t, u, st, diag, corr, e,
                                         forecast=(self.fc.state() if self.fc else None))
                top = self.corpus.suggest(sig)
                if top:
                    line += "  | closest cases: " + "; ".join(f"{c:.2f} '{cause}'" for c, _, cause, _ in top)
                line += (f"  [sig {sig['id']} conf m{sig['confidence']['measurement']:.2f}"
                         f"/M{sig['confidence']['model']:.2f}/d{sig['confidence']['detector']:.2f}]")
                a = dict(a, cases=[dict(score=c, cause=cause, action=act) for c, _, cause, act in top],
                         sig=sig["id"], confidence=sig["confidence"])
            entry = dict(t=t, kind=a["kind"], zone=a.get("zone"), text=a["text"],
                         verdict=a.get("verdict"), cases=a.get("cases", []), confidence=a.get("confidence"))
            self.alarm_feed.append(entry)
            if self.on_alarm:
                self.on_alarm(entry)
            if not self.quiet:
                print(line)

        det_b = self.twin.cores["barrel"].det if self.arch == "cores" else self.det
        fw, lb, ls, dw = det_b.feed, det_b.loop_b, det_b.loop_s, det_b.drive
        fmt = lambda v, n=2: "" if v is None else f"{v:.{n}f}"
        self.wr.writerow([f"{t:.0f}", f"{u.rpm:.1f}", fmt(u.rpm_motor, 0), fmt(getattr(dw, "ratio", None), 2),
                          f"{u.amps:.1f}",
                          "" if u.crammer_rpm is None else f"{u.crammer_rpm:.1f}",
                          "" if u.crammer_amps is None else f"{u.crammer_amps:.2f}",
                          "" if not (fw and fw.ratio) else f"{fw.ratio:.4f}",
                          (fw.state if fw else ""),
                          fmt(u.T_oil_b_supply, 1), fmt(u.T_oil_b_return, 1),
                          fmt(getattr(lb, "dT", None)), fmt((lb.watts / 1e3) if getattr(lb, "watts", None) else None),
                          (lb.state if lb else ""), fmt(getattr(lb, "approach", None), 1),
                          fmt(u.T_oil_s_supply, 1), fmt(u.T_oil_s_return, 1),
                          fmt(getattr(ls, "dT", None)), fmt((ls.watts / 1e3) if getattr(ls, "watts", None) else None),
                          (ls.state if ls else ""),
                          "" if u.T_adapter is None else f"{u.T_adapter:.1f}",
                          "" if u.P_adapter is None else f"{u.P_adapter:.1f}", f"{st.kappa:.4f}", f"{st.hw:.4f}",
                          f"{corr['drift_index']:+.4f}", f"{diag['mdot']:.3f}", f"{diag['P_mech'] / 1e3:.1f}",
                          f"{diag['fight_W'] / 1e3:.2f}", f"{self.fight_kWh:.3f}", f"{diag['H_index']:.1f}",
                          f"{diag['drool_index']:.3f}",
                          f"{diag['T_lip']:.1f}", f"{st.T_oil_b:.1f}", f"{st.T_oil_s:.1f}", f"{st.T_s:.1f}",
                          f"{h['direction']:+.2f}", f"{th['direction']:+.2f}", len(events), f"{corr['r_a']:+.2f}",
                          f"{sum(corr['q_unexpl']) / 1e3:+.2f}", f"{spread[ADAPTER]:.2f}",
                          f"{sum(spread[i] for i in BARREL) / len(BARREL):.2f}", f"{live_frac:.2f}",
                          self.msgs, self.raw]
                         + self._fc_row(settled)
                         + [f"{v:.1f}" for v in st.T_m] + [f"{v:.1f}" for v in st.T_b]
                         + [f"{v:+.3f}" for v in e] + [f"{v:+.3f}" for v in corr["r_q"]] + [f"{v:.2f}" for v in spread]
                         + [f"{M_cross:+.2f}", " | ".join(a["text"] for a in alarms)])

        self.step_i += 1
        if (self.status_path or self.on_status) and (self.step_i % self.status_every == 0
                                                     or t + dt >= self.duration):
            snap = snapshot(t, u, st, diag, corr, e, h, th, self.alarm_feed, live_frac, self.fight_kWh,
                            self.arch, self.estimator, feed=fw, loops=(lb, ls), drive=dw,
                            forecast=(self.fc.state() if self.fc else None))
            snap["progress"] = min(1.0, t / self.duration) if self.duration else 0.0
            self.snap = snap
            if self.status_path:
                write_status(self.status_path, snap)
            if self.on_status:
                self.on_status(snap)
        if self.should_stop and self.should_stop():
            return self._end(True)
        if self.pace > 0.0:
            time.sleep(self.pace)

        if not self.quiet and t >= self.next_print:
            print(f"t={t:6.0f}s  amps={u.amps:5.0f}  adapter={u.T_adapter or 0:6.1f}  twin_AD={st.T_m[ADAPTER]:6.1f}"
                  f"±{spread[ADAPTER]:.1f}  B8 melt={st.T_m[7]:6.1f}  oil={st.T_oil_b:5.1f}  kappa={st.kappa:5.2f} "
                  f"({corr['drift_index']:+.1%})  hw={st.hw:.2f}  fight={diag['fight_W'] / 1e3:4.1f}kW  "
                  f"Mh_barrel={h['M'].get('barrel', 0.0):+.3f}  dir={h['direction']:+.2f}  events={len(events)}"
                  + (f"  msgs={self.msgs}/{self.raw}" if self.arch == "cores" else "")
                  + (self._fc_note() if self.fc else ""))
            self.next_print += 300.0

        self.t = t + dt
        if hasattr(self.source, "poll"):
            t0 = time.time()
            self.u, self.faults = self.source.poll()
            self.dt = max(time.time() - t0, 0.5)          # a live sweep steps by however long it took
            self.f.flush()
        else:
            self.u, self.faults = self.source.advance()
        return True

    def _fc_note(self) -> str:
        st_ = self.fc.state()
        band = st_["band"]["T_m_AD"]
        ahead = st_["ahead"]
        return (f"  ahead(+{self.fc.horizon:.0f}s)="
                + ("--" if not ahead else f"{ahead['T_m_AD']:.1f}")
                + ("" if band is None else f"±{band:.1f}")
                + f"  scored={st_['scored']} retired={st_['retired']}")

    def _fc_row(self, settled) -> list:
        """Ten columns describing the phase-ahead layer. Blank when it is switched off, and
        blank on sweeps where nothing came due -- a settlement is an event, not a reading.
        The horizon is carried in the log so a replay can render the panel without being told."""
        if self.fc is None:
            return [""] * 10
        st_ = self.fc.state()
        ahead = st_["ahead"]
        scored = next((r for r in settled if r["kind"] == "scored"), None)
        retired = next((r for r in settled if r["kind"] == "retired"), None)
        f2 = lambda v: "" if v is None else f"{v:+.2f}"
        return [
            f"{self.fc.horizon:.0f}",
            "" if not ahead else f"{ahead['T_m_AD']:.1f}",
            "" if not scored else f"{scored['err']['T_m_AD']:+.2f}",
            "" if not scored else f"{scored['err']['T_m_barrel']:+.2f}",
            "" if not scored else f"{scored['err']['H_index']:+.1f}",
            "" if st_["band"]["T_m_AD"] is None else f"{st_['band']['T_m_AD']:.2f}",
            f2(st_["bias"]["T_m_AD"]),
            st_["scored"], st_["retired"],
            "" if not retired else retired["reason"],
        ]

    def _end(self, stopped: bool) -> bool:
        self.stopped, self.done = stopped, True
        return False

    @property
    def progress(self) -> float:
        return min(1.0, self.t / self.duration) if self.duration else 0.0

    # ------------------------------------------------------------------ the end of it
    def finish(self) -> dict:
        if not self.f.closed:
            self.f.close()
        self.done = True
        summary = dict(csv=self.csv_path, fight_kWh=round(self.fight_kWh, 2), events=self.n_events_total,
                       raw=self.raw, msgs=self.msgs, kappa=round(self.st.kappa, 4),
                       drift_index=round(self.corr.get("drift_index", 0.0), 4), hw=round(self.st.hw, 4),
                       seconds=round(self.t, 1), stopped=self.stopped, arch=self.arch,
                       estimator=self.estimator, alarms=self.alarm_feed,
                       run_id=(self.corpus.run_id if self.corpus else self.run_id_hint),
                       forecast=(self.fc.state() if self.fc else None))
        if not self.quiet:
            print(f"done: {self.csv_path}  fighting-loop waste {self.fight_kWh:.2f} kWh  "
                  f"events {self.n_events_total}  messages {self.msgs} vs raw samples {self.raw}  "
                  f"kappa {self.st.kappa:.3f} ({self.corr.get('drift_index', 0.0):+.1%})  hw {self.st.hw:.3f}")
        return summary


def run(source, p, rc, dt: float, duration: float, csv_path: str, **kw) -> dict:
    """The batch form: build the stepper, pump it to the end, hand back the summary."""
    tr = TwinRun(source, p, rc, dt, duration, csv_path, **kw)
    while tr.step():
        pass
    return tr.finish()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="extrusion_twin", description="extrusion line melt-state twin")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run the twin (simulator or live bus)")
    r.add_argument("--sim", type=float, default=0.0, help="simulate this many seconds instead of polling the bus")
    r.add_argument("--dt", type=float, default=1.0, help="simulator step, s")
    r.add_argument("--config", help="config.json (port, stations, devices, plant, recipe, manual values)")
    r.add_argument("--arch", default="mono", choices=["mono", "cores"])
    r.add_argument("--estimator", default="fixed", choices=["fixed", "enkf"])
    r.add_argument("--ne", type=int, default=32, help="ensemble size for enkf")
    r.add_argument("--delta", type=float, default=0.0, help="cores: send boundary temperature only when it moved this much (degC)")
    r.add_argument("--delta-res", dest="delta_res", type=float, default=0.0, help="cores: send residual vectors only when a component moved this much")
    r.add_argument("--hours", type=float, default=8.0, help="live run length")
    r.add_argument("--csv", default="twin_log.csv")
    r.add_argument("--status", help="write status.json every sweep for the dashboard to poll")
    r.add_argument("--corpus", help="directory for signatures.jsonl / labels.jsonl (enables corpus recording)")
    r.add_argument("--horizon", type=float, default=0.0,
                   help="run the twin this many seconds ahead of the machine and reconcile when "
                        "reality arrives. 0 (the default) switches the phase-ahead layer off")
    r.add_argument("--forecast-every", dest="fc_every", type=float, default=60.0,
                   help="issue a projection this often, seconds")
    r.add_argument("--run-id", dest="run_id")
    r.add_argument("--quiet", action="store_true")
    for name in ("rpm", "amps", "tadapter", "tcu-b", "tcu-s", "tower", "air", "jacket"):
        r.add_argument(f"--{name}", type=float, default=None, help="manual value overriding the config")
    l = sub.add_parser("label", help="label unlabeled alarm signatures (Socratic chain)")
    l.add_argument("--corpus", required=True)
    l.add_argument("--run-id", dest="run_id")
    m = sub.add_parser("match", help="show nearest labeled cases for unlabeled signatures")
    m.add_argument("--corpus", required=True)
    x = sub.add_parser("export", help="export labeled signatures in CIAER+ field order")
    x.add_argument("--corpus", required=True)
    x.add_argument("--out", default="ciaer_export.jsonl")
    w = sub.add_parser("write-config", help="write an example config.json to edit")
    w.add_argument("path")
    sv = sub.add_parser("serve", help="open the dashboard and drive the twin from it")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--dir", default="twin_data", help="where runs, configs and the corpus are kept")
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to reach the console from another device on the same network")
    sv.add_argument("--token", nargs="?", const="auto", default=None,
                    help="require a key in the address. Bare --token makes one and keeps it in the data folder")
    a = ap.parse_args(argv)

    if a.cmd == "write-config":
        with open(a.path, "w") as f:
            json.dump(EXAMPLE_CONFIG, f, indent=2)
        print(f"wrote {a.path} -- fill in ports, stations, drive registers, nameplate values")
        return 0
    if a.cmd == "serve":
        from .server import serve
        return serve(a.port, a.dir, open_browser=not a.no_browser, host=a.host, token=a.token) or 0
    if a.cmd == "label":
        return label_cli(a.corpus, a.run_id) or 0
    if a.cmd == "match":
        return match_cli(a.corpus) or 0
    if a.cmd == "export":
        n = Corpus(a.corpus).export_ciaer(a.out)
        print(f"exported {n} labeled case(s) to {a.out}")
        return 0
    if a.cmd != "run":
        ap.print_help()
        return 0

    cfg = load_config(a.config)
    manual_map = {"rpm": "rpm", "amps": "amps", "tadapter": "T_adapter", "tcu_b": "tcu_b_sv", "tcu_s": "tcu_s_sv",
                  "tower": "T_tower", "air": "T_air", "jacket": "T_feed_jacket"}
    for flag, key in manual_map.items():
        v = getattr(a, flag, None)
        if v is not None:
            cfg.setdefault("manual", {})[key] = v
    p, rc = build_plant_recipe(cfg)
    if a.sim > 0:
        source, dt, duration = SimPlant(p, rc, a.dt), a.dt, a.sim
    elif cfg.get("port"):
        source, dt, duration = LiveBus(cfg), 1.5, a.hours * 3600.0
    else:
        sys.exit("give --sim SECONDS, or --config with a port")
    run(source, p, rc, dt, duration, a.csv, arch=a.arch, estimator=a.estimator, delta=a.delta,
        delta_res=a.delta_res, ne=a.ne, corpus_dir=a.corpus, quiet=a.quiet, run_id=a.run_id,
        status_path=a.status, horizon=a.horizon, fc_every=a.fc_every)
    return 0                                            # main() is an exit status, not a result


if __name__ == "__main__":
    main()
