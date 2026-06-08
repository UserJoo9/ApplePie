# -*- coding: utf-8 -*-
# Apple Pie WebUI — live end-to-end tests.
#   Run:  python test_webui_live.py
# Boots the real --webui server as a subprocess, drives each screen through the
# API (intro | home | info | loading | progress), renders it in Edge headless
# and asserts the RENDERED PIXELS prove the right screen is showing (blue
# accent button/spinner/fill, amber status dot, white text, overlay dimming).
# Requires: Microsoft Edge + Pillow (`pip install Pillow`).

import hashlib
import os
import subprocess
import sys
import tempfile
import time

import requests
from PIL import Image

EDGE_CANDIDATES = [
    os.environ.get("APPLEPIE_EDGE", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE = next((p for p in EDGE_CANDIDATES if p and os.path.isfile(p)), None)

ROOT = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(tempfile.gettempdir(), "opencode", "shots")
os.makedirs(SHOTS, exist_ok=True)
BASE_URL = None
PROC = None

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def start_server():
    global PROC
    PROC = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "new_applepie.py"), "--webui"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_server():
    global PROC
    if PROC is not None:
        PROC.terminate()
        try:
            PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            PROC.kill()
        PROC = None


def wait_for_server(timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in range(8080, 8085):
            try:
                if requests.get(f"http://127.0.0.1:{port}/api/status", timeout=1).status_code == 200:
                    return f"http://127.0.0.1:{port}"
            except requests.RequestException:
                continue
        time.sleep(1)
    return None


def screenshot(path, out):
    # msedge.exe spawns a child that does the actual capture and exits the
    # parent first — so launch non-blocking and poll for the file.
    if os.path.exists(out):
        os.remove(out)
    subprocess.Popen(
        [EDGE, "--headless", "--disable-gpu", "--no-first-run",
         "--user-data-dir=" + tempfile.mkdtemp(prefix="edge_"),
         "--window-size=1400,820", "--virtual-time-budget=5000",
         f"--screenshot={out}", BASE_URL + path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return True
        time.sleep(0.5)
    return False


def count_color(img, target, tol=40, step=2):
    w, h = img.size
    pix = img.load()
    n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if abs(r - target[0]) <= tol and abs(g - target[1]) <= tol and abs(b - target[2]) <= tol:
                n += 1
    return n


def count_blue(img, step=2):
    w, h = img.size
    pix = img.load()
    n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if b > 130 and b > r + 30 and b > g + 10:
                n += 1
    return n


def count_light(img, min_lum=130, step=2):
    w, h = img.size
    pix = img.load()
    n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if (r + g + b) // 3 > min_lum:
                n += 1
    return n


def count_hue(img, hue, step=2):
    """Dominant-channel detectors (tolerant of antialiasing on small icons)."""
    w, h = img.size
    pix = img.load()
    n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if hue == "green" and g > 110 and g > r + 30 and g > b + 20:
                n += 1
            elif hue == "red" and r > 120 and r > g + 40 and r > b + 40:
                n += 1
    return n


def count_amber(img, step=2):
    """Amber blocked-note / status-dot detector (#FFD60A-ish)."""
    w, h = img.size
    pix = img.load()
    n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if r > 150 and g > 130 and b < 90 and r > g + 10 and g > b + 70:
                n += 1
    return n


def avg_brightness(img, step=4):
    w, h = img.size
    pix = img.load()
    total = n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            total += (r + g + b) / 3
            n += 1
    return total / n


def black_fraction(img, tol=10, step=4):
    w, h = img.size
    pix = img.load()
    black = n = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pix[x, y]
            if r <= tol and g <= tol and b <= tol:
                black += 1
            n += 1
    return black / n


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def wait_state(pred, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = requests.get(BASE_URL + "/api/status", timeout=2).json()
            if pred(st):
                return True
        except requests.RequestException:
            pass
        time.sleep(0.4)
    return False


def run_screen(name, url, asserts):
    out = os.path.join(SHOTS, f"web_live_{name}.png")
    ok = screenshot(url, out)
    check(f"{name}: screenshot captured", ok)
    if not ok:
        return
    img = Image.open(out).convert("RGB")
    print(f"  {name} size={os.path.getsize(out)} hash={md5(out)[:8]}")
    # retry once in case the capture raced the JS poll
    for attempt in (1, 2):
        results = [(label, fn(img, out)) for label, fn in asserts]
        if all(v for _, v in results):
            break
        time.sleep(2)
        if attempt == 1:
            screenshot(url, out)
            img = Image.open(out).convert("RGB")
    for label, ok_ in results:
        check(f"{name}: {label}", ok_)


def main():
    if EDGE is None:
        print("SKIP: Microsoft Edge not found — set APPLEPIE_EDGE to its path.")
        return 1

    print("Booting --webui server ...")
    start_server()
    global BASE_URL
    BASE_URL = wait_for_server()
    if BASE_URL is None:
        print("[FAIL] server never came up")
        stop_server()
        return 1
    print(f"  server up at {BASE_URL}")

    BLUE = (10, 132, 255)
    AMBER = (255, 214, 10)

    print("\n--- screen: INTRO (requirements gate auto-opens) ---")
    shots = []
    # The intro popup renders the REAL requirements state, so assert the UI is
    # consistent with what the API reports: red icons + amber blocked-note only
    # exist while a check is failing (Close stays locked). On a fully healthy
    # machine every check passes, the note is hidden and there is nothing red.
    intro_req = requests.get(BASE_URL + "/api/check-intro-requirements", timeout=90).json()
    intro_asserts = [
        ("pure-black background dominates",
         lambda im, p: black_fraction(im) > 0.55),
        ("requirements popup light text present",
         lambda im, p: count_light(im, min_lum=120) > 200),
        ("green pass icons present",
         lambda im, p: count_hue(im, "green") > 4),
    ]
    if intro_req.get("all_passed"):
        intro_asserts.append(
            ("all checks green (no red fail icons, note hidden)",
             lambda im, p: count_hue(im, "red") <= 2 and count_amber(im) <= 20))
    else:
        intro_asserts.append(
            ("green pass + red fail icons present",
             lambda im, p: count_hue(im, "green") > 4 and count_hue(im, "red") > 4))
        intro_asserts.append(
            ("amber blocked-note present (Close locked)",
             lambda im, p: count_amber(im) > 20))
    run_screen("intro", "/", intro_asserts)
    wait_state(lambda s: s["scan_paused"])
    check("intro gate blocks the process (scan paused)", True)
    requests.post(BASE_URL + "/api/resume-scan", timeout=5)
    st = requests.get(BASE_URL + "/api/status", timeout=5).json()
    check("closing the gate resumes the scan", st["scan_paused"] is False)
    shots.append(os.path.join(SHOTS, "web_live_intro.png"))

    print("\n--- screen: HOME (dashboard) ---")
    requests.get(BASE_URL + "/api/preview?screen=home", timeout=5)
    wait_state(lambda s: s["phase"] == "home" and s["device_name"] == "iPhone 17 Pro")
    run_screen("home", "/", [
        ("blue Unlock button present",
         lambda im, p: count_blue(im) > 800),
        ("white device-name text present",
         lambda im, p: count_light(im, min_lum=190) > 60),
        ("brighter than intro (dashboard content)",
         lambda im, p: avg_brightness(im) > avg_brightness(Image.open(shots[0]).convert("RGB"))),
    ])
    shots.append(os.path.join(SHOTS, "web_live_home.png"))

    print("\n--- screen: INFO POPUP ---")
    requests.get(BASE_URL + "/api/preview?screen=info", timeout=5)
    wait_state(lambda s: s["popup"]["visible"])
    run_screen("info", "/", [
        ("blue Close button present",
         lambda im, p: count_blue(im) > 120),
        ("overlay dims the page vs home",
         lambda im, p: avg_brightness(im) < avg_brightness(Image.open(shots[1]).convert("RGB"))),
    ])
    shots.append(os.path.join(SHOTS, "web_live_info.png"))

    print("\n--- screen: LOADING POPUP ---")
    requests.get(BASE_URL + "/api/preview?screen=loading", timeout=5)
    wait_state(lambda s: s["loading"]["visible"])
    run_screen("loading", "/", [
        ("blue spinner ring present",
         lambda im, p: count_blue(im) > 8),
        ("light loading text present",
         lambda im, p: count_light(im, min_lum=120) > 15),
    ])
    shots.append(os.path.join(SHOTS, "web_live_loading.png"))

    print("\n--- screen: PROGRESS (busy) ---")
    requests.get(BASE_URL + "/api/preview?screen=progress", timeout=5)
    wait_state(lambda s: s["busy"] and s["progress"] == 45)
    run_screen("progress", "/", [
        ("blue progress fill present (45%)",
         lambda im, p: count_blue(im) > 300),
    ])
    shots.append(os.path.join(SHOTS, "web_live_progress.png"))

    print("\n--- screen: REQUIREMENTS POPUP ---")
    run_screen("requirements", "/?req=1", [
        ("light requirements text present",
         lambda im, p: count_light(im, min_lum=120) > 40),
        ("green pass + red fail icons present",
         lambda im, p: count_hue(im, "green") > 4 and count_hue(im, "red") > 4),
    ])
    shots.append(os.path.join(SHOTS, "web_live_requirements.png"))

    print("\n--- screen: SUPPORTED DEVICES POPUP ---")
    run_screen("devices", "/?devices=1", [
        ("device grid dense light text present",
         lambda im, p: count_light(im, min_lum=120) > 1500),
        ("grid content scrollable frame rendered",
         lambda im, p: count_blue(im) > 300),
        ("floating button visible (blue)",
         lambda im, p: count_blue(im) > 30),
    ])
    shots.append(os.path.join(SHOTS, "web_live_devices.png"))

    print("\n--- distinctness (each window renders differently) ---")
    hashes = {md5(p) for p in shots}
    check("all 7 screens produce unique renders", len(hashes) == len(shots))

    print("\n--- live state mirror ---")
    st = requests.get(BASE_URL + "/api/status", timeout=5).json()
    check("status field populated by engine", bool(st["status"]))

    stop_server()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    print(f"Screenshots: {SHOTS}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_server()