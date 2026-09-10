"""
corpus.py -- the bridge between the twin's alarms and the CIAER+ corpus.

Every alarm becomes a SIGNATURE: the residual vectors, the twin state, the inputs, and three
confidence registers at the moment it fired. Signatures are appended to signatures.jsonl.

`label` walks the operator through the Socratic chain for each unlabeled signature --
Cause, Intuition (what you noticed before you knew), Action, Effect, Result, Shadow actions
(what else you could have done) -- and appends to labels.jsonl. IAR is left for your scorer.

`Matcher` turns a signature into a feature vector and finds the nearest labeled cases by
cosine similarity, so a new alarm arrives with "closest cases: ..." attached -- the
diagnose register, fed by tacit knowledge instead of a rulebook.

`export_ciaer` merges signature + label in CIAER+ field order (cause, intuition, action,
effect, result, shadow_actions, iar) for the Pydantic module to ingest.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from typing import List, Optional

from .model import N, ZONE_NAMES

VERDICT_CLASSES = ["downstream", "upstream", "simultaneous", "single-zone", "element", "reinforcing",
                   "recipe", "chatter", "recovered", "other"]
TEMPLATE_CLASSES = ["barrel", "screen_adapter", "die", "gradient", "die_skew", "local", "heater_vs_motor",
                    "kappa", "ct", "chatter", "other"]


def verdict_class(v: str) -> str:
    v = (v or "").lower()
    for k in VERDICT_CLASSES:
        if k in v:
            return k
    return "other"


def make_signature(alarm: dict, t: float, run_id: str, u, st, diag: dict, corr: dict, e: List[float],
                   theta: float = 0.06, forecast: Optional[dict] = None) -> dict:
    live = u.live or {}
    n_live = sum(1 for v in live.values() if v in ("bus", "analog", "sim"))
    meas_conf = n_live / max(len(live), 1)
    spread = corr.get("spread") or [0.0] * N
    model_conf = 1.0 / (1.0 + sum(spread) / N)
    det_conf = min(abs(alarm.get("value", 0.0)) / theta, 5.0) / 5.0 if alarm.get("kind") in ("template", "local") else 0.6
    return dict(
        id=str(uuid.uuid4())[:8], run_id=run_id, t=t, iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
        detector=dict(kind=alarm.get("kind"), bank=alarm.get("bank"), template=alarm.get("template"),
                      zone=alarm.get("zone"), zones=alarm.get("zones", []), value=alarm.get("value"),
                      direction=alarm.get("direction", 0.0), verdict=alarm.get("verdict"), text=alarm.get("text")),
        residuals=dict(e=[round(x, 4) for x in e], r_q=[round(x, 4) for x in corr["r_q"]],
                       r_b=[round(x, 3) for x in corr["r_b"]], r_a=round(corr["r_a"], 3), r_k=round(corr["r_k"], 4),
                       drift_index=round(corr["drift_index"], 4)),
        state=dict(T_m=[round(x, 1) for x in st.T_m], T_b=[round(x, 1) for x in st.T_b], kappa=round(st.kappa, 4),
                   hw=round(st.hw, 4), T_oil_b=round(st.T_oil_b, 1), T_oil_s=round(st.T_oil_s, 1), T_s=round(st.T_s, 1),
                   H_index=round(diag.get("H_index", 0.0), 1), drool_index=round(diag.get("drool_index", 0.0), 3),
                   fight_kW=round(diag.get("fight_W", 0.0) / 1e3, 2), T_lip=round(diag.get("T_lip", 0.0), 1)),
        inputs=dict(rpm=u.rpm, amps=round(u.amps, 1), T_adapter=u.T_adapter, P_adapter=u.P_adapter,
                    SV=list(u.SV), MV=[round(x, 3) for x in u.MV], T_tower=u.T_tower, T_air=u.T_air,
                    T_feed_jacket=u.T_feed_jacket, contacts=dict(u.contacts)),
        confidence=dict(measurement=round(meas_conf, 2), model=round(model_conf, 2), detector=round(det_conf, 2)),
        # CIAER+ asks an operator what they expected to happen and then scores it. Where the
        # phase-ahead layer is running the twin answers the same two questions about itself, in
        # the same fields, so machine and human projections sit side by side in one corpus and
        # the interesting rows are the ones where they disagree.
        projection=(None if not forecast else dict(
            horizon=forecast.get("horizon"),
            expected=forecast.get("ahead"), expected_at=forecast.get("ahead_at"),
            band=forecast.get("total"), bias=forecast.get("bias"),
            last_error=forecast.get("last_err"), n=forecast.get("n"),
            scored=forecast.get("scored"), retired=forecast.get("retired"),
            tuning=forecast.get("tuning"))),
        label=None)


class Corpus:
    def __init__(self, directory: str, run_id: Optional[str] = None):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.sig_path = os.path.join(directory, "signatures.jsonl")
        self.lab_path = os.path.join(directory, "labels.jsonl")
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.matcher = Matcher(self)

    # -- storage
    def signatures(self) -> List[dict]:
        return _read_jsonl(self.sig_path)

    def labels(self) -> dict:
        return {d["id"]: d for d in _read_jsonl(self.lab_path)}

    def record(self, alarm: dict, t: float, u, st, diag, corr, e, forecast=None) -> dict:
        sig = make_signature(alarm, t, self.run_id, u, st, diag, corr, e, forecast=forecast)
        with open(self.sig_path, "a") as f:
            f.write(json.dumps(sig) + "\n")
        return sig

    def suggest(self, sig: dict, k: int = 3) -> List[tuple]:
        return self.matcher.nearest(sig, k)

    # -- export in CIAER+ field order
    def export_ciaer(self, path: str) -> int:
        labels = self.labels()
        n = 0
        with open(path, "w") as f:
            for sig in self.signatures():
                lab = labels.get(sig["id"])
                if not lab:
                    continue
                rec = dict(id=sig["id"], when=sig["iso"], run_id=sig["run_id"],
                           cause=lab.get("cause"), intuition=lab.get("intuition"), action=lab.get("action"),
                           effect=lab.get("effect"), result=lab.get("result"),
                           shadow_actions=lab.get("shadow_actions", []), iar=lab.get("iar"),
                           operator_confidence=lab.get("confidence"), tags=lab.get("tags", []),
                           trigger=sig["detector"], confidence_registers=sig["confidence"],
                           residual_signature=sig["residuals"], state_snapshot=sig["state"], inputs=sig["inputs"])
                f.write(json.dumps(rec) + "\n")
                n += 1
        return n


class Matcher:
    """feature vector = normalized residual vectors + verdict / template one-hots + direction"""

    def __init__(self, corpus: Corpus):
        self.corpus = corpus

    @staticmethod
    def features(sig: dict) -> List[float]:
        e, rq = sig["residuals"]["e"], sig["residuals"]["r_q"]
        ne = math.sqrt(sum(x * x for x in e)) or 1.0
        nq = math.sqrt(sum(x * x for x in rq)) or 1.0
        f = [x / ne for x in e] + [x / nq for x in rq]
        vc = verdict_class(sig["detector"].get("verdict"))
        f += [1.0 if vc == k else 0.0 for k in VERDICT_CLASSES]
        tc = sig["detector"].get("template") or "other"
        f += [1.0 if tc == k else 0.0 for k in TEMPLATE_CLASSES]
        f += [sig["detector"].get("direction", 0.0) or 0.0, 1.0 if (sig["detector"].get("value") or 0.0) > 0 else -1.0]
        return f

    def nearest(self, sig: dict, k: int = 3) -> List[tuple]:
        labels = self.corpus.labels()
        if not labels:
            return []
        fx = self.features(sig)
        nx = math.sqrt(sum(x * x for x in fx)) or 1.0
        out = []
        for other in self.corpus.signatures():
            lab = labels.get(other["id"])
            if not lab or other["id"] == sig["id"]:
                continue
            fy = self.features(other)
            ny = math.sqrt(sum(y * y for y in fy)) or 1.0
            cos = sum(a * b for a, b in zip(fx, fy)) / (nx * ny)
            out.append((round(cos, 3), other["id"], lab.get("cause"), lab.get("action")))
        out.sort(reverse=True)
        return out[:k]


# ------------------------------------------------------------------ CLI pieces
SOCRATIC = [
    ("cause", "Cause -- what was actually going on? (one line)"),
    ("intuition", "Intuition -- what did you notice or feel before you knew? (sound, smell, gauge, sheet, gut)"),
    ("action", "Action -- what did you do?"),
    ("effect", "Effect -- what happened right after?"),
    ("result", "Result -- how did it end (scrap, downtime, recovered, still open)?"),
    ("shadow_actions", "Shadow actions -- what else could you have done? (separate with ;)"),
    ("confidence", "How sure are you about the cause, 1-5?"),
    ("tags", "Tags (separate with ,) -- e.g. feed, heater, tower, recipe, screen-change"),
]


def label_cli(directory: str, only_run: Optional[str] = None, inp=None, out=None):
    inp, out = inp or sys.stdin, out or sys.stdout
    corpus = Corpus(directory)
    labels = corpus.labels()
    todo = [s for s in corpus.signatures() if s["id"] not in labels and (only_run is None or s["run_id"] == only_run)]
    out.write(f"{len(todo)} unlabeled signature(s) in {directory}\n")
    for sig in todo:
        d = sig["detector"]
        out.write(f"\n--- {sig['id']}  run {sig['run_id']}  t={sig['t']:.0f}s\n    {d['text']}\n")
        top = corpus.suggest(sig)
        if top:
            out.write("    closest labeled cases: " + "; ".join(f"{c:.2f} {cid} '{cause}'" for c, cid, cause, _ in top) + "\n")
        out.write("    (enter 's' to skip, 'q' to quit)\n")
        rec = dict(id=sig["id"], labeled_at=time.strftime("%Y-%m-%dT%H:%M:%S"), iar=None)
        quit_all = False
        for key, prompt in SOCRATIC:
            out.write(f"  {prompt}\n  > ")
            out.flush()
            line = inp.readline()
            if not line:
                quit_all = True
                break
            line = line.strip()
            if line == "q":
                quit_all = True
                break
            if line == "s":
                rec = None
                break
            if key == "shadow_actions":
                rec[key] = [x.strip() for x in line.split(";") if x.strip()]
            elif key == "tags":
                rec[key] = [x.strip() for x in line.split(",") if x.strip()]
            elif key == "confidence":
                try:
                    rec[key] = int(line)
                except ValueError:
                    rec[key] = None
            else:
                rec[key] = line
        if rec and not quit_all and "cause" in rec:
            with open(corpus.lab_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            out.write(f"  saved label for {sig['id']}\n")
        if quit_all:
            break


def match_cli(directory: str, out=None):
    out = out or sys.stdout
    corpus = Corpus(directory)
    labels = corpus.labels()
    for sig in corpus.signatures():
        if sig["id"] in labels:
            continue
        top = corpus.suggest(sig)
        out.write(f"{sig['id']} t={sig['t']:.0f}s {sig['detector']['text']}\n")
        for c, cid, cause, action in top:
            out.write(f"    {c:.2f}  {cid}  cause: {cause}  |  action: {action}\n")
        if not top:
            out.write("    (no labeled cases yet)\n")


def _read_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out
