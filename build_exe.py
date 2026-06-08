#!/usr/bin/env python3
"""Build applepie.exe from this source tree with Nuitka (onefile).

    python build_exe.py
    python build_exe.py --smoke-test
    python build_exe.py --extra=--lto=yes --jobs=8

About "encryption": Nuitka (OSS) COMPILES the sources to a native binary, so no
.py file ships inside applepie.exe. That is code protection, not encryption.
Real encryption needs Nuitka Commercial; heavier obfuscation needs pyarmor run
over the sources before Nuitka sees them.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPS = ("flask", "requests", "pymobiledevice3", "colorama", "art", "pystray", "PIL")
DATA_DIRS = ("img", "data", "bin", "web")
VERSION = "0.1.0.0"


# ---------------------------------------------------------------- output bits
def step(msg):
    print(f"\n==> {msg}", flush=True)


def ok(msg):
    print(f"    [ok]   {msg}", flush=True)


def warn(msg):
    print(f"    [warn] {msg}", flush=True)


def die(msg, code=1):
    print(f"    [FAIL] {msg}", flush=True)
    sys.exit(code)


# ------------------------------------------------------------------- helpers
def have(python, code):
    """Run a snippet with `python`; return stripped stdout or '' on failure."""
    try:
        r = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip().splitlines()[0].strip() if r.stdout.strip() else ""


def deps_present(python):
    code = "import " + ", ".join(DEPS) + "; import sys; print(sys.version.split()[0])"
    return have(python, code)


def nuitka_version(python):
    return have(python, "import importlib.metadata as m; print(m.version('nuitka'))")


def ensure_nuitka(python, skip_install):
    v = nuitka_version(python)
    if v:
        return ok(f"nuitka  {v}")
    if skip_install:
        die("Nuitka is not installed and --skip-install was given")

    warn("nuitka not installed -> installing (this can take a minute)")
    r = subprocess.run([python, "-m", "pip", "install", "-U", "--disable-pip-version-check", "nuitka"])
    if r.returncode != 0:
        die("pip install nuitka failed")
    v = nuitka_version(python)
    if not v:
        die("nuitka still not importable after install")
    ok(f"nuitka  {v}")


def find_compiler():
    """MSVC cl.exe or MinGW gcc. Nuitka also downloads MinGW on demand."""
    pats = [
        r"C:\Program Files\Microsoft Visual Studio\*\*\VC\Tools\MSVC\*\bin\HostX64\x64\cl.exe",
        r"C:\Program Files (x86)\Microsoft Visual Studio\*\*\VC\Tools\MSVC\*\bin\HostX64\x64\cl.exe",
    ]
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return ok(f"MSVC    {hits[-1]}")
    gcc = shutil.which("gcc")
    if gcc:
        return ok(f"MinGW   {gcc}")
    warn("no C compiler found (no MSVC, no MinGW)")
    warn("install VS Build Tools, or pass --extra=--assume-yes-for-downloads")
    warn("so Nuitka downloads MinGW-w64 for you")


def check_tree():
    for d in DATA_DIRS:
        if not (ROOT / d).is_dir():
            die(f"missing required directory: {d}\\")
    ok(f"tree    {' '.join(d + '/' for d in DATA_DIRS)} present")
    if not (ROOT / "icon.ico").is_file():
        warn("icon.ico not found - building without a custom icon")


def clean(keep_build_dir):
    targets = ["applepie.exe"]
    if not keep_build_dir:
        targets += ["new_applepie.build", "new_applepie.dist", "new_applepie.onefile-build"]
    for t in targets:
        p = ROOT / t
        if p.exists():
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
            ok(f"removed {t}")
    # stale staging data would otherwise be baked into the exe
    staging = ROOT / "data" / "staging"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        ok("removed data\\staging\\ (would otherwise be embedded)")


def build_args(keep_build_dir, extra):
    args = [
        "--onefile",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--output-filename=applepie.exe",
        "--output-dir=.",
        # all four are required together; Nuitka rejects a partial set
        f"--company-name=ApplePie",
        f"--product-name=Apple Pie",
        f"--file-version={VERSION}",
        f"--product-version={VERSION}",
    ]
    for d in DATA_DIRS:
        if d == "bin":
            args.append("--include-raw-dir=bin=bin")
        else:
            args.append(f"--include-data-dir={d}={d}")
    args.append("--include-module=webui")
    args.append("--nofollow-import-to=IPython")
    args.append("--nofollow-import-to=pytest")
    if (ROOT / "icon.ico").is_file():
        args.append("--windows-icon-from-ico=icon.ico")
    if not keep_build_dir:
        args.append("--remove-output")
    args += list(extra)
    args.append("new_applepie.py")
    return args


def verify(exe):
    if not exe.is_file():
        die("applepie.exe was not produced")
    mb = exe.stat().st_size / 1024 / 1024
    ok(f"applepie.exe  {exe.stat().st_size:,} bytes  ({mb:.1f} MB)")

    # The onefile must carry the external tools, or it cannot talk to a phone.
    blob = exe.read_bytes()
    need = [
        b"ideviceinfo.exe",
        b"unifiedlog_iterator.exe",
        b"com.apple.MobileGestalt.plist",
        b"index.html",
    ]
    missing = [n.decode() for n in need if n not in blob]
    if missing:
        warn("missing from the exe payload: " + ", ".join(missing))
        warn("asset lookups will fail at runtime")
    else:
        ok(f"payload  all {len(need)} bundled markers found")


def smoke_test(exe, seconds):
    print(f"    launching for {seconds}s, then stopping it", flush=True)
    p = subprocess.Popen([str(exe)], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.7)
        if p.poll() is not None:
            die(f"exited early with code {p.returncode} - the binary did not boot")
    ok(f"booted and stayed up for {seconds}s")
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    ok("stopped")


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Build applepie.exe with Nuitka.")
    ap.add_argument("--python", default=sys.executable, help="interpreter that has the deps")
    ap.add_argument("--skip-install", action="store_true", help="do not pip install nuitka")
    ap.add_argument("--smoke-test", action="store_true", help="boot the exe briefly to prove it runs")
    ap.add_argument("--smoke-seconds", type=int, default=25)
    ap.add_argument("--keep-build-dir", action="store_true", help="keep .build/.dist dirs")
    ap.add_argument("--extra", action="append", default=[], metavar="FLAG",
                    help="extra Nuitka flag, repeatable (e.g. --extra=--lto=yes)")
    args = ap.parse_args()

    py = args.python
    if not py or not os.path.exists(py) or os.path.getsize(py) == 0:
        die(f"'{py}' is missing or a 0-byte stub - pass --python with a real interpreter")
    if not deps_present(py):
        die(f"'{py}' cannot import {', '.join(DEPS)} - run: {py} -m pip install -r requirements.txt")

    step("Preflight")
    ok(f"python  {py}  (v{deps_present(py)})")
    ensure_nuitka(py, args.skip_install)
    find_compiler()
    check_tree()

    step("Clean previous build")
    clean(args.keep_build_dir)

    step("Build (Nuitka onefile)")
    flags = build_args(args.keep_build_dir, args.extra)
    print("    " + " ".join([py, "-m", "nuitka", *flags]), flush=True)
    print("    (onefile compiles every included module - expect 10-40 min on a first run)\n", flush=True)

    t0 = time.time()
    rc = subprocess.run([py, "-m", "nuitka", *flags], cwd=str(ROOT)).returncode
    if rc != 0:
        die(f"Nuitka failed with exit code {rc}")
    ok(f"build finished in {time.strftime('%H:%M:%S', time.gmtime(time.time() - t0))}")

    step("Verify")
    verify(ROOT / "applepie.exe")

    step("Smoke test")
    if args.smoke_test:
        smoke_test(ROOT / "applepie.exe", args.smoke_seconds)
    else:
        ok("skipped (pass --smoke-test to boot-test the binary)")

    print()
    ok(f"DONE -> {ROOT / 'applepie.exe'}")


if __name__ == "__main__":
    main()
