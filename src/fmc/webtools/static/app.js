// Find My Car — floor plan editor front end.
// Talks only to /api/* on this same server (fmc/webtools/server.py), which
// itself reuses the same fmc modules the CLI scripts use. No build step;
// plain fetch() + canvas.

const state = {
  siteId: null,
  floor: null,
  mode: "view",
  img: null,
  imgW: 0,
  imgH: 0,
  zoom: 1,
  panX: 0,
  panY: 0,
  dragging: false,
  dragStart: null,
  controlPoints: [],   // {px,py,x,y}
  segments: [],        // {px1,py1,px2,py2}
  pendingSegmentPt: null,
  slots: [],           // {px,py,slot_id,zone}
  ingestedLocations: [],
  hasTransform: false,
  queryResult: null,
  routeResult: null,
  overlays: { locations: true, segments: true, slots: true },
  captureLocations: [],       // [{location_id, zone, floor, x, y, px, py, photo_count}]
  selectedLocationId: null,
  selectedLocationPhotos: [], // [{image_id, heading_degrees, photo_url, direction_px}]
  pendingNewLocationPoint: null, // {px, py} while the "new location" form is open
};

const canvas = document.getElementById("mapCanvas");
const ctx = canvas.getContext("2d");

// ---------------------------------------------------------------- API helpers

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res;
}

function setStatus(msg, isError) {
  const el = document.getElementById("statusMsg");
  el.textContent = msg || "";
  el.style.color = isError ? "#ff6b6b" : "#ffd479";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 6000);
}

// ---------------------------------------------------------------- coordinate math

function screenToImage(sx, sy) {
  return { x: (sx - state.panX) / state.zoom, y: (sy - state.panY) / state.zoom };
}
function imageToScreen(ix, iy) {
  return { x: ix * state.zoom + state.panX, y: iy * state.zoom + state.panY };
}

// ---------------------------------------------------------------- rendering

function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#444";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  if (!state.img) return;

  ctx.save();
  ctx.translate(state.panX, state.panY);
  ctx.scale(state.zoom, state.zoom);
  ctx.drawImage(state.img, 0, 0);
  ctx.restore();

  // Ingested capture locations (read-only overlay)
  if (state.overlays.locations) {
    for (const loc of state.ingestedLocations) {
      if (loc.px === undefined) continue;
      const s = imageToScreen(loc.px, loc.py);
      drawDot(s.x, s.y, "#2ecc71", 5);
    }
  }

  // Saved walkable segments (from server, pixel coords already resolved)
  if (state.overlays.segments) {
    for (const seg of state.savedSegments || []) {
      if (seg.px1 === undefined) continue;
      const a = imageToScreen(seg.px1, seg.py1);
      const b = imageToScreen(seg.px2, seg.py2);
      drawLine(a, b, "#3498db", 3);
    }
  }
  // Saved vehicle slots
  if (state.overlays.slots) {
    for (const slot of state.savedSlots || []) {
      if (slot.px === undefined) continue;
      const s = imageToScreen(slot.px, slot.py);
      drawSquare(s.x, s.y, "#e67e22", 7);
      drawLabel(s.x + 8, s.y - 8, slot.slot_id, "#e67e22");
    }
  }

  // In-progress editing state, drawn per mode
  if (state.mode === "control_points") {
    state.controlPoints.forEach((cp, i) => {
      const s = imageToScreen(cp.px, cp.py);
      drawDot(s.x, s.y, "#ff4757", 6);
      drawLabel(s.x + 8, s.y - 8, String(i), "#ff4757");
    });
  }
  if (state.mode === "segments") {
    state.segments.forEach((seg) => {
      const a = imageToScreen(seg.px1, seg.py1);
      const b = imageToScreen(seg.px2, seg.py2);
      drawLine(a, b, "#00d2ff", 3);
      drawDot(a.x, a.y, "#00d2ff", 5);
      drawDot(b.x, b.y, "#00d2ff", 5);
    });
    if (state.pendingSegmentPt) {
      const s = imageToScreen(state.pendingSegmentPt.px, state.pendingSegmentPt.py);
      drawDot(s.x, s.y, "#ffdd00", 6);
    }
  }
  if (state.mode === "slots") {
    state.slots.forEach((slot) => {
      const s = imageToScreen(slot.px, slot.py);
      drawSquare(s.x, s.y, "#ffdd00", 7);
      drawLabel(s.x + 8, s.y - 8, slot.slot_id || "?", "#ffdd00");
    });
  }

  // Query result marker
  if (state.queryResult && state.queryResult.matched && state.queryResult.px !== undefined) {
    const s = imageToScreen(state.queryResult.px, state.queryResult.py);
    drawStar(s.x, s.y, "#ff00ff", 10);
  }
  // Route
  if (state.routeResult && state.routeResult.waypoints_px) {
    const pts = state.routeResult.waypoints_px.map(([x, y]) => imageToScreen(x, y));
    for (let i = 0; i < pts.length - 1; i++) drawLine(pts[i], pts[i + 1], "#ff00ff", 3);
    pts.forEach((p) => drawDot(p.x, p.y, "#ff00ff", 4));
  }

  // Capture locations mode: all locations as markers, selected one gets
  // heading-direction arrows for each of its photos.
  if (state.mode === "capture_locations") {
    for (const loc of state.captureLocations) {
      if (loc.px === undefined) continue;
      const isSelected = loc.location_id === state.selectedLocationId;
      const s = imageToScreen(loc.px, loc.py);
      drawDot(s.x, s.y, isSelected ? "#ff4757" : "#9b59b6", isSelected ? 7 : 5);
      drawLabel(s.x + 9, s.y - 9, `${loc.location_id} (${loc.photo_count})`, isSelected ? "#ff4757" : "#9b59b6");
    }
    if (state.selectedLocationId) {
      const loc = state.captureLocations.find((l) => l.location_id === state.selectedLocationId);
      if (loc) {
        const base = imageToScreen(loc.px, loc.py);
        const arrowLen = 28;
        for (const photo of state.selectedLocationPhotos) {
          if (!photo.direction_px) continue;
          const tip = { x: base.x + photo.direction_px.dx * arrowLen, y: base.y + photo.direction_px.dy * arrowLen };
          drawLine(base, tip, "#00e0c0", 2);
          drawDot(tip.x, tip.y, "#00e0c0", 3);
        }
      }
    }
    if (state.pendingNewLocationPoint) {
      const s = imageToScreen(state.pendingNewLocationPoint.px, state.pendingNewLocationPoint.py);
      drawDot(s.x, s.y, "#ffdd00", 7);
    }
  }
}

function drawDot(x, y, color, r) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 1.5;
  ctx.stroke();
}
function drawSquare(x, y, color, r) {
  ctx.fillStyle = color;
  ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
  ctx.strokeStyle = "#fff";
  ctx.strokeRect(x - r, y - r, 2 * r, 2 * r);
}
function drawLine(a, b, color, w) {
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.strokeStyle = color;
  ctx.lineWidth = w;
  ctx.stroke();
}
function drawLabel(x, y, text, color) {
  ctx.font = "12px sans-serif";
  ctx.fillStyle = "#000";
  ctx.fillText(text, x + 1, y + 1);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}
function drawStar(x, y, color, r) {
  ctx.beginPath();
  for (let i = 0; i < 5; i++) {
    const angle = (i * 2 * Math.PI) / 5 - Math.PI / 2;
    const px = x + r * Math.cos(angle);
    const py = y + r * Math.sin(angle);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.fill();
  ctx.strokeStyle = "#fff";
  ctx.stroke();
}

// ---------------------------------------------------------------- canvas interaction

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const before = screenToImage(mx, my);
  const factor = e.deltaY < 0 ? 1.1 : 0.9;
  state.zoom = Math.max(0.05, Math.min(20, state.zoom * factor));
  const after = imageToScreen(before.x, before.y);
  state.panX += mx - after.x;
  state.panY += my - after.y;
  draw();
});

canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("mousedown", (e) => {
  if (e.button === 2) { // right-drag pans
    state.dragging = true;
    state.dragStart = { x: e.clientX, y: e.clientY, panX: state.panX, panY: state.panY };
  }
});
window.addEventListener("mousemove", (e) => {
  if (state.dragging) {
    state.panX = state.dragStart.panX + (e.clientX - state.dragStart.x);
    state.panY = state.dragStart.panY + (e.clientY - state.dragStart.y);
    draw();
  }
});
window.addEventListener("mouseup", () => { state.dragging = false; });

canvas.addEventListener("click", (e) => {
  if (!state.img) return;
  const rect = canvas.getBoundingClientRect();
  const p = screenToImage(e.clientX - rect.left, e.clientY - rect.top);
  handleClick(p.x, p.y);
});

function handleClick(px, py) {
  if (state.mode === "control_points") {
    state.controlPoints.push({ px, py, x: 0, y: 0 });
    renderControlPointsPanel();
  } else if (state.mode === "segments") {
    if (!state.pendingSegmentPt) {
      state.pendingSegmentPt = { px, py };
    } else {
      state.segments.push({ px1: state.pendingSegmentPt.px, py1: state.pendingSegmentPt.py, px2: px, py2: py });
      state.pendingSegmentPt = null;
      renderSegmentsPanel();
    }
  } else if (state.mode === "slots") {
    state.slots.push({ px, py, slot_id: "", zone: "" });
    renderSlotsPanel();
  } else if (state.mode === "capture_locations") {
    handleCaptureLocationClick(px, py);
  }
  draw();
}

// ---------------------------------------------------------------- mode switching

document.querySelectorAll(".mode-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mode-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.mode = btn.dataset.mode;
    state.pendingSegmentPt = null;
    state.pendingNewLocationPoint = null;
    document.getElementById("newLocationForm").style.display = "none";
    document.querySelectorAll(".panel").forEach((p) => (p.style.display = "none"));
    document.getElementById("panel-" + state.mode).style.display = "block";
    if (state.mode === "capture_locations") loadCaptureLocations();
    draw();
  });
});

document.getElementById("toggleLocations").addEventListener("change", (e) => { state.overlays.locations = e.target.checked; draw(); });
document.getElementById("toggleSegments").addEventListener("change", (e) => { state.overlays.segments = e.target.checked; draw(); });
document.getElementById("toggleSlots").addEventListener("change", (e) => { state.overlays.slots = e.target.checked; draw(); });

// ---------------------------------------------------------------- side panels

function renderControlPointsPanel() {
  const el = document.getElementById("controlPointsList");
  el.innerHTML = "";
  state.controlPoints.forEach((cp, i) => {
    const row = document.createElement("div");
    row.className = "list-item control-point-row";
    row.innerHTML = `
      <div class="point-reference"><strong>Point ${i + 1}</strong><span>px ${cp.px.toFixed(0)} · py ${cp.py.toFixed(0)}</span></div>
      <div class="coordinate-fields">
        <label>Real X <input type="number" step="any" value="${cp.x}" data-i="${i}" data-f="x" aria-label="Point ${i + 1} real-world X coordinate in metres"></label>
        <label>Real Y <input type="number" step="any" value="${cp.y}" data-i="${i}" data-f="y" aria-label="Point ${i + 1} real-world Y coordinate in metres"></label>
      </div>
      <button class="del-btn" data-i="${i}" title="Remove point ${i + 1}" aria-label="Remove point ${i + 1}">×</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      const i = +e.target.dataset.i, f = e.target.dataset.f;
      state.controlPoints[i][f] = parseFloat(e.target.value) || 0;
    });
  });
  el.querySelectorAll(".del-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      state.controlPoints.splice(+e.target.dataset.i, 1);
      renderControlPointsPanel();
      draw();
    });
  });
}

function renderSegmentsPanel() {
  const el = document.getElementById("segmentsList");
  el.innerHTML = "";
  state.segments.forEach((seg, i) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `#${i} (${seg.px1.toFixed(0)},${seg.py1.toFixed(0)}) → (${seg.px2.toFixed(0)},${seg.py2.toFixed(0)})
      <button class="del-btn" data-i="${i}">×</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll(".del-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      state.segments.splice(+e.target.dataset.i, 1);
      renderSegmentsPanel();
      draw();
    });
  });
}

function renderSlotsPanel() {
  const el = document.getElementById("slotsList");
  el.innerHTML = "";
  state.slots.forEach((slot, i) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `
      #${i}
      <input type="text" placeholder="slot_id" value="${slot.slot_id}" data-i="${i}" data-f="slot_id" style="width:70px">
      <input type="text" placeholder="zone" value="${slot.zone}" data-i="${i}" data-f="zone" style="width:60px">
      <button class="del-btn" data-i="${i}">×</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      const i = +e.target.dataset.i, f = e.target.dataset.f;
      state.slots[i][f] = e.target.value;
    });
  });
  el.querySelectorAll(".del-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      state.slots.splice(+e.target.dataset.i, 1);
      renderSlotsPanel();
      draw();
    });
  });
}

document.getElementById("undoSegmentBtn").addEventListener("click", () => {
  state.segments.pop();
  renderSegmentsPanel();
  draw();
});

// ---------------------------------------------------------------- sites / floors

async function refreshSites() {
  const sites = await api("GET", "/api/sites");
  const sel = document.getElementById("siteSelect");
  sel.innerHTML = sites.map((s) => `<option value="${s}">${s}</option>`).join("");
  if (sites.length && !state.siteId) await selectSite(sites[0]);
}

document.getElementById("createSiteBtn").addEventListener("click", async () => {
  const id = document.getElementById("newSiteId").value.trim();
  if (!id) return;
  try {
    await api("POST", `/api/sites/${encodeURIComponent(id)}`);
    await refreshSites();
    document.getElementById("siteSelect").value = id;
    await selectSite(id);
    setStatus(`Created site '${id}'`);
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("siteSelect").addEventListener("change", (e) => selectSite(e.target.value));

async function selectSite(siteId) {
  state.siteId = siteId;
  const floors = await api("GET", `/api/sites/${siteId}/floors`);
  const sel = document.getElementById("floorSelect");
  sel.innerHTML = floors.map((f) => `<option value="${f}">${f}</option>`).join("");
  await selectFloor(floors[0] ?? 1);
}

document.getElementById("createFloorBtn").addEventListener("click", async () => {
  const n = parseInt(document.getElementById("newFloorNum").value, 10);
  if (Number.isNaN(n)) return;
  await api("POST", `/api/sites/${state.siteId}/floors/${n}`);
  await selectSite(state.siteId);
  document.getElementById("floorSelect").value = n;
  await selectFloor(n);
});

document.getElementById("floorSelect").addEventListener("change", (e) => selectFloor(+e.target.value));

async function selectFloor(floor) {
  state.floor = floor;
  state.controlPoints = [];
  state.segments = [];
  state.slots = [];
  state.queryResult = null;
  state.routeResult = null;
  state.captureLocations = [];
  state.selectedLocationId = null;
  state.selectedLocationPhotos = [];
  state.pendingNewLocationPoint = null;
  document.getElementById("newLocationForm").style.display = "none";
  document.getElementById("selectedLocationPanel").style.display = "none";
  await loadFloorplanImage();
  await loadControlPoints();
  await checkTransform();
  await loadGeometry();
  await loadIngestedLocations();
  if (state.mode === "capture_locations") await loadCaptureLocations();
  draw();
}

// ---------------------------------------------------------------- floor plan

document.getElementById("uploadPdfBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("pdfFile");
  if (!fileInput.files.length || !state.siteId) { setStatus("Pick a site and a PDF first", true); return; }
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  const page = document.getElementById("pdfPage").value || 0;
  const dpi = document.getElementById("pdfDpi").value || 200;
  setStatus("Rendering PDF...");
  try {
    const res = await fetch(`/api/sites/${state.siteId}/floorplan?page=${page}&dpi=${dpi}`, { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail);
    setStatus("Floor plan rendered");
    await loadFloorplanImage();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function loadFloorplanImage() {
  if (!state.siteId) return;
  const img = new Image();
  await new Promise((resolve) => {
    img.onload = () => {
      state.img = img;
      state.imgW = img.width;
      state.imgH = img.height;
      document.getElementById("canvasHint").style.display = "none";
      fitToCanvas();
      resolve();
    };
    img.onerror = () => {
      state.img = null;
      document.getElementById("canvasHint").style.display = "block";
      resolve();
    };
    img.src = `/api/sites/${state.siteId}/floorplan/image?_=${Date.now()}`;
  });
}

function fitToCanvas() {
  if (!state.img) return;
  const scale = Math.min(canvas.width / state.imgW, canvas.height / state.imgH) * 0.95;
  state.zoom = scale;
  state.panX = (canvas.width - state.imgW * scale) / 2;
  state.panY = (canvas.height - state.imgH * scale) / 2;
}

// ---------------------------------------------------------------- control points / transform

async function loadControlPoints() {
  const points = await api("GET", `/api/sites/${state.siteId}/control-points`);
  state.controlPoints = points;
  renderControlPointsPanel();
}

document.getElementById("saveControlPointsBtn").addEventListener("click", async () => {
  try {
    await api("PUT", `/api/sites/${state.siteId}/control-points`, state.controlPoints);
    setStatus("Control points saved");
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("fitTransformBtn").addEventListener("click", async () => {
  try {
    await api("PUT", `/api/sites/${state.siteId}/control-points`, state.controlPoints);
    const result = await api("POST", `/api/sites/${state.siteId}/control-points/fit`);
    renderFitResult(result);
    document.getElementById("saveTransformBtn").disabled = false;
  } catch (e) {
    document.getElementById("fitResult").innerHTML = `<span class="warn">${e.message}</span>`;
    document.getElementById("saveTransformBtn").disabled = true;
  }
});

function renderFitResult(result) {
  const el = document.getElementById("fitResult");
  let html = `<div>Max residual: <b>${result.max_residual.toFixed(3)}m</b></div>`;
  html += `<div>Implied scale: <b>${result.implied_scale_x.toFixed(4)}, ${result.implied_scale_y.toFixed(4)}</b> m/pixel</div>`;
  if (result.warnings.length) {
    html += result.warnings.map((w) => `<div class="warn">⚠ ${w}</div>`).join("");
  } else {
    html += `<div class="ok">✓ Looks reasonable</div>`;
  }
  el.innerHTML = html;
}

document.getElementById("saveTransformBtn").addEventListener("click", async () => {
  try {
    await api("POST", `/api/sites/${state.siteId}/control-points/fit/save`);
    setStatus("Transform saved");
    await checkTransform();
    await loadGeometry();
    await loadIngestedLocations();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function checkTransform() {
  try {
    await api("GET", `/api/sites/${state.siteId}/transform`);
    state.hasTransform = true;
  } catch (e) {
    state.hasTransform = false;
  }
}

// ---------------------------------------------------------------- geometry

async function loadGeometry() {
  if (!state.floor) return;
  try {
    const geo = await api("GET", `/api/sites/${state.siteId}/floors/${state.floor}/geometry`);
    state.savedSegments = geo.segments;
    state.savedSlots = geo.slots;
  } catch (e) {
    state.savedSegments = [];
    state.savedSlots = [];
  }
}

document.getElementById("saveSegmentsBtn").addEventListener("click", async () => {
  try {
    await api("PUT", `/api/sites/${state.siteId}/floors/${state.floor}/segments`, state.segments);
    setStatus("Segments saved");
    state.segments = [];
    renderSegmentsPanel();
    await loadGeometry();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("saveSlotsBtn").addEventListener("click", async () => {
  try {
    await api("PUT", `/api/sites/${state.siteId}/floors/${state.floor}/slots`, state.slots);
    setStatus("Slots saved");
    state.slots = [];
    renderSlotsPanel();
    await loadGeometry();
    await refreshRouteSlotOptions();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function loadIngestedLocations() {
  try {
    state.ingestedLocations = await api("GET", `/api/sites/${state.siteId}/locations`);
  } catch (e) {
    state.ingestedLocations = [];
  }
}

// ---------------------------------------------------------------- capture locations + photos

async function loadCaptureLocations() {
  if (!state.siteId || !state.floor) return;
  try {
    state.captureLocations = await api("GET", `/api/sites/${state.siteId}/floors/${state.floor}/capture-locations`);
  } catch (e) {
    state.captureLocations = [];
  }
  draw();
}

function handleCaptureLocationClick(px, py) {
  // Hit-test existing locations in SCREEN space (not raw image pixels) so
  // the click tolerance feels consistent regardless of zoom level.
  const clickScreen = imageToScreen(px, py);
  const TOLERANCE_PX = 16;

  for (const loc of state.captureLocations) {
    if (loc.px === undefined) continue;
    const locScreen = imageToScreen(loc.px, loc.py);
    const dist = Math.hypot(locScreen.x - clickScreen.x, locScreen.y - clickScreen.y);
    if (dist <= TOLERANCE_PX) {
      selectCaptureLocation(loc.location_id);
      return;
    }
  }

  // No nearby existing location -- open the "create new" form at this point.
  state.pendingNewLocationPoint = { px, py };
  state.selectedLocationId = null;
  document.getElementById("selectedLocationPanel").style.display = "none";
  document.getElementById("newLocationForm").style.display = "block";
  document.getElementById("newLocationId").value = `loc_${String(state.captureLocations.length + 1).padStart(2, "0")}`;
  document.getElementById("newLocationZone").value = state._lastZoneUsed || "";
  document.getElementById("newLocationId").focus();
}

document.getElementById("createLocationBtn").addEventListener("click", async () => {
  if (!state.pendingNewLocationPoint) return;
  const locationId = document.getElementById("newLocationId").value.trim();
  const zone = document.getElementById("newLocationZone").value.trim();
  if (!locationId) { setStatus("Location ID is required", true); return; }
  if (!zone) { setStatus("Zone is required", true); return; }
  try {
    await api("POST", `/api/sites/${state.siteId}/floors/${state.floor}/capture-locations`, {
      location_id: locationId, zone, px: state.pendingNewLocationPoint.px, py: state.pendingNewLocationPoint.py,
    });
    state._lastZoneUsed = zone;
    state.pendingNewLocationPoint = null;
    document.getElementById("newLocationForm").style.display = "none";
    await loadCaptureLocations();
    await selectCaptureLocation(locationId);
    setStatus(`Created location '${locationId}'`);
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("cancelNewLocationBtn").addEventListener("click", () => {
  state.pendingNewLocationPoint = null;
  document.getElementById("newLocationForm").style.display = "none";
  draw();
});

async function selectCaptureLocation(locationId) {
  state.selectedLocationId = locationId;
  state.pendingNewLocationPoint = null;
  document.getElementById("newLocationForm").style.display = "none";

  const loc = state.captureLocations.find((l) => l.location_id === locationId);
  document.getElementById("selectedLocationId").textContent = locationId;
  document.getElementById("selectedLocationZone").textContent = loc ? `Zone: ${loc.zone}` : "";
  document.getElementById("selectedLocationPanel").style.display = "block";

  await loadLocationPhotos();
  draw();
}

async function loadLocationPhotos() {
  if (!state.selectedLocationId) return;
  try {
    state.selectedLocationPhotos = await api("GET", `/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}/photos`);
  } catch (e) {
    state.selectedLocationPhotos = [];
  }
  renderPhotoGrid();
}

function renderPhotoGrid() {
  const el = document.getElementById("photoGrid");
  el.innerHTML = "";
  state.selectedLocationPhotos.forEach((photo) => {
    const card = document.createElement("div");
    card.className = "photo-card";
    card.innerHTML = `
      <img src="${photo.photo_url}" alt="${photo.image_id}" loading="lazy">
      <div class="photo-card-controls">
        <label>Heading °
          <input type="number" min="0" max="360" step="1" value="${photo.heading_degrees}" data-image-id="${photo.image_id}" class="heading-input">
        </label>
        <span class="unsaved-badge" style="display:none">unsaved</span>
        <button class="del-btn photo-del-btn" data-image-id="${photo.image_id}" title="Remove this photo">×</button>
      </div>`;
    el.appendChild(card);
  });

  el.querySelectorAll(".heading-input").forEach((input) => {
    input.addEventListener("input", (e) => {
      e.target.closest(".photo-card-controls").querySelector(".unsaved-badge").style.display = "inline";
    });
    input.addEventListener("change", async (e) => {
      const imageId = e.target.dataset.imageId;
      const heading = parseFloat(e.target.value);
      if (Number.isNaN(heading) || heading < 0 || heading > 360) {
        setStatus("Heading must be between 0 and 360", true);
        return;
      }
      try {
        const updated = await api("PATCH", `/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}/photos/${imageId}`, { heading_degrees: heading });
        const photoIdx = state.selectedLocationPhotos.findIndex((p) => p.image_id === imageId);
        if (photoIdx >= 0) state.selectedLocationPhotos[photoIdx] = updated;
        e.target.closest(".photo-card-controls").querySelector(".unsaved-badge").style.display = "none";
        draw();
      } catch (err) { setStatus(err.message, true); }
    });
  });

  el.querySelectorAll(".photo-del-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      const imageId = e.target.dataset.imageId;
      try {
        await api("DELETE", `/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}/photos/${imageId}`);
        await loadLocationPhotos();
        await loadCaptureLocations();
      } catch (err) { setStatus(err.message, true); }
    });
  });
}

document.getElementById("addPhotoInput").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  if (!files.length || !state.selectedLocationId) return;
  const statusEl = document.getElementById("photoUploadStatus");

  let uploaded = 0;
  for (const file of files) {
    if (!["image/jpeg", "image/png"].includes(file.type)) {
      statusEl.textContent = `Skipped '${file.name}': unsupported type (only JPEG/PNG)`;
      continue;
    }
    statusEl.textContent = `Uploading ${uploaded + 1}/${files.length}...`;
    const form = new FormData();
    form.append("file", file);
    form.append("heading_degrees", "0");
    try {
      const res = await fetch(`/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}/photos`, { method: "POST", body: form });
      if (!res.ok) throw new Error((await res.json()).detail);
      uploaded++;
    } catch (err) {
      statusEl.textContent = `Failed on '${file.name}': ${err.message}`;
    }
  }
  statusEl.textContent = `Added ${uploaded}/${files.length} photo(s). Set each photo's heading above.`;
  e.target.value = ""; // allow re-selecting the same file(s) later
  await loadLocationPhotos();
  await loadCaptureLocations();
});

document.getElementById("deleteLocationBtn").addEventListener("click", async () => {
  if (!state.selectedLocationId) return;
  if (!confirm(`Delete location '${state.selectedLocationId}' and all ${state.selectedLocationPhotos.length} of its photos? This cannot be undone.`)) return;
  try {
    await api("DELETE", `/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}`);
    state.selectedLocationId = null;
    state.selectedLocationPhotos = [];
    document.getElementById("selectedLocationPanel").style.display = "none";
    await loadCaptureLocations();
    setStatus("Location deleted");
    draw();
  } catch (e) { setStatus(e.message, true); }
});

// ---------------------------------------------------------------- test query + route

document.getElementById("runQueryBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("queryFile");
  if (!fileInput.files.length) { setStatus("Pick a photo first", true); return; }
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  setStatus("Localizing...");
  try {
    const res = await fetch(`/api/sites/${state.siteId}/query`, { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail);
    const result = await res.json();
    state.queryResult = result;
    const el = document.getElementById("queryResult");
    if (result.matched) {
      el.textContent = `Matched ${result.image_id}\nfloor=${result.floor} zone=${result.zone}\nx=${result.x.toFixed(2)} y=${result.y.toFixed(2)}\nsimilarity=${result.similarity.toFixed(3)} inlier_ratio=${result.inlier_ratio.toFixed(3)}`;
      await refreshRouteSlotOptions();
      document.getElementById("runRouteBtn").disabled = state.savedSlots.length === 0;
    } else {
      el.textContent = "NO MATCH";
      document.getElementById("runRouteBtn").disabled = true;
    }
    setStatus("");
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function refreshRouteSlotOptions() {
  const sel = document.getElementById("routeSlotSelect");
  sel.innerHTML = (state.savedSlots || []).map((s) => `<option value="${s.slot_id}">${s.slot_id} (${s.zone})</option>`).join("");
}

document.getElementById("runRouteBtn").addEventListener("click", async () => {
  if (!state.queryResult || !state.queryResult.matched) { setStatus("Run a query first", true); return; }
  const slotId = document.getElementById("routeSlotSelect").value;
  try {
    const result = await api("POST", `/api/sites/${state.siteId}/route`, {
      floor: state.queryResult.floor,
      start_x: state.queryResult.x,
      start_y: state.queryResult.y,
      slot_id: slotId,
    });
    state.routeResult = result;
    document.getElementById("routeResult").textContent = `Route to ${slotId}: ${result.total_distance.toFixed(1)}m, ${result.waypoints.length} waypoints`;
    draw();
  } catch (e) { setStatus(e.message, true); }
});

// ---------------------------------------------------------------- init

refreshSites();
draw();
