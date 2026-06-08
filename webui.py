# -*- coding: utf-8 -*-
# Flask WebUI bridge for Apple Pie.
# Serves the browser UI from ./web and mirrors the engine's
# state (web_state in new_applepie.py) over a small JSON API.

import os
import socket
import threading
import time
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

_base = os.path.dirname(os.path.abspath(__file__))
_ui = None
_state = None
_lock = threading.Lock()

PASSWORD_GIST_URL = (
    "https://gist.githubusercontent.com/UserJoo9/f24b0c792ef510b729dea3b7a97272f6"
    "/raw/gistfile1.txt"
)
_current_password = None
_authenticated_tokens = set()
_auth_lock = threading.Lock()
_checker_started = False

app = Flask(__name__)


def fetch_remote_password() -> str:
    """Fetch expected password string from the GitHub Gist URL."""
    try:
        # Cache busting timestamp so GitHub CDN / proxies always return the live value
        cache_buster = int(time.time())
        url = f"{PASSWORD_GIST_URL}?_={cache_buster}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ApplePie/1.0",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="ignore").strip()
            return content
    except Exception as e:
        print(f"[*] Warning: Could not fetch password from gist: {e}")
        return ""


def _password_checker_loop():
    """Background worker: verifies the remote password every 15 seconds.
    If the remote password changes or is revoked, revokes all authenticated tokens
    to immediately lock the WebUI."""
    global _current_password
    while True:
        try:
            time.sleep(15)
            remote_pass = fetch_remote_password()
            if remote_pass:
                with _auth_lock:
                    if _current_password is not None and remote_pass != _current_password:
                        print(f"[*] Remote password changed! Locking WebUI.")
                        _authenticated_tokens.clear()
                    _current_password = remote_pass
        except Exception as e:
            print(f"[*] Password checker loop error: {e}")


def _start_password_checker():
    global _checker_started, _current_password
    with _auth_lock:
        if not _checker_started:
            _checker_started = True
            _current_password = fetch_remote_password()
            threading.Thread(target=_password_checker_loop, daemon=True).start()


def _is_authenticated(req) -> bool:
    token = req.headers.get("X-Auth-Token") or req.args.get("auth_token")
    if not token:
        # Also check JSON body if available
        if req.is_json:
            try:
                token = req.get_json().get("auth_token")
            except Exception:
                pass
    with _auth_lock:
        return bool(token and token in _authenticated_tokens)


def _find_port(start: int = 8080, tries: int = 5) -> int:
    for p in range(start, start + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    return None


def serve(ui, state, lock, port: int = 8080) -> int:
    # 'state' and 'lock' are passed by reference so Flask reads the SAME
    # dict the engine updates (importing new_applepie here would create a
    # second module instance with its own globals).
    global _ui, _state, _lock
    _ui = ui
    _state = lock
    _lock = state
    _start_password_checker()
    port = _find_port(port)
    if port is None:
        return None
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True
    ).start()
    return port


@app.route("/")
def index():
    return send_from_directory(os.path.join(_base, "web"), "index.html")


@app.route("/pages")
def pages():
    # Manual WebUI page review gallery (no device needed). Every screen is
    # reachable via /?preview=home|info|loading|progress|requirements.
    return send_from_directory(os.path.join(_base, "web"), "pages.html")


@app.route("/style.css")
def style():
    return send_from_directory(os.path.join(_base, "web"), "style.css")


@app.route("/app.js")
def script():
    return send_from_directory(os.path.join(_base, "web"), "app.js")


@app.route("/img/<path:name>")
def img(name):
    return send_from_directory(os.path.join(_base, "img"), name)


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    global _current_password
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()

    # Always ensure we have the freshest remote password
    remote_pass = fetch_remote_password()
    with _auth_lock:
        if remote_pass:
            _current_password = remote_pass
        expected = _current_password

    if not expected:
        return jsonify({"ok": False, "error": "Could not verify remote password from Gist."}), 503

    if password == expected:
        token = os.urandom(24).hex()
        with _auth_lock:
            _authenticated_tokens.add(token)
        return jsonify({"ok": True, "token": token})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


@app.route("/api/auth/check")
def api_auth_check():
    authenticated = _is_authenticated(request)
    return jsonify({"authenticated": authenticated})


@app.route("/api/status")
def api_status():
    authenticated = _is_authenticated(request)
    with _lock:
        snap = dict(_state)
        snap["logs"] = list(_state["logs"])
        snap["popup"] = dict(_state["popup"])
        snap["loading"] = dict(_state["loading"])
    snap["authenticated"] = authenticated
    return jsonify(snap)


@app.route("/api/preview")
def api_preview():
    # UI-preview helper (no device needed): fills web_state with sample data
    # so each screen can be reviewed in the browser without a phone.
    #   /api/preview?screen=home|info|loading|progress
    # The real device flow overwrites these values on the next poll.
    screen = request.args.get("screen", "home")
    with _lock:
        _state.update({
            "phase": "home",
            "status": "Device connected!",
            "connected": True,
            "device_name": "iPhone 17 Pro",
            "device_info": "iOS Version: 26.1, Supported",
            "udid": "UDID: 00008130-001E14410A30001E",
            "activation": "Activation state: No",
            "ios": "iOS Version: 26.1",
            "supported": True,
            "busy": False,
            "unlock_disabled": False,
            "unlock_text": "Unlock",
            "progress": 0,
            "popup": {"visible": False, "title": "", "message": "", "button": "Close"},
            "loading": {"visible": False, "message": ""},
            "logs": [
                "[*] Device connected!",
                "[*] Checking for updates...",
                "[*] No updates found!",
                "[*] Generating payload locally...",
                "[*] Payload ready - serving over local network...",
                "[*] Uploading payload...",
                "[*] Payload successfully deployed!",
                "[*] Cleaning up files and scanning for old files...",
                "[*] Rebooting device...",
                "[*] Done! Activate your device as usual.",
            ],
        })
        if screen == "info":
            _state["popup"] = {"visible": True, "title": "Test Title",
                               "message": "This is a test message.", "button": "Close"}
        elif screen == "loading":
            _state["loading"] = {"visible": True, "message": "Please wait..."}
        elif screen == "progress":
            _state["busy"] = True
            _state["progress"] = 45
        elif screen == "intro":
            _state["phase"] = "intro"
            _state["status"] = "Waiting for device to connect"
            _state["connected"] = False
            _state["device_name"] = ""
            _state["device_info"] = ""
            _state["udid"] = ""
            _state["activation"] = ""
            _state["ios"] = ""
    return jsonify({"ok": True, "screen": screen})


@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    if _ui is not None:
        _ui.StartHacktivating()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "engine not ready"})


@app.route("/api/check-requirements")
def api_check_requirements():
    # Pre-flight checks shown in the "Check Requirements" popup before unlock.
    if _ui is None:
        return jsonify({"ok": False, "error": "engine not ready"})
    return jsonify(_ui.check_requirements())


@app.route("/api/supported-devices")
def api_supported_devices():
    # All devices bundled in data/Maker (name, ProductType, iOS versions).
    if _ui is None:
        return jsonify({"devices": []})
    return jsonify({"devices": _ui.supported_devices()})


@app.route("/api/check-intro-requirements")
def api_check_intro_requirements():
    # Software-side checks for the intro "Check requirements" popup
    # (iTunes, 3uTools, bundled payload data / tools). No device needed.
    if _ui is None:
        return jsonify({"ok": False, "error": "engine not ready"})
    return jsonify(_ui.check_intro_requirements())


@app.route("/api/apply-firewall", methods=["POST"])
def api_apply_firewall():
    # Create the ApplePie inbound TCP <payload port> Windows Firewall rule
    # automatically. Uses the live engine instance (no duplicate module import).
    if _ui is None:
        return jsonify({"ok": False, "error": "engine not ready"})
    return jsonify(_ui.apply_firewall_rule())


@app.route("/api/pause-scan", methods=["POST"])
def api_pause_scan():
    # Intro requirements popup blocks the process: freeze the device-search
    # thread until the popup is closed (resume-scan).
    if _ui is not None:
        _ui.pause_scan()
    return jsonify({"ok": True})


@app.route("/api/resume-scan", methods=["POST"])
def api_resume_scan():
    if _ui is not None:
        _ui.resume_scan()
    return jsonify({"ok": True})


@app.route("/api/close-popup", methods=["POST"])
def api_close_popup():
    if _ui is not None:
        _ui.ClosePopup()
    return jsonify({"ok": True})