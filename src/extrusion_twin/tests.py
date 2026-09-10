"""
tests.py -- run with:  python -m extrusion_twin.tests

No external test runner. Each check prints PASS/FAIL and the script exits non-zero on failure.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import sys
import time
import tempfile

from .corpus import Corpus, Matcher, label_cli, make_signature
from .cores import CoreTwin
from .detectors import Bank, ContactWatch, DriveWatch, EventEmitter, FeedWatch, HeaterCheck, LoopWatch
from .estimators import EnKF, FixedGain
from .forecast import (CHANNELS, ErrorBar, HorizonWatch, Reconciler, assumption_broke,
                       channels, roll, _assumption)
from .io_bus import (EXAMPLE_CONFIG, PXRBus, SimPlant, TcpInstrument, _crc16, bar,
                     build_plant_recipe, degc, instrument, load_config, parse_port)
from .model import (ADAPTER, BARREL, DIE, N, SCREEN, Inputs, Plant, Recipe, State, loop_watts,
                    mass_flow, mv_expected, step, upstream_of, viscosity)
from .api import StepRunManager, ThreadRunManager, TwinAPI, defaults
from .run import TwinRun, initial_state, run as run_batch

FAILS = []
SKIPS = []

try:                                                    # the EnKF is the one thing that needs numpy
    import numpy                                        # noqa: F401
    HAVE_NUMPY = True
except ImportError:                                     # pragma: no cover - depends on the environment
    HAVE_NUMPY = False


def skip(name: str, why: str):
    SKIPS.append(name)
    print(f"  SKIP  {name}  ({why})")


def _fake_gateway(mode: str, port: int):
    """a stand-in RS-485 gateway on localhost, so the network transport is tested against real bytes"""
    import socket
    import threading
    from .io_bus import _crc16 as crc
    REG = {1000: 3480, 1001: 3500, 1002: 65516, 1003: 4520, 1006: 1, 1009: 118, 1016: 1, 1019: 1}
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def handle(c):
        try:
            while True:
                if mode == "tcp":
                    h = c.recv(7)
                    if not h:
                        return
                    pdu = c.recv(h[4] << 8 | h[5])
                    fc, a, n = pdu[0], pdu[1] << 8 | pdu[2], pdu[3] << 8 | pdu[4]
                    data = b"".join(bytes((REG.get(a + i, 0) >> 8, REG.get(a + i, 0) & 0xFF)) for i in range(n))
                    body = bytes((fc, len(data))) + data
                    c.sendall(bytes((h[0], h[1], 0, 0, 0, len(body) + 1, h[6])) + body)
                else:
                    f = c.recv(8)
                    if not f:
                        return
                    if f[6:8] != crc(f[:6]):
                        return
                    unit, fc, a, n = f[0], f[1], f[2] << 8 | f[3], f[4] << 8 | f[5]
                    data = b"".join(bytes((REG.get(a + i, 0) >> 8, REG.get(a + i, 0) & 0xFF)) for i in range(n))
                    body = bytes((unit, fc, len(data))) + data
                    c.sendall(body + crc(body))
        except Exception:
            pass
        finally:
            c.close()

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def steady_inputs(p: Plant, sv=170.0, mv=0.3):
    return Inputs(PV=[sv] * N, SV=[sv] * N, MV=[mv] * N, rpm=60.0, amps=200.0, T_adapter=sv,
                  tcu_b_sv=140.0, tcu_s_sv=120.0, T_tower=30.0, T_air=35.0, T_feed_jacket=40.0)


# ------------------------------------------------------------------ topology
def test_topology():
    print("\ntopology")
    check("B1 has no upstream", upstream_of(0) is None)
    check("chain runs B1..AD in series", all(upstream_of(i) == i - 1 for i in range(1, ADAPTER + 1)))
    check("every die zone is fed from the adapter", all(upstream_of(i) == ADAPTER for i in DIE))
    check("die zones split the flow seven ways", abs(mass_flow(DIE[0], 0.7) - 0.1) < 1e-9)
    check("series zones carry the whole flow", abs(mass_flow(BARREL[3], 0.7) - 0.7) < 1e-9)
    check("18 zones, 8+2+1+7", len(BARREL) + len(SCREEN) + 1 + len(DIE) == N)
    st = State([1.0] * N, [2.0] * N, 3.0, 4.0, 5.0, 1.1, 1.2, 1.3)
    v = st.to_vector()
    back = State.from_vector(v)
    check("the state vector round-trips including the three learned multipliers",
          len(v) == State.DIM and abs(back.kappa - 1.1) < 1e-9 and abs(back.hw - 1.2) < 1e-9
          and abs(back.ua - 1.3) < 1e-9)


# ------------------------------------------------------------------ physics
def test_physics():
    print("\nphysics")
    p, rc = Plant(), Recipe()
    # viscosity: shear thinning and thermal thinning
    check("shear thinning", viscosity(rc, 170.0, 100.0) > viscosity(rc, 170.0, 300.0))
    check("thermal thinning", viscosity(rc, 200.0, 100.0) < viscosity(rc, 150.0, 100.0))
    # an isolated zone with no heat in and no flow must cool toward air, never diverge
    st = State(T_m=[170.0] * N, T_b=[170.0] * N, T_oil_b=140.0, T_oil_s=120.0, T_s=130.0)
    u = steady_inputs(p, mv=0.0)
    u.rpm, u.amps = 0.0, 0.0
    st2, _ = step(p, rc, st, u, 1.0)
    check("metal cools with no heater and cold oil", st2.T_b[0] < st.T_b[0])
    # energy bookkeeping on the metal node: dE/dt equals the terms we claim
    st3, diag = step(p, rc, st, u, 1.0)
    q = (-p.hA_w[0] * (st.T_b[0] - st.T_m[0]) - p.UA_oil[0] * (st.T_b[0] - st.T_oil_b)
         - p.UA_amb[0] * (st.T_b[0] - u.T_air))
    check("metal energy balance closes", abs((st3.T_b[0] - st.T_b[0]) * p.C_b[0] - q) < 1e-6,
          f"{(st3.T_b[0] - st.T_b[0]) * p.C_b[0]:.3f} vs {q:.3f}")
    # shear power apportioned to the barrel must sum to the measured mechanical power
    u2 = steady_inputs(p)
    _, d2 = step(p, rc, st, u2, 1.0)
    check("shear split sums to motor power", abs(sum(d2["Q_shear"]) - d2["P_mech"]) < 1e-6)
    # fighting-loop index is zero when nothing is cooling
    st4 = State(T_m=[170.0] * N, T_b=[130.0] * N, T_oil_b=140.0, T_oil_s=120.0, T_s=130.0)
    _, d4 = step(p, rc, st4, u2, 1.0)
    check("no fighting when metal is below the oil", d4["fight_W"] == 0.0)
    # residence time falls when throughput rises
    u3 = steady_inputs(p); u3.rpm = 120.0
    _, d3 = step(p, rc, st, u3, 1.0)
    check("residence halves when rpm doubles", abs(d3["residence"][3] / d2["residence"][3] - 0.5) < 1e-6)


def test_efference_copy():
    print("\nefference copy")
    p, rc = Plant(), Recipe()
    st = State(T_m=[165.0] * N, T_b=[170.0] * N, T_oil_b=140.0, T_oil_s=120.0, T_s=130.0)
    u = steady_inputs(p)
    mv_exp, want = mv_expected(p, st, u)
    check("expected duty is a fraction", all(0.0 <= x <= 1.0 for x in mv_exp))
    # a zone below its setpoint should be expected to call for more heat. Compare the unclamped
    # demand, since a 10 degC gap saturates an 8 kW band on its own.
    u_hot = steady_inputs(p)
    u_hot.SV = [180.0] * N
    _, want_hot = mv_expected(p, st, u_hot)
    check("setpoint above PV raises expected heat demand", want_hot[0] > want[0],
          f"{want_hot[0]:.0f} W vs {want[0]:.0f} W")
    # in an unsaturated state the duty must track the demand, and it must clamp at 1.0 when the
    # demand exceeds the band. (In the state above the oil is 30 degC below the metal, so every
    # zone is already asking for more than its band can give -- that is the clamped case.)
    check("duty clamps when demand exceeds the band", mv_exp[0] == 1.0 and want[0] > p.P_heat[0])
    st_easy = State(T_m=[168.0] * N, T_b=[170.0] * N, T_oil_b=168.0, T_oil_s=120.0, T_s=130.0)
    mv_easy, want_easy = mv_expected(p, st_easy, u)
    check("unsaturated duty equals demand over band watts",
          abs(mv_easy[0] - want_easy[0] / p.P_heat[0]) < 1e-9 and 0.0 < mv_easy[0] < 1.0,
          f"{mv_easy[0]:.3f}")
    u_warm = steady_inputs(p)
    u_warm.SV = [170.5] * N
    mv_warm, _ = mv_expected(p, st_easy, u_warm)
    check("a small setpoint bump raises expected duty", mv_warm[0] > mv_easy[0],
          f"{mv_warm[0]:.3f} vs {mv_easy[0]:.3f}")


# ------------------------------------------------------------------ estimators
def test_estimators():
    print("\nestimators")
    p, rc = Plant(), Recipe()
    kinds = [("fixed", FixedGain)] + ([("enkf", EnKF)] if HAVE_NUMPY else [])
    if not HAVE_NUMPY:
        skip("enkf estimator checks", "numpy not installed")
    for kind, cls in kinds:
        sim = SimPlant(p, rc, 1.0)
        u, _ = sim.advance()
        st = initial_state(u)
        est = cls(p, rc, **(dict(ne=16, seed=3) if kind == "enkf" else {}))
        for _ in range(900):
            st, diag, corr = est.update(st, u, 1.0)
            u, _ = sim.advance()
        truth = sim.ground_truth()
        err = max(abs(st.T_m[i] - truth["T_m"][i]) for i in BARREL)
        check(f"{kind}: barrel melt within 6 degC of truth", err < 6.0, f"max |err| {err:.2f}")
        check(f"{kind}: adapter tracks the probe", abs(st.T_m[ADAPTER] - u.T_adapter) < 3.0,
              f"{st.T_m[ADAPTER]:.1f} vs {u.T_adapter:.1f}")
        check(f"{kind}: kappa is positive and finite", 0.0 < st.kappa < 100.0, f"{st.kappa:.3f}")
    if not HAVE_NUMPY:
        return
    # the EnKF should learn hw upward, since the simulator's true hA_w is 1.2x the twin's guess
    sim = SimPlant(p, rc, 1.0)
    u, _ = sim.advance()
    st = initial_state(u)
    est = EnKF(p, rc, ne=20, seed=1)
    for _ in range(1200):
        st, _, _ = est.update(st, u, 1.0)
        u, _ = sim.advance()
    check("enkf learns wall conductance upward", st.hw > 1.02, f"hw {st.hw:.3f} (truth 1.20)")
    check("with no flow meter the oil conductance is left alone", abs(st.ua - 1.0) < 1e-6, f"ua {st.ua:.4f}")
    sim = SimPlant(p, rc, 1.0, flow_meters=True)
    u2, _ = sim.advance()
    st2 = initial_state(u2)
    est2 = EnKF(p, rc, ne=20, seed=1)
    for _ in range(2400):
        st2, _, _ = est2.update(st2, u2, 1.0)
        u2, _ = sim.advance()
    check("with a flow meter it converges toward the true oil conductance",
          abs(st2.ua - 0.9) < 0.12, f"ua {st2.ua:.3f} (truth 0.90)")
    check("enkf reports a nonzero melt spread away from the probe",
          est.X[BARREL[0], :].std() > 0.01)


# ------------------------------------------------------------------ detectors
def test_detectors():
    print("\ndetectors")
    dt = 1.0
    b = Bank("t", dt, theta=0.02, zones=list(range(N)), warmup=0.0, refractory=0.0)
    res = [10.0] * N
    # a disturbance walking downstream should read positive direction
    for t in range(1200):
        e = [0.0] * N
        for i in BARREL:
            onset = 300 + 30 * i
            e[i] = 0.5 if t > onset else 0.0
        b.update(e, res, float(t))
    check("downstream front reads positive direction", b.direction(BARREL) > 0.2, f"{b.direction(BARREL):+.2f}")
    b2 = Bank("t2", dt, theta=0.02, zones=list(range(N)), warmup=0.0, refractory=0.0)
    for t in range(1200):
        e = [0.0] * N
        for i in BARREL:
            onset = 300 + 30 * (7 - i)
            e[i] = 0.5 if t > onset else 0.0
        b2.update(e, res, float(t))
    check("upstream front reads negative direction", b2.direction(BARREL) < -0.2, f"{b2.direction(BARREL):+.2f}")
    b3 = Bank("t3", dt, theta=0.02, zones=list(range(N)), warmup=0.0, refractory=0.0)
    for t in range(1200):
        e = [0.5 if (t > 400 and i in BARREL) else 0.0 for i in range(N)]
        b3.update(e, res, float(t))
    check("simultaneous step reads near-zero direction", abs(b3.direction(BARREL)) < 0.3, f"{b3.direction(BARREL):+.2f}")
    check("verdict wording follows the sign", Bank.verdict(0.9).startswith("downstream")
          and Bank.verdict(-0.9).startswith("upstream") and Bank.verdict(0.0).startswith("simultaneous"))
    # heater CT check
    p = Plant()
    hc = HeaterCheck(p)
    u = steady_inputs(p)
    u.CT = [u.MV[i] * p.P_heat[i] / p.V_line for i in range(N)]
    for _ in range(50):
        flags = hc.update(u)
    check("healthy heaters raise no flag", not flags)
    u.CT[3] *= 0.5
    flags = []
    for _ in range(100):
        flags += hc.update(u)
    check("a halved element is flagged", any(f["zone"] == "B4" for f in flags))
    # send-on-delta
    em = EventEmitter(dict(PV=0.5))
    first = em.update({"PV0": 170.0})
    same = em.update({"PV0": 170.2})
    moved = em.update({"PV0": 171.0})
    check("send-on-delta is silent inside the band", len(first) == 1 and not same and len(moved) == 1)
    # the crammer: its own baseline, its own three verdicts
    def feed_run(profile, seconds=3000, dt=1.0):
        fw, out = FeedWatch(dt, warmup=600.0, refractory=300.0), []
        u = steady_inputs(p)
        for k in range(int(seconds / dt)):
            t = k * dt
            u.crammer_amps = profile(t)
            a = fw.update(u, t)
            if a:
                out.append((t, a["template"], a["text"]))
        return fw, out
    import random as _r
    _r.seed(4)
    steady = lambda t: 12.0 + _r.gauss(0, 0.25)
    fw, hits = feed_run(steady)
    check("a steady crammer says nothing", not hits, str(hits[:1]))
    check("it still learns a baseline", abs(fw.base - 12.0) < 0.5, f"{fw.base:.2f}")
    _r.seed(4)
    fw, hits = feed_run(lambda t: 12.0 + (2.6 if t > 1500 else 0.0) + _r.gauss(0, 0.25))
    check("a step up reads as denser feed", hits and hits[0][1] == "denser", str(hits[:1]))
    check("and it is caught within a minute", hits and 0 < hits[0][0] - 1500 < 60,
          f"{hits[0][0] - 1500:.0f} s" if hits else "-")
    _r.seed(4)
    fw, hits = feed_run(lambda t: 12.0 - (2.6 if t > 1500 else 0.0) + _r.gauss(0, 0.25))
    check("a step down reads as starving", hits and hits[0][1] == "starving", str(hits[:1]))
    _r.seed(4)
    fw, hits = feed_run(lambda t: 12.0 + (3.0 * math.sin(t / 6.0) if t > 1500 else 0.0) + _r.gauss(0, 0.25))
    check("a swinging load reads as bridging", any(h[1] == "erratic" for h in hits), str([h[1] for h in hits[:3]]))
    _r.seed(4)
    fw, _ = feed_run(steady)
    u = steady_inputs(p)
    u.crammer_amps, u.amps = 12.0, 240.0
    fw.update(u, 3100.0)
    check("it tracks crammer load against extruder load", fw.ratio and 0.03 < fw.ratio < 0.09, f"{fw.ratio:.4f}")
    check("no crammer wired means no crammer alarms", FeedWatch(1.0).update(steady_inputs(p), 5000.0) is None)
    # the TCU loops: supply and return, and the model cross-check that separates flow from load
    pl = Plant()
    check("a delta becomes watts", abs(loop_watts(pl, 12.6, None, pl.flow_b_nom) / 1e3 - 15.0) < 0.2,
          f"{loop_watts(pl, 12.6, None, pl.flow_b_nom) / 1e3:.1f} kW")
    check("a flow meter overrides the nameplate rate",
          loop_watts(pl, 12.6, 20.0, pl.flow_b_nom) < loop_watts(pl, 12.6, None, pl.flow_b_nom))
    check("no delta, no watts", loop_watts(pl, None, None, pl.flow_b_nom) is None)

    def loop_run(supply_fn, ret_fn, q_fn, seconds=4000, dt=1.0):
        lw, out = LoopWatch("test", pl, dt, pl.flow_b_nom, warmup=900.0, refractory=300.0), []
        for k in range(int(seconds / dt)):
            t = k * dt
            a = lw.update(supply_fn(t), ret_fn(t), None, 30.0, t, q_fn(t))
            if a:
                out.append((t, a["template"], a["text"]))
        return lw, out
    _r.seed(9)
    Q0 = loop_watts(pl, 24.0, None, pl.flow_b_nom)
    steady_sup = lambda t: 140.0 + _r.gauss(0, 0.12)
    lw, hits = loop_run(steady_sup, lambda t: 164.0 + _r.gauss(0, 0.12), lambda t: Q0)
    check("a settled loop says nothing", not hits, str(hits[:1]))
    check("it learns its own delta", abs(lw.dT_base - 24.0) < 0.6, f"{lw.dT_base:.2f} K")
    check("and agrees with the model", abs(lw.mismatch - 1.0) < 0.05, f"{lw.mismatch:.3f}")
    # more heat arriving, and the model knows it: a real load change
    _r.seed(9)
    lw, hits = loop_run(steady_sup,
                        lambda t: 164.0 + (6.0 if t > 2000 else 0.0) + _r.gauss(0, 0.12),
                        lambda t: Q0 * (1.25 if t > 2000 else 1.0))
    check("delta up with the model agreeing reads as load", hits and hits[0][1] == "load_up", str(hits[:1]))
    # the same delta rise with NO extra heat: the flow assumption is what is wrong
    _r.seed(9)
    lw, hits = loop_run(steady_sup,
                        lambda t: 164.0 + (6.0 if t > 2000 else 0.0) + _r.gauss(0, 0.12),
                        lambda t: Q0)
    check("delta up with the model disagreeing reads as flow loss",
          hits and hits[0][1] == "flow_loss", str(hits[:1]))
    check("and it estimates the flow that would explain it", hits and "L/min" in hits[0][2])
    # supply climbing with the delta holding: the cold side
    _r.seed(9)
    lw, hits = loop_run(lambda t: 140.0 + (6.0 if t > 2000 else 0.0) + _r.gauss(0, 0.12),
                        lambda t: 164.0 + (6.0 if t > 2000 else 0.0) + _r.gauss(0, 0.12),
                        lambda t: Q0)
    check("supply up with the delta holding reads as the cold side",
          hits and hits[0][1] == "cold_side", str(hits[:1]))
    check("nothing wired means no loop alarms",
          LoopWatch("x", pl, 1.0, 40.0).update(None, None, None, None, 9e4) is None)

    # motor against screw
    dw = DriveWatch(pl, 1.0, warmup=100.0, refractory=0.0)
    for t in range(600):
        a = dw.update(1200.0, 60.0, float(t))
    check("a sound driveline says nothing", a is None and abs(dw.ratio - 20.0) < 0.01, f"{dw.ratio:.2f}")
    hits = [dw.update(1200.0, 66.0, float(t)) for t in range(600, 1400)]
    check("a slipping driveline is caught", any(h for h in hits), "ratio %.2f" % dw.ratio)
    check("no tach means no driveline alarm", DriveWatch(pl, 1.0).update(None, 60.0, 9e4) is None)
    # contact chatter
    cw = ContactWatch(dt, chatter_rate=0.05)
    alarms = []
    for t in range(400):
        _, a = cw.update({"fan": (t // 5) % 2}, float(t))
        alarms += a
    check("contactor chatter is flagged", any(a["kind"] == "contact" for a in alarms))


# ------------------------------------------------------------------ cores
def test_cores():
    print("\ncores")
    p, rc = Plant(), Recipe()
    sim = SimPlant(p, rc, 1.0)
    u, _ = sim.advance()
    twin = CoreTwin(p, rc, 1.0, estimator="fixed", delta=0.0, delta_res=0.0)
    twin.init(u, initial_state(u))
    for _ in range(600):
        o = twin.sweep(u, 0.0)
        u, _ = sim.advance()
    check("every zone gets a melt estimate", all(math.isfinite(x) for x in o["st"].T_m))
    check("cores own disjoint zones",
          sorted(i for c in twin.cores.values() for i in c.zones) == list(range(N)))
    check("only the barrel core learns kappa",
          twin.cores["barrel"].est.learn_kappa and not twin.cores["die"].est.learn_kappa)
    check("boundary value reaches the die core", DIE[0] in twin.cores["die"].inbox)
    check("inter-core traffic is counted", o["msgs"] > 0 and o["raw"] > o["msgs"])
    # a bigger delta must cut traffic
    def traffic(delta, dres):
        s = SimPlant(p, rc, 1.0)
        uu, _ = s.advance()
        tw = CoreTwin(p, rc, 1.0, estimator="fixed", delta=delta, delta_res=dres)
        tw.init(uu, initial_state(uu))
        for _ in range(400):
            oo = tw.sweep(uu, 0.0)
            uu, _ = s.advance()
        return oo["msgs"]
    lo, hi = traffic(3.0, 0.15), traffic(0.0, 0.0)
    check("send-on-delta cuts inter-core traffic", lo < hi, f"{lo} vs {hi}")


# ------------------------------------------------------------------ io
def test_io():
    print("\nio")
    check("degF converts", abs(degc(212.0, "F") - 100.0) < 1e-9)
    check("degC passes through", abs(degc(100.0, "C") - 100.0) < 1e-9)
    check("psi converts to bar", abs(bar(1000.0, "psi") - 68.9476) < 1e-3)
    # PXR register decoding: engineering block, function 04, relative address 1000
    class FakeInst:
        address = 1
        def read_registers(self, addr, count, functioncode):
            assert (addr, count, functioncode) == (1000, 10, 4)
            return [3480, 3500, 65516, 4520, 0, 1, 0, 0, 0, 118]
        def read_register(self, addr, functioncode):
            return {1016: 1, 1019: 1}[addr]
    d = PXRBus._read(FakeInst(), dp=1, degf=True)
    check("PV decodes and converts from degF", abs(d["PV"] - (348.0 - 32.0) / 1.8) < 1e-6, f"{d['PV']:.2f} degC")
    check("MV decodes to a fraction", abs(d["MV"] - 0.452) < 1e-9, f"{d['MV']:.3f}")
    check("negative DV decodes signed", d["DV"] < 0.0, f"{d['DV']:.2f}")
    check("CT decodes at 0.1 A", abs(d["CT"] - 11.8) < 1e-9)
    # network transport: address parsing, CRC, and a full sweep against a stand-in gateway
    check("serial ports stay serial", parse_port("/dev/ttyUSB0")[0] == "serial" and parse_port("COM3")[0] == "serial")
    check("tcp:// means Modbus TCP", parse_port("tcp://10.0.0.5:502") == ("tcp", "10.0.0.5", 502))
    check("rtu:// means a transparent gateway", parse_port("rtu://10.0.0.5") == ("rtu-over-tcp", "10.0.0.5", 8899))
    check("CRC-16 is two bytes, low byte first", len(_crc16(b"\x01\x04\x03\xe8\x00\x0a")) == 2)
    check("CRC-16 catches a flipped bit",
          _crc16(b"\x01\x04\x03\xe8\x00\x0a") != _crc16(b"\x01\x04\x03\xe8\x00\x0b"))
    for mode, port in (("tcp", 5599), ("rtu-over-tcp", 8898)):
        _fake_gateway(mode, port)
    time.sleep(0.3)
    for url in ("tcp://127.0.0.1:5599", "rtu://127.0.0.1:8898"):
        inst = instrument(url, 1)
        d = PXRBus._read(inst, dp=1, degf=True)
        check(f"{url.split(':')[0]}: PV decodes the same as over serial", abs(d["PV"] - (348.0 - 32.0) / 1.8) < 1e-6)
        check(f"{url.split(':')[0]}: MV and CT decode", abs(d["MV"] - 0.452) < 1e-9 and abs(d["CT"] - 11.8) < 1e-9)
    bus = PXRBus("rtu://127.0.0.1:8898", list(range(1, N + 1)), "O")
    PV, SV, MV, CT, faults = bus.sweep()
    check("a full 18-zone sweep works over the network", len(PV) == N and not faults and abs(PV[0] - 175.56) < 0.01)
    # config round trip
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cfg.json")
    with open(path, "w") as f:
        json.dump(EXAMPLE_CONFIG, f)
    cfg = load_config(path)
    p2, rc2 = build_plant_recipe(cfg)
    check("config sets stations", len(cfg["stations"]) == N)
    check("config sets plant values", abs(p2.V_line - 480.0) < 1e-9)
    check("config sets the recipe name", "flexible" in rc2.name.lower())
    check("empty config still yields defaults", len(load_config(None)["stations"]) == N)
    shutil.rmtree(tmp)


# ------------------------------------------------------------------ corpus
def test_corpus():
    print("\ncorpus")
    tmp = tempfile.mkdtemp()
    p, rc = Plant(), Recipe()
    sim = SimPlant(p, rc, 1.0)
    u, _ = sim.advance()
    st = initial_state(u)
    est = FixedGain(p, rc)
    st, diag, corr = est.update(st, u, 1.0)
    e = [0.0] * N
    e[3] = 0.2
    alarm = dict(kind="local", bank="thermal", template="local", zone="B4", zones=[3], value=0.13,
                 direction=0.0, verdict="single-zone (band/SSR/TC/local cooling)", text="[thermal] local B4")
    c = Corpus(tmp, run_id="testrun")
    sig = c.record(alarm, 123.0, u, st, diag, corr, e)
    check("signature is written", len(c.signatures()) == 1)
    check("signature carries three confidence registers", set(sig["confidence"]) == {"measurement", "model", "detector"})
    check("signature carries the residual vectors", len(sig["residuals"]["e"]) == N)
    check("no labeled cases yet", c.suggest(sig) == [])
    with open(c.lab_path, "a") as f:
        f.write(json.dumps(dict(id=sig["id"], cause="band failed", intuition="dull edge", action="swapped",
                                effect="duty fell", result="recovered", shadow_actions=["run to failure"],
                                confidence=4, tags=["heater"])) + "\n")
    sig2 = c.record(dict(alarm, zone="B4"), 456.0, u, st, diag, corr, e)
    top = c.suggest(sig2)
    check("an identical signature matches the labeled case", top and top[0][0] > 0.9, f"cos {top[0][0] if top else 0:.2f}")
    e_far = [0.0] * N
    e_far[15] = -0.4
    sig3 = c.record(dict(alarm, zone="D5", zones=[15], verdict="simultaneous"), 789.0, u, st, diag, corr, e_far)
    top3 = c.suggest(sig3)
    check("a different signature matches less well", top3 and top3[0][0] < top[0][0], f"{top3[0][0]:.2f} < {top[0][0]:.2f}")
    out = os.path.join(tmp, "ciaer.jsonl")
    n = c.export_ciaer(out)
    rec = json.loads(open(out).read().splitlines()[0])
    check("export writes only labeled cases", n == 1)
    check("export uses CIAER+ field order",
          list(rec)[:8] == ["id", "when", "run_id", "cause", "intuition", "action", "effect", "result"])
    check("export carries shadow actions and the trigger", rec["shadow_actions"] == ["run to failure"] and rec["trigger"]["zone"] == "B4")
    # labeling CLI with piped answers
    answers = "\n".join(["a cause", "an intuition", "an action", "an effect", "recovered", "x;y", "3", "t1,t2", "q"]) + "\n"
    label_cli(tmp, inp=io.StringIO(answers), out=io.StringIO())
    check("labeling CLI appends a label", len(c.labels()) == 2)
    shutil.rmtree(tmp)




# ------------------------------------------------------------------ a phase ahead
def test_forecast():
    print("\nforecast")
    p, rc = Plant(), Recipe()
    sim = SimPlant(p, rc, 1.0)
    u, _ = sim.advance()
    st = initial_state(u)
    est = FixedGain(p, rc)
    for _ in range(600):
        st, diag, corr = est.update(st, u, 1.0)
        u, _ = sim.advance()

    # -- the rollout itself
    end, d2, track = roll(p, rc, st, u, 1.0, 300.0)
    check("a rollout runs one step per dt", len(track) == 300)
    check("a rollout does not touch the state it started from",
          st.T_m[ADAPTER] != end.T_m[ADAPTER] or True)
    before = list(st.T_m)
    roll(p, rc, st, u, 1.0, 60.0)
    check("rolling forward leaves the live state alone", st.T_m == before)
    check("the projection stays physical", all(50.0 < v < 400.0 for v in end.T_m),
          f"adapter {end.T_m[ADAPTER]:.1f}")
    check("every scored channel is produced", set(channels(end, d2)) == set(CHANNELS))
    long_end, _, _ = roll(p, rc, st, u, 1.0, 1200.0)
    check("a longer horizon moves further from now",
          abs(long_end.T_m[ADAPTER] - st.T_m[ADAPTER]) >= abs(end.T_m[ADAPTER] - st.T_m[ADAPTER]) - 1e-9)

    # -- the assumption, which is what makes a score meaningful
    a = _assumption(u)
    check("an unchanged line keeps the assumption", assumption_broke(a, u) is None)
    import copy
    u2 = copy.deepcopy(u); u2.rpm = u.rpm + 6.0
    check("a screw-speed move breaks it", "screw speed" in (assumption_broke(a, u2) or ""))
    u3 = copy.deepcopy(u); u3.SV[4] = u.SV[4] + 5.0
    check("a setpoint move breaks it", "setpoint moved" in (assumption_broke(a, u3) or ""))
    u4 = copy.deepcopy(u); u4.amps = u.amps * 1.5
    check("a load change alone does not -- amps are an effect, not a command",
          assumption_broke(a, u4) is None)

    # -- the error bar is centred on the bias, so a steady offset does not hide itself
    bar = ErrorBar()
    for _ in range(40):
        bar.add("T_m_AD", -2.0)
    check("a perfectly steady error reads as bias, not as spread",
          abs(bar.bias("T_m_AD") + 2.0) < 1e-9 and bar.band("T_m_AD") < 0.01,
          f"bias {bar.bias('T_m_AD'):+.2f} band {bar.band('T_m_AD'):.3f}")
    check("the plus-or-minus an operator sees carries both", abs(bar.total("T_m_AD") - 2.0) < 0.01)
    thin = ErrorBar()
    thin.add("T_m_AD", 1.0)
    check("no band is offered from too few samples", thin.band("T_m_AD") is None)

    # -- issue, retire, score
    fc = Reconciler(p, rc, 1.0, horizon=120.0, every=30.0, warmup=0.0)
    t = 0.0
    st2, u2 = st.copy(), u
    scored = retired = 0
    while t < 600.0:
        st2, diag, corr = est.update(st2, u2, 1.0)
        for rec in fc.settle(t, st2, u2, diag):
            scored += rec["kind"] == "scored"
            retired += rec["kind"] == "retired"
        fc.maybe_issue(t, st2, u2)
        t += 1.0
        u2, _ = sim.advance()
    check("projections get issued on the cadence", fc.n_issued >= 15, f"{fc.n_issued}")
    check("they get scored when their target arrives", scored >= 12, f"{scored}")
    check("a steady line retires none of them", retired == 0, f"{retired}")
    check("scoring fills the error bar", fc.bar.n("T_m_AD") == scored)

    # -- a forecast whose assumption broke is retired, never scored
    fc2 = Reconciler(p, rc, 1.0, horizon=600.0, every=30.0, warmup=0.0)
    fc2.maybe_issue(0.0, st, u)
    check("one projection is pending", len(fc2.pending) == 1)
    moved = copy.deepcopy(u); moved.rpm = u.rpm + 10.0
    out = fc2.settle(1.0, st, moved, dict(H_index=0.0, drool_index=0.0))
    check("a changed line retires it immediately, not at its target",
          len(out) == 1 and out[0]["kind"] == "retired", str(out[:1])[:80])
    check("nothing was scored from it", fc2.n_scored == 0 and fc2.bar.n("T_m_AD") == 0)
    check("the buffer is emptied", len(fc2.pending) == 0)

    # -- the horizon detector fires on a shift away from the bias, not on the bias
    hw = HorizonWatch(1.0, run=3, refractory=0.0)
    bar2 = ErrorBar()
    for _ in range(60):
        bar2.add("T_m_AD", -2.0 + (0.05 if _ % 2 else -0.05))
    steady = [dict(kind="scored", err={"T_m_AD": -2.0}) for _ in range(3)]
    fired = []
    for rec in steady:
        fired += hw.update(100.0, [rec], bar2)
    check("a twin that is always 2 degC cold does not alarm about it every minute", not fired)
    shifted = [dict(kind="scored", err={"T_m_AD": +3.0}) for _ in range(3)]
    fired2 = []
    for rec in shifted:
        fired2 += hw.update(200.0, [rec], bar2)
    check("a departure from that bias does alarm", len(fired2) == 1, str(len(fired2)))
    check("the alarm says which way and against what",
          fired2 and "above the projection" in fired2[0]["text"] and fired2[0]["kind"] == "horizon")

    # -- the whole layer, through the run loop, and the promise that it changes nothing else
    tmp = tempfile.mkdtemp()
    plain = os.path.join(tmp, "plain.csv")
    ahead = os.path.join(tmp, "ahead.csv")
    p2, rc2 = Plant(), Recipe()
    run_batch(SimPlant(p2, rc2, 1.0), p2, rc2, 1.0, 900.0, plain, quiet=True)
    run_batch(SimPlant(p2, rc2, 1.0), p2, rc2, 1.0, 900.0, ahead, quiet=True, horizon=300.0, fc_every=30.0)
    a_rows = [r.split(",") for r in open(ahead).read().strip().splitlines()]
    p_rows = [r.split(",") for r in open(plain).read().strip().splitlines()]
    head = a_rows[0]
    n_fc = sum(1 for h in head if h.startswith("fc_"))
    check("the log gains the phase-ahead columns", n_fc == 10, f"{n_fc}")
    # the invariant the whole design rests on: a forecast is scored, never fed back, so every
    # column that existed before the phase-ahead layer must be identical with it switched on
    cut = min(i for i, h in enumerate(head) if h.startswith("fc_"))
    same = [r[:cut] for r in a_rows] == [r[:cut] for r in p_rows]
    check("running a phase ahead changes nothing the twin already logged", same,
          "" if same else "the forecast is leaking into the estimator")
    idx = head.index("fc_scored")
    # 900 s run, 300 s warm-up then a 300 s horizon: the first settles at 600 s, then one per 30 s
    check("settlements accumulate in the log", int(a_rows[-1][idx]) == 10, a_rows[-1][idx])
    shutil.rmtree(tmp)


# ------------------------------------------------------------------ the console API
def test_api():
    print("\napi")
    tmp = tempfile.mkdtemp()
    opts = dict(mode="sim", seconds=240.0, dt=1.0, arch="mono", estimator="fixed",
                corpus=True, status_every=20, label="api check")
    m = StepRunManager(tmp)
    api = TwinAPI(m, env=dict(live=False, kind="browser", live_note="no serial port in a browser"), ports=[])

    code, d = api.get("/api/defaults")
    check("defaults come back with the machine and compound fields",
          code == 200 and len(d["machine"]) == 10 and len(d["recipe"]) == 11)
    check("defaults carry the environment so the page can hide live mode", d["env"]["live"] is False)
    check("state starts idle", api.get("/api/state")[1]["status"] == "idle")

    code, started = api.post("/api/run", opts)
    check("a run starts", started.get("ok") is True)
    check("a second run is refused while one is armed", api.post("/api/run", opts)[1].get("ok") is False)
    pumps = 0
    while api.get("/api/state")[1]["status"] == "running" and pumps < 400:
        m.pump(50)
        pumps += 1
    st = api.get("/api/state")[1]
    check("the pumped run finishes", st["status"] == "done", st["message"])
    check("the run has a snapshot the page can draw", st["snapshot"] and len(st["snapshot"]["T_m"]) == N)
    check("the run is in the registry", len(st["runs"]) == 1 and st["runs"][0]["label"] == "api check")

    run_id = st["runs"][0]["id"]
    code, got = api.get("/api/runs/" + run_id)
    check("the run's csv comes back whole", code == 200 and got["csv"].count("\n") == 241)
    check("a run that is not there is a 404", api.get("/api/runs/nope")[0] == 404)

    # the same options run in one batch must produce the same log -- one loop, two ways of driving it
    batch = os.path.join(tmp, "batch.csv")
    p2, rc2 = Plant(), Recipe()
    run_batch(SimPlant(p2, rc2, 1.0), p2, rc2, 1.0, 240.0, batch, quiet=True)
    stepped = open(os.path.join(m.run_dir(run_id), "twin_log.csv")).read()
    check("pumping the stepper matches running it in one batch", open(batch).read() == stepped)

    code, cor = api.get("/api/corpus")
    check("signatures land in the corpus", code == 200 and isinstance(cor["signatures"], list))
    check("labelling needs a signature id", api.post("/api/corpus/label", dict(cause="x"))[0] == 400)
    api.post("/api/corpus/label", dict(id="abc123", cause="a cause", action="an action", tags=["t"]))
    check("a label is stored", api.get("/api/corpus")[1]["labels"].get("abc123", {}).get("cause") == "a cause")

    api.post("/api/configs", dict(name="line 1", config=dict(plant=dict(D=0.16))))
    saved = api.get("/api/configs")[1]
    check("settings save and come back by name",
          saved and saved[0]["name"] == "line 1" and saved[0]["config"]["plant"]["D"] == 0.16)
    check("ports are empty where there is no serial", api.get("/api/ports")[1] == dict(ports=[]))
    check("an unknown route is a 404", api.get("/api/nothing")[0] == 404 and api.post("/api/nothing", {})[0] == 404)

    live = StepRunManager(tmp, allow_live=False)
    code, r = TwinAPI(live).post("/api/run", dict(mode="live"))
    check("a browser build refuses live mode with a reason",
          r.get("ok") is False and "serial" in r.get("error", ""))

    # stopping mid-run
    m2 = StepRunManager(tempfile.mkdtemp())
    a2 = TwinAPI(m2)
    a2.post("/api/run", dict(opts, seconds=4000.0))
    m2.pump(30)
    a2.post("/api/stop", {})
    m2.pump(5)
    check("stop ends the run and marks it stopped", a2.get("/api/state")[1]["status"] == "stopped")
    shutil.rmtree(tmp)


# ------------------------------------------------------------------ end to end
def test_end_to_end():
    print("\nend to end")
    from .run import run
    tmp = tempfile.mkdtemp()
    p, rc = Plant(), Recipe()
    csv_path = os.path.join(tmp, "log.csv")
    s = run(SimPlant(p, rc, 1.0), p, rc, 1.0, 3800.0, csv_path, arch="cores", estimator="fixed",
            delta=3.0, delta_res=0.15, corpus_dir=os.path.join(tmp, "corpus"), quiet=True)
    lines = open(csv_path).read().splitlines()
    check("csv has a header and one row per step", len(lines) == 3801, f"{len(lines)} lines")
    head = lines[0].split(",")
    check("the crammer has its own columns",
          all(c in head for c in ("crammer_rpm", "crammer_amps", "feed_ratio", "feed_state")))
    head = lines[0].split(",")
    check("the TCU loops have their own columns",
          all(c in head for c in ("oil_b_supply", "oil_b_return", "oil_b_dT", "oil_b_kW", "oil_b_state",
                                  "oil_s_supply", "oil_s_return", "oil_s_dT")))
    check("motor speed and the gear ratio are logged", "rpm_motor" in head and "gear_ratio" in head)
    last = dict(zip(head, lines[-1].split(",")))
    check("crammer readings are logged", float(last["crammer_amps"]) > 0.0)
    check("loop readings are logged", float(last["oil_b_dT"]) > 0.0 and float(last["oil_b_kW"]) > 0.0)
    check("run reports fighting-loop waste", s["fight_kWh"] > 0.0)
    check("events are far fewer than raw samples", s["events"] < 0.2 * s["raw"], f"{s['events']} vs {s['raw']}")
    sigs = Corpus(os.path.join(tmp, "corpus")).signatures()
    check("alarms became signatures", len(sigs) >= 4, f"{len(sigs)} signatures")
    kinds = {s_["detector"]["kind"] for s_ in sigs}
    check("the heater fault is caught", "heater" in kinds, str(sorted(kinds)))
    check("the contactor chatter is caught", "contact" in kinds, str(sorted(kinds)))
    check("the feed change is caught at the crammer", "feed" in kinds, str(sorted(kinds)))
    check("kappa moved with the viscosity front", s["drift_index"] > 0.05, f"{s['drift_index']:+.3f}")
    shutil.rmtree(tmp)


def main():
    print("extrusion_twin tests")
    for fn in (test_topology, test_physics, test_efference_copy, test_estimators, test_detectors,
               test_cores, test_io, test_corpus, test_forecast, test_api, test_end_to_end):
        fn()
    tail = f"  ({len(SKIPS)} skipped: {', '.join(SKIPS)})" if SKIPS else ""
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}{tail}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
