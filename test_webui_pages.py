# -*- coding: utf-8 -*-
# Manual WebUI page review.
#   Run:  python test_webui_pages.py
# Boots the real --webui server, drives every page with sample data, and saves
# one screenshot per page to Temp\\opencode\\shots\\pages\\ so each page can be
# reviewed by eye. The in-browser gallery at http://localhost:8080/pages does
# the same thing interactively (no iPhone needed).

import os
import subprocess
import sys
import time

import requests

import test_webui_live as live  # reuse Edge screenshot helpers + server boot

SHOTS = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "opencode", "shots", "pages")
BASE_URL = None
PROC = None

PAGES = [
    ("intro",        "/",                             "Intro gate: requirements popup auto-opens on load"),
    ("home",         "/?preview=home",                "Dashboard: device header, phone + log cards, stats, Unlock"),
    ("info",         "/?preview=info",                "Info popup over dimmed page"),
    ("loading",      "/?preview=loading",             "Loading popup with spinner"),
    ("progress",     "/?preview=progress",            "Busy state: progress bar at 45%"),
    ("requirements", "/?preview=requirements",        "Check Requirements popup (pre-flight checks)"),
    ("devices",      "/?devices=1",                    "Supported Devices popup (floating button)"),
]


def main():
    global BASE_URL, PROC
    if live.EDGE is None:
        print("SKIP: Microsoft Edge not found — set APPLEPIE_EDGE to its path.")
        return 1

    os.makedirs(SHOTS, exist_ok=True)
    print("Booting --webui server ...")
    live.start_server()
    BASE_URL = live.wait_for_server()
    if BASE_URL is None:
        print("[FAIL] server never came up")
        live.stop_server()
        return 1
    live.BASE_URL = BASE_URL  # screenshot() prepends this to the path
    print(f"  server up at {BASE_URL}")
    print(f"  gallery:  {BASE_URL}/pages")

    print()
    for name, url, desc in PAGES:
        out = os.path.join(SHOTS, f"{name}.png")
        ok = live.screenshot(url, out)
        live.check(f"{name:12s} {desc:55s} -> {os.path.basename(out)}", ok)

    live.stop_server()
    print()
    print(f"RESULT: {live.PASS} passed, {live.FAIL} failed")
    print(f"Review the screenshots in: {SHOTS}")
    return 0 if live.FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        live.stop_server()