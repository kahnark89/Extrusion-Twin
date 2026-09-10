"""
api.py -- the console's API, with no transport attached to it.

The local server (`server.py`) puts HTTP in front of this. The browser build puts a
`fetch` shim in front of the same thing, running under Pyodide. Both get identical
behaviour because there is one implementation of it here and the twin itself does not
know which one is calling.

    GET  /api/state                current run state, the latest snapshot, the run list
    GET  /api/defaults             plant, recipe and run defaults, with units and help text
    GET  /api/configs              saved parameter sets
    POST /api/configs              save one   {name, config}
    GET  /api/ports                serial ports this machine can see
    GET  /api/corpus               signatures and labels
    POST /api/corpus/label         {id, cause, intuition, action, effect, result, ...}
    POST /api/export               write the CIAER+ export
    POST /api/run                  start      {mode:"sim"|"live", ...options}
    POST /api/stop                 stop the run in progress
    GET  /api/runs/<id>            summary + full CSV of a finished run

Everything lives under one directory (default ./twin_data): runs/, configs/, corpus/.
One run at a time. Starting a run while one is going is refused.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from typing import Optional

from .corpus import Corpus
from .io_bus import EXAMPLE_CONFIG, LiveBus, SimPlant, build_plant_recipe
from .model import Plant, Recipe, ZONE_NAMES
from .run import TwinRun


# ------------------------------------------------------------------ what the setup screen offers
def defaults(env: Optional[dict] = None) -> dict:
    p, rc = Plant(), Recipe()
    field = lambda key, label, value, unit="", help="", group="", step=None: dict(
        key=key, label=label, value=value, unit=unit, help=help, group=group, step=step)
    return dict(
        zones=ZONE_NAMES,
        env=env or dict(live=True, kind="local"),
        machine=[
            field("D", "Screw diameter", p.D, "m", "Off the screw drawing. Sets shear rate.", "screw", 0.001),
            field("L_zone", "Barrel zone length", p.L_zone, "m", "How much barrel each band covers.", "screw", 0.01),
            field("k_pump", "Throughput per rpm", p.k_pump, "kg/s per rpm",
                  "60 rpm at 0.0055 is about 1,190 kg/h. Fit this with an rpm step test.", "screw", 0.0001),
            field("kW_per_amp", "Shaft power per amp", p.kW_per_amp, "kW/A",
                  "Above no-load. From the motor nameplate, then trimmed by a reversal test.", "drive", 0.01),
            field("I_noload", "Motor no-load current", p.I_noload, "A",
                  "What the screw draws empty at running rpm.", "drive", 1.0),
            field("V_line", "Heater line voltage", p.V_line, "V", "Used to turn CT amps into watts.", "heaters", 1.0),
            field("solids_share", "Power spent conveying solids", p.solids_share, "fraction",
                  "The share of motor power that goes into the feed section rather than the melt.", "screw", 0.01),
            field("tau_tcu", "TCU response time", p.tau_tcu, "s",
                  "How fast the oil loop chases its setpoint.", "cooling", 1.0),
            field("UA_hx_b", "Barrel exchanger conductance", p.UA_hx_b, "W/K",
                  "Cooling authority against tower water. Drops as the exchanger fouls.", "cooling", 10.0),
            field("k_lip", "Lip heat loss", p.k_lip, "fraction",
                  "How far the unheated lip falls toward air. Drives the drool estimate.", "die", 0.01),
        ],
        recipe=[
            field("name", "Compound", rc.name, "", "Whatever you call it on the floor.", "id"),
            field("rho", "Melt density", rc.rho, "kg/m3", "", "properties", 10.0),
            field("cp", "Specific heat", rc.cp, "J/kg/K", "", "properties", 10.0),
            field("K_ref", "Stiffness at reference", rc.K_ref, "Pa.s^n",
                  "How stiff the melt is at the reference temperature. The twin learns a multiplier on this.",
                  "flow", 500.0),
            field("n", "Shear thinning index", rc.n, "", "Below 1. Lower means it thins harder under shear.",
                  "flow", 0.01),
            field("EaR", "Thermal thinning", rc.EaR, "K",
                  "Activation energy over R. Higher means viscosity falls off faster with temperature.", "flow", 100.0),
            field("T_ref", "Reference temperature", rc.T_ref, "degC", "", "flow", 1.0),
            field("EaR_deg", "Stabilizer burn rate", rc.EaR_deg, "K",
                  "How much faster the stabilizer is used up as the melt gets hotter.", "degradation", 500.0),
            field("T_ref_deg", "Degradation reference", rc.T_ref_deg, "degC", "", "degradation", 1.0),
            field("plasticizer_phr", "Plasticizer", rc.plasticizer_phr, "phr", "", "id", 1.0),
            field("filler_phr", "Filler", rc.filler_phr, "phr", "", "id", 1.0),
        ],
        options=dict(
            arch=[dict(v="mono", label="One model for the whole line",
                       help="Simplest. Every zone corrected together."),
                  dict(v="cores", label="Split into barrel, screens, die",
                       help="Each section runs on its own and only speaks when something moves. "
                            "Cuts traffic to a few percent.")],
            estimator=[dict(v="fixed", label="Fixed gains",
                            help="Light and predictable. No uncertainty band, and it will not learn "
                                 "wall conductance."),
                       dict(v="enkf", label="Ensemble filter",
                            help="Learns viscosity and wall conductance, and gives the melt a real "
                                 "plus-or-minus. Needs numpy.")],
        ),
        example_live=EXAMPLE_CONFIG,
    )


def merged_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(EXAMPLE_CONFIG))
    out.update({k: v for k, v in cfg.items() if k not in ("plant", "recipe")})
    out["plant"] = {**out.get("plant", {}), **cfg.get("plant", {})}
    out["recipe"] = {**out.get("recipe", {}), **cfg.get("recipe", {})}
    return out


# ------------------------------------------------------------------ one run at a time
class BaseRunManager:
    """Owns the data directory and the run in progress. Subclasses decide how the loop is pumped:
    a thread on a real machine, a slice at a time in a browser worker."""

    def __init__(self, root: str, allow_live: bool = True):
        self.root = os.path.abspath(root)
        self.allow_live = allow_live
        for sub in ("runs", "configs", "corpus"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self.lock = threading.Lock()
        self.stop_flag = False
        self.snapshot: Optional[dict] = None
        self.status = "idle"                      # idle | running | done | stopped | error
        self.message = ""
        self.current_id: Optional[str] = None
        self.options: dict = {}

    # -- run registry
    def runs(self):
        out = []
        d = os.path.join(self.root, "runs")
        for name in sorted(os.listdir(d), reverse=True):
            meta = os.path.join(d, name, "summary.json")
            if os.path.exists(meta):
                try:
                    with open(meta) as f:
                        out.append(json.load(f))
                except Exception:
                    pass
        return out

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.root, "runs", run_id)

    def corpus(self) -> Corpus:
        return Corpus(os.path.join(self.root, "corpus"))

    # -- building the stepper from the options the setup screen sent
    def build(self, opts: dict, run_id: str) -> TwinRun:
        cfg = merged_config(opts.get("config", {}))
        p, rc = build_plant_recipe(cfg)
        dt = float(opts.get("dt", 1.0))
        corpus_dir = os.path.join(self.root, "corpus") if opts.get("corpus", True) else None
        if opts.get("mode") == "live":
            if not self.allow_live:
                raise RuntimeError("This build cannot reach a serial bus. Run the twin locally for live data.")
            source = LiveBus(cfg)
            dt, duration, pace = 1.5, float(opts.get("hours", 8.0)) * 3600.0, 0.0
        else:
            source = SimPlant(p, rc, dt)
            duration = float(opts.get("seconds", 4200.0))
            pace = float(opts.get("pace", 0.0))
        return TwinRun(source, p, rc, dt, duration, os.path.join(self.run_dir(run_id), "twin_log.csv"),
                       arch=opts.get("arch", "mono"), estimator=opts.get("estimator", "fixed"),
                       delta=float(opts.get("delta", 0.0)), delta_res=float(opts.get("delta_res", 0.0)),
                       ne=int(opts.get("ne", 32)), corpus_dir=corpus_dir, quiet=True, run_id=run_id,
                       horizon=float(opts.get("horizon", 0.0) or 0.0),
                       fc_every=float(opts.get("fc_every", 60.0) or 60.0),
                       on_status=self._on_status, should_stop=lambda: self.stop_flag,
                       status_every=int(float(opts.get("status_every", 10)) or 1), pace=pace)

    def _begin(self, opts: dict) -> Optional[str]:
        with self.lock:
            if self.status == "running":
                return None
            self.stop_flag = False
            self.status = "running"
            self.message = ""
            self.snapshot = None
            self.current_id = time.strftime("%Y%m%d-%H%M%S")
            self.options = opts
            os.makedirs(self.run_dir(self.current_id), exist_ok=True)
            return self.current_id

    def _complete(self, tr: TwinRun, opts: dict, run_id: str):
        summary = tr.finish()
        summary.update(id=run_id, when=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       mode=opts.get("mode", "sim"), options=opts,
                       label=opts.get("label") or ("live bus" if opts.get("mode") == "live" else "simulated run"))
        rd = self.run_dir(run_id)
        with open(os.path.join(rd, "summary.json"), "w") as f:
            json.dump(summary, f, indent=1)
        with open(os.path.join(rd, "options.json"), "w") as f:
            json.dump(opts, f, indent=1)
        self.status = "stopped" if summary.get("stopped") else "done"
        self.message = f"{summary['seconds']:.0f} s of line time, {summary['events']} events"
        return summary

    def _fail(self, exc: BaseException, run_id: str):
        self.status = "error"
        self.message = f"{type(exc).__name__}: {exc}"
        try:
            with open(os.path.join(self.run_dir(run_id), "error.txt"), "w") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass

    def _on_status(self, snap: dict):
        snap["run_id"] = self.current_id
        self.snapshot = snap

    def stop(self):
        self.stop_flag = True
        return dict(ok=True)

    def start(self, opts: dict) -> dict:
        raise NotImplementedError

    def state(self) -> dict:
        return dict(status=self.status, message=self.message, run_id=self.current_id,
                    options=self.options, snapshot=self.snapshot, runs=self.runs())


class ThreadRunManager(BaseRunManager):
    """The local console: the loop runs on a worker thread and the HTTP handler stays responsive."""

    def __init__(self, root: str, allow_live: bool = True):
        super().__init__(root, allow_live)
        self.thread: Optional[threading.Thread] = None

    def start(self, opts: dict) -> dict:
        run_id = self._begin(opts)
        if run_id is None:
            return dict(ok=False, error="A run is already going. Stop it first.")
        self.thread = threading.Thread(target=self._work, args=(opts, run_id), daemon=True)
        self.thread.start()
        return dict(ok=True, run_id=run_id)

    def _work(self, opts: dict, run_id: str):
        try:
            tr = self.build(opts, run_id)
            while tr.step():
                pass
            self._complete(tr, opts, run_id)
        except Exception as exc:
            self._fail(exc, run_id)


class StepRunManager(BaseRunManager):
    """For a host with no threads and no right to block -- a browser worker. `start()` only
    arms the run; `pump(n)` advances it n sweeps and returns, so the host can yield in between."""

    def __init__(self, root: str, allow_live: bool = False):
        super().__init__(root, allow_live)
        self.tr: Optional[TwinRun] = None

    def start(self, opts: dict) -> dict:
        run_id = self._begin(opts)
        if run_id is None:
            return dict(ok=False, error="A run is already going. Stop it first.")
        try:
            self.tr = self.build(opts, run_id)
        except Exception as exc:
            self._fail(exc, run_id)
            return dict(ok=False, error=self.message)
        return dict(ok=True, run_id=run_id)

    def pump(self, n: int = 200) -> dict:
        """Advance up to n sweeps. Returns {running, progress} so the host knows whether to come back."""
        if self.tr is None or self.status != "running":
            return dict(running=False, progress=1.0)
        try:
            for _ in range(max(1, int(n))):
                if not self.tr.step():
                    self._complete(self.tr, self.options, self.current_id)
                    self.tr = None
                    return dict(running=False, progress=1.0)
        except Exception as exc:
            self._fail(exc, self.current_id or "unknown")
            self.tr = None
            return dict(running=False, progress=1.0, error=self.message)
        return dict(running=True, progress=self.tr.progress)


# ------------------------------------------------------------------ routing, transport-free
class TwinAPI:
    def __init__(self, manager: BaseRunManager, env: Optional[dict] = None, ports=None):
        self.m = manager
        self.env = env or dict(live=True, kind="local")
        self._ports = ports if ports is not None else list_ports

    def get(self, path: str):
        m = self.m
        if path == "/api/state":
            return 200, m.state()
        if path == "/api/defaults":
            return 200, defaults(self.env)
        if path == "/api/configs":
            d = os.path.join(m.root, "configs")
            out = []
            for name in sorted(os.listdir(d)):
                if name.endswith(".json"):
                    with open(os.path.join(d, name)) as f:
                        out.append(dict(name=name[:-5], config=json.load(f)))
            return 200, out
        if path == "/api/ports":
            ports = self._ports() if callable(self._ports) else list(self._ports)
            return 200, dict(ports=ports)
        if path == "/api/corpus":
            c = m.corpus()
            return 200, dict(signatures=c.signatures()[-200:], labels=c.labels())
        if path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            rd = m.run_dir(run_id)
            if not os.path.isdir(rd):
                return 404, dict(error="no such run")
            summary = {}
            sp = os.path.join(rd, "summary.json")
            if os.path.exists(sp):
                with open(sp) as f:
                    summary = json.load(f)
            csv_text = ""
            cp = os.path.join(rd, "twin_log.csv")
            if os.path.exists(cp):
                with open(cp) as f:
                    csv_text = f.read()
            return 200, dict(summary=summary, csv=csv_text)
        return 404, dict(error="not found")

    def post(self, path: str, body: dict):
        m = self.m
        body = body or {}
        if path == "/api/run":
            return 200, m.start(body)
        if path == "/api/stop":
            return 200, m.stop()
        if path == "/api/configs":
            name = "".join(ch for ch in str(body.get("name", "unnamed"))
                           if ch.isalnum() or ch in "-_ ").strip() or "unnamed"
            with open(os.path.join(m.root, "configs", name + ".json"), "w") as f:
                json.dump(body.get("config", {}), f, indent=1)
            return 200, dict(ok=True, name=name)
        if path == "/api/corpus/label":
            if not body.get("id"):
                return 400, dict(error="need a signature id")
            c = m.corpus()
            rec = dict(id=body.get("id"), labeled_at=time.strftime("%Y-%m-%dT%H:%M:%S"), iar=None,
                       cause=body.get("cause", ""), intuition=body.get("intuition", ""),
                       action=body.get("action", ""), effect=body.get("effect", ""),
                       result=body.get("result", ""), shadow_actions=body.get("shadow_actions", []),
                       confidence=body.get("confidence"), tags=body.get("tags", []))
            with open(c.lab_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            return 200, dict(ok=True)
        if path == "/api/export":
            c = m.corpus()
            out = os.path.join(m.root, "ciaer_export.jsonl")
            n = c.export_ciaer(out)
            text = open(out).read() if os.path.exists(out) else ""
            return 200, dict(ok=True, path=out, count=n, jsonl=text)
        return 404, dict(error="not found")


def list_ports():
    try:
        from serial.tools import list_ports as lp
        return [dict(device=p.device, description=p.description) for p in lp.comports()]
    except Exception:
        return []
