/*
 * twin-worker.js -- the twin itself, running as Python in a worker thread.
 *
 * The static build has no server behind it, so the console's API is served here instead:
 * Pyodide loads the real `extrusion_twin` package and `extrusion_twin.api.TwinAPI` answers the
 * same routes the local Python server answers. There is no second implementation of the
 * physics, the detectors or the corpus -- this is the package, run in a browser.
 *
 * The one thing that has to differ is who owns the loop. A worker may not block, so the
 * run is driven by `StepRunManager.pump(n)` in slices sized to about a frame's worth of
 * work, and the worker yields between them so `/api/state` keeps answering while a run
 * is going.
 *
 * Runs, saved settings and the corpus live in IDBFS at /data, which is the browser's own
 * storage. Nothing is sent anywhere.
 *
 * This is a module worker: Pyodide 314 dropped classic-worker support, so the runtime is
 * pulled in with a dynamic import() rather than importScripts().
 */
/* eslint-env worker */

let pyodide = null;
let api = null;          // python: json_get / json_post / pump
let numpyLoaded = false;
let pumping = false;
let cfg = { indexURL: "", bundle: "extrusion_twin_bundle.json" };

const post = (msg) => self.postMessage(msg);
const note = (text, detail) => post({ type: "boot", text, detail });

// ------------------------------------------------------------------ bring Python up
async function boot(options) {
  cfg = Object.assign(cfg, options || {});
  note("Fetching the Python runtime", "about 12 MB, cached by the browser after the first visit");
  const base = new URL(cfg.indexURL, self.location.href).href;   // works for a CDN or a vendored copy
  const { loadPyodide } = await import(/* webpackIgnore: true */ base + "pyodide.mjs");
  pyodide = await loadPyodide({ indexURL: base });

  note("Opening local storage");
  pyodide.FS.mkdirTree("/data");
  pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, "/data");
  await syncIn();

  note("Unpacking the twin");
  const bundle = await (await fetch(cfg.bundle, { cache: "no-cache" })).json();
  pyodide.FS.mkdirTree("/lib/extrusion_twin");
  for (const [name, text] of Object.entries(bundle.files)) {
    const path = "/lib/extrusion_twin/" + name;
    const dir = path.slice(0, path.lastIndexOf("/"));
    pyodide.FS.mkdirTree(dir);
    pyodide.FS.writeFile(path, text, { encoding: "utf8" });
  }

  note("Starting the console");
  api = pyodide.runPython(`
import json, sys
sys.path.insert(0, "/lib")
from extrusion_twin.api import StepRunManager, TwinAPI

_manager = StepRunManager("/data", allow_live=False)
_api = TwinAPI(_manager, ports=[], env=dict(
    live=False, kind="browser",
    live_note=("A browser tab cannot open a serial port. Run "
               "python -m extrusion_twin serve on the machine wired to the bus, "
               "or drop a twin_log.csv here to replay one.")))

def _get(path):
    code, obj = _api.get(path)
    return json.dumps([code, obj])

def _post(path, body):
    code, obj = _api.post(path, json.loads(body or "{}"))
    return json.dumps([code, obj])

def _pump(n):
    return json.dumps(_manager.pump(n))

{"get": _get, "post": _post, "pump": _pump}
`).toJs({ dict_converter: Object.fromEntries });

  post({ type: "ready", version: bundle.version, python: pyodide.version });
}

// ------------------------------------------------------------------ IDBFS both ways
const sync = (populate) => new Promise((res, rej) =>
  pyodide.FS.syncfs(populate, (err) => (err ? rej(err) : res())));
const syncIn = () => sync(true).catch(() => {});      // first read; empty storage is not an error
const syncOut = () => sync(false).catch((e) => post({ type: "warn", text: "Could not save to browser storage: " + e }));

// ------------------------------------------------------------------ the run, a slice at a time
async function pumpLoop() {
  if (pumping) return;
  pumping = true;
  let slice = 60;                                     // grows to whatever fits in ~90 ms of work
  try {
    for (;;) {
      const t0 = performance.now();
      const r = JSON.parse(api.pump(slice));
      const dtms = performance.now() - t0;
      if (!r.running) break;
      if (dtms < 60) slice = Math.min(4000, Math.round(slice * 1.6));
      else if (dtms > 140) slice = Math.max(10, Math.round(slice * 0.6));
      await new Promise((res) => setTimeout(res, 0));  // let /api/state through between slices
    }
  } finally {
    pumping = false;
    await syncOut();
    post({ type: "run-finished" });
  }
}

// ------------------------------------------------------------------ requests from the page
const WRITES_TO_DISK = new Set(["/api/configs", "/api/corpus/label", "/api/export"]);

self.onmessage = async (ev) => {
  const m = ev.data || {};
  if (m.type === "boot") {
    try {
      await boot(m.options);
    } catch (err) {
      post({ type: "boot-failed", text: String((err && err.message) || err) });
    }
    return;
  }
  if (m.type !== "request") return;
  const { id, method, path, body } = m;
  try {
    if (!api) throw new Error("the twin is still starting");
    if (method === "POST" && path === "/api/run") {
      const opts = JSON.parse(body || "{}");
      if (opts.estimator === "enkf" && !numpyLoaded) {
        note("Fetching numpy for the ensemble filter", "about 8 MB, once");
        await pyodide.loadPackage("numpy");
        numpyLoaded = true;
        post({ type: "ready-again" });
      }
    }
    const raw = method === "POST" ? api.post(path, body || "{}") : api.get(path);
    const [code, obj] = JSON.parse(raw);
    post({ type: "response", id, code, obj });
    if (method === "POST" && path === "/api/run" && obj && obj.ok) pumpLoop();
    if (method === "POST" && WRITES_TO_DISK.has(path)) syncOut();
  } catch (err) {
    post({ type: "response", id, code: 500, obj: { error: String((err && err.message) || err) } });
  }
};
