"use strict";

const $ = (id) => document.getElementById(id);
const preview = $("preview");
const statusPill = $("status-pill");
const recBadge = $("rec-badge");
const recTime = $("rec-time");
const offline = $("offline");
const offlineMsg = $("offline-msg");
const recordBtn = $("record-btn");
const toast = $("toast");

let recording = false;
let toastTimer = null;

function showToast(msg) {
  toast.textContent = msg;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 2500);
}

function fmtTime(s) {
  const m = Math.floor(s / 60).toString().padStart(2, "0");
  const sec = (s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || res.statusText);
  }
  return data;
}

// --------------------------------------------------------------------- //
// Preview stream lifecycle
// --------------------------------------------------------------------- //
function startStream() {
  preview.src = "/stream?t=" + Date.now();
}

// --------------------------------------------------------------------- //
// Status polling
// --------------------------------------------------------------------- //
function applyRotation(rot) {
  preview.classList.remove("rot90", "rot270");
  if (rot === 90) preview.classList.add("rot90");
  else if (rot === 270) preview.classList.add("rot270");
}

async function poll() {
  try {
    const s = await api("/api/status");
    if (s.available) {
      offline.classList.add("hidden");
      if (!preview.src) startStream();
      recording = s.recording;
      if (recording) {
        statusPill.textContent = "● REC";
        statusPill.className = "pill pill-rec";
        recBadge.classList.remove("hidden");
        recTime.textContent = fmtTime(s.elapsed);
        recordBtn.classList.add("active");
      } else {
        statusPill.textContent = "live";
        statusPill.className = "pill pill-on";
        recBadge.classList.add("hidden");
        recordBtn.classList.remove("active");
      }
      applyRotation(s.settings.rotation);
      syncSettingsForm(s.settings);
    } else {
      statusPill.textContent = "offline";
      statusPill.className = "pill pill-off";
      offline.classList.remove("hidden");
      offlineMsg.textContent = s.error || "Camera offline";
      preview.removeAttribute("src");
    }
  } catch (e) {
    statusPill.textContent = "no link";
    statusPill.className = "pill pill-off";
  }
}

// --------------------------------------------------------------------- //
// Controls
// --------------------------------------------------------------------- //
$("photo-btn").addEventListener("click", async () => {
  try {
    $("flash").classList.add("fire");
    setTimeout(() => $("flash").classList.remove("fire"), 350);
    const d = await api("/api/photo", { method: "POST" });
    showToast("Saved " + d.file);
  } catch (e) {
    showToast("Photo failed: " + e.message);
  }
});

recordBtn.addEventListener("click", async () => {
  try {
    if (recording) {
      await api("/api/record/stop", { method: "POST" });
      showToast("Recording saved");
    } else {
      await api("/api/record/start", { method: "POST" });
      showToast("Recording started");
    }
    poll();
  } catch (e) {
    showToast("Record failed: " + e.message);
  }
});

$("reconnect-btn").addEventListener("click", async () => {
  showToast("Reconnecting…");
  await api("/api/camera/reconnect", { method: "POST" }).catch(() => {});
  startStream();
  poll();
});

// --------------------------------------------------------------------- //
// Panels
// --------------------------------------------------------------------- //
const backdrop = $("backdrop");
function openPanel(p) {
  p.classList.remove("hidden");
  backdrop.classList.remove("hidden");
}
function closePanels() {
  $("settings-panel").classList.add("hidden");
  $("gallery-panel").classList.add("hidden");
  backdrop.classList.add("hidden");
}
backdrop.addEventListener("click", closePanels);
document.querySelectorAll("[data-close]").forEach((b) =>
  b.addEventListener("click", closePanels)
);

$("settings-btn").addEventListener("click", () => {
  openPanel($("settings-panel"));
  loadHotspot();
});
$("gallery-btn").addEventListener("click", () => {
  openPanel($("gallery-panel"));
  loadGallery();
});

// --------------------------------------------------------------------- //
// Hotspot toggle
// --------------------------------------------------------------------- //
const hotspotRow = $("hotspot-row");
const hotspotToggle = $("set-hotspot");

async function loadHotspot() {
  try {
    const s = await api("/api/hotspot");
    hotspotRow.classList.toggle("hidden", !s.configured);
    hotspotToggle.checked = !!s.active;
  } catch (e) {
    hotspotRow.classList.add("hidden");
  }
}

hotspotToggle.addEventListener("change", async () => {
  const enabled = hotspotToggle.checked;
  if (!enabled && !confirm("Turn off the hotspot? You may lose your connection.")) {
    hotspotToggle.checked = true;
    return;
  }
  try {
    const s = await api("/api/hotspot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    hotspotToggle.checked = !!s.active;
    showToast(enabled ? "Hotspot on" : "Hotspot off");
  } catch (e) {
    // Request may not return if we just cut our own link; restore best-guess.
    hotspotToggle.checked = !enabled;
    showToast("Hotspot: " + e.message);
  }
});

// --------------------------------------------------------------------- //
// Settings form
// --------------------------------------------------------------------- //
let lastSettings = null;
const lensRow = $("lens-row");

function syncSettingsForm(s) {
  if (JSON.stringify(s) === JSON.stringify(lastSettings)) return;
  lastSettings = JSON.parse(JSON.stringify(s));
  $("set-resolution").value = s.resolution;
  $("set-fps").value = String(s.fps);
  $("set-autofocus").value = s.autofocus;
  $("set-lens").value = s.lens_position;
  $("set-rotation").value = String(s.rotation);
  $("set-hflip").checked = s.hflip;
  $("set-vflip").checked = s.vflip;
  lensRow.classList.toggle("hidden", s.autofocus !== "manual");
}

async function pushSettings() {
  const body = {
    resolution: $("set-resolution").value,
    fps: parseInt($("set-fps").value, 10),
    autofocus: $("set-autofocus").value,
    lens_position: parseFloat($("set-lens").value),
    rotation: parseInt($("set-rotation").value, 10),
    hflip: $("set-hflip").checked,
    vflip: $("set-vflip").checked,
  };
  lensRow.classList.toggle("hidden", body.autofocus !== "manual");
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    lastSettings = null; // force re-sync on next poll
    // Resolution/rotation change restarts the camera; refresh the stream.
    startStream();
    showToast("Settings applied");
  } catch (e) {
    showToast(e.message);
  }
}

[
  "set-resolution", "set-fps", "set-autofocus",
  "set-rotation", "set-hflip", "set-vflip",
].forEach((id) => $(id).addEventListener("change", pushSettings));
$("set-lens").addEventListener("change", pushSettings);

// Tap-to-focus when in single-AF mode
$("viewport").addEventListener("click", (e) => {
  // (Region-of-interest AF could be added here; for now single-AF only.)
});

// --------------------------------------------------------------------- //
// People analytics (Hailo)
// --------------------------------------------------------------------- //
const overlay = $("overlay");
const octx = overlay.getContext("2d");
const statsHud = $("stats-hud");
const analyticsToggle = $("set-analytics");
const analyticsStatus = $("analytics-status");
const aiBtn = $("ai-btn");
let analyticsOn = false;

async function setAnalytics(enabled) {
  try {
    const s = await api("/api/analytics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    showToast(s.enabled ? "AI on" : "AI off — saving battery");
    pollAnalytics();
  } catch (e) {
    showToast("AI: " + e.message);
  }
}

function drawOverlay(boxes) {
  const w = overlay.clientWidth, h = overlay.clientHeight;
  if (overlay.width !== w || overlay.height !== h) {
    overlay.width = w;
    overlay.height = h;
  }
  octx.clearRect(0, 0, w, h);
  octx.lineWidth = 2;
  octx.font = "12px system-ui, sans-serif";
  for (const b of boxes) {
    const x = b.x * w, y = b.y * h, bw = b.w * w, bh = b.h * h;
    const color = b.looking ? "#3ddc72" : "#4f8cff";
    octx.strokeStyle = color;
    octx.fillStyle = color;
    octx.strokeRect(x, y, bw, bh);
    octx.fillText("#" + b.id + (b.looking ? " •" : ""), x + 2, Math.max(10, y - 3));
  }
}

const calibBadge = $("calib-badge");
const calibTime = $("calib-time");
const calibOffsets = $("calib-offsets");

async function pollAnalytics() {
  let s;
  try {
    s = await api("/api/analytics");
  } catch (e) {
    return;
  }
  analyticsToggle.checked = s.enabled;
  analyticsOn = s.enabled;
  aiBtn.classList.toggle("active", s.enabled);
  aiBtn.title = s.enabled
    ? "AI on — tap to turn off and save battery"
    : "AI off — tap to enable people analytics";
  if (s.available && s.enabled) {
    statsHud.classList.remove("hidden");
    $("stat-live").textContent = s.live_count;
    $("stat-looking").textContent = s.looking_now;
    $("stat-unique").textContent = s.unique_total;
    $("stat-viewed").textContent = s.viewed_total;
    $("stat-dwell").textContent = s.avg_attention + "s";
    drawOverlay(s.boxes);
  } else {
    statsHud.classList.add("hidden");
    octx.clearRect(0, 0, overlay.width, overlay.height);
  }
  if (analyticsStatus) {
    analyticsStatus.textContent = s.error
      ? "Error: " + s.error
      : s.enabled
      ? `Running · ${s.fps} fps`
      : s.available
      ? "Paused"
      : "Hailo not ready";
  }
  if (calibOffsets) {
    calibOffsets.textContent =
      `Current offset: yaw ${s.yaw_offset > 0 ? "+" : ""}${s.yaw_offset}°, ` +
      `pitch ${s.pitch_offset > 0 ? "+" : ""}${s.pitch_offset}°`;
  }
  if (s.calibrating) {
    calibBadge.classList.remove("hidden");
    calibTime.textContent = Math.ceil(s.calib_remaining) + "s";
  } else {
    calibBadge.classList.add("hidden");
  }
}

analyticsToggle.addEventListener("change", () => setAnalytics(analyticsToggle.checked));
aiBtn.addEventListener("click", () => setAnalytics(!analyticsOn));

$("analytics-reset").addEventListener("click", async () => {
  try {
    await api("/api/analytics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reset: true }),
    });
    showToast("Count reset");
    pollAnalytics();
  } catch (e) {
    showToast("Reset failed: " + e.message);
  }
});

$("analytics-calibrate").addEventListener("click", async () => {
  try {
    if (!analyticsOn) await setAnalytics(true);
    await api("/api/analytics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ calibrate: true, seconds: 5 }),
    });
    showToast("Look at the ad now…");
  } catch (e) {
    showToast("Calibrate failed: " + e.message);
  }
});

// --------------------------------------------------------------------- //
// Gallery
// --------------------------------------------------------------------- //
async function loadGallery() {
  const grid = $("gallery-grid");
  grid.innerHTML = "";
  let items = [];
  try {
    items = await api("/api/media");
  } catch (e) {
    showToast("Gallery error: " + e.message);
  }
  $("gallery-empty").classList.toggle("hidden", items.length > 0);
  for (const it of items) {
    const cell = document.createElement("div");
    cell.className = "thumb";
    const url = `/media/${it.kind}/${encodeURIComponent(it.name)}`;
    if (it.kind === "photo") {
      cell.innerHTML = `<img loading="lazy" src="${url}" alt="${it.name}">`;
    } else {
      cell.innerHTML =
        `<video preload="metadata" src="${url}#t=0.1"></video>` +
        `<span class="vid-tag">▶ ${fmtSize(it.size)}</span>`;
    }
    cell.addEventListener("click", () => openLightbox(it));
    grid.appendChild(cell);
  }
}

// --------------------------------------------------------------------- //
// Lightbox
// --------------------------------------------------------------------- //
const lightbox = $("lightbox");
let currentItem = null;

function openLightbox(it) {
  currentItem = it;
  const url = `/media/${it.kind}/${encodeURIComponent(it.name)}`;
  const body = $("lightbox-body");
  body.innerHTML =
    it.kind === "photo"
      ? `<img src="${url}">`
      : `<video src="${url}" controls autoplay playsinline></video>`;
  const dl = $("lb-download");
  dl.href = url + "?download=1";
  dl.setAttribute("download", it.name);
  lightbox.classList.remove("hidden");
}

function closeLightbox() {
  lightbox.classList.add("hidden");
  $("lightbox-body").innerHTML = "";
  currentItem = null;
}
document.querySelectorAll("[data-lb-close]").forEach((b) =>
  b.addEventListener("click", closeLightbox)
);
lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) closeLightbox();
});

$("lb-delete").addEventListener("click", async () => {
  if (!currentItem) return;
  if (!confirm("Delete " + currentItem.name + "?")) return;
  try {
    await api(
      `/api/media/${currentItem.kind}/${encodeURIComponent(currentItem.name)}`,
      { method: "DELETE" }
    );
    closeLightbox();
    loadGallery();
    showToast("Deleted");
  } catch (e) {
    showToast("Delete failed: " + e.message);
  }
});

// --------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------- //
preview.addEventListener("error", () => {
  // Stream dropped (e.g. camera restart) — retry shortly.
  setTimeout(startStream, 1500);
});

startStream();
poll();
setInterval(poll, 1000);
pollAnalytics();
setInterval(pollAnalytics, 250);
