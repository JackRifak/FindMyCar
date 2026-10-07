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
  transform: null,            // {a,b,c,d,tx,ty} from transform.json
  probe: null,                // {px, py, x, y} selected point in facility metres
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

// keep in sync with fmc/fusion/map_matching.py DEFAULT_CORRIDOR_WIDTH_M
const DEFAULT_CORRIDOR_WIDTH_M = 3;

/** floor-plan pixels per metre: from the fitted transform, else from saved segments */
function pxPerMeter() {
  const t = state.transform;
  if (t && [t.a, t.b, t.c, t.d].every(Number.isFinite)) {
    const mPerPx = Math.sqrt(Math.abs(t.a * t.d - t.b * t.c));
    if (mPerPx > 0) return 1 / mPerPx;
  }
  return state.pxPerMeter || null;
}

function segWidthM(seg) {
  return Number.isFinite(seg.width) && seg.width > 0 ? seg.width : DEFAULT_CORRIDOR_WIDTH_M;
}

/**
 * Bands can be asymmetric: width_left / width_right = metres left / right of the line
 * looking from (x1,y1) to (x2,y2) in MAP coords (y up) — same as the server.
 * Image y points down, so which screen side is map-"left" depends on the georeference.
 */
function sideKey(screenSide) {
  const t = state.transform;
  const det = t ? t.a * t.d - t.b * t.c : -1; // usual plan: y flipped → det < 0
  return screenSide * Math.sign(det || -1) > 0 ? "width_left" : "width_right";
}

/** metres from the line to the band edge on screen side +1 / -1 */
function sideWidthM(seg, screenSide) {
  const v = seg[sideKey(screenSide)];
  return Number.isFinite(v) && v >= 0 ? v : segWidthM(seg) / 2;
}

function totalWidthM(seg) {
  return sideWidthM(seg, 1) + sideWidthM(seg, -1);
}

/**
 * band geometry on screen for a segment: line a→b, unit normal n, edge offsets in px
 * (hp on side +1, hm on side -1). null when the scale isn't known yet.
 */
function bandScreen(seg) {
  const ppm = pxPerMeter();
  if (!ppm || seg.px1 === undefined) return null;
  const a = imageToScreen(seg.px1, seg.py1);
  const b = imageToScreen(seg.px2, seg.py2);
  const len = Math.hypot(b.x - a.x, b.y - a.y);
  if (len < 1) return null;
  const n = { x: -(b.y - a.y) / len, y: (b.x - a.x) / len };
  const k = ppm * state.zoom;
  return { a, b, n, hp: sideWidthM(seg, 1) * k, hm: sideWidthM(seg, -1) * k };
}

function edgeOffset(g, side) {
  return side > 0 ? g.hp : g.hm;
}

/** which band edge (if any) is under screen point (mx, my) — for width dragging */
function bandEdgeAt(mx, my) {
  if (state.mode !== "segments" || !state.overlays.segments) return null;
  let best = null;
  state.segments.forEach((seg, i) => {
    const g = bandScreen(seg);
    if (!g) return;
    for (const side of [1, -1]) {
      const ox = g.n.x * edgeOffset(g, side) * side;
      const oy = g.n.y * edgeOffset(g, side) * side;
      const p = { x: g.a.x + ox, y: g.a.y + oy };
      const q = { x: g.b.x + ox, y: g.b.y + oy };
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      const t = Math.max(0, Math.min(1, ((mx - p.x) * dx + (my - p.y) * dy) / (dx * dx + dy * dy || 1)));
      const d = Math.hypot(mx - (p.x + t * dx), my - (p.y + t * dy));
      // the midpoint handle is a bigger target than the rest of the edge
      const hd = Math.hypot(mx - (p.x + q.x) / 2, my - (p.y + q.y) / 2);
      const score = Math.min(d, hd - 4);
      if (score <= 7 && (!best || score < best.score)) best = { i, side, score };
    }
  });
  return best;
}

/** floor-plan pixels per metre, from saved segments that have both world + pixel ends */
function pxPerMeterFrom(segs) {
  const r = (segs || [])
    .filter((s) => s.px1 != null && s.x1 != null)
    .map((s) => {
      const m = Math.hypot(s.x2 - s.x1, s.y2 - s.y1);
      return m > 0.5 ? Math.hypot(s.px2 - s.px1, s.py2 - s.py1) / m : null;
    })
    .filter((v) => v != null)
    .sort((p, q) => p - q);
  return r.length ? r[Math.floor(r.length / 2)] : null;
}

// ---------------------------------------------------------------- API helpers

// The editor runs standalone (served at "/") or mounted inside the main API server at
// "/floor-plan/". API paths are written as "/api/…"; prefix them with wherever this page lives.
const ROOT = location.pathname.replace(/\/[^/]*$/, "");
function url(path) {
  return typeof path === "string" && path.startsWith("/api/") ? ROOT + path : path;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url(path), opts);
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
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError);
  el.classList.toggle("is-ok", !!msg && !isError);
  if (msg) {
    setTimeout(() => {
      if (el.textContent === msg) {
        el.textContent = "";
        el.classList.remove("is-error", "is-ok");
      }
    }, 6000);
  }
}

const MODE_HINTS = {
  view: "Click the plan to read facility X/Y",
  control_points: "Click corners on the plan, then enter real metres",
  segments: "Click path endpoints in pairs along corridors",
  slots: "Click each parking bay, then set ID / zone",
  capture_locations: "Click empty space for a new location, or a marker to edit",
  query: "Upload a photo to test localization on this floor",
};

function updateModeContext() {
  const el = document.getElementById("modeContext");
  if (el) el.textContent = MODE_HINTS[state.mode] || "";
}

function updateWorkspaceTitle() {
  const el = document.getElementById("workspaceTitle");
  if (!el) return;
  if (state.siteId && state.floor != null && state.floor !== "") {
    el.textContent = `${state.siteId} · floor ${state.floor}`;
  } else if (state.siteId) {
    el.textContent = `${state.siteId} · pick a floor`;
  } else {
    el.textContent = "Floor plan canvas";
  }
}

function showCanvasHint(html) {
  const hint = document.getElementById("canvasHint");
  if (!hint) return;
  hint.innerHTML = html;
  hint.style.display = "flex";
}

function hideCanvasHint() {
  const hint = document.getElementById("canvasHint");
  if (!hint) return;
  hint.style.display = "none";
}

function zoomBy(factor) {
  if (!state.img) return;
  const mx = canvas.width / 2;
  const my = canvas.height / 2;
  const before = screenToImage(mx, my);
  state.zoom = Math.max(0.05, Math.min(20, state.zoom * factor));
  const after = imageToScreen(before.x, before.y);
  state.panX += mx - after.x;
  state.panY += my - after.y;
  draw();
}

function floorBase() {
  if (!state.siteId || state.floor == null || state.floor === "") return null;
  return url(`/api/sites/${state.siteId}/floors/${encodeURIComponent(state.floor)}`);
}

// ---------------------------------------------------------------- coordinate math

function screenToImage(sx, sy) {
  return { x: (sx - state.panX) / state.zoom, y: (sy - state.panY) / state.zoom };
}
function imageToScreen(ix, iy) {
  return { x: ix * state.zoom + state.panX, y: iy * state.zoom + state.panY };
}

function pixelToWorld(px, py) {
  const t = state.transform;
  if (!t) return null;
  // same as FloorPlanTransform.pixel_to_world
  return {
    x: t.a * px + t.b * py + t.tx,
    y: t.c * px + t.d * py + t.ty,
  };
}

function setProbe(px, py) {
  const world = pixelToWorld(px, py);
  state.probe = world
    ? { px, py, x: world.x, y: world.y }
    : { px, py, x: null, y: null };
  renderProbePanel();
  updateCoordReadout(state.probe);
}

function renderProbePanel() {
  const pxEl = document.getElementById("probePx");
  const xEl = document.getElementById("probeX");
  const yEl = document.getElementById("probeY");
  const hint = document.getElementById("probeHint");
  if (!pxEl) return;
  const p = state.probe;
  if (!p) {
    pxEl.textContent = "—";
    xEl.textContent = "—";
    yEl.textContent = "—";
  } else {
    pxEl.textContent = `${p.px.toFixed(1)}, ${p.py.toFixed(1)}`;
    xEl.textContent = p.x == null ? "n/a" : `${p.x.toFixed(3)} m`;
    yEl.textContent = p.y == null ? "n/a" : `${p.y.toFixed(3)} m`;
  }
  if (hint) {
    hint.textContent = state.hasTransform
      ? `Values use floor ${state.floor} transform.json (facility metres).`
      : `No transform for floor ${state.floor || "—"} — fit & save control points first.`;
  }
}

function updateCoordReadout(probe) {
  const el = document.getElementById("coordReadout");
  if (!el) return;
  if (!probe) {
    el.textContent = state.hasTransform
      ? "Click the plan to read real X/Y"
      : "No transform — calibrate first";
    return;
  }
  if (probe.x == null || probe.y == null) {
    el.textContent = `px ${probe.px.toFixed(0)}, ${probe.py.toFixed(0)} · no transform`;
    return;
  }
  el.textContent = `X ${probe.x.toFixed(3)} m · Y ${probe.y.toFixed(3)} m  (px ${probe.px.toFixed(0)}, ${probe.py.toFixed(0)})`;
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
      if (loc.px === undefined || loc.py === undefined) continue;
      const s = imageToScreen(loc.px, loc.py);
      drawDot(s.x, s.y, "#2ecc71", 5);
      drawLabel(s.x + 8, s.y - 8, loc.location_id || "?", "#2ecc71");
    }
  }

  // walkable segments (full editable list loaded from server + new draws)
  if (state.overlays.segments) {
    const segColor = state.mode === "segments" ? "#00d2ff" : "#3498db";
    for (const seg of state.segments) {
      if (seg.px1 === undefined) continue;
      const a = imageToScreen(seg.px1, seg.py1);
      const b = imageToScreen(seg.px2, seg.py2);
      // corridor band (segment = centre line): what localization treats as "inside"
      const g = bandScreen(seg);
      if (g) {
        const i = state.segments.indexOf(seg);
        const active = state.widthDrag?.i === i || state.bandHover?.i === i;
        // band polygon — the two sides can differ, so not a thick centred line
        ctx.save();
        ctx.globalAlpha = active ? 0.26 : 0.16;
        ctx.fillStyle = segColor;
        ctx.beginPath();
        ctx.moveTo(a.x + g.n.x * g.hp, a.y + g.n.y * g.hp);
        ctx.lineTo(b.x + g.n.x * g.hp, b.y + g.n.y * g.hp);
        ctx.lineTo(b.x - g.n.x * g.hm, b.y - g.n.y * g.hm);
        ctx.lineTo(a.x - g.n.x * g.hm, a.y - g.n.y * g.hm);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
        if (state.mode === "segments") {
          // draggable edges + midpoint handles — each side independently
          for (const side of [1, -1]) {
            const ox = g.n.x * edgeOffset(g, side) * side;
            const oy = g.n.y * edgeOffset(g, side) * side;
            const hot = (state.widthDrag?.i === i && state.widthDrag.side === side)
              || (!state.widthDrag && state.bandHover?.i === i && state.bandHover.side === side);
            ctx.save();
            ctx.setLineDash([6, 4]);
            ctx.strokeStyle = hot ? "#ffd166" : segColor;
            ctx.lineWidth = hot ? 2.5 : 1.5;
            ctx.beginPath();
            ctx.moveTo(a.x + ox, a.y + oy);
            ctx.lineTo(b.x + ox, b.y + oy);
            ctx.stroke();
            ctx.restore();
            drawDot((a.x + b.x) / 2 + ox, (a.y + b.y) / 2 + oy, hot ? "#ffd166" : segColor, hot ? 6 : 4.5);
          }
          if (state.widthDrag?.i === i) {
            const s = state.widthDrag.side;
            const off = edgeOffset(g, s) + 16;
            drawLabel(
              (a.x + b.x) / 2 + g.n.x * off * s,
              (a.y + b.y) / 2 + g.n.y * off * s,
              `${sideWidthM(seg, s).toFixed(1)} m · total ${totalWidthM(seg).toFixed(1)} m`,
              "#ffd166",
            );
          }
        }
      }
      drawLine(a, b, segColor, 3);
      if (state.mode === "segments") {
        drawDot(a.x, a.y, segColor, 5);
        drawDot(b.x, b.y, segColor, 5);
      }
    }
  }
  // vehicle slots (full editable list; slots-mode redraws them highlighted)
  if (state.overlays.slots && state.mode !== "slots") {
    for (const slot of state.slots) {
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
      drawLabel(s.x + 8, s.y - 8, String(i + 1), "#ff4757");
    });
  }
  if (state.mode === "segments" && state.pendingSegmentPt) {
    const s = imageToScreen(state.pendingSegmentPt.px, state.pendingSegmentPt.py);
    drawDot(s.x, s.y, "#ffdd00", 6);
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

  // selected probe point (overview click → real X/Y)
  if (state.probe) {
    const s = imageToScreen(state.probe.px, state.probe.py);
    drawCrosshair(s.x, s.y, "#ffdd00", 12);
    if (state.probe.x != null && state.probe.y != null) {
      drawLabel(s.x + 10, s.y - 10, `${state.probe.x.toFixed(2)}, ${state.probe.y.toFixed(2)}`, "#ffdd00");
    }
  }
}

function drawCrosshair(x, y, color, r) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x - r, y);
  ctx.lineTo(x + r, y);
  ctx.moveTo(x, y - r);
  ctx.lineTo(x, y + r);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(x, y, 3.5, 0, 2 * Math.PI);
  ctx.stroke();
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
    return;
  }
  if (e.button === 0) { // left-press on a corridor band edge: drag to resize it
    const rect = canvas.getBoundingClientRect();
    const hit = bandEdgeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (hit) {
      e.preventDefault();
      state.widthDrag = { i: hit.i, side: hit.side, moved: false };
      canvas.style.cursor = "grabbing";
      draw();
    }
  }
});
window.addEventListener("mousemove", (e) => {
  if (state.dragging) {
    state.panX = state.dragStart.panX + (e.clientX - state.dragStart.x);
    state.panY = state.dragStart.panY + (e.clientY - state.dragStart.y);
    draw();
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  if (state.widthDrag) {
    const seg = state.segments[state.widthDrag.i];
    const ppm = pxPerMeter();
    if (!seg || !ppm) return;
    // only the grabbed edge moves: its offset = mouse distance from the line on that side
    // (crossing over to the other side just collapses this edge to the minimum)
    const p = screenToImage(mx, my);
    const dx = seg.px2 - seg.px1;
    const dy = seg.py2 - seg.py1;
    const len = Math.hypot(dx, dy) || 1;
    const signed = ((p.x - seg.px1) * -dy + (p.y - seg.py1) * dx) / len; // + = screen side +1
    const side = state.widthDrag.side;
    const step = e.shiftKey ? 0.1 : 0.5; // hold Shift for fine steps
    const m = +Math.max(0, Math.min(20, Math.round((signed * side) / ppm / step) * step)).toFixed(1);
    const key = sideKey(side);
    if (m !== sideWidthM(seg, side)) {
      // pin the other side to its current value before the first asymmetric edit
      const otherKey = sideKey(-side);
      if (!Number.isFinite(seg[otherKey])) seg[otherKey] = +sideWidthM(seg, -side).toFixed(2);
      seg[key] = m;
      seg.width = +totalWidthM(seg).toFixed(2);
      state.widthDrag.moved = true;
      const inp = document.querySelector(`#segmentsList input[data-w="${state.widthDrag.i}"]`);
      if (inp) inp.value = seg.width;
      draw();
    }
    return;
  }
  // hover feedback on band edges
  if (e.target === canvas) {
    const hit = bandEdgeAt(mx, my);
    const changed = (hit?.i ?? -1) !== (state.bandHover?.i ?? -1)
      || (hit?.side ?? 0) !== (state.bandHover?.side ?? 0);
    state.bandHover = hit;
    canvas.style.cursor = hit ? "grab" : "";
    if (changed) draw();
  }
});
window.addEventListener("mouseup", () => {
  state.dragging = false;
  if (state.widthDrag) {
    // the click that ends a resize must not also drop a segment point
    state.suppressClick = true;
    const { moved } = state.widthDrag;
    state.widthDrag = null;
    canvas.style.cursor = state.bandHover ? "grab" : "";
    if (moved) {
      renderSegmentsPanel();
      setStatus("Corridor width changed — click Save segments to keep it");
    }
    draw();
  }
});

canvas.addEventListener("click", (e) => {
  if (state.suppressClick) {
    state.suppressClick = false;
    return;
  }
  if (!state.img) return;
  const rect = canvas.getBoundingClientRect();
  const p = screenToImage(e.clientX - rect.left, e.clientY - rect.top);
  handleClick(p.x, p.y);
});

function handleClick(px, py) {
  if (state.mode === "view" || state.mode === "query") {
    setProbe(px, py);
  } else if (state.mode === "control_points") {
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
    updateModeContext();
    if (state.mode === "capture_locations") loadCaptureLocations();
    draw();
  });
});

const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const zoomFitBtn = document.getElementById("zoomFitBtn");
if (zoomInBtn) zoomInBtn.addEventListener("click", () => zoomBy(1.2));
if (zoomOutBtn) zoomOutBtn.addEventListener("click", () => zoomBy(1 / 1.2));
if (zoomFitBtn) {
  zoomFitBtn.addEventListener("click", () => {
    fitToCanvas();
    draw();
  });
}

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
      <label class="seg-width" title="Total corridor width in metres. Typing a value centres the band on the line; drag either edge on the plan to set each side separately. Blank = default ${DEFAULT_CORRIDOR_WIDTH_M} m">
        <input type="number" min="0.5" max="30" step="0.5" placeholder="${DEFAULT_CORRIDOR_WIDTH_M}" value="${Number.isFinite(seg.width) ? seg.width : ""}" data-w="${i}"> m
      </label>
      <button class="del-btn" data-i="${i}">×</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll("input[data-w]").forEach((inp) => {
    inp.addEventListener("change", (e) => {
      const v = parseFloat(e.target.value);
      const seg = state.segments[+e.target.dataset.w];
      seg.width = Number.isFinite(v) && v > 0 ? v : null;
      // a typed total re-centres the band (drag an edge for an off-centre corridor)
      seg.width_left = null;
      seg.width_right = null;
      draw();
    });
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
  updateWorkspaceTitle();
  const floors = await api("GET", `/api/sites/${siteId}/floors`);
  const sel = document.getElementById("floorSelect");
  sel.innerHTML = floors.map((f) => `<option value="${f}">${f}</option>`).join("");
  await selectFloor(String(floors[0] ?? "1"));
}

document.getElementById("createFloorBtn").addEventListener("click", async () => {
  const name = (document.getElementById("newFloorNum").value || "").trim();
  if (!name) return;
  const enc = encodeURIComponent(name);
  await api("POST", `/api/sites/${state.siteId}/floors/${enc}`);
  document.getElementById("newFloorNum").value = "";
  await selectSite(state.siteId);
  document.getElementById("floorSelect").value = name;
  await selectFloor(name);
});

document.getElementById("floorSelect").addEventListener("change", (e) => selectFloor(e.target.value));

async function selectFloor(floor) {
  state.floor = String(floor);
  state.controlPoints = [];
  state.segments = [];
  state.slots = [];
  state.savedSegments = [];
  state.savedSlots = [];
  state.pendingSegmentPt = null;
  state.queryResult = null;
  state.routeResult = null;
  state.captureLocations = [];
  state.selectedLocationId = null;
  state.selectedLocationPhotos = [];
  state.pendingNewLocationPoint = null;
  state.probe = null;
  state.transform = null;
  state.hasTransform = false;
  state.img = null;
  document.getElementById("newLocationForm").style.display = "none";
  document.getElementById("selectedLocationPanel").style.display = "none";
  const fitEl = document.getElementById("fitResult");
  if (fitEl) fitEl.innerHTML = "";
  document.getElementById("saveTransformBtn").disabled = true;
  updateWorkspaceTitle();
  renderControlPointsPanel();
  renderSegmentsPanel();
  renderSlotsPanel();
  renderProbePanel();
  updateCoordReadout(null);
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
  const base = floorBase();
  if (!fileInput.files.length || !base) { setStatus("Pick a site, floor, and PDF first", true); return; }
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  const page = document.getElementById("pdfPage").value || 0;
  const dpi = document.getElementById("pdfDpi").value || 200;
  setStatus(`Rendering PDF for floor ${state.floor}...`);
  try {
    const res = await fetch(`${base}/floorplan?page=${page}&dpi=${dpi}`, { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail);
    setStatus(`Floor plan rendered for floor ${state.floor}`);
    await loadFloorplanImage();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function loadFloorplanImage() {
  const base = floorBase();
  if (!base) return;
  const img = new Image();
  await new Promise((resolve) => {
    img.onload = () => {
      state.img = img;
      state.imgW = img.width;
      state.imgH = img.height;
      hideCanvasHint();
      fitToCanvas();
      resolve();
    };
    img.onerror = () => {
      state.img = null;
      showCanvasHint(
        `<strong>No plan for floor ${state.floor}</strong>` +
        `<span>Upload a PDF and tap Render plan for this floor.</span>`
      );
      resolve();
    };
    img.src = `${base}/floorplan/image?_=${Date.now()}`;
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
  const base = floorBase();
  if (!base) return;
  try {
    state.controlPoints = await api("GET", `${base}/control-points`);
  } catch (e) {
    state.controlPoints = [];
  }
  renderControlPointsPanel();
}

document.getElementById("saveControlPointsBtn").addEventListener("click", async () => {
  const base = floorBase();
  if (!base) return;
  try {
    await api("PUT", `${base}/control-points`, state.controlPoints);
    setStatus(`Control points saved for floor ${state.floor}`);
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("fitTransformBtn").addEventListener("click", async () => {
  const base = floorBase();
  if (!base) return;
  try {
    await api("PUT", `${base}/control-points`, state.controlPoints);
    const result = await api("POST", `${base}/control-points/fit`);
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
  const base = floorBase();
  if (!base) return;
  try {
    await api("POST", `${base}/control-points/fit/save`);
    setStatus(`Transform saved for floor ${state.floor}`);
    await checkTransform();
    await loadGeometry();
    await loadIngestedLocations();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function checkTransform() {
  const base = floorBase();
  if (!base) {
    state.transform = null;
    state.hasTransform = false;
    renderProbePanel();
    updateCoordReadout(null);
    return;
  }
  try {
    const t = await api("GET", `${base}/transform`);
    state.transform = t;
    state.hasTransform = true;
  } catch (e) {
    state.transform = null;
    state.hasTransform = false;
  }
  if (state.probe) setProbe(state.probe.px, state.probe.py);
  else {
    renderProbePanel();
    updateCoordReadout(null);
  }
}

// ---------------------------------------------------------------- geometry

async function loadGeometry() {
  if (!state.floor) return;
  try {
    const floorPath = encodeURIComponent(state.floor);
    const geo = await api("GET", `/api/sites/${state.siteId}/floors/${floorPath}/geometry`);
    state.savedSegments = geo.segments || [];
    state.savedSlots = geo.slots || [];
    // editable lists include everything already on disk (PUT replaces whole floor list)
    state.segments = state.savedSegments
      .filter((s) => s.px1 != null && s.py1 != null && s.px2 != null && s.py2 != null)
      .map((s) => ({
        px1: s.px1, py1: s.py1, px2: s.px2, py2: s.py2,
        width: s.width ?? null, width_left: s.width_left ?? null, width_right: s.width_right ?? null,
      }));
    state.pxPerMeter = pxPerMeterFrom(state.savedSegments);
    state.slots = state.savedSlots
      .filter((s) => s.px != null && s.py != null)
      .map((s) => ({
        px: s.px,
        py: s.py,
        slot_id: s.slot_id || "",
        zone: s.zone || "",
      }));
  } catch (e) {
    state.savedSegments = [];
    state.savedSlots = [];
    state.segments = [];
    state.slots = [];
  }
  renderSegmentsPanel();
  renderSlotsPanel();
}

document.getElementById("saveSegmentsBtn").addEventListener("click", async () => {
  try {
    const payload = state.segments.map((s) => ({
      px1: s.px1, py1: s.py1, px2: s.px2, py2: s.py2,
      width: Number.isFinite(s.width) && s.width > 0 ? s.width : null,
      width_left: Number.isFinite(s.width_left) && s.width_left >= 0 ? s.width_left : null,
      width_right: Number.isFinite(s.width_right) && s.width_right >= 0 ? s.width_right : null,
    }));
    await api("PUT", `/api/sites/${state.siteId}/floors/${encodeURIComponent(state.floor)}/segments`, payload);
    setStatus(`Segments saved (${payload.length})`);
    await loadGeometry();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

document.getElementById("saveSlotsBtn").addEventListener("click", async () => {
  try {
    const payload = state.slots.map((s) => ({
      px: s.px, py: s.py, slot_id: s.slot_id, zone: s.zone,
    }));
    await api("PUT", `/api/sites/${state.siteId}/floors/${encodeURIComponent(state.floor)}/slots`, payload);
    setStatus(`Slots saved (${payload.length})`);
    await loadGeometry();
    await refreshRouteSlotOptions();
    draw();
  } catch (e) { setStatus(e.message, true); }
});

async function loadIngestedLocations() {
  const base = floorBase();
  if (!base) {
    state.ingestedLocations = [];
    return;
  }
  try {
    state.ingestedLocations = await api("GET", `${base}/locations`);
    const withPx = state.ingestedLocations.filter((l) => l.px !== undefined).length;
    if (state.ingestedLocations.length) {
      setStatus(`${state.ingestedLocations.length} ingested location(s) on floor ${state.floor}` +
        (withPx ? "" : " (no transform — calibrate to place on plan)"));
    }
  } catch (e) {
    state.ingestedLocations = [];
  }
}

// ---------------------------------------------------------------- capture locations + photos

async function loadCaptureLocations() {
  if (!state.siteId || !state.floor) return;
  try {
    state.captureLocations = await api("GET", `/api/sites/${state.siteId}/floors/${encodeURIComponent(state.floor)}/capture-locations`);
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
    await api("POST", `/api/sites/${state.siteId}/floors/${encodeURIComponent(state.floor)}/capture-locations`, {
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

document.getElementById("buildIndexBtn").addEventListener("click", async () => {
  if (!state.siteId) {
    setStatus("Select a site before building embeddings", true);
    return;
  }
  const button = document.getElementById("buildIndexBtn");
  const status = document.getElementById("buildIndexStatus");
  button.disabled = true;
  status.textContent = "Building site embedding index... This may take a while.";
  try {
    const result = await api("POST", `/api/sites/${state.siteId}/embeddings/build`);
    status.textContent = `Index built: ${result.indexed_images} photos, ${result.embedding_dim}-dimensional embeddings.`;
  } catch (error) {
    status.textContent = error.message;
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
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
      <img src="${url(photo.photo_url)}" alt="${photo.image_id}" loading="lazy">
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
        await loadLocationPhotos(); // heading edits may change the ID suffix and filename
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
      const res = await fetch(url(`/api/sites/${state.siteId}/capture-locations/${state.selectedLocationId}/photos`), { method: "POST", body: form });
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
  // try the floor being viewed first (other mapped floors are still tried)
  if (state.floor != null && state.floor !== "") form.append("floor", String(state.floor));
  const btn = document.getElementById("runQueryBtn");
  const el = document.getElementById("queryResult");
  btn.disabled = true;
  btn.textContent = "Localizing…";
  el.textContent = "Loading the VPR pipeline and 3D map — this can take a while…";
  setStatus("Localizing...");
  try {
    const res = await fetch(url(`/api/sites/${state.siteId}/query`), { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const result = await res.json();
    state.queryResult = result;
    const size = result.query_size
      ? `\nimage ${result.source_size.join("x")}`
        + (result.source_size[0] !== result.query_size[0] ? ` → ${result.query_size.join("x")}` : "")
      : "";
    if (result.matched) {
      const inl = result.num_inliers != null ? `\ninliers=${result.num_inliers}/${result.num_matches}` : "";
      el.textContent = `Matched via ${(result.method || "vpr").toUpperCase()}\nfloor=${result.floor}`
        + `\nx=${result.x.toFixed(2)} y=${result.y.toFixed(2)} heading=${Math.round(result.heading ?? 0)}°`
        + `\nconf=${result.similarity.toFixed(2)} inlier_ratio=${result.inlier_ratio.toFixed(3)}${inl}${size}`
        + (result.px === undefined ? `\n(no floorplan transform for floor ${result.floor} — can't draw it)` : "")
        + (state.floor != null && String(result.floor) !== String(state.floor)
          ? `\n⚠ matched floor ${result.floor} — you're viewing floor ${state.floor}` : "");
      await refreshRouteSlotOptions();
      document.getElementById("runRouteBtn").disabled = state.savedSlots.length === 0;
    } else {
      el.textContent = `NO MATCH${size}\n${result.reason || ""}`;
      document.getElementById("runRouteBtn").disabled = true;
    }
    setStatus("");
    draw();
  } catch (e) {
    el.textContent = `Error: ${e.message}`;
    setStatus(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Localize";
  }
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

updateModeContext();
updateWorkspaceTitle();
refreshSites();
draw();
