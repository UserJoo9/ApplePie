<div align="center">

<img src="https://raw.githubusercontent.com/UserJoo9/ApplePie/main/icon.png" width="120" alt="Apple Pie" />

# Apple Pie

**Free, open-source iCloud activation-lock bypass for A12+ iPhones & iPads**

[![License](https://img.shields.io/github/license/UserJoo9/ApplePie?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/platform-Windows-0078d4?style=flat-square&logo=windows&logoColor=white)](https://github.com/UserJoo9/ApplePie)
[![iOS](https://img.shields.io/badge/iOS-16%20→%2026.1-black?style=flat-square&logo=apple&logoColor=white)](https://github.com/UserJoo9/ApplePie)
[![Devices](https://img.shields.io/badge/devices-54%20supported-green?style=flat-square)](https://github.com/UserJoo9/ApplePie)

**[🇦🇪 اقرأ بالعربي](README.ar.md)**

</div>

---

Everything runs locally — no PHP, no VPS, no external server. Apple Pie builds and serves all payloads from your PC over USB + local Wi-Fi. The only interface is a browser at `http://localhost:8080`.

---

## How it works

iOS re-reads a MobileGestalt cache and a Books/iTunes metadata database during activation. Apple Pie plants a crafted set of files on the device and drives it through three reboots, tricking iOS into treating the locked device as already activated.

Three payload stages are built locally and served to the phone:

| Stage | File | Format | Purpose |
|-------|------|--------|---------|
| 1 | `fixedfile` | EPUB ZIP | Fake `MobileGestalt.plist` for your model |
| 2 | `belliloveu.png` | SQLite | Books DB rebuilt with stage 1 URL injected |
| 3 | `apllefuckedhhh.png` | SQLite | Downloads DB with stage 2 URL + GUID injected |

The **SystemGroup GUID** (Books DB container ID) is the key the bypass is built around — found automatically from device logs.

---

## Requirements

- Windows 10 / 11
- Python 3.11+ (or use the prebuilt `applepie.exe`)
- iTunes installed (Apple USB drivers)
- 3uTools installed *(recommended for driver compatibility)*
- iPhone or iPad on the **activation-lock screen**, connected via USB and on the **same Wi-Fi as the PC**
- Windows Firewall: inbound TCP **8001** allowed (the app can create this rule automatically)

---

## Quick start (from source)

```
pip install -r requirements.txt
python new_applepie.py
```

Open **http://localhost:8080**. Follow the on-screen checklist — the intro popup walks you through every requirement and locks the UI until all checks pass.

> Pass a known GUID to skip the auto-search step:
> `python new_applepie.py <GUID>`

---

## Build a standalone exe

```
python -m nuitka --onefile --assume-yes-for-downloads --windows-console-mode=disable ^
  --output-filename=applepie.exe ^
  --include-data-dir=img=img --include-data-dir=data=data ^
  --include-raw-dir=bin=bin --include-data-dir=web=web ^
  --nofollow-import-to=IPython --nofollow-import-to=pytest ^
  new_applepie.py
```

*(Full command in `build.txt`.)*
Nuitka will download MinGW64 automatically if no C compiler is found.

---

## Supported devices

**54 devices** — iPhone XS through iPhone 17 series, iPad 8 through iPad Pro M2 / iPad Air M2 / iPad mini 6.
Click the gear icon in the UI to browse the full list and filter by model or iOS version.

Adding support for a new model: drop a valid `com.apple.MobileGestalt.plist` dump into `data/Maker/<ProductType>/` — no code change needed.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| iTunes / 3uTools not found | Install them, press **Re-check** |
| Firewall rule missing | Press **Apply** (runs as Administrator) |
| "Device not found" | Re-plug USB, accept "Trust This Computer" on the phone, check Apple drivers |
| LAN reachability fails | Wrong NIC / VPN active — check `ipconfig`, ensure port 8001 is open |
| "Could not get GUID" | Erase with Apple Configurator 2 and retry; or pass the GUID as a CLI argument |
| "Done" but still locked | Erase with Apple Configurator 2 and retry |

---

## Tests

```
python -m unittest test_webui -v    # 31 unit/integration tests
python test_webui_live.py           # 28 E2E checks (Edge headless)
python test_webui_pages.py          # 7 page-review screenshots
```

---

## Project layout

```
new_applepie.py          Engine — all business logic (ApplePieEngine)
webui.py                 Flask bridge (16 API routes)
web/                     Browser UI (HTML / CSS / JS)
data/
  Maker/<prd>/           MobileGestalt gestalt library (54 devices)
  downloads.28.png       Stage-3 SQL dump
  BLDatabaseManager.png  Stage-2 SQL dump
  badfile.plist          Served to the device
bin/
  libimobiledevice/      ideviceinfo.exe, idevicediagnostics.exe + DLLs
  dependencies/          unifiedlog_iterator.exe
requirements.txt
build.txt                Nuitka build command
```

---

## Disclaimer

For use on devices **you legally own**. Bypassing an activation lock on a device you do not own may be illegal in your jurisdiction.

---

<!-- If all checks pass but the device stays locked, examine what the phone actually receives — not what the server sends. -->
