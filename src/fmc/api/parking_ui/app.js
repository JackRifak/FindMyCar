const ARRIVE_M = 2.0; // along-route remaining / approach-node threshold
const ARRIVE_SLOT_M = 8.0; // euclidean to slot (bays sit off the walkable line)
const LEG_ADVANCE_M = 0.7; // snap to next walk leg
const ENTER_CONNECTOR_M = 3.0; // enter lift/stairs guidance (PDR freezes in elevator)
/** Floor B: soft prefer this far from lift door before fresh XR */
const RESUME_XR_CLEAR_M = 1.5;
/** after settle, start XR even if still near the lift (DeviceMotion/dist often stall) */
const RESUME_XR_FORCE_MS = 3500;
const RESUME_XR_SETTLE_MS = 1200;
const XR_TRACK_LOST_MS = 4500; // allow time to point at floor / calibrate before killing XR
const XR_TRACK_GRACE_MS = 12000; // after session start, ignore tracking-lost handoff
/** after this + walk motion, assume user exited lift (camera-nav used VPR; XR often can't) */
const ELEVATOR_AUTO_MS = 7000;
const ELEVATOR_WALK_MOTION = 1.6;
const SKIP_CALIBRATION = true; // temporarily bypass phone calibration UI

const state = {
  deviceId: "parking-mobile-" + Math.random().toString(36).slice(2, 8),
  workflowStep: "destination",
  hasLocalizedPosition: false,
  slotList: [],
  position: { floor: "1", x: 0, y: 0, heading: 0, confidence: 0, tracking: false },
  calibration: {
    status: "idle",
    step: 0,
    stepKey: "stillness",
    stepLabel: "Waiting for calibration to begin.",
    strideScaleFactor: 1.0,
    completed: {
      stillness: false,
      compass: false,
      walk: false,
    },
  },
  route: null,
  routeScreenPoint: null,
  activeLegIndex: 0,
  inFloorTransition: false,
  floorTransitionAt: 0, // when elevator/stairs guidance started
  arrivedShown: false,
  liveVpr: {
    running: false,
    requestInFlight: false,
    lastCaptureTime: 0,
    nextCaptureAllowedAt: 0,
  },
  lastLocalizeMethod: null, // "pnp" | "image_vpr" | null
  navActive: false, // Start nav camera guidance
  webXrActive: false,
  motion: {
    gravityEstimate: null,
    stepArmed: false,
    peakAcceleration: 0,
    valleyAcceleration: 0,
    lastStepTime: 0,
    lastMagnitude: null,
    lastMeaningfulMotionAt: 0,
    cumulativeMotion: 0,
  },
  stream: null,
  video: null,
  lastVideoTime: null,
  lastVideoFrameAt: 0,
  camWatchWired: false,
  suppressXrResume: false, // true while handing XR → camera
  pendingXrResume: false, // want fresh XR after landing lock + clear lobby
  xrResumeReadyAt: 0, // earliest time to try WebXR after landing
  xrResumeBlocked: false, // XR died this floor — stay on camera
  xrResumeAttempts: 0, // soft retries after denied / camera busy
  camOwner: "camera", // "camera" | "xr" — exclusive ownership
  walkSinceLand: 0, // motion since landOnFloor (not cleared by PnP fixes)
  landingPose: null, // PnP/VPR lock on Floor B — origin for fresh XR
  awaitingLandingLock: false, // true from lift handoff until PnP on dest floor
  floorOrder: [], // site floor labels, bottom→top when available
  lastCompassSample: null,
  rawHeadingHistory: [],
  headingOffset: 0,
  gyroBias: 0,
  sessionReady: false,
};

const ui = {
  calibrationStatus: document.getElementById("calibrationStatus"),
  calibrationHint: document.getElementById("calibrationHint"),
  calibrationVisual: document.getElementById("calibrationVisual"),
  calibrationSteps: [...document.querySelectorAll(".cal-step")],
  calibrationButtons: [...document.querySelectorAll(".step-action")],
  workflowPanels: [...document.querySelectorAll("[data-workflow-panel]")],
  workflowSteps: [...document.querySelectorAll("[data-workflow-step]")],
  destinationContinueBtn: document.getElementById("destinationContinueBtn"),
  calibrationContinueBtn: document.getElementById("calibrationContinueBtn"),
  calibrationBackBtn: document.getElementById("calibrationBackBtn"),
  navigationBackBtn: document.getElementById("navigationBackBtn"),
  selectedSlotSummary: document.getElementById("selectedSlotSummary"),
  localizeBtn: document.getElementById("localizeBtn"),
  groundTruthLocationId: document.getElementById("groundTruthLocationId"),
  diagnosticLocations: document.getElementById("diagnosticLocations"),
  captureTriggerMode: document.getElementById("captureTriggerMode"),
  captureMaxDimension: document.getElementById("captureMaxDimension"),
  captureJpegQuality: document.getElementById("captureJpegQuality"),
  blurGuardEnabled: document.getElementById("blurGuardEnabled"),
  routeBtn: document.getElementById("routeBtn"),
  localizationStatus: document.getElementById("localizationStatus"),
  slotInput: document.getElementById("slotInput"),
  slotSuggestions: document.getElementById("slotSuggestions"),
  slotSuggestionsMini: document.getElementById("slotSuggestionsMini"),
  cameraView: document.getElementById("cameraView"),
  sessionState: document.getElementById("sessionState"),
  positionState: document.getElementById("positionState"),
  confidenceState: document.getElementById("confidenceState"),
  metricFloor: document.getElementById("metricFloor"),
  metricCoords: document.getElementById("metricCoords"),
  metricHeading: document.getElementById("metricHeading"),
  metricDistance: document.getElementById("metricDistance"),
  routeBadge: document.getElementById("routeBadge"),
  routeDestination: document.getElementById("routeDestination"),
  routeDistance: document.getElementById("routeDistance"),
  routeTotalDistance: document.getElementById("routeTotalDistance"),
  routeStatus: document.getElementById("routeStatus"),
  turnInstructions: document.getElementById("turnInstructions"),
  routeSvg: document.getElementById("routeSvg"),
  // camera HUD
  statusChip: document.getElementById("statusChip"),
  statusLine: document.getElementById("statusLine"),
  arHint: document.getElementById("arHint"),
  turnBanner: document.getElementById("turnBanner"),
  turnLabel: document.getElementById("turnLabel"),
  turnDistance: document.getElementById("turnDistance"),
  turnIcon: document.getElementById("turnIcon"),
  hudSlot: document.getElementById("hudSlot"),
  slotSheet: document.getElementById("slotSheet"),
  calSheet: document.getElementById("calSheet"),
  slotSheetBtn: document.getElementById("slotSheetBtn"),
  arriveOverlay: document.getElementById("arriveOverlay"),
  arriveSlot: document.getElementById("arriveSlot"),
  arriveDismissBtn: document.getElementById("arriveDismissBtn"),
  floorContinueBtn: document.getElementById("floorContinueBtn"),
  startArBtn: document.getElementById("startArBtn"),
  arStage: document.getElementById("arStage"),
  arCanvas: document.getElementById("arCanvas"),
  floorArrows: document.getElementById("floorArrows"),
  floorPath: document.getElementById("floorPath"),
  liftHud: document.getElementById("liftHud"),
  liftHudGlyph: document.getElementById("liftHudGlyph"),
  liftHudDir: document.getElementById("liftHudDir"),
  liftHudFloor: document.getElementById("liftHudFloor"),
  liftCamCanvas: document.getElementById("liftCamCanvas"),
};

let webXrNav = null;
let liftHandoffBusy = false;
let xrResumeBusy = false;
let xrResumePollTimer = 0;
/** "continue" = manual floor advance · "start_ar" = user-gesture WebXR after lift */
let floorBtnMode = "continue";
let liftCamRaf = 0;
let xrLiftWatchTimer = 0;
let xrHealthTimer = 0;
let xrTrackLostAt = 0;
let xrSessionStartedAt = 0;

function setStatusChip(text, kind = "warn") {
  if (ui.sessionState) ui.sessionState.textContent = text;
  if (!ui.statusChip) return;
  ui.statusChip.textContent = text;
  ui.statusChip.classList.remove("is-live", "is-warn", "is-bad");
  ui.statusChip.classList.add(kind === "live" ? "is-live" : kind === "bad" ? "is-bad" : "is-warn");
}

function setStatusMsg(msg) {
  if (ui.localizationStatus) ui.localizationStatus.textContent = msg;
  if (ui.statusLine) ui.statusLine.textContent = msg;
}

function setHint(msg, show = true) {
  if (!ui.arHint) return;
  ui.arHint.hidden = !show || !msg;
  ui.arHint.textContent = msg || "";
}

function showSheet(name) {
  if (ui.slotSheet) ui.slotSheet.hidden = name !== "slot";
  if (ui.calSheet) ui.calSheet.hidden = name !== "cal";
}

function hideSheets() {
  if (ui.slotSheet) ui.slotSheet.hidden = true;
  if (ui.calSheet) ui.calSheet.hidden = true;
}

function showArrivalCelebration(slotId) {
  if (state.arrivedShown) return;
  state.arrivedShown = true;
  state.navActive = false;
  if (webXrNav) {
    webXrNav.dispose();
    webXrNav = null;
  }
  state.webXrActive = false;
  ui.arStage?.classList.remove("webxr-on");
  showFloorArrows(false);
  setRouteBtnMode("start");
  stopLiveVprCapture({ keepCamera: true });
  const label = slotId || "your destination";
  if (ui.arriveSlot) ui.arriveSlot.textContent = slotId || "your car";
  if (ui.arriveOverlay) {
    ui.arriveOverlay.hidden = false;
    if (ui.turnBanner) ui.turnBanner.hidden = true;
  } else if (ui.turnBanner) {
    ui.turnBanner.hidden = false;
    if (ui.turnLabel) ui.turnLabel.textContent = "You have reached";
    if (ui.turnDistance) ui.turnDistance.textContent = label;
  }
  setStatusChip("Arrived", "live");
  setStatusMsg(`You have reached ${label}`);
  setHint(`You have reached ${label}`, true);
}

function hideArrivalCelebration() {
  if (ui.arriveOverlay) ui.arriveOverlay.hidden = true;
}

function normFloor(f) {
  const s = String(f ?? "").trim();
  return s || "1";
}

function sameFloor(a, b) {
  return normFloor(a).toLowerCase() === normFloor(b).toLowerCase();
}

/** rank floors bottom→top (B2 < B1 < G < 1 < 2) */
function floorRank(f) {
  const s = normFloor(f);
  const order = state.floorOrder || [];
  const idx = order.findIndex((x) => sameFloor(x, s));
  if (idx >= 0) return idx;
  const u = s.toUpperCase();
  const bas = u.match(/^B(\d+)$/);
  if (bas) return -Number(bas[1]);
  if (u === "G" || u === "GF" || u === "LG") return 0;
  const n = Number(u);
  if (Number.isFinite(n)) return n;
  return 0;
}

/** "up" | "down" | "straight" from transition floors */
function verticalDir(fromFloor, toFloor) {
  const d = floorRank(toFloor) - floorRank(fromFloor);
  if (d > 0) return "up";
  if (d < 0) return "down";
  return "straight";
}

function showLiftHud(tf) {
  if (!ui.liftHud || !tf) {
    hideLiftHud();
    return;
  }
  const dir = verticalDir(tf.from_floor, tf.to_floor);
  const to = normFloor(tf.to_floor);
  ui.liftHud.hidden = false;
  ui.liftHud.classList.toggle("is-down", dir === "down");
  if (ui.liftHudGlyph) {
    ui.liftHudGlyph.textContent = dir === "down" ? "↓" : "↑";
  }
  if (ui.liftHudDir) {
    ui.liftHudDir.textContent = dir === "down"
      ? "Go down"
      : dir === "up"
        ? "Go up"
        : "Change floor";
  }
  if (ui.liftHudFloor) {
    ui.liftHudFloor.textContent = `Floor ${to}`;
  }
}

function hideLiftHud() {
  if (ui.liftHud) ui.liftHud.hidden = true;
}

async function ensureFloorOrder() {
  if (state.floorOrder?.length) return;
  try {
    const res = await fetch("/map/floors");
    if (!res.ok) return;
    const data = await res.json();
    const floors = (data.floors || []).map(normFloor);
    if (floors.length) state.floorOrder = floors;
  } catch (_) { /* ignore */ }
}

function destFloorId() {
  if (!state.route) return null;
  return normFloor(state.route.dest_floor ?? state.route.floor);
}

function onDestFloor() {
  const dest = destFloorId();
  if (!dest) return false;
  return sameFloor(state.position.floor, dest);
}

function setFloorContinueVisible(on, toFloor = "") {
  // fallback only — primary path auto-detects like camera-nav VPR
  const btn = ui.floorContinueBtn;
  if (!btn) return;
  // never wipe the post-landing Start AR CTA
  if (!on && (floorBtnMode === "start_ar" || state.pendingXrResume)) return;
  floorBtnMode = "continue";
  btn.hidden = !on;
  if (on) {
    btn.textContent = toFloor
      ? `I'm on Floor ${toFloor} — continue`
      : "I've left the elevator — continue";
  }
  ui.turnBanner?.classList.toggle("turn-banner--action", Boolean(on));
}

/** Chrome immersive-ar needs a tap — show CTA and free the camera ahead of time */
function promptStartArAfterLand() {
  if (!state.landingPose) return;
  if (state.xrResumeBlocked) return;
  if (state.webXrActive || webXrNav) return;
  if (!state.navActive) return;

  state.pendingXrResume = true;

  // release getUserMedia now so the tap can requestSession without a long await
  stopLiveVprCapture({ keepCamera: false });
  state.stream?.getTracks?.().forEach((t) => {
    try { t.stop(); } catch (_) { /* ignore */ }
  });
  state.stream = null;
  try {
    if (ui.cameraView) ui.cameraView.srcObject = null;
  } catch (_) { /* ignore */ }
  document.body.classList.remove("cam-live");

  floorBtnMode = "start_ar";
  const btn = ui.floorContinueBtn;
  if (btn) {
    btn.hidden = false;
    btn.textContent = "Tap to start AR";
  }
  if (ui.startArBtn) {
    ui.startArBtn.hidden = false;
    ui.startArBtn.textContent = "Tap to start AR";
  }
  if (ui.turnBanner) ui.turnBanner.hidden = false;
  ui.turnBanner?.classList.toggle("turn-banner--action", true);
  hideLiftHud();
  setStatusChip("Tap Start AR", "live");
  setStatusMsg("Floor locked — tap Start AR");
  setHint("Tap Start AR to place floor arrows", true);
  void logClientDiagnostic({
    event: "start_ar_prompt",
    nav_mode: "camera",
    reason: "landing_pnp",
  });
}

function setStartArVisible(on) {
  if (!on) {
    floorBtnMode = "continue";
    if (ui.floorContinueBtn) ui.floorContinueBtn.hidden = true;
    if (ui.startArBtn) ui.startArBtn.hidden = true;
    ui.turnBanner?.classList.toggle("turn-banner--action", false);
    return;
  }
  promptStartArAfterLand();
}

/** resume live /localize (PnP first) so landing floor advances without Continue */
function xrRemainM() {
  if (!webXrNav || !(webXrNav.pathLen > 0)) return null;
  return Math.max(0, webXrNav.pathLen - (webXrNav.progressM || 0));
}

function stopXrLiftWatch() {
  if (xrLiftWatchTimer) {
    clearInterval(xrLiftWatchTimer);
    xrLiftWatchTimer = 0;
  }
  xrTrackLostAt = 0;
}

function xrInTrackGrace() {
  return xrSessionStartedAt > 0
    && (performance.now() - xrSessionStartedAt) < XR_TRACK_GRACE_MS;
}

/** poll XR tracking only — keep AR near the lift; hand off when pose dies hard */
function startXrLiftWatch() {
  stopXrLiftWatch();
  if (!hasFloorChangeAhead()) return;
  xrLiftWatchTimer = setInterval(() => {
    if (!state.webXrActive || !webXrNav) {
      stopXrLiftWatch();
      return;
    }
    if (!hasFloorChangeAhead()) return;
    if (xrInTrackGrace()) return;
    const lostMs = xrTrackLostAt ? performance.now() - xrTrackLostAt : 0;
    if (lostMs >= XR_TRACK_LOST_MS) {
      stopXrLiftWatch();
      void handXrToCamera("dead");
    }
  }, 350);
}

function startFloorDetectVpr() {
  if (!state.hasLocalizedPosition || !state.route) return;
  if (state.liveVpr.running) return;
  state.liveVpr.running = true;
  state.liveVpr.lastCaptureTime = 0; // capture soon
  state.motion.cumulativeMotion = 0;
  state.motion.lastMeaningfulMotionAt = performance.now();
  // in lift: always use getUserMedia (XR passthrough freezes in the cabin)
  if (state.inFloorTransition) {
    const open = state.stream ? Promise.resolve(true) : reopenCameraHard({ delayMs: 200 });
    open.then((ok) => {
      if (!ok || !state.stream) {
        state.liveVpr.running = false;
        return;
      }
      kickCameraPreview();
      runLiveVprCaptureLoop();
    }).catch((err) => {
      state.liveVpr.running = false;
      console.warn("floor-detect camera", err);
    });
    return;
  }
  // XR grab ok without getUserMedia
  if (state.webXrActive && webXrNav?.grabFrameBlob) {
    runLiveVprCaptureLoop();
    return;
  }
  ensureCamera().then((ok) => {
    if (!ok || !state.stream) {
      state.liveVpr.running = false;
      return;
    }
    kickCameraPreview();
    runLiveVprCaptureLoop();
  }).catch((err) => {
    state.liveVpr.running = false;
    console.warn("floor-detect camera", err);
  });
}

function routeDestPoint() {
  // parking bay — never the elevator door at end of a mid-route walk leg
  const slotId = ui.slotInput?.value?.trim();
  const slot = (state.slotList || []).find((s) => s.slot_id === slotId);
  if (slot && Number.isFinite(slot.x) && Number.isFinite(slot.y)) {
    return [slot.x, slot.y];
  }
  const dest = destFloorId();
  if (state.route?.legs?.length) {
    for (let i = state.route.legs.length - 1; i >= 0; i -= 1) {
      const leg = state.route.legs[i];
      if (leg?.floor_transition) continue;
      if (dest && String(leg.floor) !== dest) continue;
      if (leg.waypoints?.length) return leg.waypoints[leg.waypoints.length - 1];
    }
  }
  const wps = state.route?.waypoints;
  if (wps?.length) return wps[wps.length - 1];
  return null;
}

/** still need lift/stairs before the bay */
function hasFloorChangeAhead() {
  if (!state.route?.legs?.length) return false;
  if (state.inFloorTransition) return true;
  return Boolean(nextTransitionLeg(state.activeLegIndex || 0));
}

function hasReachedDestination(progress) {
  if (!state.route || !onDestFloor()) return false;
  if (state.inFloorTransition) return false;
  // elevator / stairs still ahead — end of this floor is NOT the bay
  if (hasFloorChangeAhead()) return false;

  const leg = activeRouteLeg();
  if (leg?.floor_transition) return false;

  const pos = state.position;
  const dest = routeDestPoint();
  const distSlot = dest
    ? Math.hypot(pos.x - dest[0], pos.y - dest[1])
    : Infinity;

  // bay coords are often a few meters off the corridor — use looser radius
  // only when dest point is the real slot (same floor, no connector left)
  if (distSlot <= ARRIVE_SLOT_M) return true;

  const rem = progress?.remainingDistance;
  if (typeof rem === "number" && rem <= ARRIVE_M) return true;

  // corridor approach node (waypoint before the off-path slot spur)
  const wps = leg?.waypoints?.length ? leg.waypoints : state.route.waypoints;
  if (wps?.length >= 2) {
    const approach = wps[wps.length - 2];
    const distApproach = Math.hypot(pos.x - approach[0], pos.y - approach[1]);
    if (distApproach <= ARRIVE_M) return true;
  }
  return false;
}

function elevatorBanner(tf, remM = 0) {
  if (!tf) {
    return {
      label: "Take elevator to your floor",
      distanceM: Math.max(0, remM),
      isArrival: false,
      kind: remM <= ENTER_CONNECTOR_M ? "arrive" : "straight",
      isFloorChange: true,
    };
  }
  const cid = tf.connector_id || "";
  const atDoor = remM <= ENTER_CONNECTOR_M;
  const dir = verticalDir(tf.from_floor, tf.to_floor);
  const go = dir === "down" ? "Go down" : dir === "up" ? "Go up" : "Change floor";
  return {
    label: atDoor
      ? `${go} · ${connectorLabel(tf)}${cid ? ` (${cid})` : ""} to Floor ${tf.to_floor}`
      : `Walk to ${cid || "lift"} · then ${go.toLowerCase()} to Floor ${tf.to_floor}`,
    distanceM: atDoor ? 0 : Math.max(0, remM),
    isArrival: false,
    kind: atDoor ? dir : "straight",
    isFloorChange: true,
  };
}

function activeRouteLeg() {
  if (!state.route?.legs?.length) return null;
  const idx = Math.max(0, Math.min(state.activeLegIndex || 0, state.route.legs.length - 1));
  return state.route.legs[idx];
}

function nextTransitionLeg(fromIdx = state.activeLegIndex || 0) {
  if (!state.route?.legs?.length) return null;
  for (let i = Math.max(0, fromIdx); i < state.route.legs.length; i += 1) {
    if (state.route.legs[i]?.floor_transition) return state.route.legs[i];
  }
  return null;
}

function connectorLabel(tf) {
  if (!tf) return "connector";
  const ctype = String(tf.type || "stairs");
  if (ctype === "elevator") return "Elevator";
  if (ctype === "ramp") return "Ramp";
  return "Stairs";
}

function enterConnectorLeg() {
  if (!state.route?.legs?.length) return false;
  const from = state.activeLegIndex || 0;
  let entered = false;
  for (let i = from; i < state.route.legs.length; i += 1) {
    if (state.route.legs[i]?.floor_transition) {
      state.activeLegIndex = i;
      state.inFloorTransition = true;
      state.floorTransitionAt = performance.now();
      state.motion.cumulativeMotion = 0;
      entered = true;
      break;
    }
  }
  if (!entered) {
    for (let i = 0; i < state.route.legs.length; i += 1) {
      if (state.route.legs[i]?.floor_transition) {
        const tf = state.route.legs[i].floor_transition;
        if (sameFloor(tf.from_floor, state.position.floor)) {
          state.activeLegIndex = i;
          state.inFloorTransition = true;
          state.floorTransitionAt = performance.now();
          state.motion.cumulativeMotion = 0;
          entered = true;
          break;
        }
      }
    }
  }
  if (entered) {
    // cabin: XR should already be off from approach; keep getUserMedia + PnP
    void handoffCameraDuringLift();
  }
  return entered;
}

/** nudge video so android webview keeps compositing frames */
function kickCameraPreview() {
  const v = ui.cameraView;
  if (!v) return;
  if (state.stream && v.srcObject !== state.stream) v.srcObject = state.stream;
  if (v.paused) v.play().catch(() => {});
  v.style.transform = "translateZ(0)";
  requestAnimationFrame(() => { v.style.transform = ""; });
  document.body.classList.add("cam-live");
}

/**
 * After WebXR, tracks often stay "live" but frozen.
 * Always tear down and reopen getUserMedia.
 */
async function reopenCameraHard({ delayMs = 400 } = {}) {
  const v = ui.cameraView;
  if (v) {
    try { v.pause(); } catch (_) { /* ignore */ }
    try { v.srcObject = null; } catch (_) { /* ignore */ }
  }
  if (state.stream) {
    state.stream.getTracks().forEach((t) => {
      try { t.stop(); } catch (_) { /* ignore */ }
    });
    state.stream = null;
  }
  document.body.classList.remove("cam-live");
  if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
  if (!navigator.mediaDevices?.getUserMedia) return false;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
    if (v) {
      v.srcObject = state.stream;
      v.muted = true;
      v.setAttribute("playsinline", "");
      v.setAttribute("webkit-playsinline", "");
      await v.play();
    }
    document.body.classList.add("cam-live");
    return waitForCameraFrame(5000);
  } catch (err) {
    console.warn("reopenCameraHard", err);
    return false;
  }
}

/** blit <video> → canvas every frame — unfreezes android webview preview */
function startLiftCamMirror() {
  stopLiftCamMirror();
  const canvas = ui.liftCamCanvas;
  const video = ui.cameraView;
  if (!canvas || !video) return;
  canvas.hidden = false;
  document.body.classList.add("lift-cam");
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) return;

  let lastFrameAt = performance.now();
  let reopenTries = 0;
  let rvfcId = 0;

  const onVideoFrame = () => {
    lastFrameAt = performance.now();
    if (state.inFloorTransition && typeof video.requestVideoFrameCallback === "function") {
      rvfcId = video.requestVideoFrameCallback(onVideoFrame);
    }
  };
  if (typeof video.requestVideoFrameCallback === "function") {
    rvfcId = video.requestVideoFrameCallback(onVideoFrame);
  }

  const tick = () => {
    if (!state.inFloorTransition) {
      if (rvfcId && typeof video.cancelVideoFrameCallback === "function") {
        try { video.cancelVideoFrameCallback(rvfcId); } catch (_) { /* ignore */ }
      }
      stopLiftCamMirror();
      return;
    }
    const w = video.videoWidth | 0;
    const h = video.videoHeight | 0;
    if (w > 2 && h > 2) {
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      try {
        ctx.drawImage(video, 0, 0, w, h);
      } catch (_) { /* ignore */ }
    }
    // no decoded frames → hard reopen (track can stay "live" while frozen)
    const now = performance.now();
    const stalled = typeof video.requestVideoFrameCallback === "function"
      ? now - lastFrameAt > 2500
      : (video.readyState < 2 || video.paused);
    if (stalled && reopenTries < 3 && !liftHandoffBusy) {
      reopenTries += 1;
      lastFrameAt = now;
      void reopenCameraHard({ delayMs: 400 }).then((ok) => {
        if (ok) {
          kickCameraPreview();
          lastFrameAt = performance.now();
          if (typeof video.requestVideoFrameCallback === "function") {
            rvfcId = video.requestVideoFrameCallback(onVideoFrame);
          }
        }
      });
    }
    if (video.paused && state.stream) video.play().catch(() => {});
    liftCamRaf = requestAnimationFrame(tick);
  };
  liftCamRaf = requestAnimationFrame(tick);
}

function stopLiftCamMirror() {
  if (liftCamRaf) {
    cancelAnimationFrame(liftCamRaf);
    liftCamRaf = 0;
  }
  if (ui.liftCamCanvas) {
    ui.liftCamCanvas.hidden = true;
  }
  document.body.classList.remove("lift-cam");
}

/** meters left on current walk leg toward lift/stairs door */
function remToConnectorM() {
  const leg = activeRouteLeg();
  const wps = leg?.waypoints;
  if (!wps?.length || leg?.floor_transition) return Infinity;
  const last = wps[wps.length - 1];
  return Math.hypot(
    Number(state.position.x) - Number(last[0]),
    Number(state.position.y) - Number(last[1]),
  );
}

/** meters from start of current walk leg (usually the lift door) */
function distFromLegStartM() {
  const leg = activeRouteLeg();
  const wps = leg?.waypoints;
  if (!wps?.length || leg?.floor_transition) return Infinity;
  const a = wps[0];
  return Math.hypot(
    Number(state.position.x) - Number(a[0]),
    Number(state.position.y) - Number(a[1]),
  );
}

/** cabin phase: XR → camera if needed; keep PnP running (biased to to_floor) */
async function handoffCameraDuringLift() {
  state.navActive = true;
  state.awaitingLandingLock = true;
  state.landingPose = null;
  state.pendingXrResume = false;
  setRouteBtnMode("stop");

  const tf = activeRouteLeg()?.floor_transition
    || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
  if (tf) showLiftHud(tf);
  showFloorArrows(false);

  try {
    if (state.webXrActive || webXrNav) {
      await handXrToCamera("lift");
    } else if (!state.stream) {
      await ensureCamera().catch(() => false)
        || await reopenCameraHard({ delayMs: 200 });
    }

    state.camOwner = "camera";
    kickCameraPreview();
    startLiftCamMirror();

    if (!state.stream) {
      setStatusMsg("Camera unavailable in lift — use Continue on the next floor");
      return;
    }
    startFloorDetectVpr();
    const dir = tf ? verticalDir(tf.from_floor, tf.to_floor) : "up";
    const to = tf ? normFloor(tf.to_floor) : "—";
    setStatusMsg(
      dir === "down"
        ? `In lift — go down to Floor ${to}`
        : `In lift — go up to Floor ${to}`,
    );
  } catch (err) {
    console.warn("lift camera", err);
    setStatusMsg("Lift camera handoff failed — tap Continue on landing floor");
  }
}

/** ARCore steals the camera from getUserMedia on Android — pause during lift/camera nav */
async function pauseArVioForCamera() {
  stopArTracking();
  if (!window.ArVio || !arVioStarted) return;
  try {
    await ArVio.stop();
  } catch (err) {
    console.warn("ArVio stop", err);
  }
  arVioStarted = false;
}

/** kill + restart live VPR — same path as tapping Localize after a stall */
function restartLiveVprCapture() {
  // waiting for Start AR tap — keep camera free for requestSession
  if (state.pendingXrResume && floorBtnMode === "start_ar") return;
  state.liveVpr.running = false;
  state.liveVpr.requestInFlight = false;
  state.liveVpr.lastCaptureTime = 0;
  state.liveVpr.nextCaptureAllowedAt = 0;
  setTimeout(() => {
    void (async () => {
      if (!state.navActive || state.workflowStep !== "navigation") return;
      if (!state.hasLocalizedPosition || !state.route) return;
      if (state.camOwner === "xr" || state.webXrActive) return;
      if (state.pendingXrResume && floorBtnMode === "start_ar") return;
      if (!state.stream) {
        await ensureCamera().catch(() => false)
          || await reopenCameraHard({ delayMs: 200 }).catch(() => false);
      }
      kickCameraPreview();
      startLiveVprCapture();
    })();
  }, 50);
}

function stopXrHealthWatch() {
  if (xrHealthTimer) {
    clearInterval(xrHealthTimer);
    xrHealthTimer = 0;
  }
}

/** if XR pose dies, give the camera back to getUserMedia + PnP */
function startXrHealthWatch() {
  stopXrHealthWatch();
  xrHealthTimer = setInterval(() => {
    if (!state.webXrActive || !webXrNav) {
      stopXrHealthWatch();
      return;
    }
    // in cabin (floor transition) → camera for PnP
    if (state.inFloorTransition) {
      stopXrHealthWatch();
      void handXrToCamera("lift");
      return;
    }
    if (xrInTrackGrace()) return;
    if (xrTrackLostAt && performance.now() - xrTrackLostAt > XR_TRACK_LOST_MS) {
      stopXrHealthWatch();
      void handXrToCamera("dead");
    }
  }, 300);
}

function clearOfLiftForXr() {
  if (state.inFloorTransition || state.xrResumeBlocked) return false;
  const readyAt = state.xrResumeReadyAt || 0;
  if (performance.now() < readyAt) return false;
  const leg = activeRouteLeg();
  const wps = leg?.waypoints?.length ? leg.waypoints : [];
  if (leg?.floor_transition || wps.length < 2) return false;
  const fromLift = distFromLegStartM();
  const readyFor = performance.now() - readyAt;
  // soft clear of door; force soon so AR isn't stuck waiting on motion/PDR
  if (
    Number.isFinite(fromLift)
    && fromLift < RESUME_XR_CLEAR_M
    && readyFor < RESUME_XR_FORCE_MS
  ) {
    return false;
  }
  return true;
}

/** keep reminding user to tap — never auto requestSession (no user gesture → denied) */
function scheduleXrResumePoll() {
  if (xrResumePollTimer) return;
  xrResumePollTimer = setInterval(() => {
    if (
      !state.pendingXrResume
      || state.webXrActive
      || webXrNav
      || state.xrResumeBlocked
      || !state.navActive
    ) {
      clearInterval(xrResumePollTimer);
      xrResumePollTimer = 0;
      return;
    }
    if (xrResumeBusy) return;
    promptStartArAfterLand();
  }, 2500);
}

function stopXrResumePoll() {
  if (!xrResumePollTimer) return;
  clearInterval(xrResumePollTimer);
  xrResumePollTimer = 0;
}

/**
 * Fresh XR session on Floor B — never resumes the Floor A session.
 * Prefer calling from a click (fromGesture) — Chrome denies immersive-ar without activation.
 */
async function claimXrSlice({ fromGesture = false } = {}) {
  if (xrResumeBusy || liftHandoffBusy) return false;
  if (state.webXrActive || webXrNav) return true;
  if (!state.landingPose) return false;
  if (!fromGesture && !clearOfLiftForXr()) return false;
  if (!state.navActive || state.workflowStep !== "navigation") return false;
  if (state.xrResumeBlocked) return false;

  xrResumeBusy = true;
  state.xrResumeAttempts = (state.xrResumeAttempts || 0) + 1;
  setStartArVisible(false);
  try {
    if (webXrNav) {
      try {
        if (webXrNav.stopAsync) await webXrNav.stopAsync();
        else webXrNav.stop?.();
        webXrNav.dispose?.();
      } catch (_) { /* ignore */ }
      webXrNav = null;
    }
    state.webXrActive = false;

    // camera should already be free from promptStartArAfterLand — keep awaits tiny for gesture
    stopLiveVprCapture({ keepCamera: false });
    state.stream?.getTracks?.().forEach((t) => {
      try { t.stop(); } catch (_) { /* ignore */ }
    });
    state.stream = null;
    try {
      if (ui.cameraView) ui.cameraView.srcObject = null;
    } catch (_) { /* ignore */ }
    document.body.classList.remove("cam-live");
    void pauseArVioForCamera();
    if (ui.arCanvas) ui.arCanvas.style.display = "";

    const lp = state.landingPose;
    const ok = await startWebXrNav({
      facilityPose: {
        x: Number(lp.x),
        y: Number(lp.y),
        heading: Number(lp.heading),
      },
      fresh: true,
      skipCamRelease: true,
      fromGesture: Boolean(fromGesture),
    });
    if (!ok) {
      state.camOwner = "camera";
      state.pendingXrResume = true;
      state.xrResumeBlocked = false;
      const camOk = await ensureCamera().catch(() => false)
        || await reopenCameraHard({ delayMs: 400 });
      kickCameraPreview();
      startCameraNav();
      if (camOk) restartLiveVprCapture();
      // do not auto requestSession again — wait for another tap
      promptStartArAfterLand();
      scheduleXrResumePoll();
      setStatusMsg("AR denied — tap Start AR again");
      return false;
    }
    state.camOwner = "xr";
    state.pendingXrResume = false;
    state.awaitingLandingLock = false;
    state.xrResumeAttempts = 0;
    setStartArVisible(false);
    stopXrResumePoll();
    startXrHealthWatch();
    setStatusChip("AR · Floor B", "live");
    setStatusMsg("Fresh AR session — calibrated from landing PnP");
    setHint("New XR session on this floor (not a resume of the lift session)", true);
    return true;
  } finally {
    xrResumeBusy = false;
  }
}

/**
 * Time-slice: getUserMedia + PnP owns the camera.
 * reason "lift" = approaching cabin; "dead" = XR tracking failed.
 */
async function handXrToCamera(reason = "lift") {
  if (liftHandoffBusy) return;
  liftHandoffBusy = true;
  stopXrHealthWatch();
  stopXrLiftWatch();
  state.navActive = true;
  setRouteBtnMode("stop");
  void logClientDiagnostic({
    event: "webxr_end",
    nav_mode: "camera",
    reason: String(reason || "lift"),
  });

  try {
    state.suppressXrResume = true;
    const nav = webXrNav;
    webXrNav = null;
    state.webXrActive = false;
    state.camOwner = "camera";
    ui.arStage?.classList.remove("webxr-on");
    if (ui.arCanvas) ui.arCanvas.style.display = "none";

    if (nav) {
      try {
        if (nav.stopAsync) await nav.stopAsync();
        else nav.stop?.();
        nav.dispose?.();
      } catch (err) {
        console.warn("handXrToCamera stop", err);
      }
    }

    await pauseArVioForCamera();
    let camOk = await reopenCameraHard({ delayMs: 700 });
    if (!camOk) camOk = await reopenCameraHard({ delayMs: 1000 });
    if (!camOk) camOk = await ensureCamera().catch(() => false);
    kickCameraPreview();
    showFloorArrows(false);
    startCameraNav();
    if (camOk) restartLiveVprCapture();

    if (reason === "dead") {
      // don't fight XR again on this floor
      state.xrResumeBlocked = true;
      state.pendingXrResume = false;
      setStatusChip("Camera · XR lost", "warn");
      setStatusMsg("AR lost tracking — camera + PnP until the bay");
      setHint("Follow the turn banner until AR is available", true);
    } else {
      // approaching / in lift — wait for PnP on destination floor
      state.awaitingLandingLock = true;
      state.landingPose = null;
      state.pendingXrResume = false;
      state.xrResumeBlocked = false;
      setStatusChip("Camera · lift", "live");
      setStatusMsg("Camera + PnP through the lift — fresh AR after floor lock");
      setHint("No XR in the cabin — AR restarts from PnP on the next floor", true);
    }
  } catch (err) {
    console.warn("handXrToCamera", err);
  } finally {
    liftHandoffBusy = false;
    state.suppressXrResume = false;
  }
}

/**
 * After landing PnP — prompt for a tap. Auto requestSession is always denied on Chrome.
 */
function maybeResumeXrAfterPnp(pos) {
  if (!state.pendingXrResume || state.xrResumeBlocked) return;
  if (!state.landingPose) return;
  if (!state.navActive || state.workflowStep !== "navigation") return;
  if (state.inFloorTransition || state.webXrActive || webXrNav) return;
  if (liftHandoffBusy || state.suppressXrResume || xrResumeBusy) return;
  if (!(Number(state.landingPose.confidence) > 0) && !(Number(pos?.confidence) > 0)) return;

  promptStartArAfterLand();
  scheduleXrResumePoll();
}

/** mutate state onto the walk leg for toFloor (no HUD refresh) */
function landOnFloor(toFloor, transitionIdx = state.activeLegIndex || 0) {
  if (!state.route?.legs?.length) return false;
  const dest = normFloor(toFloor || destFloorId());
  state.position.floor = dest;
  if (ui.metricFloor) ui.metricFloor.textContent = dest;

  let walkIdx = -1;
  for (let i = Math.max(0, transitionIdx); i < state.route.legs.length; i += 1) {
    const leg = state.route.legs[i];
    if (leg?.floor_transition) continue;
    if (sameFloor(leg.floor, dest) && (leg.waypoints?.length || 0) > 0) {
      walkIdx = i;
      break;
    }
  }
  if (walkIdx < 0) {
    walkIdx = state.route.legs.findIndex((leg) => (
      !leg.floor_transition
      && sameFloor(leg.floor, dest)
      && (leg.waypoints?.length || 0) > 0
    ));
  }
  state.activeLegIndex = walkIdx >= 0
    ? walkIdx
    : Math.min(transitionIdx + 1, state.route.legs.length - 1);
  state.inFloorTransition = false;
  state.floorTransitionAt = 0;
  state.arrivedShown = false;
  // camera owns cam; fresh XR only after landingPose + clear lobby
  state.camOwner = "camera";
  state.xrResumeBlocked = false;
  state.xrResumeReadyAt = performance.now() + RESUME_XR_SETTLE_MS;
  state.motion.cumulativeMotion = 0;
  state.walkSinceLand = 0;
  // pendingXrResume / landingPose set by applyVprFix on real lock (not here)
  hideArrivalCelebration();
  hideLiftHud();
  stopLiftCamMirror();
  if (ui.arCanvas) ui.arCanvas.style.display = "none";
  // keep Start AR CTA if PnP already locked this landing
  if (!(state.pendingXrResume && state.landingPose)) {
    setFloorContinueVisible(false);
  }
  void pauseArVioForCamera().then(async () => {
    if (state.pendingXrResume && state.landingPose) {
      promptStartArAfterLand();
      return;
    }
    const ok = await ensureCamera().catch(() => false)
      || await reopenCameraHard({ delayMs: 300 });
    kickCameraPreview();
    if (state.navActive) {
      showFloorArrows(false);
      setStatusChip("Camera · Floor B", "live");
      setStatusMsg(
        state.landingPose
          ? "Floor locked — tap Start AR"
          : "On landing floor — need PnP lock before AR",
      );
      if (ok) restartLiveVprCapture();
    }
  });
  return true;
}

function refreshPathAfterFloorChange({ redraw = false } = {}) {
  const leg = activeRouteLeg();
  const wps = leg?.waypoints?.length ? leg.waypoints : [];
  if (redraw && wps.length >= 2) {
    drawRoute(wps, ui.slotInput?.value?.trim() || "");
  }

  hideLiftHud();

  // never continue / patch an old XR session across floors
  if (webXrNav || state.webXrActive) {
    try {
      webXrNav?.stop?.();
      webXrNav?.dispose?.();
    } catch (_) { /* ignore */ }
    webXrNav = null;
    state.webXrActive = false;
    state.camOwner = "camera";
    ui.arStage?.classList.remove("webxr-on");
  }

  const fl = normFloor(state.position.floor);
  setStatusChip(`Floor ${fl}`, "live");
  setHint("Follow the path on this floor", true);

  if (state.navActive && !state.webXrActive) {
    showFloorArrows(false);
    startLiveVprCapture();
    setStatusMsg(
      state.pendingXrResume && state.landingPose
        ? `Floor ${fl} locked — AR starting shortly`
        : state.awaitingLandingLock
          ? `Floor ${fl} — pointing for PnP lock…`
          : `Floor ${fl} — camera guidance`,
    );
  }
}

/** leave elevator/stairs — show next floor walk banner + AR path */
function exitFloorTransition(forcedFloor = null) {
  if (!state.route?.legs?.length) return false;

  let idx = state.activeLegIndex || 0;
  let tf = state.route.legs[idx]?.floor_transition || null;
  if (!tf) {
    const upcoming = nextTransitionLeg(idx);
    tf = upcoming?.floor_transition || null;
    if (upcoming) {
      const found = state.route.legs.indexOf(upcoming);
      if (found >= 0) idx = found;
    }
  }
  const toFloor = normFloor(forcedFloor || tf?.to_floor || destFloorId());
  if (!landOnFloor(toFloor, idx)) return false;
  refreshPathAfterFloorChange({ redraw: true });
  updateRouteProgress();
  return true;
}

function syncActiveLeg() {
  if (!state.route?.legs?.length) {
    state.activeLegIndex = 0;
    state.inFloorTransition = false;
    return;
  }

  // sticky: once in a lift/stairs leg, stay until VPR reports the landing floor
  if (state.inFloorTransition) {
    let cur = state.route.legs[state.activeLegIndex];
    if (!cur?.floor_transition) {
      enterConnectorLeg();
      cur = state.route.legs[state.activeLegIndex];
    }
    if (cur?.floor_transition) {
      const userFloor = normFloor(state.position.floor);
      const toFloor = normFloor(cur.floor_transition.to_floor);
      const fromFloor = normFloor(cur.floor_transition.from_floor);
      if (sameFloor(userFloor, toFloor)) {
        landOnFloor(toFloor, state.activeLegIndex);
        // fall through — pick walk leg + let updateRouteProgress paint banner
      } else if (userFloor && !sameFloor(userFloor, fromFloor) && !sameFloor(userFloor, toFloor)) {
        state.inFloorTransition = false;
      } else {
        return; // still riding the connector
      }
    }
  }

  if (state.inFloorTransition) return;

  const userFloor = normFloor(state.position.floor);
  let idx = state.route.legs.findIndex((leg) => {
    if (leg.floor_transition) return false;
    return sameFloor(leg.floor, userFloor) && (leg.waypoints?.length || 0) > 0;
  });
  if (idx < 0) idx = Math.max(0, state.activeLegIndex || 0);
  state.activeLegIndex = idx;
}

function nextTurnGuidance(waypoints, traveledDistance = 0) {
  // multi-floor: prefer active leg transition / walk instruction
  const leg = activeRouteLeg();
  if (leg?.floor_transition) {
    const tf = leg.floor_transition;
    const dir = verticalDir(tf.from_floor, tf.to_floor);
    const go = dir === "down" ? "Go down" : dir === "up" ? "Go up" : "Change floor";
    // leg.distance is routing penalty (e.g. 12m), NOT walking remaining
    return {
      label: `${go} to Floor ${tf.to_floor}`,
      distanceM: 0,
      isArrival: false,
      kind: dir,
      isFloorChange: true,
    };
  }
  if (leg?.waypoints?.length >= 2) {
    const progress = routeProgressForWaypoints(leg.waypoints);
    const upcoming = nextTransitionLeg((state.activeLegIndex || 0) + 1);
    // near the lift/stairs — keep showing meters-to-door (not elevator penalty)
    if (upcoming?.floor_transition && progress.remainingDistance < 12) {
      const tf = upcoming.floor_transition;
      const cid = tf.connector_id || "connector";
      const rem = Math.max(0, progress.remainingDistance);
      const atDoor = rem <= ENTER_CONNECTOR_M;
      const dir = verticalDir(tf.from_floor, tf.to_floor);
      const go = dir === "down" ? "Go down" : dir === "up" ? "Go up" : "Change floor";
      return {
        label: atDoor
          ? `${go} · ${connectorLabel(tf)} (${cid}) to Floor ${tf.to_floor}`
          : `Walk to ${cid} · then ${go.toLowerCase()} to Floor ${tf.to_floor}`,
        // don't freeze a stale ~2m while standing in the lift
        distanceM: atDoor ? 0 : rem,
        isArrival: false,
        kind: atDoor ? dir : "straight",
        isFloorChange: true,
      };
    }
    const instructions = buildTurnInstructions(leg.waypoints);
    for (const instruction of instructions) {
      const remaining = instruction.distanceAlongRoute - progress.distanceAlongRoute;
      if (!instruction.isArrival && remaining < -1) continue;
      // last walk cue before a lift: prefer route leg text ("Walk to PL3-N03")
      if (instruction.isArrival && upcoming?.floor_transition) {
        const tf = upcoming.floor_transition;
        return {
          label: leg.instruction || `Walk to ${tf.connector_id || "lift"}`,
          distanceM: Math.max(0, remaining),
          isArrival: false,
          kind: "straight",
          isFloorChange: false,
        };
      }
      return {
        label: instruction.label,
        distanceM: Math.max(0, remaining),
        isArrival: !!instruction.isArrival,
        kind: turnKind(instruction.label),
      };
    }
    return {
      label: leg.instruction || "Continue",
      distanceM: progress.remainingDistance,
      isArrival: progress.remainingDistance < ARRIVE_M,
      kind: progress.remainingDistance < ARRIVE_M ? "arrive" : "straight",
    };
  }

  const instructions = buildTurnInstructions(waypoints);
  for (const instruction of instructions) {
    const remaining = instruction.distanceAlongRoute - traveledDistance;
    if (!instruction.isArrival && remaining < -1) continue;
    return {
      label: instruction.label,
      distanceM: Math.max(0, remaining),
      isArrival: !!instruction.isArrival,
      kind: turnKind(instruction.label),
    };
  }
  return { label: "Continue", distanceM: 0, isArrival: true, kind: "arrive" };
}

function routeProgressForWaypoints(waypoints) {
  const position = state.position;
  let cumulativeDistance = 0;
  let nearestOffset = Infinity;
  let distanceAlongRoute = 0;
  for (let index = 1; index < waypoints.length; index += 1) {
    const [startX, startY] = waypoints[index - 1];
    const [endX, endY] = waypoints[index];
    const segmentX = endX - startX;
    const segmentY = endY - startY;
    const segmentLengthSquared = segmentX * segmentX + segmentY * segmentY;
    const segmentLength = Math.sqrt(segmentLengthSquared);
    if (segmentLength === 0) continue;
    const projection = Math.max(0, Math.min(1,
      ((position.x - startX) * segmentX + (position.y - startY) * segmentY) / segmentLengthSquared,
    ));
    const projectedX = startX + projection * segmentX;
    const projectedY = startY + projection * segmentY;
    const offset = Math.hypot(position.x - projectedX, position.y - projectedY);
    if (offset < nearestOffset) {
      nearestOffset = offset;
      distanceAlongRoute = cumulativeDistance + projection * segmentLength;
    }
    cumulativeDistance += segmentLength;
  }
  return {
    distanceAlongRoute,
    remainingDistance: Math.max(0, cumulativeDistance - distanceAlongRoute),
  };
}

function turnKind(label) {
  const t = String(label || "").toLowerCase();
  if (t.includes("go down") || t.includes("down to")) return "down";
  if (t.includes("go up") || t.includes("up to")) return "up";
  if (t.includes("left")) return "left";
  if (t.includes("right")) return "right";
  if (t.includes("around") || t.includes("u-turn")) return "uturn";
  if (t.includes("arrive") || t.includes("destination") || t.includes("reached")) return "arrive";
  return "straight";
}

// distinct glyphs — never rotate a straight arrow for left/right
const TURN_ICON_PATHS = {
  straight: "M12 20V4M5 11l7-7 7 7",
  up: "M12 20V4M5 11l7-7 7 7",
  down: "M12 4v16M5 13l7 7 7-7",
  left: "M18 20v-7a4 4 0 0 0-4-4H5M9 5 5 9l4 4",
  right: "M6 20v-7a4 4 0 0 1 4-4h9M15 5l4 4-4 4",
  uturn: "M9 7v7a3 3 0 0 0 6 0V8M15 8l-3-3M15 8l3-3",
  arrive: "M12 3v11M8 11l4 4 4-4M6 19h12",
};

function setTurnIcon(kind) {
  if (!ui.turnIcon) return;
  const path = ui.turnIcon.querySelector("path");
  const key = TURN_ICON_PATHS[kind] ? kind : "straight";
  if (path) path.setAttribute("d", TURN_ICON_PATHS[key]);
  ui.turnIcon.style.transform = "none";
  ui.turnIcon.style.opacity = kind === "arrive" ? "0.55" : "1";
  ui.turnIcon.dataset.kind = key;
}

/** active walk polyline for AR / HUD */
function guideWaypoints() {
  const leg = activeRouteLeg();
  if (leg?.waypoints?.length >= 2) return leg.waypoints;
  return state.route?.waypoints || [];
}

/** +turn = left — same cross-product as buildTurnInstructions */
function nextTurnFromLegs(legs, traveledDistance = 0) {
  const list = legs || [];
  let along = 0;
  for (let i = 0; i < list.length; i++) {
    const leg = list[i];
    const abs = Math.abs(leg.turn || 0);
    if (i > 0 && abs >= 30 && traveledDistance <= along + 1.5) {
      const kind = abs >= 150 ? "uturn" : leg.turn > 0 ? "left" : "right";
      const label =
        kind === "uturn" ? "Turn around" : kind === "left" ? "Turn left" : "Turn right";
      return {
        label,
        kind,
        distanceM: Math.max(0, along - traveledDistance),
        isArrival: false,
      };
    }
    along += leg.dist || 0;
  }
  const remain = Math.max(0, along - traveledDistance);
  // current-floor polyline ends at the lift — not the parking bay
  if (remain < 12 && hasFloorChangeAhead()) {
    const tf = nextTransitionLeg((state.activeLegIndex || 0) + 1)?.floor_transition
      || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition
      || activeRouteLeg()?.floor_transition;
    const ban = elevatorBanner(tf, remain);
    return { ...ban, kind: ban.kind };
  }
  if (remain < ARRIVE_M) {
    if (!onDestFloor() || hasFloorChangeAhead()) {
      const tf = nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
      return elevatorBanner(tf, 0);
    }
    return { label: "You've arrived", kind: "arrive", distanceM: 0, isArrival: true };
  }
  return { label: "Continue straight", kind: "straight", distanceM: remain, isArrival: false };
}

function normHeadingDelta(targetDeg, headingDeg) {
  return ((targetDeg - headingDeg + 540) % 360) - 180;
}

/** facility bearing: 0° = +Y, 90° = +X (same as PnP / PDR) */
function bearingTo(ax, ay, bx, by) {
  let d = (Math.atan2(bx - ax, by - ay) * 180) / Math.PI;
  if (d < 0) d += 360;
  return d;
}

/** point ahead on polyline from current position (meters along route) */
function lookAheadOnRoute(waypoints, x, y, aheadM = 6) {
  const wps = waypoints || [];
  if (wps.length < 2) return null;

  let bestD = Infinity;
  let bestI = 0;
  let bestT = 0;
  let acc = 0;
  let bestAcc = 0;
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i];
    const b = wps[i + 1];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy || 1e-12;
    const len = Math.sqrt(len2);
    let t = ((x - a[0]) * dx + (y - a[1]) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const px = a[0] + dx * t;
    const py = a[1] + dy * t;
    const d = Math.hypot(x - px, y - py);
    if (d < bestD) {
      bestD = d;
      bestI = i;
      bestT = t;
      bestAcc = acc;
    }
    acc += len;
  }

  let left = aheadM;
  let i = bestI;
  let t = bestT;
  while (i < wps.length - 1 && left > 0) {
    const a = wps[i];
    const b = wps[i + 1];
    const segLen = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1e-6;
    const remainOnSeg = segLen * (1 - t);
    if (left <= remainOnSeg) {
      const u = t + left / segLen;
      return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
    }
    left -= remainOnSeg;
    i += 1;
    t = 0;
  }
  const last = wps[wps.length - 1];
  return [Number(last[0]), Number(last[1])];
}

/**
 * tilt chevrons from real heading error vs route look-ahead
 * (not a fake left/right from the banner text alone)
 */
function updateFloorArrowTilt(next) {
  if (!ui.floorPath || !state.navActive || state.webXrActive) return;
  const wps = guideWaypoints();
  if (!wps || wps.length < 2) return;

  // "Turn left · 4 m" → look along the straight approach (not past the corner)
  // only peek past the turn when you're almost there
  let ahead = 5;
  if (next && (next.kind === "left" || next.kind === "right" || next.kind === "uturn")) {
    const d = Number(next.distanceM);
    if (Number.isFinite(d)) {
      if (d > 3.5) {
        // stay on the straight segment before the turn
        ahead = Math.min(5, Math.max(1.5, d - 0.8));
      } else {
        // near the corner — lean into the turn
        ahead = Math.max(3, d + 2.2);
      }
    }
  }

  const target = lookAheadOnRoute(wps, state.position.x, state.position.y, ahead);
  if (!target) return;

  const want = bearingTo(state.position.x, state.position.y, target[0], target[1]);
  const rel = normHeadingDelta(want, state.position.heading);
  // screen lean: negative = left, positive = right (matches needed turn)
  const z = Math.max(-55, Math.min(55, rel));
  ui.floorPath.style.transform = `translateX(-50%) rotateX(62deg) rotateZ(${z}deg)`;
}

function showFloorArrows(on) {
  // DOM chevrons only for legacy; guidance arrows live in WebXR — keep camera/PnP clear
  if (!ui.floorArrows) return;
  ui.floorArrows.hidden = true;
  if (ui.floorPath) {
    ui.floorPath.style.transform = "translateX(-50%) rotateX(62deg) rotateZ(0deg)";
  }
}

function paintTurnBanner(next) {
  if (!ui.turnBanner || !next) return;
  ui.turnBanner.hidden = false;
  if (ui.turnLabel) ui.turnLabel.textContent = next.label;
  if (ui.turnDistance) {
    const now =
      next.isArrival
      || next.distanceM < 1
      || (next.isFloorChange && next.distanceM < 1);
    ui.turnDistance.textContent = now ? "Now" : `${Math.round(next.distanceM)} m`;
  }
  const kind = next.kind || turnKind(next.label) || "straight";
  setTurnIcon(kind);
  if (state.inFloorTransition && next.isFloorChange) {
    const tf = activeRouteLeg()?.floor_transition;
    if (tf) showLiftHud(tf);
  } else if (!next.isFloorChange) {
    hideLiftHud();
  }
  updateFloorArrowTilt(next);
}

function updateTurnHud(traveledDistance = 0) {
  const slotId = ui.slotInput?.value?.trim() || "—";
  if (ui.hudSlot) ui.hudSlot.textContent = slotId;
  if (ui.selectedSlotSummary) ui.selectedSlotSummary.textContent = slotId;

  if (!state.route || !ui.turnBanner) {
    if (ui.turnBanner) ui.turnBanner.hidden = true;
    return;
  }

  if (state.arrivedShown) {
    ui.turnBanner.hidden = Boolean(ui.arriveOverlay && !ui.arriveOverlay.hidden);
    if (!ui.turnBanner.hidden) {
      if (ui.turnLabel) ui.turnLabel.textContent = "You have reached";
      if (ui.turnDistance) ui.turnDistance.textContent = slotId;
      setTurnIcon("arrive");
    }
    return;
  }

  const next = nextTurnGuidance(state.route.waypoints, traveledDistance);
  paintTurnBanner(next);
  if (!next.isFloorChange && !state.inFloorTransition) {
    setHint("", false);
  }
}

function circularMeanDeg(degrees) {
  let sinSum = 0;
  let cosSum = 0;
  for (const d of degrees) {
    sinSum += Math.sin((d * Math.PI) / 180);
    cosSum += Math.cos((d * Math.PI) / 180);
  }
  let mean = (Math.atan2(sinSum, cosSum) * 180) / Math.PI;
  if (mean < 0) mean += 360;
  return mean;
}

function circularSpreadDeg(samples) {
  if (samples.length < 2) return 0;
  const sinSum = samples.reduce((sum, d) => sum + Math.sin((d * Math.PI) / 180), 0);
  const cosSum = samples.reduce((sum, d) => sum + Math.cos((d * Math.PI) / 180), 0);
  const r = Math.hypot(sinSum, cosSum) / samples.length;
  return Math.min(180, Math.sqrt(-2 * Math.log(Math.max(r, 1e-9))) * 180 / Math.PI);
}

function markCalibrationSkipped() {
  state.calibration.completed.stillness = true;
  state.calibration.completed.compass = true;
  state.calibration.completed.walk = true;
  state.calibration.status = "skipped";
}

function updateCalibrationReadiness() {
  if (SKIP_CALIBRATION) markCalibrationSkipped();
  const allDone = Object.values(state.calibration.completed).every(Boolean);
  state.sessionReady = allDone;
  setStatusChip(allDone ? (SKIP_CALIBRATION ? "Ready" : "Calibrated") : "Ready", allDone ? "live" : "warn");
  ui.localizeBtn.disabled = !allDone;
  updateWorkflowControls();
}

function canEnterWorkflowStep(step) {
  if (step === "destination") return true;
  if (step === "calibration") {
    if (SKIP_CALIBRATION) return false;
    return Boolean(ui.slotInput.value.trim());
  }
  if (step === "navigation") {
    if (SKIP_CALIBRATION) return Boolean(ui.slotInput?.value?.trim());
    return state.sessionReady;
  }
  return false;
}

function updateWorkflowControls() {
  const hasDestination = Boolean(ui.slotInput.value.trim());
  ui.destinationContinueBtn.disabled = !hasDestination;
  if (ui.calibrationContinueBtn) ui.calibrationContinueBtn.disabled = !state.sessionReady;
  if (ui.selectedSlotSummary) {
  ui.selectedSlotSummary.textContent = hasDestination ? ui.slotInput.value.trim() : "No slot selected";
  }
  if (ui.hudSlot) ui.hudSlot.textContent = hasDestination ? ui.slotInput.value.trim() : "—";

  (ui.calibrationSteps || []).forEach((node) => {
    const key = node.dataset.step === "magnetometer" ? "compass" : node.dataset.step;
    const done = !!state.calibration.completed[key];
    node.classList.toggle("is-done", done);
  });

  ui.workflowSteps.forEach((button) => {
    const step = button.dataset.workflowStep;
    const isCurrent = step === state.workflowStep;
    const isComplete = step === "destination" ? hasDestination : step === "calibration" && state.sessionReady;
    button.disabled = !canEnterWorkflowStep(step);
    button.classList.toggle("is-active", isCurrent);
    button.classList.toggle("is-complete", isComplete);
    if (isCurrent) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
}

let liveHeartbeatTimer = null;
let arTrackTimer = null;
let arVioStarted = false;

function endLiveLocalization() {
  if (liveHeartbeatTimer) {
    clearInterval(liveHeartbeatTimer);
    liveHeartbeatTimer = null;
  }
  stopArTracking();
  // clear cyan avatar in 3D viewer for this device
  const url = `/position/${encodeURIComponent(state.deviceId)}/end`;
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url);
      return;
    }
  } catch (_) { /* fall through */ }
  fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}

function startLiveHeartbeat() {
  if (liveHeartbeatTimer) return;
  liveHeartbeatTimer = setInterval(() => {
    if (state.workflowStep !== "navigation" || !state.hasLocalizedPosition) return;
    fetch(`/position/${encodeURIComponent(state.deviceId)}/heartbeat`, { method: "POST" })
      .catch(() => {});
  }, 45000);
}

async function ensureArVio() {
  if (!window.ArVio) return false;
  if (arVioStarted) return true;
  try {
    const ok = await ArVio.isAvailable();
    if (!ok) return false;
    await ArVio.start();
    arVioStarted = true;
    return true;
  } catch (err) {
    console.warn("ARCore unavailable", err);
    return false;
  }
}

function stopArTracking() {
  if (arTrackTimer) {
    clearInterval(arTrackTimer);
    arTrackTimer = null;
  }
}

function startArTracking() {
  stopArTracking();
  arTrackTimer = setInterval(async () => {
    if (!state.hasLocalizedPosition || state.workflowStep !== "navigation") return;
    if (!window.ArVio || !arVioStarted) return;
    try {
      const pose = await ArVio.getPose();
      if (!pose || pose.tracking !== "tracking") return;
      const res = await fetch(`/track/${encodeURIComponent(state.deviceId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timestamp: pose.timestamp || Date.now() / 1000,
          x: pose.x,
          y: pose.y,
          z: pose.z,
          qw: pose.qw,
          qx: pose.qx,
          qy: pose.qy,
          qz: pose.qz,
          tracking_status: pose.tracking,
        }),
      });
      if (!res.ok) return;
      const pos = await res.json();
      if (!pos.tracking) return;
      state.position = {
        floor: pos.floor,
        x: pos.x,
        y: pos.y,
        heading: pos.heading,
        confidence: pos.confidence,
        tracking: true,
      };
      setStatusChip("ARCore VIO", "live");
      if (ui.positionState) ui.positionState.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)}`;
      ui.metricCoords.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)} m`;
      ui.metricHeading.textContent = `${Math.round(pos.heading)}°`;
      updateRouteStatus();
    } catch (_) { /* keep last */ }
  }, 200);
}

function setWorkflowStep(step, moveFocus = false) {
  // calibration sheet disabled — jump straight to navigation
  if (SKIP_CALIBRATION && step === "calibration") {
    step = "navigation";
  }
  if (!canEnterWorkflowStep(step)) return;

  if (state.workflowStep === "navigation" && step !== "navigation") {
    stopLiveVprCapture({ keepCamera: true });
    endLiveLocalization();
  }

  state.workflowStep = step;
  ui.workflowPanels.forEach((panel) => {
    panel.hidden = panel.dataset.workflowPanel !== step;
  });
  updateWorkflowControls();

  if (step === "destination") {
    const onHome = !document.getElementById("shell")?.hidden;
    if (!onHome) showSheet("slot");
    else hideSheets();
    setHint("Choose your parking slot to begin", true);
    setStatusMsg(SKIP_CALIBRATION
      ? "Select a parking slot, then continue to localize."
      : "Select a parking slot, then continue to calibration.");
  } else if (step === "calibration") {
    showSheet("cal");
    setHint("Calibrate sensors before localizing", true);
    setStatusMsg("Complete stillness, compass, and walk calibration.");
  } else if (step === "navigation") {
    hideSheets();
    setHint(state.hasLocalizedPosition
      ? (state.route ? "" : "Tap Start nav for AR chevrons")
      : "Point the camera and tap Localize", !state.route);
    setStatusMsg(state.hasLocalizedPosition
      ? "Localized. Start nav for WebXR floor arrows."
      : "Camera ready. Tap Localize to lock your position.");
    ensureCamera().catch(() => {});
    ensureArVio().catch(() => {});
  }

  if (moveFocus) {
    const focusEl = step === "destination"
      ? ui.slotInput
      : step === "calibration"
        ? ui.calibrationContinueBtn
        : ui.localizeBtn;
    focusEl?.focus?.({ preventScroll: true });
  }
}

function setCalibrationStep(stepLabel, stepIndex = null, stepKey = null) {
  state.calibration.stepLabel = stepLabel;
  if (ui.calibrationStatus) ui.calibrationStatus.textContent = stepLabel;
  ui.calibrationSteps.forEach((node, index) => {
    node.classList.toggle("is-active", stepIndex === index || (stepIndex === null && index === state.calibration.step));
  });

  const animationKey = stepKey || state.calibration.stepKey || "stillness";
  state.calibration.stepKey = animationKey;
  if (ui.calibrationVisual) {
  ui.calibrationVisual.classList.remove("step-stillness", "step-compass", "step-walk");
  ui.calibrationVisual.classList.add(`step-${animationKey}`);
  }

  const hints = {
    stillness: "Hold the phone perfectly still to stabilize the sensors.",
    compass: "Rotate the phone slowly in a figure-8 motion to calibrate heading.",
    walk: "Take a few steady steps forward to tune your stride.",
  };
  if (ui.calibrationHint) ui.calibrationHint.textContent = hints[animationKey] || stepLabel;
}

async function requestPermissions() {
  if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
    const permission = await DeviceMotionEvent.requestPermission();
    if (permission !== "granted") throw new Error("Motion permission denied");
  }

  if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
    const permission = await DeviceOrientationEvent.requestPermission();
    if (permission !== "granted") {
      console.warn("Orientation permission denied; using relative fallback mode.");
    }
  }

  window.addEventListener("devicemotion", onDeviceMotion, true);
  window.addEventListener("deviceorientation", onDeviceOrientation, true);
  window.addEventListener("deviceorientationabsolute", onDeviceOrientation, true);
}

// Calculates true tilt-compensated compass heading from DeviceOrientation Euler angles (alpha, beta, gamma).
// Handles the phone being held flat (facing up), tilted upright (VPR camera capture), or pitched/rolled.
function computeEulerHeading(event) {
  if (!event) return null;

  // iOS Safari provides webkitCompassHeading directly (0-360 clockwise from North)
  if (typeof event.webkitCompassHeading === "number" && !Number.isNaN(event.webkitCompassHeading)) {
    return event.webkitCompassHeading;
  }

  if (typeof event.alpha !== "number" || Number.isNaN(event.alpha)) {
    return null;
  }

  const alpha = event.alpha;
  const beta = event.beta;
  const gamma = event.gamma;

  // If beta/gamma aren't available, fall back to flat alpha
  if (beta === null || beta === undefined || gamma === null || gamma === undefined) {
    return (360 - alpha) % 360;
  }

  const degToRad = Math.PI / 180;
  const _radAlpha = alpha * degToRad; // Z-axis rotation
  const _radBeta = beta * degToRad;   // X-axis tilt [-180, 180]
  const _radGamma = gamma * degToRad; // Y-axis roll [-90, 90]

  const cA = Math.cos(_radAlpha);
  const sA = Math.sin(_radAlpha);
  const cB = Math.cos(_radBeta);
  const sB = Math.sin(_radBeta);
  const cG = Math.cos(_radGamma);
  const sG = Math.sin(_radGamma);

  // Unit vector pointing out along device's forward direction (+Y on screen, pointing towards phone top)
  // Projected onto the horizontal world ground plane:
  // Vx = -cos(alpha)*sin(gamma) - sin(alpha)*sin(beta)*cos(gamma)
  // Vy = -sin(alpha)*sin(gamma) + cos(alpha)*sin(beta)*cos(gamma)
  const vx = -cA * sG - sA * sB * cG;
  const vy = -sA * sG + cA * sB * cG;

  let heading = (Math.atan2(vx, vy) * 180) / Math.PI;
  if (heading < 0) heading += 360;

  return heading;
}

function onDeviceOrientation(event) {
  const heading = computeEulerHeading(event);
  if (heading === null || !Number.isFinite(heading)) return;

  state.lastCompassSample = heading;
  state.rawHeadingHistory.push(heading);
  if (state.rawHeadingHistory.length > 10) state.rawHeadingHistory.shift();
  updatePositionFromHeading();
}

function onDeviceMotion(event) {
  const acceleration = event.accelerationIncludingGravity || event.acceleration;
  if (acceleration && state.route && state.workflowStep === "navigation") {
    const magnitude = Math.hypot(acceleration.x || 0, acceleration.y || 0, acceleration.z || 0);
    const motion = state.motion;
    if (motion.lastMagnitude !== null) {
      const magnitudeChange = Math.abs(magnitude - motion.lastMagnitude);
      motion.cumulativeMotion += magnitudeChange;
      if (state.pendingXrResume) state.walkSinceLand += magnitudeChange;
      if (magnitudeChange >= 0.25) motion.lastMeaningfulMotionAt = performance.now();
    }
    motion.lastMagnitude = magnitude;

    if (state.hasLocalizedPosition) {
      motion.gravityEstimate = motion.gravityEstimate === null
        ? magnitude
        : 0.92 * motion.gravityEstimate + 0.08 * magnitude;
      detectNavigationStep(magnitude - motion.gravityEstimate, performance.now());
    }
  }

}

function detectNavigationStep(dynamicAcceleration, now) {
  const motion = state.motion;
  const threshold = 1.2;

  if (dynamicAcceleration > threshold) {
    if (!motion.stepArmed && now - motion.lastStepTime > 280) {
      motion.stepArmed = true;
      motion.peakAcceleration = dynamicAcceleration;
      motion.valleyAcceleration = 0;
    } else if (motion.stepArmed && dynamicAcceleration > motion.peakAcceleration) {
      motion.peakAcceleration = dynamicAcceleration;
    }
    return;
  }

  if (!motion.stepArmed) return;
  motion.valleyAcceleration = Math.min(motion.valleyAcceleration, dynamicAcceleration);
  if (dynamicAcceleration >= 0.1) return;

  motion.stepArmed = false;
  motion.lastStepTime = now;
  advancePositionByStep(0.7 * state.calibration.strideScaleFactor);
}

function advancePositionByStep(stepLength) {
  const heading = state.position.heading * Math.PI / 180;
  state.position.x += stepLength * Math.sin(heading);
  state.position.y += stepLength * Math.cos(heading);
  state.position.tracking = true;
  state.position.confidence = Math.max(0.08, state.position.confidence * 0.985);
  setStatusChip("Tracking (PDR)", "live");
  setStatusMsg("Live position from steps / VIO. Localize again to correct drift.");
  updateRouteStatus();
}

async function runCalibrationStep(stepKey) {
  const ordered = ["stillness", "compass", "walk"];
  if (!ordered.includes(stepKey)) return;

  if (state.calibration.status === "running") return;

  state.calibration.status = "running";
  ui.calibrationButtons.forEach((button) => {
    button.disabled = true;
  });

  try {
    await requestPermissions();
  } catch (error) {
    setCalibrationStep(`Calibration needs motion access: ${error.message}`);
    state.calibration.status = "idle";
    ui.calibrationButtons.forEach((button) => {
      button.disabled = false;
    });
    return;
  }

  const runner = {
    stillness: calibrateStillness,
    compass: calibrateCompass,
    walk: calibrateWalk,
  };

  await runner[stepKey]();

  state.calibration.status = "idle";
  updateCalibrationReadiness();
  const nextIndex = ordered.findIndex((key) => !state.calibration.completed[key]);
  if (nextIndex !== -1) {
    const nextKey = ordered[nextIndex];
    const prompts = [
      "Step 1/3 — Hold the phone still for 2.5 seconds…",
      "Step 2/3 — Rotate the phone in a slow figure-8…",
      "Step 3/3 — Walk 5 steps forward to tune stride.",
    ];
    state.calibration.step = nextIndex;
    setCalibrationStep(prompts[nextIndex], nextIndex, nextKey);
  }
  ui.calibrationButtons.forEach((button) => {
    button.disabled = false;
  });
}

function calibrateStillness() {
  return new Promise((resolve) => {
    state.calibration.step = 0;
    setCalibrationStep("Step 1/3 — Hold the phone still for 2.5 seconds…", 0, "stillness");
    const samples = [];
    const handler = (event) => {
      const acc = event.accelerationIncludingGravity || event.acceleration;
      if (acc) {
        samples.push(Math.hypot(acc.x || 0, acc.y || 0, acc.z || 0));
      }
      if (event.rotationRate) {
        const rot = event.rotationRate.alpha || event.rotationRate.gamma || 0;
        state.gyroBias = (state.gyroBias + rot) / 2;
      }
    };

    window.addEventListener("devicemotion", handler, true);
    setTimeout(() => {
      window.removeEventListener("devicemotion", handler, true);
      if (samples.length) {
        state.calibration.strideScaleFactor = 1.0;
      }
      state.calibration.completed.stillness = true;
      setCalibrationStep("Step 1/3 complete. Baseline is stable.", 0, "stillness");
      updateCalibrationReadiness();
      resolve();
    }, 2500);
  });
}

function calibrateCompass() {
  return new Promise(async (resolve) => {
    state.calibration.step = 1;
    setCalibrationStep("Step 2/3 — Rotate the phone in a slow figure-8…", 1, "compass");

    const headingSamples = [];
    const orientHandler = (event) => {
      const heading = computeEulerHeading(event);
      if (heading !== null && !Number.isNaN(heading)) headingSamples.push(heading);
    };

    window.addEventListener("deviceorientation", orientHandler, true);
    window.addEventListener("deviceorientationabsolute", orientHandler, true);

    await new Promise((r) => setTimeout(r, 6000));

    window.removeEventListener("deviceorientation", orientHandler, true);
    window.removeEventListener("deviceorientationabsolute", orientHandler, true);

    const spread = circularSpreadDeg(headingSamples);
    if (headingSamples.length >= 5) {
      const avg = circularMeanDeg(headingSamples);
      state.rawHeadingHistory = [avg];
      state.calibration.completed.compass = true;
      setCalibrationStep(`Step 2/3 complete. Compass coverage: ${spread.toFixed(0)}° spread.`, 1, "compass");
    } else {
      setCalibrationStep("Step 2/3 skipped: no orientation samples were received.", 1, "compass");
    }

    updateCalibrationReadiness();
    resolve();
  });
}

function calibrateWalk() {
  return new Promise((resolve) => {
    state.calibration.step = 2;
    setCalibrationStep("Step 3/3 — Walk 5 steps forward to tune stride.", 2, "walk");
    setTimeout(() => {
      state.calibration.completed.walk = true;
      setCalibrationStep("Step 3/3 complete. Stride estimate updated.", 2, "walk");
      updateCalibrationReadiness();
      resolve();
    }, 2500);
  });
}

function updatePositionFromHeading() {
  const currentHeading = state.lastCompassSample === null
    ? state.position.heading
    : (state.lastCompassSample + state.headingOffset + 360) % 360;
  state.position.heading = currentHeading;
  // keep chevrons locked to heading vs route while navigating
  if (state.navActive && state.route) {
    const progress = routeProgressForWaypoints(guideWaypoints());
    const next = nextTurnGuidance(state.route.waypoints, progress.distanceAlongRoute);
    updateFloorArrowTilt(next);
  }
  ui.metricHeading.textContent = `${Math.round(currentHeading)}°`;
}

async function initSlots() {
  try {
    const res = await fetch("/slots");
    if (!res.ok) throw new Error("Failed to load slot list");
    const payload = await res.json();
    state.slotList = payload.slots || [];
    const datalist = ui.slotSuggestions;
    datalist.innerHTML = state.slotList.map((slot) => `<option value="${slot.slot_id}"></option>`).join("");
    ui.slotSuggestionsMini.innerHTML = state.slotList.slice(0, 8).map((slot) => `<button class="chip" type="button">${slot.slot_id}</button>`).join("");
    ui.slotSuggestionsMini.querySelectorAll(".chip").forEach((button) => {
      button.addEventListener("click", () => {
        ui.slotInput.value = button.textContent.trim();
        ui.slotInput.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
    const locationResponse = await fetch("/diagnostics/locations");
    if (locationResponse.ok) {
      const locations = await locationResponse.json();
      ui.diagnosticLocations.replaceChildren();
      for (const location of locations) {
        const option = document.createElement("option");
        option.value = location.location_id;
        option.label = `${location.floor} ${location.zone} (${location.x.toFixed(1)}, ${location.y.toFixed(1)})`;
        ui.diagnosticLocations.appendChild(option);
      }
    }
  } catch (error) {
    console.warn("Using fallback slot suggestions.", error);
    const fallback = ["87-04C", "87-04B", "87-04A", "87-03C", "86-03A", "86-04A"];
    ui.slotSuggestionsMini.innerHTML = fallback.map((slot) => `<button class="chip" type="button">${slot}</button>`).join("");
    ui.slotSuggestionsMini.querySelectorAll(".chip").forEach((button) => {
      button.addEventListener("click", () => {
        ui.slotInput.value = button.textContent.trim();
        ui.slotInput.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
  }
}

async function waitForCameraFrame(timeoutMs = 4000) {
  const video = ui.cameraView;
  if (!video) return false;
  if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 2) {
    return true;
  }
  const start = performance.now();
  while (performance.now() - start < timeoutMs) {
    if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 2) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

function kickVideoFrameWatch(v) {
  if (!v || typeof v.requestVideoFrameCallback !== "function") return;
  const onFrame = () => {
    state.lastVideoFrameAt = performance.now();
    if (state.stream && typeof v.requestVideoFrameCallback === "function") {
      v.requestVideoFrameCallback(onFrame);
    }
  };
  v.requestVideoFrameCallback(onFrame);
}

function wireCameraWatchdog() {
  const v = ui.cameraView;
  if (!v) return;
  kickVideoFrameWatch(v);
  if (state.camWatchWired) return;
  state.camWatchWired = true;
  v.addEventListener("pause", () => {
    if (state.workflowStep !== "navigation" && state.workflowStep !== "destination") return;
    if (!state.stream || state.webXrActive) return;
    console.warn("camera video paused — auto-resume");
    v.play().catch(() => {});
  });
  v.addEventListener("stalled", () => {
    if (state.webXrActive) return;
    console.warn("camera video stalled — reopening stream");
    void ensureCamera(true);
  });
}

async function ensureLiveCameraFrame() {
  if (state.webXrActive) return;
  const track = state.stream?.getVideoTracks?.()[0];
  if (!track || track.readyState !== "live" || track.muted) {
    console.warn("camera track lost or muted — restarting stream");
    const ok = await ensureCamera(true);
    if (!ok) throw new Error("Camera track unavailable");
  }
  const v = ui.cameraView;
  if (!v || v.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    throw new Error("Camera frame is not ready yet.");
  }
  if (v.paused) {
    await v.play().catch(() => {});
  }
  if (
    typeof v.requestVideoFrameCallback === "function"
    && state.lastVideoFrameAt > 0
    && performance.now() - state.lastVideoFrameAt > 2500
  ) {
    console.warn("no new video frames — reopening stream");
    const ok = await ensureCamera(true);
    if (!ok) throw new Error("Camera feed frozen");
  }
  const t = v.currentTime;
  if (state.lastVideoTime != null && t === state.lastVideoTime && t > 0) {
    throw new Error("Camera feed is frozen or hasn't updated new frame");
  }
  if (t > 0) state.lastVideoTime = t;
}

async function ensureCamera(forceReopen = false) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    alert("This browser does not support camera capture.");
    return false;
  }

  const track = state.stream?.getVideoTracks?.()[0];
  const cameraIsActive = Boolean(track)
    && track.readyState === "live"
    && !track.muted;

  if (cameraIsActive && !forceReopen) {
    if (ui.cameraView.srcObject !== state.stream) ui.cameraView.srcObject = state.stream;
    if (ui.cameraView.paused) await ui.cameraView.play().catch(() => {});
    document.body.classList.add("cam-live");
    wireCameraWatchdog();
    return waitForCameraFrame();
  }

  state.stream?.getTracks().forEach((t) => {
    try { t.stop(); } catch (_) { /* ignore */ }
  });
  state.stream = null;
  state.lastVideoTime = null;
  state.lastVideoFrameAt = 0;
  try {
    if (ui.cameraView) ui.cameraView.srcObject = null;
  } catch (_) { /* ignore */ }

  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } },
      audio: false,
    });
    ui.cameraView.srcObject = state.stream;
    ui.cameraView.muted = true;
    ui.cameraView.setAttribute("playsinline", "");
    await ui.cameraView.play();
    document.body.classList.add("cam-live");
    wireCameraWatchdog();
    const ready = await waitForCameraFrame();
    if (!ready) {
      setStatusMsg("Camera opened but no frame yet — try Localize again.");
      return false;
    }
    return true;
  } catch (error) {
    console.error(error);
    alert("Camera access is required to localize your position.");
    return false;
  }
}

function varianceOfLaplacian(imageData, width, height) {
  const pixels = imageData.data;
  const gray = new Float32Array(width * height);
  for (let index = 0; index < gray.length; index += 1) {
    const pixel = index * 4;
    gray[index] = 0.299 * pixels[pixel] + 0.587 * pixels[pixel + 1] + 0.114 * pixels[pixel + 2];
  }

  // subsample — full-res laplacian on main thread can stall the camera compositor
  const step = Math.max(1, Math.round(Math.min(width, height) / 240));
  let sum = 0;
  let sumSquared = 0;
  let count = 0;
  for (let y = 1; y < height - 1; y += step) {
    for (let x = 1; x < width - 1; x += step) {
      const index = y * width + x;
      const laplacian = 4 * gray[index] - gray[index - 1] - gray[index + 1] - gray[index - width] - gray[index + width];
      sum += laplacian;
      sumSquared += laplacian * laplacian;
      count += 1;
    }
  }
  const mean = sum / Math.max(count, 1);
  return Math.max(0, sumSquared / Math.max(count, 1) - mean * mean);
}

async function logClientDiagnostic(event) {
  try {
    await fetch("/diagnostics/client-event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: state.deviceId, ...event }),
    });
  } catch (error) {
    console.debug("Client diagnostic event could not be uploaded.", error);
  }
}

async function encodeCameraViewFrame(maxDimension, jpegQuality) {
  try {
    await ensureLiveCameraFrame();
  } catch (err) {
    console.warn("live camera frame check failed", err);
    return null;
  }
  if (!state.stream || ui.cameraView.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    return null;
  }
  const sourceWidth = ui.cameraView.videoWidth;
  const sourceHeight = ui.cameraView.videoHeight;
  if (sourceWidth < 2 || sourceHeight < 2) return null;
  const scale = Math.min(1, maxDimension / Math.max(sourceWidth, sourceHeight));
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const width = Math.max(1, Math.round(sourceWidth * scale));
  const height = Math.max(1, Math.round(sourceHeight * scale));
  canvas.width = width;
  canvas.height = height;
  ctx.drawImage(ui.cameraView, 0, 0, width, height);
  const clientLaplacianVariance = varianceOfLaplacian(
    ctx.getImageData(0, 0, width, height),
    width,
    height,
  );
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", jpegQuality));
  if (!blob) return null;
  return {
    blob,
    sourceWidth,
    sourceHeight,
    width,
    height,
    clientLaplacianVariance,
    frameSource: "getUserMedia",
  };
}

async function encodeXrCameraFrame(maxDimension, jpegQuality) {
  if (!state.webXrActive || !webXrNav?.grabFrameBlob) return null;
  try {
    const blob = await webXrNav.grabFrameBlob({ maxDim: maxDimension, quality: jpegQuality });
    if (!blob) return null;
    return {
      blob,
      sourceWidth: 0,
      sourceHeight: 0,
      width: 0,
      height: 0,
      clientLaplacianVariance: 999, // skip blur gate — XR frames already gated by device
      frameSource: "webxr-camera",
    };
  } catch (err) {
    console.debug("xr camera frame", err?.message || err);
    return null;
  }
}

async function captureVprPosition({ trigger = "manual", applyBlurGuard = false } = {}) {
  const maxDimension = Number(ui.captureMaxDimension.value) || 1280;
  const jpegQuality = Number(ui.captureJpegQuality.value) || 0.8;

  // during AR floor change: XR passthrough → PnP (same /localize as camera-nav)
  let frame = null;
  if (state.webXrActive && state.inFloorTransition) {
    frame = await encodeXrCameraFrame(maxDimension, jpegQuality);
  }
  if (!frame) {
    frame = await encodeCameraViewFrame(maxDimension, jpegQuality);
  }
  if (!frame && state.webXrActive) {
    frame = await encodeXrCameraFrame(maxDimension, jpegQuality);
  }
  if (!frame) throw new Error("Camera frame is not ready yet.");

  const diagnostics = {
    trigger,
    motion_score: state.motion.cumulativeMotion,
    client_laplacian_variance: frame.clientLaplacianVariance,
    source_width: frame.sourceWidth,
    source_height: frame.sourceHeight,
    encoded_width: frame.width,
    encoded_height: frame.height,
    jpeg_quality: jpegQuality,
    frame_source: frame.frameSource,
    screen_orientation: screen.orientation?.type || "unknown",
  };

  // elevator: don't drop frames — PnP needs a shot at the landing floor
  const blurFloor = state.inFloorTransition ? 40 : 80;
  if (
    applyBlurGuard
    && frame.frameSource !== "webxr-camera"
    && ui.blurGuardEnabled.checked
    && frame.clientLaplacianVariance < blurFloor
  ) {
    await logClientDiagnostic({ event: "frame_skipped_blur", ...diagnostics });
    return { skipped: true, diagnostics };
  }

  const form = new FormData();
  form.append("image", frame.blob, "parking-frame.jpg");
  const groundTruth = ui.groundTruthLocationId.value.trim();
  if (groundTruth) form.append("ground_truth_location_id", groundTruth);
  // during elevator: bias PnP/VPR to landing floor (camera-nav auto-advance)
  let priorFloor = state.position?.floor ?? state.route?.floor;
  if (state.inFloorTransition) {
    const tf = activeRouteLeg()?.floor_transition
      || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
    priorFloor = tf?.to_floor ?? priorFloor;
  } else {
    priorFloor = activeRouteLeg()?.floor ?? priorFloor;
  }
  if (priorFloor != null && priorFloor !== "") {
    form.append("prior_floor", String(priorFloor));
  }
  form.append("capture_trigger", String(trigger));
  form.append("nav_mode", clientNavMode());
  for (const [key, value] of Object.entries(diagnostics)) form.append(key, String(value));

  // lock ARCore frame at the moment of this fix
  try {
    if (await ensureArVio()) {
      const pose = await ArVio.getPose();
      if (pose && pose.tracking === "tracking") {
        form.append("vio_x", String(pose.x));
        form.append("vio_y", String(pose.y));
        form.append("vio_z", String(pose.z));
        form.append("vio_qw", String(pose.qw));
        form.append("vio_qx", String(pose.qx));
        form.append("vio_qy", String(pose.qy));
        form.append("vio_qz", String(pose.qz));
      }
    }
  } catch (_) { /* PDR-only localize */ }

  const response = await fetch(`/localize?device_id=${encodeURIComponent(state.deviceId)}`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "Localization failed");
  }
  return { position: await response.json(), diagnostics };
}

/** server-side nav debug: webxr | camera | idle */
function clientNavMode() {
  if (state.webXrActive) return "webxr";
  if (state.navActive && state.stream) return "camera";
  if (state.navActive) return "camera_no_stream";
  return "idle";
}

function localizeMethodLabel(method) {
  const m = String(method || "").toLowerCase();
  if (m === "pnp") return "PnP";
  if (m.includes("vpr") || m === "image_vpr") return "Image VPR";
  return "Localize";
}

function applyVprFix(pos) {
  const prevFloor = state.position.floor;
  const landed = state.inFloorTransition
    && sameFloor(pos.floor, activeRouteLeg()?.floor_transition?.to_floor
      || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition?.to_floor);

  state.position = {
    floor: pos.floor,
    x: pos.x,
    y: pos.y,
    heading: pos.heading,
    confidence: pos.confidence,
    tracking: true,
  };
  state.hasLocalizedPosition = true;
  state.lastLocalizeMethod = pos.method || null;
  state.motion.gravityEstimate = null;
  state.motion.lastMagnitude = null;
  state.motion.cumulativeMotion = 0;
  const smoothedRawHeading = state.rawHeadingHistory.length > 0
    ? circularMeanDeg(state.rawHeadingHistory)
    : state.lastCompassSample;
  if (smoothedRawHeading !== null) {
    state.headingOffset = (pos.heading - smoothedRawHeading + 360) % 360;
  }

  const via = localizeMethodLabel(pos.method);
  const isPnp = String(pos.method || "").toLowerCase() === "pnp";
  setStatusChip(isPnp ? "PnP fix" : "VPR fix", "live");
  setStatusMsg(
    landed
      ? `Floor ${pos.floor} detected — continuing to your bay`
      : (`${via} lock · ${pos.x.toFixed(2)}, ${pos.y.toFixed(2)} m`
        + (isPnp && pos.num_inliers != null ? ` · ${pos.num_inliers} inliers` : "")
        + " · VIO/PDR between fixes"),
  );
  ui.localizeBtn.textContent = "Relocalize";
  ui.routeBtn.disabled = false;
  if (ui.positionState) ui.positionState.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)}`;
  if (ui.confidenceState) ui.confidenceState.textContent = pos.confidence.toFixed(2);
  ui.metricFloor.textContent = pos.floor ?? "—";
  ui.metricCoords.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)} m`;
  ui.metricHeading.textContent = `${Math.round(pos.heading)}°`;
  startLiveHeartbeat();
  // don't start ArCore during lift / camera-owned slices
  if (
    !state.inFloorTransition
    && !state.awaitingLandingLock
    && !state.pendingXrResume
    && state.camOwner !== "camera"
  ) {
    ensureArVio().then((ok) => {
      if (ok) startArTracking();
    });
  }
  updateRouteStatus();

  // landing: real PnP/VPR lock → pose becomes origin for a fresh XR session
  const landFl = (() => {
    const tf = activeRouteLeg()?.floor_transition
      || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
    if (tf) return normFloor(tf.to_floor);
    const leg = activeRouteLeg();
    if (leg && !leg.floor_transition) return normFloor(leg.floor);
    return destFloorId();
  })();
  const onLandingFloor = sameFloor(pos.floor, landFl);
  // after elevator: on dest walk leg even if lift flags were cleared early
  const afterConnector = Boolean(
    state.route?.legs?.some((l) => l.floor_transition)
    && !state.inFloorTransition
    && !hasFloorChangeAhead()
    && sameFloor(pos.floor, destFloorId())
    && state.camOwner === "camera"
    && !state.webXrActive
  );
  if (
    pos.tracking
    && onLandingFloor
    && (landed || state.inFloorTransition || state.awaitingLandingLock
      || state.pendingXrResume || afterConnector)
  ) {
    const firstLock = !state.landingPose;
    state.landingPose = {
      x: Number(pos.x),
      y: Number(pos.y),
      heading: Number(pos.heading),
      floor: normFloor(pos.floor),
      method: pos.method || null,
      confidence: Number(pos.confidence) || 0,
    };
    state.awaitingLandingLock = false;
    state.pendingXrResume = true;
    // only reset walk clock on the first lock — later PnP must not block clear-of-lobby
    if (firstLock) {
      state.xrResumeReadyAt = performance.now() + RESUME_XR_SETTLE_MS;
      state.walkSinceLand = 0;
      state.xrResumeAttempts = 0;
      setStatusMsg(
        `${localizeMethodLabel(pos.method)} locked Floor ${pos.floor} — tap Start AR`,
      );
      scheduleXrResumePoll();
    }
    if (state.inFloorTransition || landed) {
      exitFloorTransition(landFl);
    }
    promptStartArAfterLand();
  }
  maybeResumeXrAfterPnp(pos);
}

async function localizeParkingPosition() {
  if (SKIP_CALIBRATION) markCalibrationSkipped();
  if (!state.sessionReady) {
    alert(SKIP_CALIBRATION
      ? "Select a parking slot first, then tap Continue."
      : "Please complete the 3-step calibration before localizing.");
    return;
  }
  if (state.liveVpr.requestInFlight) return;

  state.liveVpr.requestInFlight = true;
  ui.localizeBtn.disabled = true;
  try {
    const available = await ensureCamera();
    if (!available) return;

    const capture = await captureVprPosition({ trigger: "manual" });
    if (capture.skipped) return;
    const pos = capture.position;
    if (!pos.tracking || pos.confidence <= 0) {
      setStatusChip("PnP/VPR miss", "warn");
      setStatusMsg(state.hasLocalizedPosition
        ? "PnP + image VPR miss — continuing with VIO/PDR. Relocalize to correct drift."
        : "No verified pose yet (PnP needs 3D map; else image VPR). Try a clearer view.");
      return;
    }

    applyVprFix(pos);
    const slotId = ui.slotInput.value.trim();
    if (slotId && state.workflowStep === "navigation") {
      try {
        await updateRouteForSlot(slotId);
        startLiveVprCapture();
        setHint("", false);
      } catch (error) {
        setStatusMsg(`Position locked, but route failed: ${error.message}`);
      }
    }
  } catch (error) {
    console.error(error);
    setStatusChip("Error", "bad");
    setStatusMsg(`Localization failed: ${error.message}`);
    if (!state.hasLocalizedPosition) alert(`Localization failed: ${error.message}`);
  } finally {
    state.liveVpr.requestInFlight = false;
    ui.localizeBtn.disabled = !state.sessionReady;
  }
}

function startLiveVprCapture() {
  const xrGrab = state.webXrActive && Boolean(webXrNav?.grabFrameBlob);
  // was blocking all VPR during XR — log showed zero /localize after Start nav
  if (
    state.liveVpr.running
    || !state.hasLocalizedPosition
    || !state.route
    || (!state.stream && !xrGrab)
    || state.workflowStep !== "navigation"
  ) return;

  state.liveVpr.running = true;
  state.liveVpr.lastCaptureTime = performance.now();
  state.motion.lastMeaningfulMotionAt = performance.now();
  state.motion.cumulativeMotion = 0;
  const via = localizeMethodLabel(state.lastLocalizeMethod);
  setStatusMsg(
    state.inFloorTransition
      ? "Detecting your floor after the lift…"
      : `Live guidance · ${via} + VIO/PDR between fixes`,
  );
  runLiveVprCaptureLoop();
}

function stopLiveVprCapture({ keepCamera = false } = {}) {
  state.liveVpr.running = false;
  state.motion.lastMagnitude = null;
  if (keepCamera) return;
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
  }
  if (ui.cameraView) ui.cameraView.srcObject = null;
  document.body.classList.remove("cam-live");
}

function setRouteBtnMode(mode) {
  if (!ui.routeBtn) return;
  if (mode === "stop") {
    ui.routeBtn.textContent = "Stop nav";
    ui.routeBtn.classList.add("is-exit-ar");
    ui.routeBtn.disabled = false;
  } else {
    ui.routeBtn.textContent = "Start nav";
    ui.routeBtn.classList.remove("is-exit-ar");
    ui.routeBtn.disabled = !state.hasLocalizedPosition;
  }
}

function stopWebXrSession({ resumeCamera = false } = {}) {
  if (webXrNav) {
    webXrNav.dispose();
    webXrNav = null;
  }
  state.webXrActive = false;
  ui.arStage?.classList.remove("webxr-on");
  if (resumeCamera && state.route && state.workflowStep === "navigation") {
    startCameraNav();
    setStatusMsg("WebXR ended — camera + PnP continues.");
  }
}

function stopCameraNav() {
  stopXrLiftWatch();
  stopXrHealthWatch();
  stopXrResumePoll();
  stopWebXrSession({ resumeCamera: false });
  state.navActive = false;
  state.pendingXrResume = false;
  state.xrResumeBlocked = false;
  state.xrResumeReadyAt = 0;
  state.camOwner = "camera";
  state.landingPose = null;
  state.awaitingLandingLock = false;
  state.xrResumeAttempts = 0;
  setStartArVisible(false);
  showFloorArrows(false);
  hideLiftHud();
  stopLiftCamMirror();
  if (ui.arCanvas) ui.arCanvas.style.display = "";
  setRouteBtnMode("start");
  stopLiveVprCapture({ keepCamera: true });
  if (ui.turnBanner) ui.turnBanner.hidden = true;
  setStatusMsg("Navigation stopped.");
}

/** camera + PnP/VPR guidance — no DOM chevrons (those are WebXR-only) */
function startCameraNav() {
  state.navActive = true;
  state.webXrActive = false;
  state.camOwner = "camera";
  void logClientDiagnostic({ event: "nav_mode", nav_mode: "camera", reason: "floor_chevrons_pnp" });
  showFloorArrows(false);
  setRouteBtnMode("stop");
  const via = localizeMethodLabel(state.lastLocalizeMethod);
  setStatusChip("Navigating", "live");
  setStatusMsg(`Camera · ${via} + VIO/PDR — AR arrows when WebXR is on`);
  setHint("Follow the turn banner — floor arrows appear in AR", true);
  updateRouteProgress();
  startLiveVprCapture();
}

/**
 * WebXR floor chevrons.
 * @param {{ facilityPose?: {x,y,heading}, fresh?: boolean }} [opts]
 * fresh=true → new session after lift (never continues Floor A XR)
 */
function waitForParkingWebXr(timeoutMs = 4000) {
  if (window.ParkingWebXr) return Promise.resolve(window.ParkingWebXr);
  return new Promise((resolve) => {
    const done = () => {
      cleanup();
      resolve(window.ParkingWebXr || null);
    };
    const cleanup = () => {
      clearTimeout(t);
      window.removeEventListener("parking-webxr-ready", done);
    };
    window.addEventListener("parking-webxr-ready", done, { once: true });
    const t = setTimeout(done, timeoutMs);
  });
}

async function startWebXrNav(opts = {}) {
  // no XR only while already in cabin / after XR died this floor
  if (state.inFloorTransition || liftHandoffBusy || state.xrResumeBlocked) return false;

  const api = window.ParkingWebXr || await waitForParkingWebXr();
  if (!api) {
    console.warn("WebXR: ParkingWebXr not loaded — check webxr-nav.js / three.module.js");
    void logClientDiagnostic({ event: "webxr_skip", reason: "module_missing" });
    return false;
  }
  if (!state.route || !ui.arCanvas || !ui.arStage) return false;

  // current floor walk leg only (never the connector / other floors)
  const wps = guideWaypoints();
  if (!wps || wps.length < 2) return false;

  // skip isSessionSupported await when launched from a tap — preserves user activation
  if (!opts.fromGesture) {
    let canXr = false;
    try {
      canXr = await api.canUseWebXr();
    } catch (err) {
      console.warn("WebXR: isSessionSupported failed", err);
      canXr = false;
    }
    if (!canXr) {
      void logClientDiagnostic({
        event: "webxr_skip",
        reason: window.isSecureContext ? "unsupported_or_denied" : "insecure_context",
      });
      return false;
    }
  }

  // WebXR needs exclusive camera — always drop getUserMedia first
  if (!opts.skipCamRelease) {
    stopLiveVprCapture({ keepCamera: false });
    state.stream?.getTracks?.().forEach((t) => {
      try { t.stop(); } catch (_) { /* ignore */ }
    });
    state.stream = null;
    try {
      if (ui.cameraView) ui.cameraView.srcObject = null;
    } catch (_) { /* ignore */ }
    document.body.classList.remove("cam-live");
    showFloorArrows(false);
    // Android/ARCore needs a longer gap after continuous VPR getUserMedia
    await new Promise((r) => setTimeout(r, opts.fresh ? 700 : 280));
  } else {
    showFloorArrows(false);
  }
  const facilityPose = opts.facilityPose || {
    x: Number(state.position.x),
    y: Number(state.position.y),
    heading: Number(state.position.heading),
  };
  const remWps = api.remainingWaypoints
    ? api.remainingWaypoints(wps, facilityPose.x, facilityPose.y)
    : wps;
  const legs = api.legsFromWaypoints(remWps);
  const distanceM = legs.reduce((s, l) => s + l.dist, 0)
    || Number(state.route.total_distance)
    || 1;

  // always a brand-new navigator — never reuse across floors
  try {
    webXrNav?.dispose?.();
  } catch (_) { /* ignore */ }
  webXrNav = null;

  webXrNav = new api.WebXrNav({
    overlayRoot: ui.arStage,
    canvas: ui.arCanvas,
    waypoints: wps,
    legs,
    distanceM,
    facilityPose,
    // pin bay when this floor's path ends at the slot (not a lift door)
    destXY: (!hasFloorChangeAhead() && onDestFloor()) ? routeDestPoint() : null,
    handlers: {
      onStep: (idx) => {
        const along = legs.slice(0, idx).reduce((s, l) => s + l.dist, 0);
        paintTurnBanner(nextTurnFromLegs(legs, along));
      },
      onWalk: (progress, remain) => {
        xrTrackLostAt = 0;
        if (ui.metricDistance) {
          ui.metricDistance.textContent = hasFloorChangeAhead() && remain <= ENTER_CONNECTOR_M
            ? "Now"
            : `${remain.toFixed(1)} m`;
        }
        // keep AR in the hallway; only hand off once we enter the connector
        if (state.inFloorTransition) {
          void handXrToCamera("lift");
        }
        const next = nextTurnFromLegs(legs, progress);
        paintTurnBanner({
          ...next,
          distanceM: Math.min(next.distanceM, remain),
        });
      },
      onTracking: (ok) => {
        if (ok) {
          xrTrackLostAt = 0;
          return;
        }
        if (xrInTrackGrace()) return;
        if (!xrTrackLostAt) xrTrackLostAt = performance.now();
        const lost = performance.now() - xrTrackLostAt;
        if (lost > XR_TRACK_LOST_MS) {
          void handXrToCamera(state.inFloorTransition ? "lift" : "dead");
        }
      },
      onArrived: () => {
        // end of this floor's AR path — often the elevator, not the bay
        if (!onDestFloor() || hasFloorChangeAhead()) {
          enterConnectorLeg();
          void handXrToCamera("lift");
          const tf = activeRouteLeg()?.floor_transition
            || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
          const ban = elevatorBanner(tf, 0);
          paintTurnBanner(ban);
          setStatusChip(connectorLabel(tf), "live");
          setStatusMsg(ban.label);
          setHint(ban.label, true);
          updateRouteProgress();
          return;
        }
        setStatusChip("Arrived", "live");
        setStatusMsg("You've arrived at your bay.");
        paintTurnBanner({
          label: "You've arrived",
          kind: "arrive",
          distanceM: 0,
          isArrival: true,
        });
        showArrivalCelebration(ui.slotInput?.value?.trim() || "");
      },
      onEnd: () => {
        stopXrLiftWatch();
        stopXrHealthWatch();
        webXrNav = null;
        state.webXrActive = false;
        state.camOwner = "camera";
        ui.arStage?.classList.remove("webxr-on");
        void logClientDiagnostic({
          event: "webxr_end",
          nav_mode: "camera",
          reason: "session_end",
        });
        if (state.suppressXrResume || liftHandoffBusy) return;
        if (state.route && state.workflowStep === "navigation" && state.navActive) {
          void (async () => {
            await ensureCamera().catch(() => false)
              || await reopenCameraHard({ delayMs: 400 });
            startCameraNav();
            restartLiveVprCapture();
          })();
        } else {
          setRouteBtnMode("start");
          state.navActive = false;
        }
      },
      onErr: (why) => {
        stopXrLiftWatch();
        stopXrHealthWatch();
        webXrNav?.dispose();
        webXrNav = null;
        state.webXrActive = false;
        state.camOwner = "camera";
        ui.arStage?.classList.remove("webxr-on");
        void logClientDiagnostic({
          event: "webxr_end",
          nav_mode: "camera",
          reason: `err_${why || "unknown"}`,
        });
        // denied/unsupported = session never started (often camera still held) — soft fail
        if (why === "denied" || why === "unsupported") {
          if (state.pendingXrResume || state.landingPose) {
            state.xrResumeBlocked = false;
            state.pendingXrResume = true;
            state.xrResumeReadyAt = performance.now() + 2000;
            scheduleXrResumePoll();
          }
          return;
        }
        if (state.navActive) void handXrToCamera("dead");
      },
    },
  });

  const ok = await webXrNav.start();
  if (!ok) {
    console.warn("WebXR: session start failed — falling back to camera + PnP");
    void logClientDiagnostic({ event: "webxr_skip", reason: "session_start_failed" });
    try { webXrNav?.dispose?.(); } catch (_) { /* ignore */ }
    webXrNav = null;
    return false;
  }

  // force recalibrate on first floor hit — never carry Floor A alignment
  webXrNav.calibrated = false;
  webXrNav.arrived = false;
  webXrNav.progressM = 0;
  webXrNav.maxProgressM = 0;

  state.webXrActive = true;
  state.navActive = true;
  state.camOwner = "xr";
  xrSessionStartedAt = performance.now();
  xrTrackLostAt = 0;
  ui.arStage.classList.add("webxr-on");
  if (ui.arCanvas) ui.arCanvas.style.display = "block";
  void logClientDiagnostic({
    event: "webxr_start",
    nav_mode: "webxr",
    reason: opts.fresh ? "fresh_floor" : "start_nav",
  });
  setRouteBtnMode("stop");
  setStatusChip(opts.fresh ? "AR · new floor" : "AR nav", "live");
  if (hasFloorChangeAhead()) {
    startXrLiftWatch();
    startXrHealthWatch();
    setStatusMsg("AR on — point at the floor to place arrows (keep AR near the lift)");
    setHint("Point the camera at the floor ahead", true);
  } else if (opts.fresh) {
    startXrHealthWatch();
    setStatusMsg("Fresh AR on this floor — origin from landing PnP");
    setHint("Point at the floor to calibrate the new session", true);
  } else {
    startXrHealthWatch();
    setStatusMsg("Point at the floor ahead — lime chevrons guide the route");
    setHint("Point the camera at the floor to place AR arrows", true);
  }
  return true;
}

async function runLiveVprCaptureLoop() {
  const minimumIntervalMs = 300;
  const motionThreshold = 1.5;
  const stillnessWindowMs = 350;

  while (state.liveVpr.running) {
    if (state.workflowStep !== "navigation" || !state.route) {
      stopLiveVprCapture();
      return;
    }
    // in lift we want getUserMedia, not a frozen XR grab
    if (state.inFloorTransition && state.webXrActive) {
      await handoffCameraDuringLift();
    }
    // pure single-floor XR: no need for continuous /localize
    if (state.webXrActive && !state.inFloorTransition && !hasFloorChangeAhead()) {
      await new Promise((r) => setTimeout(r, 200));
      continue;
    }
    if (state.inFloorTransition || state.xrResumeBlocked) kickCameraPreview();
    const canXrGrab = state.webXrActive && Boolean(webXrNav?.grabFrameBlob);
    if (!state.stream && !canXrGrab) {
      // after land, pendingXrResume is cleared — used to kill the loop here (manual Localize still worked)
      if (state.navActive || state.inFloorTransition || state.pendingXrResume || state.xrResumeBlocked) {
        const ok = await ensureCamera().catch(() => false)
          || await reopenCameraHard({ delayMs: 300 }).catch(() => false);
        if (!ok) {
          await new Promise((r) => setTimeout(r, 800));
          continue;
        }
        kickCameraPreview();
      } else {
        stopLiveVprCapture();
        return;
      }
    }

    const now = performance.now();
    const elapsed = now - state.liveVpr.lastCaptureTime;
    // elevator: poll faster so PnP hits landing floor soon after doors open
    const maximumIntervalMs = state.inFloorTransition ? 1200 : 2000;
    const movedEnough = state.motion.cumulativeMotion >= motionThreshold;
    const intervalElapsed = elapsed >= maximumIntervalMs;
    const triggerMode = ui.captureTriggerMode.value;
    const triggerAfterStillness = triggerMode === "motion_then_stillness"
      && now - state.motion.lastMeaningfulMotionAt >= stillnessWindowMs;
    const motionReady = movedEnough && (triggerMode !== "motion_then_stillness" || triggerAfterStillness);
    const floorPoll = state.inFloorTransition && elapsed >= 800;

    if (
      !state.liveVpr.requestInFlight
      && elapsed >= minimumIntervalMs
      && now >= state.liveVpr.nextCaptureAllowedAt
      && (motionReady || intervalElapsed || floorPoll)
    ) {
      state.liveVpr.requestInFlight = true;
      const trigger = floorPoll
        ? "floor_detect"
        : (intervalElapsed && !motionReady
        ? "max_interval"
          : triggerMode === "motion_then_stillness" ? "motion_then_stillness" : "motion");

      try {
        const capture = await captureVprPosition({
          trigger,
          applyBlurGuard: !state.inFloorTransition,
        });
        if (capture.skipped) {
          state.liveVpr.nextCaptureAllowedAt = performance.now() + 400;
          setStatusMsg(`Skipped blurry frame (sharpness ${capture.diagnostics.client_laplacian_variance.toFixed(1)})`);
        } else {
        state.motion.cumulativeMotion = 0;
        state.liveVpr.lastCaptureTime = performance.now();
        const fix = capture.position;
          if (state.liveVpr.running) {
        if (!fix.tracking || fix.confidence <= 0) {
              setStatusMsg(
                state.inFloorTransition
                  ? "Looking for Floor lock (PnP)…"
                  : "VPR miss — continuing with VIO/PDR until next fix",
              );
        } else {
              const via = localizeMethodLabel(fix.method);
          applyVprFix(fix);
          try {
            await updateRouteForSlot(ui.slotInput.value.trim());
          } catch (error) {
                setStatusMsg(`${via} ok, route refresh failed: ${error.message}`);
              }
              if (!state.inFloorTransition) {
                if (state.pendingXrResume && state.landingPose) {
                  setStatusMsg(`Floor ${state.position.floor} locked — tap Start AR`);
                  promptStartArAfterLand();
                  scheduleXrResumePoll();
                } else {
                  setStatusMsg(`Live guidance active · ${via} + VIO/PDR`);
                }
              }
              // after land: wait for Start AR tap — do not reclaim camera for VPR
              if (state.camOwner === "camera" && state.pendingXrResume) {
                state.liveVpr.requestInFlight = false;
                state.liveVpr.running = false;
                return;
              }
            }
          }
        }
      } catch (error) {
        console.warn("Live localize failed; PDR will continue.", error);
        // frame not ready after land — reopen like manual Localize
        if (/frame is not ready|camera/i.test(String(error.message || ""))) {
          await ensureCamera().catch(() => false)
            || await reopenCameraHard({ delayMs: 200 }).catch(() => false);
          kickCameraPreview();
        }
        setStatusMsg(
          state.inFloorTransition
            ? `Floor detect retry… ${error.message}`
            : `Live VPR unavailable — VIO/PDR continues. ${error.message}`,
        );
      } finally {
        state.liveVpr.requestInFlight = false;
      }
      continue;
    }

    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

function buildTurnInstructions(waypoints) {
  const points = waypoints.filter((point, index) => {
    if (index === 0) return true;
    const previous = waypoints[index - 1];
    return Math.hypot(point[0] - previous[0], point[1] - previous[1]) >= 0.1;
  });
  if (points.length < 2) {
    return [{ label: "At your destination", distanceAlongRoute: 0, isArrival: true }];
  }

  const instructions = [];
  let distanceAlongRoute = 0;

  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const incomingX = current[0] - previous[0];
    const incomingY = current[1] - previous[1];
    distanceAlongRoute += Math.hypot(incomingX, incomingY);

    if (index === points.length - 1) continue;

    const next = points[index + 1];
    const outgoingX = next[0] - current[0];
    const outgoingY = next[1] - current[1];
    const cross = incomingX * outgoingY - incomingY * outgoingX;
    const dot = incomingX * outgoingX + incomingY * outgoingY;
    const angle = Math.atan2(cross, dot) * 180 / Math.PI;

    if (Math.abs(angle) < 30) continue;

    const label = Math.abs(angle) >= 150 ? "Turn around" : angle > 0 ? "Turn left" : "Turn right";
    instructions.push({ label, distanceAlongRoute });
  }

  if (instructions.length === 0) {
    return [{ label: "Continue straight to your destination", distanceAlongRoute, isArrival: true }];
  }

  instructions.push({ label: "Arrive at your destination", distanceAlongRoute, isArrival: true });
  return instructions;
}

function renderTurnInstructions(waypoints, traveledDistance = 0) {
  updateTurnHud(traveledDistance);
  if (!ui.turnInstructions) return;
  ui.turnInstructions.replaceChildren();
  for (const instruction of buildTurnInstructions(waypoints)) {
    const remainingDistance = instruction.distanceAlongRoute - traveledDistance;
    if (!instruction.isArrival && remainingDistance < -1) continue;

    const item = document.createElement("li");
    item.className = "turn-instruction";

    const label = document.createElement("span");
    label.textContent = instruction.label;
    item.appendChild(label);

    const distance = document.createElement("strong");
    distance.textContent = remainingDistance < ARRIVE_M
      ? "Now"
      : `In ${Math.round(remainingDistance)} m`;
    item.appendChild(distance);
    ui.turnInstructions.appendChild(item);
  }
}

async function updateRouteForSlot(slotId) {
  const response = await fetch(`/route/${encodeURIComponent(state.deviceId)}?slot_id=${encodeURIComponent(slotId)}`);
  if (!response.ok) {
    let msg = "No route found";
    const raw = await response.text().catch(() => "");
    try {
      const body = JSON.parse(raw);
      msg = body.detail || body.error || msg;
    } catch (_) {
      if (raw) msg = raw;
    }
    if (typeof msg === "string" && msg.toLowerCase().includes("no known position")) {
      state.hasLocalizedPosition = false;
      state.route = null;
    }
    throw new Error(msg);
  }

  const route = await response.json();
  await ensureFloorOrder();
  const keepNav = state.navActive || state.webXrActive;
  state.route = route;
  if (!keepNav) {
    state.activeLegIndex = 0;
    state.inFloorTransition = false;
    state.arrivedShown = false;
    hideArrivalCelebration();
  }
  syncActiveLeg();
  if (keepNav && state.inFloorTransition === false && onDestFloor()) {
    // route refresh after landing — snap to dest walk leg
    const walkIdx = state.route.legs.findIndex((leg) => (
      !leg.floor_transition
      && sameFloor(leg.floor, state.position.floor)
      && (leg.waypoints?.length || 0) > 0
    ));
    if (walkIdx >= 0) state.activeLegIndex = walkIdx;
  }
  if (ui.routeBadge) {
  ui.routeBadge.classList.remove("neutral", "warning");
  ui.routeBadge.classList.add("success");
  ui.routeBadge.textContent = "Guidance ready";
  }
  if (ui.routeDestination) ui.routeDestination.textContent = slotId;
  if (ui.routeTotalDistance) ui.routeTotalDistance.textContent = `${route.total_distance.toFixed(1)} m`;
  const destFloor = String(route.dest_floor ?? route.floor);
  const startFloor = String(route.floor);
  if (ui.routeStatus) {
    ui.routeStatus.textContent = destFloor !== startFloor
      ? `Floor ${startFloor} → ${destFloor}`
      : `Floor ${startFloor}`;
  }
  setStatusChip(destFloor !== startFloor ? `Nav ${startFloor}→${destFloor}` : "Navigating", "live");
  setStatusMsg(`Route ready · ${route.total_distance.toFixed(1)} m to ${slotId}`);
  setHint("", false);
  const leg = activeRouteLeg();
  const guideWps = leg?.waypoints?.length ? leg.waypoints : route.waypoints;
  renderTurnInstructions(guideWps);
  drawRoute(guideWps, slotId);
}

async function navigateToSlot() {
  // toggle: Start nav ↔ Stop nav
  if (state.navActive) {
    stopCameraNav();
    return;
  }

  const slotId = ui.slotInput.value.trim();
  if (!slotId) {
    alert("Enter a slot ID to navigate.");
    showSheet("slot");
    return;
  }
  if (!state.hasLocalizedPosition) {
    setStatusMsg("Localize first to establish your current position.");
    setHint("Point the camera and tap Localize", true);
    return;
  }

  ui.routeBtn.disabled = true;
  try {
    await updateRouteForSlot(slotId);

    state.landingPose = null;
    state.awaitingLandingLock = false;
    state.pendingXrResume = false;
    state.xrResumeBlocked = false;

    // XR stays available near the lift — only cabin handoff drops it
    const xrOk = await startWebXrNav({ fresh: false });
    if (xrOk) return;

    setStatusMsg("WebXR unavailable — camera + live localize (no floor arrows)");
    const camOk = state.stream || await ensureCamera();
    if (!camOk) {
      setStatusMsg("Route ready, but camera is unavailable.");
      setRouteBtnMode("start");
      return;
    }
    if (hasFloorChangeAhead()) state.awaitingLandingLock = true;
    startCameraNav();
  } catch (error) {
    console.error(error);
    showFloorArrows(false);
    if (ui.turnBanner) ui.turnBanner.hidden = true;
    if (ui.routeBadge) {
    ui.routeBadge.classList.remove("neutral", "success");
    ui.routeBadge.classList.add("warning");
    ui.routeBadge.textContent = "Route unavailable";
    }
    if (ui.routeStatus) ui.routeStatus.textContent = "No path";
    if (ui.routeDistance) ui.routeDistance.textContent = "0.0 m";
    if (ui.routeDestination) ui.routeDestination.textContent = slotId;
    setStatusChip("No route", "bad");
    setStatusMsg(error.message);
    setRouteBtnMode("start");
    alert(error.message);
  } finally {
    if (!state.navActive) {
      ui.routeBtn.disabled = !state.hasLocalizedPosition;
    }
  }
}

function updateRouteStatus() {
  if (ui.positionState) ui.positionState.textContent = `${state.position.x.toFixed(2)}, ${state.position.y.toFixed(2)}`;
  ui.metricCoords.textContent = `${state.position.x.toFixed(2)}, ${state.position.y.toFixed(2)} m`;
  ui.metricFloor.textContent = state.position.floor ?? "—";
  ui.metricHeading.textContent = `${Math.round(state.position.heading)}°`;
  if (ui.confidenceState) ui.confidenceState.textContent = state.position.confidence.toFixed(2);
  ui.metricDistance.textContent = state.route ? `${routeProgress().remainingDistance.toFixed(1)} m` : "—";
  updateRouteProgress();
}

function routeProgress() {
  const waypoints = state.route.waypoints;
  const position = state.position;
  let cumulativeDistance = 0;
  let nearestOffset = Infinity;
  let distanceAlongRoute = 0;

  for (let index = 1; index < waypoints.length; index += 1) {
    const [startX, startY] = waypoints[index - 1];
    const [endX, endY] = waypoints[index];
    const segmentX = endX - startX;
    const segmentY = endY - startY;
    const segmentLengthSquared = segmentX * segmentX + segmentY * segmentY;
    const segmentLength = Math.sqrt(segmentLengthSquared);
    if (segmentLength === 0) continue;

    const projection = Math.max(0, Math.min(1,
      ((position.x - startX) * segmentX + (position.y - startY) * segmentY) / segmentLengthSquared,
    ));
    const projectedX = startX + projection * segmentX;
    const projectedY = startY + projection * segmentY;
    const offset = Math.hypot(position.x - projectedX, position.y - projectedY);

    if (offset < nearestOffset) {
      nearestOffset = offset;
      distanceAlongRoute = cumulativeDistance + projection * segmentLength;
    }
    cumulativeDistance += segmentLength;
  }

  const totalDistance = state.route.total_distance ?? cumulativeDistance;
  return {
    distanceAlongRoute,
    remainingDistance: Math.max(0, totalDistance - distanceAlongRoute),
  };
}

function updateRouteProgress() {
  if (!state.route) {
    if (ui.turnBanner) ui.turnBanner.hidden = true;
    return;
  }

  const wasInTransition = state.inFloorTransition;
  syncActiveLeg();
  // just landed via VPR floor change — refresh dest-floor AR path once
  if (wasInTransition && !state.inFloorTransition) {
    refreshPathAfterFloorChange({ redraw: false });
  }
  // camera slice: keep VPR alive; try XR slice when clear of lift
  if (!state.inFloorTransition && state.hasLocalizedPosition && state.navActive) {
    if (state.camOwner === "camera" && !state.webXrActive && !state.liveVpr.running) {
      restartLiveVprCapture();
    }
    if (state.pendingXrResume && !state.xrResumeBlocked && !state.webXrActive) {
      maybeResumeXrAfterPnp(state.position);
    }
  }

  const leg = activeRouteLeg();
  if (state.inFloorTransition && leg?.floor_transition) {
    const tf = leg.floor_transition;
    const dir = verticalDir(tf.from_floor, tf.to_floor);
    const go = dir === "down" ? "Go down" : dir === "up" ? "Go up" : "Change floor";
    const msg = `${go} to Floor ${tf.to_floor}`;
    showLiftHud(tf);
    // auto-detect landing via /localize (PnP first) — same as camera-nav
    startFloorDetectVpr();
    const dwell = performance.now() - (state.floorTransitionAt || performance.now());
    const walkedOut = state.motion.cumulativeMotion >= ELEVATOR_WALK_MOTION;
    if (dwell >= ELEVATOR_AUTO_MS && walkedOut) {
      exitFloorTransition(tf.to_floor);
      return;
    }
    setHint(msg, true);
    setStatusMsg(
      dwell > 2500
        ? `${msg} · PnP detecting Floor ${tf.to_floor}…`
        : msg,
    );
    updateTurnHud(0);
    // manual fallback only if auto-detect is slow (camera-nav rarely needs this)
    setFloorContinueVisible(dwell >= 18000, tf.to_floor);
    ui.metricDistance.textContent = "Now";
    return;
  }

  hideLiftHud();
  // don't hide Start AR CTA after landing lock
  if (!(state.pendingXrResume && state.landingPose) && floorBtnMode !== "start_ar") {
    setFloorContinueVisible(false);
  } else {
    promptStartArAfterLand();
  }

  const guideWps = leg?.waypoints?.length ? leg.waypoints : state.route.waypoints;
  const progress = guideWps?.length
    ? routeProgressForWaypoints(guideWps)
    : routeProgress();
  if (ui.routeDistance) ui.routeDistance.textContent = `${progress.remainingDistance.toFixed(1)} m`;
  if (ui.routeTotalDistance) ui.routeTotalDistance.textContent = `${state.route.total_distance.toFixed(1)} m`;
  ui.metricDistance.textContent = `${progress.remainingDistance.toFixed(1)} m`;
  const destFloor = destFloorId();
  const upcoming = nextTransitionLeg((state.activeLegIndex || 0) + 1);
  const hasMoreLegs = Boolean(state.route.legs)
    && (state.activeLegIndex || 0) < state.route.legs.length - 1;
  const reached = hasReachedDestination(progress);
  if (ui.routeStatus) {
    ui.routeStatus.textContent = reached
      ? `Arrived · Floor ${destFloor}`
      : `Floor ${state.position.floor ?? state.route.floor}`;
  }
  // preview lift/stairs while walking toward it
  if (!reached && upcoming?.floor_transition && progress.remainingDistance < 12) {
    const tf = upcoming.floor_transition;
    setHint(
      progress.remainingDistance <= ENTER_CONNECTOR_M
        ? (tf.instruction || `Take ${connectorLabel(tf)} (${tf.connector_id || ""}) to Floor ${tf.to_floor}`)
        : `Walk to ${tf.connector_id || "lift"} · then ${connectorLabel(tf)} to Floor ${tf.to_floor}`,
      true,
    );
  }
  if (!leg?.floor_transition && !state.inFloorTransition) {
    const lastWp = guideWps?.[guideWps.length - 1];
    const distLift = lastWp
      ? Math.hypot(state.position.x - lastWp[0], state.position.y - lastWp[1])
      : Infinity;
    // enter lift/stairs when close — XR hands off only after connector entry
    const nearConnector = Boolean(upcoming?.floor_transition)
      && (progress.remainingDistance <= ENTER_CONNECTOR_M || distLift <= ENTER_CONNECTOR_M);
    if (hasMoreLegs && nearConnector) {
      if (enterConnectorLeg()) {
        updateRouteProgress();
        return;
      }
    }
    if (hasMoreLegs && progress.remainingDistance < LEG_ADVANCE_M) {
      state.activeLegIndex += 1;
      const nxt = activeRouteLeg();
      state.inFloorTransition = Boolean(nxt?.floor_transition);
      updateRouteProgress();
      return;
    }
    if (reached) {
      showArrivalCelebration(ui.slotInput?.value?.trim() || "");
    }
  } else if (reached && !hasFloorChangeAhead()) {
    showArrivalCelebration(ui.slotInput?.value?.trim() || "");
  }

  const marker = ui.routeSvg?.querySelector("#livePositionMarker");
  if (marker && state.routeScreenPoint) {
    const point = state.routeScreenPoint([state.position.x, state.position.y]);
    marker.setAttribute("cx", String(point.x));
    marker.setAttribute("cy", String(point.y));
  }

  renderTurnInstructions(guideWps, progress.distanceAlongRoute);
}

function drawRoute(waypoints, slotId) {
  const svg = ui.routeSvg;
  if (!svg) {
    updateRouteProgress();
    return;
  }
  svg.innerHTML = "";

  const w = 420;
  const h = 220;
  const pad = 22;

  const xs = waypoints.map(([x]) => x);
  const ys = waypoints.map(([, y]) => y);
  const minX = Math.min(...xs, 0);
  const maxX = Math.max(...xs, 0);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 0);

  const scaleX = (w - pad * 2) / Math.max(1, maxX - minX || 1);
  const scaleY = (h - pad * 2) / Math.max(1, maxY - minY || 1);
  const scale = Math.min(scaleX, scaleY);

  const toPoint = ([x, y]) => ({
    x: pad + (x - minX) * scale,
    y: h - pad - (y - minY) * scale,
  });
  state.routeScreenPoint = toPoint;

  const pathPoints = waypoints.map(toPoint);
  const d = pathPoints.map((pt, index) => `${index === 0 ? "M" : "L"}${pt.x} ${pt.y}`).join(" ");

  const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  background.setAttribute("x", "0");
  background.setAttribute("y", "0");
  background.setAttribute("width", String(w));
  background.setAttribute("height", String(h));
  background.setAttribute("fill", "#0d0d0d");
  svg.appendChild(background);

  const grid = document.createElementNS("http://www.w3.org/2000/svg", "path");
  grid.setAttribute("d", "M 22 110 H 398 M 210 20 V 200");
  grid.setAttribute("stroke", "rgba(255,255,255,0.08)");
  grid.setAttribute("stroke-width", "1");
  svg.appendChild(grid);

  const routePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  routePath.setAttribute("d", d);
  routePath.setAttribute("fill", "none");
  routePath.setAttribute("stroke", "#fdd835");
  routePath.setAttribute("stroke-width", "4");
  routePath.setAttribute("stroke-linecap", "round");
  routePath.setAttribute("stroke-linejoin", "round");
  svg.appendChild(routePath);

  const start = toPoint(waypoints[0]);
  const end = toPoint(waypoints[waypoints.length - 1]);

  const startDot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  startDot.setAttribute("cx", String(start.x));
  startDot.setAttribute("cy", String(start.y));
  startDot.setAttribute("r", "5");
  startDot.setAttribute("fill", "#ffffff");
  svg.appendChild(startDot);

  const positionMarker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  positionMarker.setAttribute("id", "livePositionMarker");
  positionMarker.setAttribute("r", "7");
  positionMarker.setAttribute("fill", "#38bdf8");
  positionMarker.setAttribute("stroke", "#ffffff");
  positionMarker.setAttribute("stroke-width", "2");
  positionMarker.setAttribute("aria-label", "Live position");
  svg.appendChild(positionMarker);

  const endDot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  endDot.setAttribute("cx", String(end.x));
  endDot.setAttribute("cy", String(end.y));
  endDot.setAttribute("r", "6");
  endDot.setAttribute("fill", "#fdd835");
  svg.appendChild(endDot);

  const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
  label.setAttribute("x", String(end.x + 10));
  label.setAttribute("y", String(end.y - 10));
  label.setAttribute("fill", "#fdd835");
  label.setAttribute("font-size", "12");
  label.setAttribute("font-weight", "700");
  label.textContent = slotId;
  svg.appendChild(label);
  updateRouteProgress();
}

async function refreshCurrentPosition() {
  if (state.hasLocalizedPosition) return;

  try {
    const response = await fetch(`/position/${encodeURIComponent(state.deviceId)}`);
    if (!response.ok) return;
    const pos = await response.json();
    state.position = {
      floor: pos.floor,
      x: pos.x,
      y: pos.y,
      heading: pos.heading,
      confidence: pos.confidence,
      tracking: pos.tracking,
    };
    updateRouteStatus();
  } catch (error) {
    console.warn("Unable to refresh current position.", error);
  }
}

window.addEventListener("pagehide", () => {
  stopCameraNav();
  stopLiveVprCapture({ keepCamera: false });
  endLiveLocalization();
});
window.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && state.workflowStep !== "navigation") {
    endLiveLocalization();
  }
});

ui.calibrationButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const stepKey = button.dataset.stepKey;
    runCalibrationStep(stepKey).catch((error) => {
      setCalibrationStep(`Calibration error: ${error.message}`);
    });
  });
});

ui.workflowSteps.forEach((button) => {
  button.addEventListener("click", () => {
    setWorkflowStep(button.dataset.workflowStep, true);
  });
});

ui.destinationContinueBtn.addEventListener("click", () => {
  setWorkflowStep(SKIP_CALIBRATION ? "navigation" : "calibration", true);
});
ui.calibrationBackBtn?.addEventListener("click", () => setWorkflowStep("destination", true));
ui.calibrationContinueBtn?.addEventListener("click", () => setWorkflowStep("navigation", true));
ui.navigationBackBtn?.addEventListener("click", () => {
  setWorkflowStep(SKIP_CALIBRATION ? "destination" : "calibration", true);
});
ui.slotSheetBtn?.addEventListener("click", () => showSheet("slot"));
document.querySelectorAll("[data-close-sheet]").forEach((el) => {
  el.addEventListener("click", () => {
    if (state.workflowStep === "navigation") hideSheets();
    else if (el.dataset.closeSheet === "cal" && canEnterWorkflowStep("navigation")) hideSheets();
    else if (el.dataset.closeSheet === "slot" && state.workflowStep !== "destination") hideSheets();
  });
});
ui.slotInput.addEventListener("input", updateWorkflowControls);
ui.localizeBtn.addEventListener("click", localizeParkingPosition);
ui.routeBtn.addEventListener("click", () => {
  navigateToSlot().catch((err) => {
    console.error(err);
    setStatusMsg(`Nav failed: ${err.message}`);
    setRouteBtnMode("start");
  });
});
ui.arriveDismissBtn?.addEventListener("click", () => {
  hideArrivalCelebration();
  setHint("Tap Slot to navigate somewhere else", true);
});
ui.floorContinueBtn?.addEventListener("click", () => {
  if (floorBtnMode === "start_ar") {
    void claimXrSlice({ fromGesture: true });
    return;
  }
  const tf = activeRouteLeg()?.floor_transition
    || nextTransitionLeg(state.activeLegIndex || 0)?.floor_transition;
  exitFloorTransition(tf?.to_floor || destFloorId());
});
ui.startArBtn?.addEventListener("click", () => {
  void claimXrSlice({ fromGesture: true });
});
ui.slotInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    setWorkflowStep(SKIP_CALIBRATION ? "navigation" : "calibration", true);
  }
});

window.addEventListener("load", async () => {
  await initSlots();
  await ensureFloorOrder();
  updateCalibrationReadiness();
  setWorkflowStep("destination");
  if (!SKIP_CALIBRATION) {
  setCalibrationStep("Waiting for calibration to begin.", 0, "stillness");
  }
  updateRouteStatus();
  setInterval(refreshCurrentPosition, 2000);
});

/** called from home.js after bay is chosen */
function startGuidanceFromHome(bay) {
  const id = String(bay || "").trim().toUpperCase();
  if (!id) return;
  if (ui.slotInput) {
    ui.slotInput.value = id;
    ui.slotInput.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (SKIP_CALIBRATION) markCalibrationSkipped();
  updateCalibrationReadiness();
  hideSheets();
  setWorkflowStep("navigation", true);
  setStatusMsg(`Bay ${id} — point camera and tap Localize`);
  setHint("Point the camera and tap Localize", true);
}

window.ParkingUiEntry = {
  startGuidance: startGuidanceFromHome,
  showHome: () => {
    if (typeof window.ParkingHome?.showHome === "function") {
      window.ParkingHome.showHome();
      setWorkflowStep("destination");
    }
  },
};

