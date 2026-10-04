const state = {
  deviceId: "parking-mobile-" + Math.random().toString(36).slice(2, 8),
  workflowStep: "destination",
  hasLocalizedPosition: false,
  slotList: [],
  position: { floor: 1, x: 0, y: 0, heading: 0, confidence: 0, tracking: false },
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
  liveVpr: {
    running: false,
    requestInFlight: false,
    lastCaptureTime: 0,
    nextCaptureAllowedAt: 0,
  },
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
};

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

function nextTurnGuidance(waypoints, traveledDistance = 0) {
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

function turnKind(label) {
  const t = String(label || "").toLowerCase();
  if (t.includes("left")) return "left";
  if (t.includes("right")) return "right";
  if (t.includes("around") || t.includes("u-turn")) return "uturn";
  if (t.includes("arrive") || t.includes("destination")) return "arrive";
  return "straight";
}

function updateTurnHud(traveledDistance = 0) {
  const slotId = ui.slotInput?.value?.trim() || "—";
  if (ui.hudSlot) ui.hudSlot.textContent = slotId;
  if (ui.selectedSlotSummary) ui.selectedSlotSummary.textContent = slotId;

  if (!state.route || !ui.turnBanner) {
    if (ui.turnBanner) ui.turnBanner.hidden = true;
    return;
  }

  const next = nextTurnGuidance(state.route.waypoints, traveledDistance);
  ui.turnBanner.hidden = false;
  if (ui.turnLabel) ui.turnLabel.textContent = next.label;
  if (ui.turnDistance) {
    ui.turnDistance.textContent = next.isArrival && next.distanceM < 0.7
      ? "Now"
      : next.distanceM < 1
        ? "Now"
        : `${Math.round(next.distanceM)} m`;
  }
  if (ui.turnIcon) {
    const deg = { left: -90, right: 90, uturn: 180, arrive: 0, straight: 0 }[next.kind] || 0;
    ui.turnIcon.style.transform = `rotate(${deg}deg)`;
    ui.turnIcon.style.opacity = next.kind === "arrive" ? "0.55" : "1";
  }
  setHint("", false);
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

function updateCalibrationReadiness() {
  const allDone = Object.values(state.calibration.completed).every(Boolean);
  state.sessionReady = allDone;
  setStatusChip(allDone ? "Calibrated" : "Ready", allDone ? "live" : "warn");
  ui.localizeBtn.disabled = !allDone;
  updateWorkflowControls();
}

function canEnterWorkflowStep(step) {
  if (step === "destination") return true;
  if (step === "calibration") return Boolean(ui.slotInput.value.trim());
  if (step === "navigation") return state.sessionReady;
  return false;
}

function updateWorkflowControls() {
  const hasDestination = Boolean(ui.slotInput.value.trim());
  ui.destinationContinueBtn.disabled = !hasDestination;
  ui.calibrationContinueBtn.disabled = !state.sessionReady;
  if (ui.selectedSlotSummary) {
    ui.selectedSlotSummary.textContent = hasDestination ? ui.slotInput.value.trim() : "No slot selected";
  }
  if (ui.hudSlot) ui.hudSlot.textContent = hasDestination ? ui.slotInput.value.trim() : "—";

  ui.calibrationSteps.forEach((node) => {
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
    showSheet("slot");
    setHint("Choose your parking slot to begin", true);
    setStatusMsg("Select a parking slot, then continue to calibration.");
  } else if (step === "calibration") {
    showSheet("cal");
    setHint("Calibrate sensors before localizing", true);
    setStatusMsg("Complete stillness, compass, and walk calibration.");
  } else if (step === "navigation") {
    hideSheets();
    setHint(state.hasLocalizedPosition
      ? (state.route ? "" : "Tap Start nav for turn-by-turn guidance")
      : "Point the camera and tap Localize", !state.route);
    setStatusMsg(state.hasLocalizedPosition
      ? "Localized. Start nav for live guidance on the camera."
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
        option.label = `F${location.floor} ${location.zone} (${location.x.toFixed(1)}, ${location.y.toFixed(1)})`;
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

async function ensureCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    alert("This browser does not support camera capture.");
    return false;
  }

  const cameraIsActive = state.stream?.getVideoTracks().some((track) => track.readyState === "live");
  if (cameraIsActive) {
    if (ui.cameraView.srcObject !== state.stream) ui.cameraView.srcObject = state.stream;
    if (ui.cameraView.paused) await ui.cameraView.play();
    return true;
  }

  state.stream?.getTracks().forEach((track) => track.stop());
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" },
      audio: false,
    });
    ui.cameraView.srcObject = state.stream;
    await ui.cameraView.play();
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

  let sum = 0;
  let sumSquared = 0;
  let count = 0;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
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

async function captureVprPosition({ trigger = "manual", applyBlurGuard = false } = {}) {
  if (!state.stream || ui.cameraView.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    throw new Error("Camera frame is not ready yet.");
  }

  const sourceWidth = ui.cameraView.videoWidth;
  const sourceHeight = ui.cameraView.videoHeight;
  const maxDimension = Number(ui.captureMaxDimension.value) || 1280;
  const scale = Math.min(1, maxDimension / Math.max(sourceWidth, sourceHeight));
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const width = Math.max(1, Math.round(sourceWidth * scale));
  const height = Math.max(1, Math.round(sourceHeight * scale));
  canvas.width = width;
  canvas.height = height;
  ctx.drawImage(ui.cameraView, 0, 0, width, height);
  const clientLaplacianVariance = varianceOfLaplacian(ctx.getImageData(0, 0, width, height), width, height);
  const jpegQuality = Number(ui.captureJpegQuality.value) || 0.8;
  const diagnostics = {
    trigger,
    motion_score: state.motion.cumulativeMotion,
    client_laplacian_variance: clientLaplacianVariance,
    source_width: sourceWidth,
    source_height: sourceHeight,
    encoded_width: width,
    encoded_height: height,
    jpeg_quality: jpegQuality,
    screen_orientation: screen.orientation?.type || "unknown",
  };

  if (applyBlurGuard && ui.blurGuardEnabled.checked && clientLaplacianVariance < 80) {
    await logClientDiagnostic({ event: "frame_skipped_blur", ...diagnostics });
    return { skipped: true, diagnostics };
  }

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", jpegQuality));
  if (!blob) throw new Error("Browser failed to encode the camera frame.");

  const form = new FormData();
  form.append("image", blob, "parking-frame.jpg");
  const groundTruth = ui.groundTruthLocationId.value.trim();
  if (groundTruth) form.append("ground_truth_location_id", groundTruth);
  for (const [key, value] of Object.entries(diagnostics)) form.append(key, String(value));

  // lock ARCore frame at the moment of this VPR fix
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

function applyVprFix(pos) {
  state.position = {
    floor: pos.floor,
    x: pos.x,
    y: pos.y,
    heading: pos.heading,
    confidence: pos.confidence,
    tracking: true,
  };
  state.hasLocalizedPosition = true;
  state.motion.gravityEstimate = null;
  state.motion.lastMagnitude = null;
  state.motion.cumulativeMotion = 0;
  const smoothedRawHeading = state.rawHeadingHistory.length > 0
    ? circularMeanDeg(state.rawHeadingHistory)
    : state.lastCompassSample;
  if (smoothedRawHeading !== null) {
    state.headingOffset = (pos.heading - smoothedRawHeading + 360) % 360;
  }

  setStatusChip("VPR fix", "live");
  setStatusMsg(`Locked at ${pos.x.toFixed(2)}, ${pos.y.toFixed(2)} m · live VIO/PDR between fixes`);
  ui.localizeBtn.textContent = "Relocalize";
  ui.routeBtn.disabled = false;
  if (ui.positionState) ui.positionState.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)}`;
  if (ui.confidenceState) ui.confidenceState.textContent = pos.confidence.toFixed(2);
  ui.metricFloor.textContent = pos.floor ?? "—";
  ui.metricCoords.textContent = `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)} m`;
  ui.metricHeading.textContent = `${Math.round(pos.heading)}°`;
  startLiveHeartbeat();
  ensureArVio().then((ok) => {
    if (ok) startArTracking();
  });
  updateRouteStatus();
}

async function localizeParkingPosition() {
  if (!state.sessionReady) {
    alert("Please complete the 3-step calibration before localizing.");
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
      setStatusChip("VPR miss", "warn");
      setStatusMsg(state.hasLocalizedPosition
        ? "VPR miss — continuing with VIO/PDR. Relocalize to correct drift."
        : "No verified position yet. Move to a recognizable area and try again.");
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
  if (
    state.liveVpr.running
    || !state.hasLocalizedPosition
    || !state.route
    || !state.stream
    || state.workflowStep !== "navigation"
  ) return;

  state.liveVpr.running = true;
  state.liveVpr.lastCaptureTime = performance.now();
  state.motion.lastMeaningfulMotionAt = performance.now();
  state.motion.cumulativeMotion = 0;
  setStatusMsg("Live guidance active · VPR + VIO/PDR between fixes");
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
}

async function runLiveVprCaptureLoop() {
  const minimumIntervalMs = 300;
  const maximumIntervalMs = 2000;
  const motionThreshold = 1.5;
  const stillnessWindowMs = 350;

  while (state.liveVpr.running) {
    if (state.workflowStep !== "navigation" || !state.route || !state.stream) {
      stopLiveVprCapture();
      return;
    }

    const now = performance.now();
    const elapsed = now - state.liveVpr.lastCaptureTime;
    const movedEnough = state.motion.cumulativeMotion >= motionThreshold;
    const intervalElapsed = elapsed >= maximumIntervalMs;
    const triggerMode = ui.captureTriggerMode.value;
    const triggerAfterStillness = triggerMode === "motion_then_stillness"
      && now - state.motion.lastMeaningfulMotionAt >= stillnessWindowMs;
    const motionReady = movedEnough && (triggerMode !== "motion_then_stillness" || triggerAfterStillness);

    if (
      !state.liveVpr.requestInFlight
      && elapsed >= minimumIntervalMs
      && now >= state.liveVpr.nextCaptureAllowedAt
      && (motionReady || intervalElapsed)
    ) {
      state.liveVpr.requestInFlight = true;
      const trigger = intervalElapsed && !motionReady
        ? "max_interval"
        : triggerMode === "motion_then_stillness" ? "motion_then_stillness" : "motion";

      try {
        const capture = await captureVprPosition({ trigger, applyBlurGuard: true });
        if (capture.skipped) {
          state.liveVpr.nextCaptureAllowedAt = performance.now() + 400;
          setStatusMsg(`Skipped blurry frame (sharpness ${capture.diagnostics.client_laplacian_variance.toFixed(1)})`);
          continue;
        }
        state.motion.cumulativeMotion = 0;
        state.liveVpr.lastCaptureTime = performance.now();
        const fix = capture.position;
        if (!state.liveVpr.running) return;

        if (!fix.tracking || fix.confidence <= 0) {
          setStatusMsg("VPR miss — continuing with VIO/PDR until next fix");
        } else {
          applyVprFix(fix);
          try {
            await updateRouteForSlot(ui.slotInput.value.trim());
          } catch (error) {
            setStatusMsg(`VPR ok, route refresh failed: ${error.message}`);
          }
          setStatusMsg("Live guidance active · VPR + VIO/PDR");
        }
      } catch (error) {
        console.warn("Live VPR capture failed; PDR will continue.", error);
        setStatusMsg(`Live VPR unavailable — VIO/PDR continues. ${error.message}`);
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
    distance.textContent = remainingDistance < 0.7
      ? "Now"
      : `In ${Math.round(remainingDistance)} m`;
    item.appendChild(distance);
    ui.turnInstructions.appendChild(item);
  }
}

async function updateRouteForSlot(slotId) {
  const response = await fetch(`/route/${encodeURIComponent(state.deviceId)}?slot_id=${encodeURIComponent(slotId)}`);
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "No route found");
  }

  const route = await response.json();
  state.route = route;
  if (ui.routeBadge) {
    ui.routeBadge.classList.remove("neutral", "warning");
    ui.routeBadge.classList.add("success");
    ui.routeBadge.textContent = "Guidance ready";
  }
  if (ui.routeDestination) ui.routeDestination.textContent = slotId;
  if (ui.routeTotalDistance) ui.routeTotalDistance.textContent = `${route.total_distance.toFixed(1)} m`;
  if (ui.routeStatus) ui.routeStatus.textContent = `Floor ${route.floor}`;
  setStatusChip("Navigating", "live");
  setStatusMsg(`Route ready · ${route.total_distance.toFixed(1)} m to ${slotId}`);
  setHint("", false);
  renderTurnInstructions(route.waypoints);
  drawRoute(route.waypoints, slotId);
}

async function navigateToSlot() {
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

  try {
    await updateRouteForSlot(slotId);
    const camOk = state.stream || await ensureCamera();
    if (camOk) {
      startLiveVprCapture();
    } else {
      setStatusMsg("Route ready, but camera is unavailable for live VPR.");
    }
  } catch (error) {
    console.error(error);
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
    alert(error.message);
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

  const progress = routeProgress();
  if (ui.routeDistance) ui.routeDistance.textContent = `${progress.remainingDistance.toFixed(1)} m`;
  if (ui.routeTotalDistance) ui.routeTotalDistance.textContent = `${state.route.total_distance.toFixed(1)} m`;
  ui.metricDistance.textContent = `${progress.remainingDistance.toFixed(1)} m`;
  if (ui.routeStatus) {
    ui.routeStatus.textContent = progress.remainingDistance < 0.7
      ? `Arrived · Floor ${state.route.floor}`
      : `Floor ${state.route.floor}`;
  }
  if (progress.remainingDistance < 0.7) {
    setStatusChip("Arrived", "live");
  }

  const marker = ui.routeSvg?.querySelector("#livePositionMarker");
  if (marker && state.routeScreenPoint) {
    const point = state.routeScreenPoint([state.position.x, state.position.y]);
    marker.setAttribute("cx", String(point.x));
    marker.setAttribute("cy", String(point.y));
  }

  renderTurnInstructions(state.route.waypoints, progress.distanceAlongRoute);
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

ui.destinationContinueBtn.addEventListener("click", () => setWorkflowStep("calibration", true));
ui.calibrationBackBtn.addEventListener("click", () => setWorkflowStep("destination", true));
ui.calibrationContinueBtn.addEventListener("click", () => setWorkflowStep("navigation", true));
ui.navigationBackBtn?.addEventListener("click", () => setWorkflowStep("calibration", true));
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
ui.routeBtn.addEventListener("click", navigateToSlot);
ui.slotInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    setWorkflowStep("calibration", true);
  }
});

window.addEventListener("load", async () => {
  await initSlots();
  updateCalibrationReadiness();
  setWorkflowStep("destination");
  setCalibrationStep("Waiting for calibration to begin.", 0, "stillness");
  updateRouteStatus();
  setInterval(refreshCurrentPosition, 2000);
});
