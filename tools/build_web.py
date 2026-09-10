#!/usr/bin/env python3
"""
build_web.py -- assemble the static console into dist/.

    python3 tools/build_web.py [--out dist] [--pyodide URL]

There is no bundler and no node step. The build copies four things:

  index.html                 dashboard.html with one extra <script> tag
  browser-api.js             the fetch shim
  twin-worker.js             the worker that runs Pyodide
  extrusion_twin_bundle.json    every .py file of the package, as text

The worker writes those .py files straight into Pyodide's filesystem and imports them, so
the page runs the same package a local install runs. Nothing is transpiled, nothing is
vendored twice, and `python3 tools/build_web.py` is the whole Netlify build command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "src", "extrusion_twin")
WEB = os.path.join(ROOT, "web")

# server.py is the local HTTP transport and tests/validate are developer tools -- the browser
# build needs none of them, and leaving them out keeps the bundle to what actually runs.
SKIP = {"server.py", "tests.py", "validate.py"}


def version() -> str:
    src = open(os.path.join(PKG, "__init__.py")).read()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else "0"


def bundle() -> dict:
    files = {}
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and name not in SKIP:
            with open(os.path.join(PKG, name), encoding="utf-8") as f:
                files[name] = f.read()
    if "api.py" not in files or "model.py" not in files:
        sys.exit("build: the package is missing modules the console needs")
    return files



# Pyodide's own files, when the console is to be served from a network that cannot reach a CDN.
# The runtime is four files; each extra package is one wheel named by the lockfile.
PYODIDE_CORE = ("pyodide.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")
# the emscripten glue has been called both of these across releases, and newer builds also
# ship an ES-module entry point; take whichever ones the release actually has
PYODIDE_MAYBE = ("pyodide.asm.mjs", "pyodide.asm.js", "pyodide.mjs", "pyodide.d.ts", "ffi.d.ts")


def vendor_pyodide(out: str, base: str, packages=("numpy",)) -> str:
    """Copy the runtime next to the page and return the URL the worker should use for it."""
    dest = os.path.join(out, "pyodide")
    os.makedirs(dest, exist_ok=True)

    def grab(name, required=True):
        target = os.path.join(dest, name)
        if os.path.exists(target) and os.path.getsize(target) > 0:
            print(f"  have     {name}")
            return True
        try:
            with urllib.request.urlopen(base + name, timeout=600) as r, open(target, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as exc:
            if os.path.exists(target):
                os.remove(target)
            if required:
                sys.exit(f"build: could not fetch {base + name}: {exc}")
            print(f"  skipped  {name}  (not in this release)")
            return False
        print(f"  fetched  {name}  ({os.path.getsize(target):,} bytes)")
        return True

    for name in PYODIDE_CORE:
        grab(name)
    got_glue = [n for n in PYODIDE_MAYBE if grab(n, required=False)]
    if not any(n.startswith("pyodide.asm.") for n in got_glue):
        sys.exit("build: this pyodide release has no pyodide.asm.* glue where expected")
    lock = json.load(open(os.path.join(dest, "pyodide-lock.json")))
    wanted, seen = list(packages), set()
    while wanted:                                       # wheels plus whatever they depend on
        name = wanted.pop()
        if name in seen:
            continue
        seen.add(name)
        entry = lock["packages"].get(name)
        if not entry:
            sys.exit(f"build: pyodide has no package called {name!r}")
        grab(entry["file_name"])
        wanted.extend(entry.get("depends", []))
    return "pyodide/"


def build(out: str, pyodide_url: str) -> None:
    os.makedirs(out, exist_ok=True)

    files = bundle()
    payload = dict(version=version(), files=files)
    blob = json.dumps(payload, separators=(",", ":"))
    with open(os.path.join(out, "extrusion_twin_bundle.json"), "w", encoding="utf-8") as f:
        f.write(blob)

    # a hash in the query string so a redeploy is never served a stale worker or bundle
    tag = hashlib.sha256((blob + open(os.path.join(WEB, "twin-worker.js")).read()
                          + open(os.path.join(WEB, "browser-api.js")).read()).encode()).hexdigest()[:10]

    for name in ("browser-api.js", "twin-worker.js"):
        shutil.copyfile(os.path.join(WEB, name), os.path.join(out, name))
    for extra in ("robots.txt", "_headers", "_redirects"):
        src = os.path.join(WEB, extra)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(out, extra))

    html = open(os.path.join(PKG, "dashboard.html"), encoding="utf-8").read()
    inject = (
        "<script>window.EXTRUSION_TWIN_WEB={indexURL:%s,worker:%s,bundle:%s};</script>\n"
        '<script src="browser-api.js?v=%s"></script>\n'
    ) % (json.dumps(pyodide_url), json.dumps("twin-worker.js?v=" + tag),
         json.dumps("extrusion_twin_bundle.json?v=" + tag), tag)
    if "<script>" not in html:
        sys.exit("build: dashboard.html has no script to sit in front of")
    # the shim has to install its fetch override before the page's own script runs
    html = html.replace("<script>", inject + "<script>", 1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    total = sum(len(v) for v in files.values())
    print(f"built {out}/")
    print(f"  index.html                {len(html):>9,} bytes")
    print(f"  browser-api.js            {os.path.getsize(os.path.join(out, 'browser-api.js')):>9,} bytes")
    print(f"  twin-worker.js            {os.path.getsize(os.path.join(out, 'twin-worker.js')):>9,} bytes")
    print(f"  extrusion_twin_bundle.json   {len(blob):>9,} bytes  ({len(files)} modules, {total:,} bytes of Python)")
    print(f"  pyodide from              {pyodide_url}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="build the static console")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--pyodide", default="https://cdn.jsdelivr.net/pyodide/v314.0.6/full/",
                    help="where the browser fetches Pyodide from; point it at a local copy to self-host")
    ap.add_argument("--vendor-pyodide", action="store_true",
                    help="download Pyodide and numpy into the build, so the page needs no CDN at all")
    a = ap.parse_args(argv)
    url = a.pyodide if a.pyodide.endswith("/") else a.pyodide + "/"
    if a.vendor_pyodide:
        os.makedirs(a.out, exist_ok=True)
        print("vendoring pyodide:")
        url = vendor_pyodide(a.out, url)
    build(a.out, url)


if __name__ == "__main__":
    main()
