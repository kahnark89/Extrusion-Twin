#!/usr/bin/env python3
"""
build_demo.py -- a single self-contained HTML file showing a finished run.

    python3 tools/build_demo.py [--out demo.html] [--seconds 4200] [--horizon 300]

Why this exists: the hosted console needs Pyodide, which needs a real web host. A recorded
run needs nothing -- one file, opened from disk, emailed, or published anywhere. It is the
shortest path between "the twin works" and somebody seeing that it does.

It is not a mock. The run is produced here by the real package, and the page is
`dashboard.html` unmodified: the same plots, the same scrubber, the same plain-language
alarms, the same corpus screen. What replaces the server is a small stub that answers the
console's own API routes out of the recorded payload, so nothing on the page knows the
difference. Labels typed on the Cases screen are kept in the browser rather than dropped.

The one thing it cannot do is start a new run, and it says so when asked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from extrusion_twin.api import defaults                      # noqa: E402
from extrusion_twin.io_bus import SimPlant                   # noqa: E402
from extrusion_twin.model import Plant, Recipe               # noqa: E402
from extrusion_twin.run import TwinRun                       # noqa: E402

PAGE = os.path.join(ROOT, "src", "extrusion_twin", "dashboard.html")


def record(seconds: float, dt: float, horizon: float, fc_every: float, arch: str,
           estimator: str, ne: int, workdir: str) -> dict:
    p, rc = Plant(), Recipe()
    csv_path = os.path.join(workdir, "twin_log.csv")
    corpus = os.path.join(workdir, "corpus")
    # signatures.jsonl is append-only by design, so a rebuild would otherwise stack this run's
    # alarms on top of the last one's and the Cases screen would show each of them twice
    shutil.rmtree(corpus, ignore_errors=True)
    tr = TwinRun(SimPlant(p, rc, dt), p, rc, dt, seconds, csv_path, arch=arch, estimator=estimator,
                 ne=ne, delta=0.3, delta_res=0.02, corpus_dir=corpus, quiet=True, run_id="demo",
                 horizon=horizon, fc_every=fc_every)
    while tr.step():
        pass
    summary = tr.finish()
    summary.update(id="demo", when=time.strftime("%Y-%m-%dT%H:%M:%S"), mode="sim",
                   label=f"simulated run, {seconds / 60:.0f} minutes, six injected faults",
                   options=dict(mode="sim", seconds=seconds, dt=dt, arch=arch,
                                estimator=estimator, ne=ne, horizon=horizon, fc_every=fc_every))
    sig_path = os.path.join(corpus, "signatures.jsonl")
    sigs = [json.loads(l) for l in open(sig_path)] if os.path.exists(sig_path) else []
    return dict(
        defaults=defaults(dict(live=False, kind="recording",
                               live_note="This page is a recording. Runs happen in the live console.")),
        summary=summary, signatures=sigs, csv=open(csv_path).read())


STUB = """
<script>
/* The console's own API, answered from a recording instead of from a server. Every route the
   page asks for is here, so the page itself is unmodified and unaware. */
(function(){
  "use strict";
  const P = window.__RUN__;
  const KEY = "extrusion_twin_demo_labels";
  const labels = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch(e){ return {}; } };
  const reply = (obj, code) => new Response(JSON.stringify(obj),
    {status: code || 200, headers: {"Content-Type": "application/json"}});
  const native = window.fetch.bind(window);

  window.fetch = function(input, init){
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const path = url.replace(/^https?:\\/\\/[^/]+/, "").split("?")[0];
    if (!path.startsWith("/api/")) return native(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();

    if (path === "/api/state") return Promise.resolve(reply({
      status: "done", message: P.summary.seconds.toFixed(0) + " s of line time, "
        + P.summary.events + " events",
      run_id: "demo", options: P.summary.options, snapshot: null, runs: [P.summary]}));
    if (path === "/api/defaults") return Promise.resolve(reply(P.defaults));
    if (path === "/api/configs") return Promise.resolve(reply([]));
    if (path === "/api/ports")   return Promise.resolve(reply({ports: []}));
    if (path === "/api/corpus")  return Promise.resolve(reply({signatures: P.signatures, labels: labels()}));
    if (path === "/api/runs/demo") return Promise.resolve(reply({summary: P.summary, csv: P.csv}));

    if (method === "POST" && path === "/api/corpus/label"){
      const body = JSON.parse((init && init.body) || "{}");
      const all = labels();
      all[body.id] = body;
      try { localStorage.setItem(KEY, JSON.stringify(all)); } catch(e){}
      return Promise.resolve(reply({ok: true}));
    }
    if (method === "POST" && path === "/api/run") return Promise.resolve(reply({ok: false,
      error: "This page is a recording of a finished run. To start new ones, run the console: "
           + "extrusion-twin serve"}));
    if (method === "POST" && path === "/api/stop") return Promise.resolve(reply({ok: true}));
    if (method === "POST" && path === "/api/configs") return Promise.resolve(reply({ok: true, name: "demo"}));
    return Promise.resolve(reply({error: "not found"}, 404));
  };

  /* Open the recording through the page's own file path, so the replay is set up exactly as
     it would be for a log dropped on the console. */
  function load(){
    const el = document.getElementById("csvfile");
    if (!el) return setTimeout(load, 60);
    const dt = new DataTransfer();
    dt.items.add(new File([P.csv], "twin_log.csv", {type: "text/csv"}));
    el.files = dt.files;
    el.dispatchEvent(new Event("change"));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => setTimeout(load, 400));
  else setTimeout(load, 400);
})();
</script>
"""

BANNER = """
<section class="block" id="recnote" style="border-left:3px solid var(--pine);margin:18px 0">
  <h2>A recorded run, not a live one</h2>
  <h3>Everything on this page came out of the twin. Nothing is drawn from made-up numbers.</h3>
  <p class="note" style="max-width:70ch;margin:0">
    Seventy minutes of simulated line time against a stand-in plant that has its own constants and
    six faults built into it, replayed here at whatever speed you scrub. <b>Watch</b> is the run
    itself &mdash; drag the scrubber or press Play. <b>Cases</b> holds every alarm it raised, and
    anything you write there is kept in this browser. <b>Set&nbsp;up</b> shows the real controls with
    the real defaults; starting a run needs the console, which is
    <code>extrusion-twin serve</code>.
  </p>
</section>
"""


def build(out: str, payload: dict) -> None:
    html = open(PAGE, encoding="utf-8").read()
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
    title = re.search(r"<title>(.*?)</title>", head, re.S).group(1)
    style = re.search(r"<style>.*?</style>", head, re.S).group(0)
    icon = re.search(r'<link rel="icon"[^>]*>', head)

    body = body.replace('<div id="view-watch" hidden>', '<div id="view-watch" hidden>' + BANNER, 1)

    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    parts = [f"<title>{title}</title>", icon.group(0) if icon else "", style,
             f"<script>window.__RUN__={blob};</script>", STUB, body]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"wrote {out}  ({os.path.getsize(out) / 1e6:.2f} MB)")
    print(f"  {payload['summary']['seconds']:.0f} s of line time, "
          f"{len(payload['summary']['alarms'])} alarms, {len(payload['signatures'])} signatures")


def main(argv=None):
    ap = argparse.ArgumentParser(description="build a single-file recording of a run")
    ap.add_argument("--out", default=os.path.join(ROOT, "demo.html"))
    ap.add_argument("--seconds", type=float, default=4200.0)
    ap.add_argument("--dt", type=float, default=2.0, help="2 s halves the file for the same line time")
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--forecast-every", dest="fc_every", type=float, default=30.0)
    ap.add_argument("--arch", default="cores", choices=["mono", "cores"])
    ap.add_argument("--estimator", default="enkf", choices=["fixed", "enkf"])
    ap.add_argument("--ne", type=int, default=16)
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args(argv)
    work = a.workdir or os.path.join(ROOT, ".demo_build")
    os.makedirs(work, exist_ok=True)
    build(a.out, record(a.seconds, a.dt, a.horizon, a.fc_every, a.arch, a.estimator, a.ne, work))


if __name__ == "__main__":
    main()
