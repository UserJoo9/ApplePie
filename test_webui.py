# -*- coding: utf-8 -*-
# Apple Pie WebUI test suite — unit/integration level.
#   Run:  python -m unittest test_webui -v
# Covers:
#   - static routes (/, style.css, app.js, img assets)
#   - /api/status JSON shape + live state mirror (logs/progress/popups/devices)
#   - POST /api/unlock (starts the real Hacktivating thread) + /api/close-popup
#   - /api/preview screen drivers (home | info | loading | progress)
#   - /api/check-requirements pre-flight checks

import os
import shutil
import sys
import tempfile
import unittest

import new_applepie as ap
import webui

# ---- shared engine fixture (pure backend) -------------------------------
_UI = None


def setUpModule():
    global _UI
    _UI = ap.ApplePieEngine()
    # wire the Flask bridge to the REAL engine state (by reference)
    webui._ui = _UI
    webui._state = ap.web_state
    webui._lock = ap._web_lock
    ap.web_state["logs"] = []


def tearDownModule():
    ap.web_state["logs"] = []


class StaticRoutesTest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_index_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        r.close()
        for marker in ("intro-screen", "home-screen", "popup-overlay",
                       "loading-overlay", "app.js", "style.css", "banner.png"):
            self.assertIn(marker, body)

    def test_css_js_served(self):
        for path in ("/style.css", "/app.js"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200)
            r.close()

    def test_img_assets_served(self):
        for name in ("banner.png", "ios26hello.png"):
            r = self.c.get("/img/" + name)
            self.assertEqual(r.status_code, 200)
            self.assertGreater(len(r.data), 1000)
            r.close()


class StatusAPITest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_status_shape(self):
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()
        for key in ("phase", "status", "connected", "device_name", "device_info",
                    "udid", "activation", "ios", "supported", "progress",
                    "busy", "unlock_disabled", "unlock_text", "logs",
                    "popup", "loading"):
            self.assertIn(key, s)
        self.assertIsInstance(s["logs"], list)
        self.assertIsInstance(s["popup"], dict)
        self.assertIsInstance(s["loading"], dict)


class StateBridgeTest(unittest.TestCase):
    """widget operations -> web_state mirror (the browser's data source)."""

    def test_log_mirror(self):
        before = len(ap.web_state["logs"])
        _UI._log("[*] unit test log line")
        self.assertEqual(ap.web_state["logs"][before], "[*] unit test log line")

    def test_progress_mirror(self):
        _UI.setProgress(progress=30)
        self.assertEqual(ap.web_state["progress"], 30)

    def test_info_popup_mirror(self):
        _UI.ShowInfoPopup(button_text="Okay", message="hello", title="Hi")
        self.assertTrue(ap.web_state["popup"]["visible"])
        self.assertEqual(ap.web_state["popup"]["title"], "Hi")
        self.assertEqual(ap.web_state["popup"]["message"], "hello")
        self.assertEqual(ap.web_state["popup"]["button"], "Okay")
        _UI.ClosePopup()
        self.assertFalse(ap.web_state["popup"]["visible"])

    def test_loading_popup_mirror(self):
        _UI.ShowLoadingPopup("please wait")
        self.assertTrue(ap.web_state["loading"]["visible"])
        self.assertEqual(ap.web_state["loading"]["message"], "please wait")
        _UI.HideLoadingPopup()
        self.assertFalse(ap.web_state["loading"]["visible"])

    def test_popup_via_api(self):
        c = webui.app.test_client()
        _UI.ShowInfoPopup(button_text="Close", message="m", title="t")
        s = c.get("/api/status").get_json()
        self.assertTrue(s["popup"]["visible"])
        self.assertEqual(s["popup"]["title"], "t")
        c.post("/api/close-popup")
        self.assertFalse(ap.web_state["popup"]["visible"])


class AuthAPITest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_auth_status_without_token(self):
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["authenticated"])

    def test_login_incorrect_password(self):
        r = self.c.post("/api/auth/login", json={"password": "wrong_password_123"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])

    def test_login_correct_password_and_token_flow(self):
        # The password currently on Gist is ApplePie2026
        # Test dynamically by testing with the current password
        with webui._auth_lock:
            pass_val = webui._current_password or "ApplePie2026"
            webui._current_password = pass_val

        r = self.c.post("/api/auth/login", json={"password": pass_val})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        token = data["token"]
        self.assertTrue(bool(token))

        # Test authenticated status with token in header
        r2 = self.c.get("/api/status", headers={"X-Auth-Token": token})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["authenticated"])

        # Test token revocation on password change
        with webui._auth_lock:
            webui._current_password = "NewPassword2026"
            webui._authenticated_tokens.clear()

        r3 = self.c.get("/api/status", headers={"X-Auth-Token": token})
        self.assertEqual(r3.status_code, 200)
        self.assertFalse(r3.get_json()["authenticated"])


class ScanPauseTest(unittest.TestCase):
    """Intro requirements popup blocks the process: pause-scan freezes the
    device-search thread (scan_paused flag), resume-scan unfreezes it."""

    def setUp(self):
        self.c = webui.app.test_client()
        _UI.resume_scan()

    def test_pause_endpoint_freezes_scan(self):
        r = self.c.post("/api/pause-scan")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(ap.web_state["scan_paused"])
        self.assertTrue(_UI._scan_paused)

    def test_resume_endpoint_unfreezes_scan(self):
        self.c.post("/api/pause-scan")
        r = self.c.post("/api/resume-scan")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ap.web_state["scan_paused"])
        self.assertFalse(_UI._scan_paused)

    def test_scan_paused_in_status_api(self):
        self.c.post("/api/pause-scan")
        s = self.c.get("/api/status").get_json()
        self.assertTrue(s["scan_paused"])
        self.c.post("/api/resume-scan")
        s = self.c.get("/api/status").get_json()
        self.assertFalse(s["scan_paused"])


class ActionRoutesTest(unittest.TestCase):
    def test_unlock_starts_pipeline(self):
        c = webui.app.test_client()
        r = c.post("/api/unlock")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            if ap.web_state["busy"]:
                break
            time.sleep(0.2)
        self.assertTrue(ap.web_state["busy"], "Hacktivating did not set busy")
        self.assertIn("[*] Process started!", ap.web_state["logs"])
        # let the no-device hack thread finish on its own (it exits via sys.exit)
        time.sleep(7)


class PreviewScreenTest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_preview_home(self):
        r = self.c.get("/api/preview?screen=home")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["screen"], "home")
        s = self.c.get("/api/status").get_json()
        self.assertEqual(s["phase"], "home")
        self.assertEqual(s["device_name"], "iPhone 17 Pro")
        self.assertEqual(s["device_info"], "iOS Version: 26.1, Supported")
        self.assertEqual(s["udid"], "UDID: 00008130-001E14410A30001E")
        self.assertGreater(len(s["logs"]), 5)

    def test_preview_info(self):
        self.c.get("/api/preview?screen=info")
        s = self.c.get("/api/status").get_json()
        self.assertTrue(s["popup"]["visible"])
        self.assertEqual(s["popup"]["title"], "Test Title")
        self.assertEqual(s["popup"]["message"], "This is a test message.")

    def test_preview_loading(self):
        self.c.get("/api/preview?screen=loading")
        s = self.c.get("/api/status").get_json()
        self.assertTrue(s["loading"]["visible"])
        self.assertEqual(s["loading"]["message"], "Please wait...")

    def test_preview_progress(self):
        self.c.get("/api/preview?screen=progress")
        s = self.c.get("/api/status").get_json()
        self.assertTrue(s["busy"])
        self.assertEqual(s["progress"], 45)


class CheckRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_endpoint_shape(self):
        r = self.c.get("/api/check-requirements")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data["all_passed"], bool)
        self.assertIsInstance(data["checks"], list)
        self.assertGreaterEqual(len(data["checks"]), 4)
        for c in data["checks"]:
            self.assertIn("name", c)
            self.assertIn("passed", c)
            self.assertIn("detail", c)
            self.assertIsInstance(c["passed"], bool)
            self.assertTrue(c["detail"])

    def test_device_check_reported(self):
        data = _UI.check_requirements()
        names = [c["name"] for c in data["checks"]]
        self.assertIn("Device connected", names)
        self.assertIn("Device supported", names)
        self.assertIn("Payload server", names)
        self.assertIn("LAN reachability", names)
        # payload server check is environment-dependent; it must never throw
        for c in data["checks"]:
            self.assertIn(c["passed"], (True, False))

    def test_all_passed_matches_checks(self):
        data = _UI.check_requirements()
        self.assertEqual(data["all_passed"],
                         all(c["passed"] for c in data["checks"]))


class IntroRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_endpoint_shape(self):
        r = self.c.get("/api/check-intro-requirements")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data["all_passed"], bool)
        self.assertIsInstance(data["checks"], list)
        self.assertGreaterEqual(len(data["checks"]), 3)
        for c in data["checks"]:
            self.assertIn("name", c)
            self.assertIn("passed", c)
            self.assertIn("detail", c)
            self.assertIsInstance(c["passed"], bool)
            self.assertTrue(c["detail"])

    def test_software_checks_reported(self):
        data = _UI.check_intro_requirements()
        names = [c["name"] for c in data["checks"]]
        self.assertIn("iTunes", names)
        self.assertIn("3uTools", names)
        self.assertIn("Software requirements", names)
        for c in data["checks"]:
            self.assertIn(c["passed"], (True, False))

    def test_firewall_check_reported(self):
        data = _UI.check_intro_requirements()
        names = [c["name"] for c in data["checks"]]
        self.assertTrue(any("Firewall" in n for n in names),
                        f"firewall check missing: {names}")
        fw = next(c for c in data["checks"] if "Firewall" in c["name"])
        self.assertIsInstance(fw["passed"], bool)
        self.assertTrue(fw["detail"])

    def test_apply_firewall_endpoint(self):
        r = self.c.post("/api/apply-firewall")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("ok", data)
        self.assertIn("detail", data)
        # After applying, the intro firewall check should pass.
        intro = _UI.check_intro_requirements()
        fw = next(c for c in intro["checks"] if "Firewall" in c["name"])
        self.assertTrue(fw["passed"], fw["detail"])

    def test_all_passed_matches_checks(self):
        data = _UI.check_intro_requirements()
        self.assertEqual(data["all_passed"],
                         all(c["passed"] for c in data["checks"]))


class SupportedDevicesTest(unittest.TestCase):
    def setUp(self):
        self.c = webui.app.test_client()

    def test_endpoint_shape(self):
        r = self.c.get("/api/supported-devices")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data["devices"], list)
        self.assertGreaterEqual(len(data["devices"]), 5)
        for d in data["devices"]:
            self.assertIn("product_type", d)
            self.assertIn("name", d)
            self.assertIn("versions", d)
            self.assertIsInstance(d["versions"], list)
            self.assertTrue(d["product_type"])
            self.assertTrue(d["name"])

    def test_issue_devices_present(self):
        devices = _UI.supported_devices()
        pts = [d["product_type"] for d in devices]
        self.assertIn("iPhone13,3", pts)   # iPhone 12 Pro (issue #10)
        self.assertIn("iPhone11,2", pts)   # iPhone XS (issue #7)
        d = next(x for x in devices if x["product_type"] == "iPhone13,3")
        # version-specific dir present => only that concrete version is listed
        self.assertEqual(d["versions"], ["17.3.1"])

    def test_versions_reflect_per_device_dumps(self):
        devices = _UI.supported_devices()
        for d in devices:
            fdir = ap._data(f"Maker/{d['product_type'].replace(',', '-')}")
            specific = sorted(
                entry for entry in os.listdir(fdir)
                if os.path.isdir(os.path.join(fdir, entry))
                and os.path.exists(os.path.join(fdir, entry, "com.apple.MobileGestalt.plist"))
            )
            if specific:
                self.assertEqual(d["versions"], specific,
                                 f"{d['product_type']} should list its own version dirs")
            else:
                self.assertEqual(d["versions"], ["All"],
                                 f"{d['product_type']} generic-only should report All")

    def test_fab_present_in_html(self):
        r = self.c.get("/")
        body = r.get_data(as_text=True)
        r.close()
        self.assertIn('id="devices-fab"', body)
        self.assertIn('id="devices-overlay"', body)

    def test_devices_toolbar_in_html(self):
        r = self.c.get("/")
        body = r.get_data(as_text=True)
        r.close()
        self.assertIn('id="devices-search"', body)
        self.assertIn('id="devices-filter"', body)
        self.assertIn('data-f="all"', body)
        self.assertIn('data-f="iPhone"', body)
        self.assertIn('data-f="iPad"', body)

    def test_filter_and_search_sources(self):
        devices = _UI.supported_devices()
        iPhones = [d for d in devices if d["product_type"].lower().startswith("iphone")]
        iPads = [d for d in devices if d["product_type"].lower().startswith("ipad")]
        self.assertGreater(len(iPhones), 0)
        self.assertGreater(len(iPads), 0)
        # search matches on name and product_type (JS mirrors this logic)
        needle = "pro"
        hits = [d for d in devices if "pro" in (d["name"] + " " + d["product_type"]).lower()]
        self.assertGreater(len(hits), 0)
        self.assertEqual(len(iPhones) + len(iPads), len(devices))


class GestaltResolverTest(unittest.TestCase):
    """data/Maker gestalt selection: exact version > generic > nearest version."""

    def setUp(self):
        self.eng = ap.ApplePieEngine()
        self.tmp = tempfile.mkdtemp()
        self._real_data = ap._data
        self._patch()

    def tearDown(self):
        ap._data = self._real_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self):
        ap._data = lambda name: os.path.join(self.tmp, name.replace("/", os.sep))

    def _make(self, prd, version=None):
        rel = f"Maker/{prd}"
        if version:
            rel += f"/{version}"
        d = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "com.apple.MobileGestalt.plist")
        with open(p, "wb") as f:
            f.write(b"PLIST:" + prd.encode() + b":" + str(version).encode())
        return p

    def test_exact_version_dump_wins(self):
        exact = self._make("iPhone99-1", "17.3.1")
        self._make("iPhone99-1")  # generic must lose to the exact version
        got, ver = self.eng._resolve_gestalt("iPhone99,1", "17.3.1")
        self.assertEqual(got, exact)
        self.assertEqual(ver, "17.3.1")

    def test_generic_dump_used_when_version_absent(self):
        generic = self._make("iPhone99-1")
        got, ver = self.eng._resolve_gestalt("iPhone99,1", "16.0")
        self.assertEqual(got, generic)
        self.assertIsNone(ver)

    def test_nearest_version_when_only_version_dumps_exist(self):
        v1731 = self._make("iPhone99-1", "17.3.1")
        v185 = self._make("iPhone99-1", "18.5")
        # 17.3.2 is closer to 17.3.1 than to 18.5
        got, ver = self.eng._resolve_gestalt("iPhone99,1", "17.3.2")
        self.assertEqual(got, v1731)
        self.assertEqual(ver, "17.3.1")
        # 18.5.1 is closer to 18.5
        got, ver = self.eng._resolve_gestalt("iPhone99,1", "18.5.1")
        self.assertEqual(got, v185)
        self.assertEqual(ver, "18.5")

    def test_version_only_device_is_supported(self):
        self._make("iPhone99-1", "17.3.1")
        self.assertTrue(self.eng.is_device_supported("iPhone99,1", "17.4"))
        self.assertTrue(self.eng.is_device_supported("iPhone99,1", "17.3.1"))

    def test_unknown_device_unsupported(self):
        self.assertFalse(self.eng.is_device_supported("iPhone99,9", "17.3.1"))
        self.assertIsNone(self.eng.generate_payloads("iPhone99,9", "x", "y", "17.3.1"))

    def test_every_bundled_gestalt_resolves(self):
        """Each version dump in data/Maker resolves for its own iOS version."""
        ap._data = self._real_data  # real bundle, not the temp tree
        maker = self._real_data("Maker")
        checked = 0
        for folder in sorted(os.listdir(maker)):
            fdir = os.path.join(maker, folder)
            if not os.path.isdir(fdir):
                continue
            prd = folder.replace("-", ",")
            for entry in sorted(os.listdir(fdir)):
                epath = os.path.join(fdir, entry)
                if not os.path.isdir(epath):
                    continue
                want = os.path.join(epath, "com.apple.MobileGestalt.plist")
                if not os.path.exists(want):
                    continue
                got, ver = self.eng._resolve_gestalt(prd, entry)
                self.assertIsNotNone(got, f"{prd}/{entry} did not resolve")
                self.assertEqual(os.path.normcase(got), os.path.normcase(want),
                                 f"{prd}/{entry} did not resolve to its own dump")
                self.assertEqual(ver, entry)
                checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)