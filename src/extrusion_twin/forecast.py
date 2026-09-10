"""
forecast.py -- the twin run a phase ahead of the machine, and reconciled when reality arrives.

Every M sweeps the twin takes its corrected state and rolls it forward H seconds with no
corrections at all, holding the exogenous inputs where they are. That rollout is a
*projection under an assumption*, not a prediction: it says where the melt goes **if nothing
changes**. The assumption is recorded with it, because it is what makes the later score
meaningful.

When real time reaches a forecast's target, the forecast is either

  RETIRED   the assumption broke -- somebody moved rpm, changed a setpoint, started a
            different product. The forecast is invalid, not wrong, and scoring it would
            teach the twin to predict operator behaviour, which it cannot do.
  SCORED    the assumption held. The error between what was projected and what the
            corrected twin now says is the model's own mistake, integrated over H seconds.

Three things use the scored errors, and one thing deliberately does not.

  uses      the empirical error bar (percentiles per horizon, per channel), the horizon
            detector bank, and the corpus projection pair.
  does not  the state estimator. A filter residual is an *observation of the state*; a
            forecast error is a *score of the model*. Feeding the second into the first
            gives you two loops with different time constants moving the same kappa and hw,
            and they will fight. This is the same rule the oil-flow work arrived at:
            measured quantities go into the estimator, assumed ones stay in the detectors.

No parameter tuning happens here. The reconciler measures and reports bias; it never
writes it back. That is deliberate for a first pass -- the loop should be watched for a
while before it is given authority over anything.

Why the rollout is possible at all: `mv_expected` already computes the duty each zone
*should* need. In a rollout there is no measured MV to use, because MV is what the
controllers are going to decide, so the efference copy becomes the controller model and
closes on the projected metal temperature rather than the measured one.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Dict, List, Optional

from .model import ADAPTER, BARREL, DIE, N, SCREEN, Inputs, Plant, Recipe, State, mv_expected, step

# channels the forecast is scored on -- the states with enough thermal inertia to be worth
# projecting, plus the two cumulative indices that turn a projection into a clock
CHANNELS = ("T_m_AD", "T_m_barrel", "T_b_barrel", "T_oil_b", "H_index", "drool_index")

# an assumption is "still holding" while every one of these sits inside its tolerance
ASSUMPTIONS = dict(rpm=1.5,           # rpm -- a screw-speed move invalidates the flow term
                   SV=1.0,            # degC on any zone setpoint
                   tcu_b_sv=2.0,      # degC, barrel TCU setpoint
                   tcu_s_sv=2.0)


def _assumption(u: Inputs) -> dict:
    """The exogenous inputs the rollout froze. Anything in here moving retires the forecast."""
    return dict(rpm=u.rpm, SV=list(u.SV), tcu_b_sv=u.tcu_b_sv, tcu_s_sv=u.tcu_s_sv,
                recipe=getattr(u, "recipe_id", None))


def assumption_broke(a: dict, u: Inputs) -> Optional[str]:
    """None while the projection is still about the same machine; otherwise why it is not."""
    if a.get("recipe") != getattr(u, "recipe_id", None):
        return "product changed"
    if abs(a["rpm"] - u.rpm) > ASSUMPTIONS["rpm"]:
        return f"screw speed moved {u.rpm - a['rpm']:+.0f} rpm"
    if abs(a["tcu_b_sv"] - u.tcu_b_sv) > ASSUMPTIONS["tcu_b_sv"]:
        return "barrel TCU setpoint moved"
    if abs(a["tcu_s_sv"] - u.tcu_s_sv) > ASSUMPTIONS["tcu_s_sv"]:
        return "screw TCU setpoint moved"
    worst, zone = 0.0, None
    for i in range(N):
        d = abs(a["SV"][i] - u.SV[i])
        if d > worst:
            worst, zone = d, i
    if worst > ASSUMPTIONS["SV"]:
        from .model import ZONE_NAMES
        return f"{ZONE_NAMES[zone]} setpoint moved {u.SV[zone] - a['SV'][zone]:+.0f} degC"
    return None


def channels(st: State, diag: dict) -> Dict[str, float]:
    """The scored quantities, read off a state -- projected or realised, same function."""
    return {
        "T_m_AD": st.T_m[ADAPTER],
        "T_m_barrel": sum(st.T_m[i] for i in BARREL) / len(BARREL),
        "T_b_barrel": sum(st.T_b[i] for i in BARREL) / len(BARREL),
        "T_oil_b": st.T_oil_b,
        "H_index": diag.get("H_index", 0.0),
        "drool_index": diag.get("drool_index", 0.0),
    }


# ------------------------------------------------------------------ the rollout
def roll(p: Plant, rc: Recipe, st: State, u: Inputs, dt: float, horizon: float,
         tau_ctrl: float = 120.0):
    """Integrate forward `horizon` seconds with no corrections, the controllers modelled by
    the efference copy. Returns (state at the horizon, diagnostics there, the track).

    The track is every step, so the console can draw the projected path rather than only its
    endpoint, and so a forecast can be scored at intermediate horizons as well as at its own."""
    n = max(1, int(round(horizon / dt)))
    cur = st.copy()
    fu = _frozen(u)
    track = []
    diag = {}
    for _ in range(n):
        # the controllers do not see the truth either -- they see the metal, so close on it
        mv, _ = mv_expected(p, cur, fu, tau_ctrl=tau_ctrl)
        fu.MV = mv
        fu.PV = list(cur.T_b)
        cur, diag = step(p, rc, cur, fu, dt)
        track.append(channels(cur, diag))
    return cur, diag, track


def _frozen(u: Inputs) -> Inputs:
    """A copy of the inputs with the exogenous ones held. MV and PV are overwritten each step
    by the efference copy; everything else is what we are assuming stays put."""
    return Inputs(
        PV=list(u.PV), SV=list(u.SV), MV=list(u.MV), rpm=u.rpm, amps=u.amps,
        T_adapter=None, CT=None, tcu_b_sv=u.tcu_b_sv, tcu_s_sv=u.tcu_s_sv,
        T_tower=u.T_tower, T_air=u.T_air, T_feed_jacket=u.T_feed_jacket,
        rpm_motor=u.rpm_motor, crammer_rpm=u.crammer_rpm, crammer_amps=u.crammer_amps)


# ------------------------------------------------------------------ one issued forecast
class Forecast:
    __slots__ = ("issued_at", "target_at", "horizon", "assumption", "at_target", "track", "kappa", "hw")

    def __init__(self, issued_at: float, horizon: float, assumption: dict,
                 at_target: Dict[str, float], track: List[Dict[str, float]], kappa: float, hw: float):
        self.issued_at = issued_at
        self.horizon = horizon
        self.target_at = issued_at + horizon
        self.assumption = assumption
        self.at_target = at_target
        self.track = track
        self.kappa, self.hw = kappa, hw


# ------------------------------------------------------------------ the empirical error bar
class ErrorBar:
    """What the forecast error has actually been at this horizon, per channel.

    This is the only uncertainty estimate in the twin that can see model-form error. The
    ensemble spread cannot: it only knows about the uncertainty the model already represents,
    and the model's own missing physics is by construction outside that. So a band measured
    from realised forecast errors is both cheaper than rolling the ensemble and more honest.
    """

    def __init__(self, keep: int = 400):
        self.keep = keep
        self.err: Dict[str, deque] = {}

    def add(self, ch: str, err: float):
        self.err.setdefault(ch, deque(maxlen=self.keep)).append(err)

    def n(self, ch: str) -> int:
        return len(self.err.get(ch, ()))

    def bias(self, ch: str):
        """Signed mean error over the long window. A model that is *steadily* wrong has a
        non-zero bias and a small spread, and that is a commissioning finding rather than a
        shift alarm -- it says a parameter is off, not that something happened."""
        d = self.err.get(ch)
        if not d or len(d) < 12:
            return None
        return sum(d) / len(d)

    def band(self, ch: str, q: float = 0.90):
        """The spread of past errors **about their own bias**, not about zero.

        Centring matters. Taking the percentile of |error| instead would fold a constant bias
        into the width of the band, so a model that is reliably 2 degC cold would carry a 2 degC
        band and never trip anything -- the two mistakes cancelling to make both invisible.
        Centred, the same case reads as bias -2.0 with a band of 0.2, which is the truth:
        precise, and wrong by a fixed amount.

        None until there is enough to say anything -- an error bar computed from four samples
        is a decoration, not a number."""
        d = self.err.get(ch)
        if not d or len(d) < 12:
            return None
        m = sum(d) / len(d)
        spread = sorted(abs(x - m) for x in d)
        return max(spread[min(len(spread) - 1, int(q * len(spread)))], 1e-6)

    def total(self, ch: str, q: float = 0.90):
        """Bias plus spread: what to actually draw as the plus-or-minus on a projection, since
        an operator reading the number cares about how far off it will be, not about which
        part of that is systematic."""
        b, w = self.bias(ch), self.band(ch, q)
        return None if b is None else abs(b) + w


# ------------------------------------------------------------------ issue, retire, score
class Reconciler:
    """Issues forecasts on a cadence, and settles them when their target time arrives."""

    def __init__(self, p: Plant, rc: Recipe, dt: float, horizon: float = 600.0,
                 every: float = 60.0, keep: int = 400, sigma: float = 3.0, warmup: float = 300.0):
        self.p, self.rc, self.dt = p, rc, dt
        self.horizon, self.every, self.warmup = horizon, every, warmup
        self.sigma = sigma
        self.pending: deque = deque()
        self.bar = ErrorBar(keep)
        self.next_issue = 0.0
        self.last: Optional[dict] = None          # the most recent settlement, for the log
        self.live: Optional[Forecast] = None      # the newest forecast, for the console track
        self.n_issued = self.n_scored = self.n_retired = 0

    # -- issue
    def maybe_issue(self, t: float, st: State, u: Inputs) -> Optional[Forecast]:
        if t < self.warmup or t < self.next_issue:
            return None
        self.next_issue = t + self.every
        end, diag, track = roll(self.p, self.rc, st, u, self.dt, self.horizon)
        f = Forecast(t, self.horizon, _assumption(u), channels(end, diag), track, st.kappa, st.hw)
        self.pending.append(f)
        self.live = f
        self.n_issued += 1
        return f

    # -- retire or score whatever has come due
    def settle(self, t: float, st: State, u: Inputs, diag: dict) -> List[dict]:
        """Called every sweep. Retires forecasts whose assumption broke -- immediately, not at
        their target, because a forecast known to be invalid should stop occupying the buffer
        and should never be scored. Scores the ones that reach their target intact."""
        out = []
        keep = deque()
        now = channels(st, diag)
        for f in self.pending:
            why = assumption_broke(f.assumption, u)
            if why:
                self.n_retired += 1
                out.append(dict(kind="retired", issued_at=f.issued_at, target_at=f.target_at,
                                horizon=f.horizon, reason=why, age=t - f.issued_at))
                continue
            if t + 1e-9 < f.target_at:
                keep.append(f)
                continue
            self.n_scored += 1
            errs = {ch: now[ch] - f.at_target[ch] for ch in CHANNELS}
            for ch, e in errs.items():
                self.bar.add(ch, e)
            rec = dict(kind="scored", issued_at=f.issued_at, target_at=f.target_at,
                       horizon=f.horizon, err=errs,
                       projected=dict(f.at_target), realised=dict(now))
            out.append(rec)
            self.last = rec
        self.pending = keep
        return out

    # -- what the console and the log want
    def state(self) -> dict:
        bands = {ch: self.bar.band(ch) for ch in CHANNELS}
        bias = {ch: self.bar.bias(ch) for ch in CHANNELS}
        total = {ch: self.bar.total(ch) for ch in CHANNELS}
        return dict(horizon=self.horizon, issued=self.n_issued, scored=self.n_scored,
                    retired=self.n_retired, n=self.bar.n("T_m_AD"),
                    band=bands, bias=bias, total=total,
                    # measured and reported. Nothing here is written back into the estimator
                    # or into the plant constants -- see the module docstring.
                    tuning="observed only",
                    ahead=(dict(self.live.at_target) if self.live else None),
                    ahead_at=(self.live.target_at if self.live else None),
                    track=([f["T_m_AD"] for f in self.live.track] if self.live else None),
                    last_err=(dict(self.last["err"]) if self.last else None))


# ------------------------------------------------------------------ the horizon detector
class HorizonWatch:
    """A detector bank on forecast error rather than on filter residual.

    The point of it: a filter residual is one step of model mismatch, which for a small bias
    is buried in noise. A forecast error is the same mismatch integrated over the horizon, so
    a bias that is invisible instantaneously is H/dt times larger here. That is exactly the
    shape of the slow barrel-wide drift the twin is otherwise worst at seeing.

    It fires on *consistency*, not on size: a single large error is a disturbance, several in
    a row with the same sign is the model being wrong about something.

    And it fires on departure from the model's **established bias**, not from zero. A twin
    that has always run 2 degC cold at this horizon is not news every minute; the same twin
    suddenly running 2 degC warm is. The bias window is long (hundreds of settlements) and the
    detector window is short (a few), so a real change shows up in the short one well before
    the long one absorbs it.
    """

    def __init__(self, dt: float, sigma: float = 3.0, run: int = 3, refractory: float = 600.0):
        self.sigma, self.run, self.refractory = sigma, run, refractory
        self.recent: Dict[str, deque] = {}
        self.last_fire: Dict[str, float] = {}

    WHERE = {
        "T_m_AD": ("the melt at the adapter", "downstream of the barrel"),
        "T_m_barrel": ("the melt through the barrel", "the barrel heat balance"),
        "T_b_barrel": ("the barrel metal", "the heaters or the oil loop"),
        "T_oil_b": ("the barrel oil loop", "the TCU or the exchanger"),
        "H_index": ("the stabilizer budget", "heat history along the melt path"),
        "drool_index": ("the lip", "the die end"),
    }

    def update(self, t: float, settled: List[dict], bar: ErrorBar) -> List[dict]:
        alarms = []
        for rec in settled:
            if rec["kind"] != "scored":
                continue
            for ch, e in rec["err"].items():
                d = self.recent.setdefault(ch, deque(maxlen=self.run))
                d.append(e)
                band, bias = bar.band(ch), bar.bias(ch)
                if band is None or len(d) < self.run:
                    continue
                if t - self.last_fire.get(ch, -1e9) < self.refractory:
                    continue
                # measured from where this twin normally sits, not from zero
                dev = [x - bias for x in d]
                if not all(abs(x) > band * self.sigma for x in dev):
                    continue
                if not (all(x > 0 for x in dev) or all(x < 0 for x in dev)):
                    continue
                self.last_fire[ch] = t
                mean = sum(dev) / len(dev)
                what, where = self.WHERE.get(ch, (ch, "the model"))
                hot = mean > 0
                unit = "" if ch in ("H_index", "drool_index") else " degC"
                alarms.append(dict(
                    kind="horizon", bank="forecast", template="horizon", zone=None, zones=[],
                    value=round(mean, 3), direction=0.0,
                    verdict=f"forecast bias ({where})",
                    text=(f"[horizon] {ch} {mean:+.2f}{unit} off its usual "
                          f"{bias:+.2f}{unit} bias, past a {band:.2f}{unit} band "
                          f"{len(d)} settlements running: reality is "
                          f"{'above' if hot else 'below'} the projection for {what}")))
        return alarms
