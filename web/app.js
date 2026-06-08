/* Apple Pie WebUI — polls /api/status and renders the engine state. */
"use strict";

const $ = (sel) => document.querySelector(sel);
let lastLogs = 0;
let lastLogText = null;
let authToken = sessionStorage.getItem("applepie_auth_token") || "";

async function fetchStatus() {
  try {
    const headers = {};
    if (authToken) {
      headers["X-Auth-Token"] = authToken;
    }
    const res = await fetch("/api/status", { headers });
    return await res.json();
  } catch (e) {
    return null;
  }
}

function render(state) {
  /* -------- phase -------- */
  if (state.phase === "home") {
    $("#intro-screen").classList.remove("active");
    $("#home-screen").classList.add("active");
  } else {
    $("#home-screen").classList.remove("active");
    $("#intro-screen").classList.add("active");
  }

  /* -------- intro -------- */
  $("#status-text").textContent = state.status || "Starting…";
  const dot = $("#status-dot");
  dot.classList.remove("ok", "wait");
  dot.classList.add(state.connected ? "ok" : "wait");

  /* -------- device fields -------- */
  $("#device-name").textContent = state.device_name || "iPhone";
  $("#device-info").textContent = state.device_info || "Waiting for device…";
  $("#stat-activation").textContent = state.activation || "—";
  $("#stat-udid").textContent = state.udid || "—";
  $("#stat-ios").textContent = state.ios || "—";

  /* -------- logs (delta + resync) -------- */
  const logSync = (state) => {
    if (!state.logs) return;
    if (state.logs.length < lastLogs) {
      // Server trimmed the log ring (cap of 500) or preview/reset replaced it:
      // the monotonic cursor is now ahead of the array, so the console would
      // freeze forever. Replay the tail instead.
      lastLogs = 0;
    } else if (state.logs.length === lastLogs && lastLogs > 0) {
      // Same length but the ring rolled (append + cap keeps length constant):
      // detect by comparing the last known line with the current tail.
      if (state.logs[state.logs.length - 1] !== lastLogText) {
        lastLogs = 0;
      }
    }
    if (state.logs.length > lastLogs) {
      const consoleEl = $("#log-console");
      for (let i = lastLogs; i < state.logs.length; i++) {
        const line = document.createElement("div");
        line.className = "log-line";
        line.textContent = state.logs[i];
        consoleEl.appendChild(line);
      }
      lastLogs = state.logs.length;
      lastLogText = state.logs[state.logs.length - 1];
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
  };
  logSync(state);
  $("#log-badge").textContent = state.busy ? "working" : "idle";

  /* -------- action: button vs progress -------- */
  const btn = $("#unlock-btn");
  const pw = $("#progress-wrap");
  if (state.busy) {
    btn.classList.add("hidden");
    pw.classList.remove("hidden");
    const pct = Math.min(100, Math.max(0, Math.round(state.progress || 0)));
    $("#progress-fill").style.width = pct + "%";
    $("#progress-pct").textContent = pct + "%";
  } else {
    pw.classList.add("hidden");
    btn.classList.remove("hidden");
    btn.textContent = state.unlock_text || "Unlock";
    btn.disabled = !!state.unlock_disabled;
  }

  /* -------- info popup -------- */
  const po = $("#popup-overlay");
  if (state.popup && state.popup.visible) {
    $("#popup-title").textContent = state.popup.title || "Message";
    $("#popup-message").textContent = state.popup.message || "";
    $("#popup-close").textContent = state.popup.button || "Close";
    po.classList.remove("hidden");
  } else {
    po.classList.add("hidden");
  }

  /* -------- loading popup -------- */
  const lo = $("#loading-overlay");
  if (state.loading && state.loading.visible) {
    $("#loading-message").textContent = state.loading.message || "Connecting…";
    lo.classList.remove("hidden");
  } else {
    lo.classList.add("hidden");
  }
}

async function poll() {
  const st = await fetchStatus();
  if (st) {
    // If not authenticated, lock the UI immediately
    const lockOverlay = $("#lock-overlay");
    if (!st.authenticated) {
      authToken = "";
      sessionStorage.removeItem("applepie_auth_token");
      if (lockOverlay.classList.contains("hidden")) {
        lockOverlay.classList.remove("hidden");
        $("#lock-password").value = "";
        $("#lock-password").focus();
      }
      // do not proceed with rendering or gates if locked
      setTimeout(poll, 800);
      return;
    } else {
      if (!lockOverlay.classList.contains("hidden")) {
        lockOverlay.classList.add("hidden");
      }
    }

    render(st);
    // Auto-open the intro requirements gate the moment the intro is on screen
    // (and no deep-link forced another screen/popup). Seen once; Close or the
    // intro Check-requirements button re-opens it.
    if (st.phase === "intro" && !gateShown &&
        !preview && params.get("req") !== "1" && params.get("intro-req") !== "1" &&
        params.get("devices") !== "1") {
      gateShown = true;
      openRequirements("/api/check-intro-requirements", "intro");
    }
  }
  setTimeout(poll, 800);
}

let reqMode = "unlock";
let gateShown = false;

async function openRequirements(endpoint, mode) {
  endpoint = endpoint || "/api/check-requirements";
  mode = mode || "unlock";
  reqMode = mode;
  if (mode === "intro") {
    fetch("/api/pause-scan", { method: "POST" }).catch(() => {});
  }
  let data = null;
  try {
    const res = await fetch(endpoint);
    data = await res.json();
  } catch (e) {
    data = { all_passed: false, checks: [
      { name: "Engine", passed: false, detail: "Could not reach the engine." },
    ] };
  }
  renderRequirements(data, mode);
}

function renderRequirements(data, mode) {
  const list = $("#req-list");
  list.innerHTML = "";
  (data.checks || []).forEach((c) => {
    const item = document.createElement("div");
    item.className = "req-item " + (c.passed ? "ok" : "fail");
    const icon = document.createElement("span");
    icon.className = "req-icon";
    icon.textContent = c.passed ? "\u2713" : "\u2717";
    const body = document.createElement("div");
    body.className = "req-body";
    const nm = document.createElement("div");
    nm.className = "req-name";
    nm.textContent = c.name || "";
    const dt = document.createElement("div");
    dt.className = "req-detail";
    dt.textContent = c.detail || "";
    body.appendChild(nm);
    body.appendChild(dt);
    item.appendChild(icon);
    item.appendChild(body);
    list.appendChild(item);
  });
  const proceed = $("#req-proceed");
  proceed.style.display = mode === "intro" ? "none" : "";
  proceed.disabled = !data.all_passed;

  const recheck = $("#req-recheck");
  const note = $("#req-blocked-note");
  const close = $("#req-close");
  const apply = $("#req-apply");
  const fwFailed = (data.checks || []).some((c) =>
    /firewall/i.test(c.name || "") && !c.passed);
  if (mode === "intro") {
    // Block the intro page: Close stays locked until every check passes.
    close.disabled = !data.all_passed;
    recheck.classList.toggle("hidden", data.all_passed);
    note.classList.toggle("hidden", data.all_passed);
    note.textContent = data.all_passed ? "" :
      "Fix the failed items, then press Re-check to unlock Close.";
    // Auto-fix: show Apply only when the firewall check is failing.
    apply.classList.toggle("hidden", !fwFailed);
  } else {
    close.disabled = false;
    recheck.classList.add("hidden");
    note.classList.add("hidden");
    apply.classList.add("hidden");
  }
  $("#req-overlay").classList.remove("hidden");
}

$("#unlock-btn").addEventListener("click", () => openRequirements());
$("#intro-req-btn").addEventListener("click", () => openRequirements("/api/check-intro-requirements", "intro"));

$("#req-recheck").addEventListener("click", () => {
  if (reqMode === "intro") {
    openRequirements("/api/check-intro-requirements", "intro");
  }
});

$("#req-apply").addEventListener("click", async () => {
  const apply = $("#req-apply");
  apply.disabled = true;
  apply.textContent = "Applying…";
  try {
    await fetch("/api/apply-firewall", { method: "POST" });
  } catch (e) {}
  apply.disabled = false;
  apply.textContent = "Apply";
  if (reqMode === "intro") {
    openRequirements("/api/check-intro-requirements", "intro");
  }
});

$("#req-proceed").addEventListener("click", async () => {
  $("#req-overlay").classList.add("hidden");
  try { await fetch("/api/unlock", { method: "POST" }); } catch (e) {}
});

$("#req-close").addEventListener("click", () => {
  if ($("#req-close").disabled) return;  // intro popup is locked until all checks pass
  $("#req-overlay").classList.add("hidden");
  if (reqMode === "intro") {
    fetch("/api/resume-scan", { method: "POST" }).catch(() => {});
    reqMode = "unlock";
  }
});

/* -------- supported devices FAB -------- */
let _devicesAll = [];
let _devicesFilter = "all";
let _devicesQuery = "";

async function openDevices() {
  try {
    const res = await fetch("/api/supported-devices");
    _devicesAll = (await res.json()).devices || [];
  } catch (e) { _devicesAll = []; }
  renderDevices();
  $("#devices-overlay").classList.remove("hidden");
  const si = $("#devices-search");
  si.focus();
}

function renderDevices() {
  const list = $("#devices-list");
  list.innerHTML = "";
  const q = _devicesQuery.trim().toLowerCase();
  const shown = _devicesAll.filter((d) => {
    const kind = (d.product_type || "").toLowerCase().startsWith("ipad") ? "iPad" : "iPhone";
    if (_devicesFilter !== "all" && kind !== _devicesFilter) return false;
    if (!q) return true;
    const hay = ((d.name || "") + " " + (d.product_type || "")).toLowerCase();
    return hay.indexOf(q) !== -1;
  });
  if (!shown.length) {
    const none = document.createElement("div");
    none.className = "device-cell fail";
    none.textContent = _devicesAll.length
      ? "No devices match your search."
      : "No bundled device data found.";
    list.appendChild(none);
    return;
  }
  shown.forEach((d) => {
    const cell = document.createElement("div");
    cell.className = "device-cell";
    const nm = document.createElement("div");
    nm.className = "device-name";
    nm.textContent = d.name || d.product_type;
    const versions = document.createElement("div");
    versions.className = "device-versions";
    const vs = (d.versions && d.versions.length) ? d.versions : ["All"];
    vs.forEach((v) => {
      const chip = document.createElement("span");
      chip.className = "device-ver";
      chip.textContent = v === "All" ? "All iOS" : "iOS " + v;
      versions.appendChild(chip);
    });
    cell.appendChild(nm);
    cell.appendChild(versions);
    list.appendChild(cell);
  });
}

$("#devices-fab").addEventListener("click", openDevices);
$("#devices-close").addEventListener("click", () => {
  $("#devices-overlay").classList.add("hidden");
});
$("#devices-search").addEventListener("input", (e) => {
  _devicesQuery = e.target.value;
  renderDevices();
});
document.querySelectorAll("#devices-filter .df-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    _devicesFilter = btn.dataset.f;
    document.querySelectorAll("#devices-filter .df-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    renderDevices();
  });
});

// deep-link capture helpers (manual page review + live tests):
//   http://host/?preview=home|info|loading|progress|requirements
//   http://host/?req=1        -> opens the unlock requirements popup
//   http://host/?intro-req=1  -> opens the intro (software) requirements popup
//   http://host/?devices=1    -> opens the supported-devices popup
const params = new URLSearchParams(location.search);
const preview = params.get("preview");
if (preview === "requirements") {
  openRequirements();
} else if (preview) {
  fetch("/api/preview?screen=" + encodeURIComponent(preview)).catch(() => {});
}
if (params.get("req") === "1") openRequirements();
if (params.get("intro-req") === "1") {
  openRequirements("/api/check-intro-requirements", "intro");
}
if (params.get("devices") === "1") openDevices();

$("#popup-close").addEventListener("click", async () => {
  try { await fetch("/api/close-popup", { method: "POST" }); } catch (e) {}
});

/* -------- lock / auth form handler -------- */
const lockForm = $("#lock-form");
if (lockForm) {
  lockForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const passInput = $("#lock-password");
    const errorEl = $("#lock-error");
    const btn = $("#lock-submit-btn");
    const password = passInput.value.trim();
    if (!password) return;

    btn.disabled = true;
    btn.textContent = "Verifying…";
    errorEl.classList.add("hidden");

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password })
      });
      const data = await res.json();
      if (res.ok && data.token) {
        authToken = data.token;
        sessionStorage.setItem("applepie_auth_token", authToken);
        $("#lock-overlay").classList.add("hidden");
        errorEl.classList.add("hidden");
        // Force an immediate poll to refresh state
        poll();
      } else {
        errorEl.textContent = data.error || "Incorrect password";
        errorEl.classList.remove("hidden");
        passInput.select();
      }
    } catch (err) {
      errorEl.textContent = "Connection error. Please try again.";
      errorEl.classList.remove("hidden");
    } finally {
      btn.disabled = false;
      btn.textContent = "Unlock Access";
    }
  });
}

poll();