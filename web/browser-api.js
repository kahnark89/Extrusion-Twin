/*
 * browser-api.js -- makes the console's `fetch("/api/…")` calls land in a worker instead of a server.
 *
 * dashboard.html is unmodified between the local build and this one: it asks for /api/state,
 * /api/run and the rest exactly as it does when python -m extrusion_twin serve is behind it.
 * This file intercepts those calls and answers them from `twin-worker.js`, which is running
 * the actual Python package. Everything else on the page -- the plots, the scrubber, the
 * CSV replay, the labelling -- is the same code doing the same thing.
 *
 * Anything that is not /api/… goes to the network untouched.
 */
(function () {
  "use strict";

  const CONFIG = Object.assign({
    indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.6/full/",
    worker: "twin-worker.js",
    bundle: "extrusion_twin_bundle.json"
  }, window.EXTRUSION_TWIN_WEB || {});

  const pending = new Map();
  let seq = 0;
  let ready = false;
  let bootError = null;
  const waiters = [];                                  // resolved once Python is up

  // ---------------------------------------------------------------- the "starting up" panel
  const boot = document.createElement("div");
  boot.id = "twin-boot";
  boot.innerHTML =
    '<div class="twin-boot-card">' +
    '<div class="twin-boot-spin" aria-hidden="true"></div>' +
    '<b>Starting the twin</b>' +
    '<p id="twin-boot-text">Fetching the Python runtime</p>' +
    '<p class="twin-boot-sub" id="twin-boot-sub">The twin runs in this tab. Nothing is uploaded, ' +
    'and runs you save stay in this browser.</p>' +
    '<p class="twin-boot-sub">Already have a <code>twin_log.csv</code>? Drop it anywhere on this page ' +
    'to replay it without waiting.</p>' +
    "</div>";
  const style = document.createElement("style");
  style.textContent =
    "#twin-boot{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;" +
    "background:var(--paper,#f4f5f3);padding:24px}" +
    "#twin-boot[hidden]{display:none}" +
    ".twin-boot-card{max-width:34em;text-align:center;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;color:#12171a}" +
    ".twin-boot-card b{display:block;font-size:19px;font-weight:640;letter-spacing:-.02em;margin-bottom:6px}" +
    ".twin-boot-card p{margin:0 0 8px;color:#6e7a79}" +
    ".twin-boot-card .twin-boot-sub{font-size:12.5px}" +
    ".twin-boot-card code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;" +
    "background:#fff;border:1px solid #e2e6e4;border-radius:5px;padding:1px 5px}" +
    ".twin-boot-spin{width:26px;height:26px;margin:0 auto 16px;border-radius:50%;" +
    "border:2.5px solid #cfd6d3;border-top-color:#0f4c46;animation:twin-spin .9s linear infinite}" +
    "@keyframes twin-spin{to{transform:rotate(1turn)}}" +
    "@media(prefers-reduced-motion:reduce){.twin-boot-spin{animation:none}}" +
    ".twin-act{display:inline-block;margin-top:6px;border-radius:9px;background:#0f4c46;color:#fff;" +
    "font:inherit;font-size:14px;font-weight:560;padding:10px 18px;cursor:pointer}";
  document.head.appendChild(style);
  const mount = () => document.body.appendChild(boot);
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);

  const say = (text, detail) => {
    const a = document.getElementById("twin-boot-text");
    const b = document.getElementById("twin-boot-sub");
    if (a) a.textContent = text;
    if (b && detail) b.textContent = detail;
  };
  const failed = (text) => {
    boot.innerHTML =
      '<div class="twin-boot-card"><b>The twin could not start</b>' +
      "<p>" + String(text).replace(/[<&]/g, "") + "</p>" +
      '<p class="twin-boot-sub">This build needs to fetch a Python runtime the first time it is opened, ' +
      "so it needs a network connection on that first visit. You can still drop a " +
      "<code>twin_log.csv</code> on this page to replay a run.</p>" +
      '<p><label class="twin-act" tabindex="0">Open a twin_log.csv' +
      '<input type="file" accept=".csv" id="twin-boot-csv" hidden></label></p></div>';
    // replaying a CSV needs no Python at all, so offer that rather than a dead page
    const picker = document.getElementById("twin-boot-csv");
    if (picker) picker.addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      boot.hidden = true;
      const real = document.getElementById("csvfile");
      if (real) {                                      // hand it to the page's own reader
        const dt = new DataTransfer();
        dt.items.add(f);
        real.files = dt.files;
        real.dispatchEvent(new Event("change"));
      }
    });
  };

  // ---------------------------------------------------------------- the worker
  const worker = new Worker(CONFIG.worker, { type: "module" });   // pyodide 314 needs a module worker
  worker.onmessage = (ev) => {
    const m = ev.data || {};
    if (m.type === "boot") return say(m.text, m.detail);
    if (m.type === "ready" || m.type === "ready-again") {
      ready = true;
      boot.hidden = true;
      while (waiters.length) waiters.shift()();
      return;
    }
    if (m.type === "boot-failed") { bootError = m.text; return failed(m.text); }
    if (m.type === "warn") { console.warn("[extrusion_twin]", m.text); return; }
    if (m.type === "response") {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      p(new Response(JSON.stringify(m.obj), {
        status: m.code, headers: { "Content-Type": "application/json" }
      }));
    }
  };
  worker.onerror = (e) => failed(e.message || "the worker stopped");
  worker.postMessage({ type: "boot", options: { indexURL: CONFIG.indexURL, bundle: CONFIG.bundle } });

  const whenReady = () => ready ? Promise.resolve()
    : bootError ? Promise.reject(new Error(bootError))
    : new Promise((res) => waiters.push(res));

  const ask = (method, path, body) => new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    worker.postMessage({ type: "request", id, method, path, body });
  });

  // ---------------------------------------------------------------- the shim
  const IDLE = { status: "idle", message: "", run_id: null, options: {}, snapshot: null, runs: [] };
  const native = window.fetch.bind(window);

  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    if (!path.startsWith("/api/")) return native(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();

    // Before Python is up: keep answering /api/state so the page shows its own shell rather
    // than the "console is not running" panel, and make everything else wait.
    if (!ready) {
      if (method === "GET" && path === "/api/state") {
        return Promise.resolve(new Response(JSON.stringify(IDLE),
          { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return whenReady().then(() => ask(method, path, init && init.body))
        .catch((e) => new Response(JSON.stringify({ error: String(e.message || e) }),
          { status: 503, headers: { "Content-Type": "application/json" } }));
    }
    return ask(method, path, init && init.body);
  };
})();
