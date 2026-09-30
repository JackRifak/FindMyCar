// Find My Car — Hybrid VPR + PDR Live Position Client
// Combines periodic server-side VPR fixes with client-side Pedestrian Dead Reckoning (PDR).
// VPR corrects drift and calibrates orientation; PDR carries continuous position in the gaps.
// Coordinate convention: docs/01_coordinate_system.md (0° = +Y North, 90° = +X East).
//
// Pre-session sensor calibration (docs/07_pdr.md "Sensor calibration wizard"):
// a 3-step wizard (stillness -> magnetometer figure-8 -> short calibration walk) run
// before Start Session to reduce PDR/heading error at the source, plus an in-session
// stride-length auto-refinement that keeps adjusting using real VPR fixes as they land.

const state = {
  deviceId: "live-" + Math.random().toString(36).slice(2, 8),
  capturing: false,
  sending: false,
  stream: null,
  video: null,
  canvas: null,
  trailCanvas: null,
  trailCtx: null,

  // VPR network metrics
  lastCaptureTime: 0,
  lastMotionMagnitude: null,
  cumulativeMotion: 0,
  avgLatencyMs: 0,
  framesSent: 0,
  vprMatches: 0,

  // Hybrid Position State
  pos: {
    floor: 1,
    x: 0.0,
    y: 0.0,
    heading: 0.0,
    confidence: 0.0,
    tracking: false,
  },
  trackingInitialized: false,
  mode: "STANDBY", // "STANDBY" | "VPR_LOCKED" | "PDR_CARRYING" | "VPR_MISS"

  // PDR Metrics
  totalSteps: 0,
  stepsSinceFix: 0,
  distanceWalked: 0.0,
  lastCorrectionDrift: null,
  lastVprFix: null, // { x, y, distanceWalkedAtFix } -- used for in-session stride refinement

  // Trajectory history for 2D visualizer
  // { x, y, heading, source: 'vpr'|'pdr'|'miss', t, drift }
  trail: [],

  // Sensors & Step Detection
  rawHeading: 0.0,
  rawHeadingHistory: [], // rolling buffer of recent raw headings, smooths offset calc at each VPR fix
  rawMag: null, // { x, y, z } from the live Magnetometer sensor, if calibration enabled it
  lastAccelVector: null, // { x, y, z }, used for tilt compensation in device_calibrated compass mode
  headingOffset: 0.0,
  hasHeadingCalibration: false,
  gyroYaw: 0.0,
  lastGyroTimestamp: null,

  // Step detector state
  gravityEMA: 9.81,
  accelWindow: [],
  isStepArmed: false,
  peakAcc: 0,
  valleyAcc: 0,
  lastStepTime: 0,

  // Pre-session calibration (see initCalibrationState)
  calibration: null,

  // Logging & CSV export
  log: [],
};

function initCalibrationState() {
  state.calibration = {
    status: "none", // none | running | done | skipped
    stepLabel: "",
    // Stillness step results
    gyroBias: 0, // deg/s, subtracted from every rotationRate sample for the rest of the session
    gravityMagnitude: 9.81,
    // Magnetometer figure-8 step results
    magSupported: null, // null = not tried yet; true/false once attempted
    magCalibrated: false,
    magHardIron: { x: 0, y: 0, z: 0 },
    magSoftIronScale: { x: 1, y: 1, z: 1 },
    magSensor: null, // live Magnetometer instance kept running post-calibration
    // Walk step results
    walkStepsDetected: 0,
    // Continuous in-session refinement (updated on later VPR fixes too, not just at calibration time)
    strideScaleFactor: 1.0,
  };
}
initCalibrationState();

// ---------------------------------------------------------------- Settings

function getSettings() {
  return {
    // VPR
    minIntervalMs: +document.getElementById("minInterval").value || 300,
    maxIntervalMs: +document.getElementById("maxInterval").value || 2000,
    motionThreshold: +document.getElementById("motionThreshold").value || 1.5,
    maxDimension: +document.getElementById("maxDimension").value || 640,

    // PDR
    pdrEnabled: document.getElementById("pdrEnabled").checked,
    adaptiveStride: document.getElementById("adaptiveStride").checked,
    stepLength: +document.getElementById("stepLength").value || 0.7,
    stepThreshold: +document.getElementById("stepThreshold").value || 1.2,
    compassMode: document.getElementById("compassMode").value || "vpr_calibrated",
    manualHeadingOffset: +document.getElementById("manualHeadingOffset").value || 0,
  };
}

// ---------------------------------------------------------------- Heading & Orientation

function circularMeanDeg(degrees) {
  let sumSin = 0,
    sumCos = 0;
  for (const d of degrees) {
    sumSin += Math.sin((d * Math.PI) / 180);
    sumCos += Math.cos((d * Math.PI) / 180);
  }
  let mean = (Math.atan2(sumSin, sumCos) * 180) / Math.PI;
  if (mean < 0) mean += 360;
  return mean;
}

// Calculates true tilt-compensated compass heading from DeviceOrientation Euler angles (alpha, beta, gamma).
// Handles the phone being held flat (facing up), tilted upright (VPR camera capture), or pitched/rolled.
function computeEulerHeading(event) {
  if (!event) return null;

  // iOS Safari provides webkitCompassHeading directly (0-360 clockwise from North)
  if (typeof event.webkitCompassHeading === "number" && !isNaN(event.webkitCompassHeading)) {
    return event.webkitCompassHeading;
  }

  if (typeof event.alpha !== "number" || isNaN(event.alpha)) {
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
  if (heading === null || isNaN(heading)) return;

  state.rawHeading = heading;

  // Rolling buffer of recent raw headings. Used instead of one instantaneous
  // sample when computing the offset at a VPR fix -- a single noisy compass
  // reading right at the fix moment is exactly what produces the "false turn
  // right after a correction" artifact documented in docs/07_pdr.md.
  state.rawHeadingHistory.push(heading);
  if (state.rawHeadingHistory.length > 10) state.rawHeadingHistory.shift();

  updateLiveHeading();
}

function computeTiltCompensatedHeading() {
  const a = state.lastAccelVector;
  const m = state.rawMag;
  if (!a || !m) return null;

  const c = state.calibration;
  const mx = (m.x - c.magHardIron.x) * c.magSoftIronScale.x;
  const my = (m.y - c.magHardIron.y) * c.magSoftIronScale.y;
  const mz = (m.z - c.magHardIron.z) * c.magSoftIronScale.z;

  const norm = Math.hypot(a.x, a.y, a.z) || 1;
  const gx = a.x / norm,
    gy = a.y / norm;

  const pitch = Math.asin(-gx);
  const roll = Math.asin(gy / Math.cos(pitch));

  const mxComp = mx * Math.cos(pitch) + mz * Math.sin(pitch);
  const myComp = mx * Math.sin(roll) * Math.sin(pitch) + my * Math.cos(roll) - mz * Math.sin(roll) * Math.cos(pitch);

  let headingDeg = (Math.atan2(myComp, mxComp) * 180) / Math.PI;
  // Conversion to compass bearing (0-360, clockwise from north). The exact
  // sign/offset here depends on the device's accelerometer/magnetometer axis
  // convention, which varies across devices and isn't fully standardized --
  // this is why "Device-Calibrated" is an opt-in, clearly experimental compass
  // mode rather than the default. Validate against a couple of known bearings
  // on the actual test device before trusting it; flip signs above if it reads
  // 180° off or mirrored.
  headingDeg = (450 - headingDeg) % 360;
  return headingDeg;
}

function updateLiveHeading() {
  const cfg = getSettings();
  let effectiveHeading;

  if (cfg.compassMode === "device_calibrated") {
    const tiltHeading = computeTiltCompensatedHeading();
    const base = tiltHeading !== null ? tiltHeading : state.rawHeading; // fall back if mag unavailable
    effectiveHeading = (base + state.headingOffset + cfg.manualHeadingOffset + 360) % 360;
  } else if (cfg.compassMode === "vpr_calibrated") {
    effectiveHeading = (state.rawHeading + state.headingOffset + cfg.manualHeadingOffset + 360) % 360;
  } else if (cfg.compassMode === "relative_gyro") {
    effectiveHeading = (state.gyroYaw + cfg.manualHeadingOffset + 360) % 360;
  } else {
    // Raw compass
    effectiveHeading = (state.rawHeading + cfg.manualHeadingOffset + 360) % 360;
  }

  state.pos.heading = effectiveHeading;
}

// ---------------------------------------------------------------- Motion & Step Detection

function onDeviceMotion(event) {
  const now = performance.now();
  const acc = event.accelerationIncludingGravity || event.acceleration;

  // 1. Adaptive VPR trigger accumulation (matching original behavior)
  if (acc) {
    const ax = acc.x || 0,
      ay = acc.y || 0,
      az = acc.z || 0;
    const mag = Math.hypot(ax, ay, az);
    state.lastAccelVector = { x: ax, y: ay, z: az };

    if (state.lastMotionMagnitude !== null) {
      state.cumulativeMotion += Math.abs(mag - state.lastMotionMagnitude);
    }
    state.lastMotionMagnitude = mag;

    // 2. High-pass / dynamic acceleration extraction for Step Detection
    // Update baseline gravity estimate via exponential moving average (EMA)
    state.gravityEMA = 0.92 * state.gravityEMA + 0.08 * mag;
    const dynamicAcc = mag - state.gravityEMA;

    detectStep(dynamicAcc, now);
  }

  // 3. Gyroscope integration for relative yaw (bias-corrected from calibration)
  if (event.rotationRate && event.rotationRate.alpha !== null) {
    // alpha or gamma depending on portrait orientation
    const rotZ = (event.rotationRate.alpha || event.rotationRate.gamma || 0) - state.calibration.gyroBias;
    if (state.lastGyroTimestamp !== null) {
      const dt = (now - state.lastGyroTimestamp) / 1000;
      if (dt > 0 && dt < 0.2) {
        state.gyroYaw = (state.gyroYaw + rotZ * dt + 360) % 360;
      }
    }
    state.lastGyroTimestamp = now;
  }
}

function detectStep(dynAcc, now) {
  const cfg = getSettings();
  if (!cfg.pdrEnabled) return;

  const minStepIntervalMs = 280; // maximum human cadence ~ 3.5 steps/sec
  const threshold = cfg.stepThreshold;

  // Track peak acceleration during foot strike
  if (dynAcc > threshold) {
    if (!state.isStepArmed && now - state.lastStepTime > minStepIntervalMs) {
      state.isStepArmed = true;
      state.peakAcc = dynAcc;
      state.valleyAcc = 0;
    } else if (state.isStepArmed && dynAcc > state.peakAcc) {
      state.peakAcc = dynAcc;
    }
  } else if (state.isStepArmed) {
    if (dynAcc < state.valleyAcc) {
      state.valleyAcc = dynAcc;
    }
    // When acceleration crosses down past zero into the valley, step is confirmed
    if (dynAcc < -0.3 || (dynAcc < 0.1 && state.peakAcc > threshold)) {
      state.isStepArmed = false;
      state.lastStepTime = now;

      // Stride estimation
      let stride = cfg.stepLength;
      if (cfg.adaptiveStride) {
        // Weinberg stride estimation formula: L = K * (a_peak - a_valley)^0.25
        const bounce = Math.max(0.1, state.peakAcc - state.valleyAcc);
        const kWeinberg = 0.43;
        stride = kWeinberg * Math.pow(bounce, 0.25);
        // Clamp to physically realistic adult pedestrian bounds
        stride = Math.min(1.05, Math.max(0.42, stride));
      }

      // Apply the session's stride-scale factor -- starts at 1.0, refined once
      // real VPR fixes give us actual distance-per-step ground truth (see
      // refineStrideScaleFactor in onVprResult).
      stride *= state.calibration.strideScaleFactor;

      processStep(stride);
    }
  }
}

// ---------------------------------------------------------------- PDR Step Execution

function processStep(stepLength) {
  updateLiveHeading();
  const hdgDeg = state.pos.heading;
  const hdgRad = (hdgDeg * Math.PI) / 180;

  // docs/01_coordinate_system.md: 0° = +Y North, 90° = +X East
  const dx = stepLength * Math.sin(hdgRad);
  const dy = stepLength * Math.cos(hdgRad);

  // If tracking has not been initialized by VPR yet, we still allow starting at (0,0)
  if (!state.trackingInitialized) {
    state.trackingInitialized = true;
    state.pos.tracking = true;
    state.pos.confidence = 0.6;
  }

  // Carry the position forward
  state.pos.x += dx;
  state.pos.y += dy;
  state.totalSteps++;
  state.stepsSinceFix++;
  state.distanceWalked += stepLength;

  // Gentle confidence decay between VPR fixes
  state.pos.confidence = Math.max(0.08, state.pos.confidence * 0.985);
  state.mode = "PDR_CARRYING";

  // Record in trail
  state.trail.push({
    x: state.pos.x,
    y: state.pos.y,
    heading: hdgDeg,
    source: "pdr",
    t: Date.now(),
  });
  if (state.trail.length > 1000) state.trail.shift();

  // Log entry
  logEntry(state.pos, 0, "step", "PDR");

  // Update UI and Canvas
  updateDashboard();
  renderCanvas();
}

// ---------------------------------------------------------------- VPR Relocalisation & Fusion

function refineStrideScaleFactor(vprFix) {
  // Uses actual distance-per-step between two consecutive genuine VPR fixes to
  // correct the stride-length estimate for THIS session/device/gait, on top of
  // whatever the pre-session calibration walk or the generic Weinberg formula
  // guessed. Blended (EMA), not a hard override, so one noisy pair of fixes
  // can't swing the multiplier on its own.
  if (!state.lastVprFix || state.stepsSinceFix <= 0) return;

  const fixDistance = Math.hypot(vprFix.x - state.lastVprFix.x, vprFix.y - state.lastVprFix.y);
  const pdrDistanceSinceLastFix = state.distanceWalked - state.lastVprFix.distanceWalkedAtFix;

  // Guard against confusable/wrong-location fixes: if this correction is
  // BIGGER than the distance PDR itself walked since the last fix, the new fix
  // more likely landed on a different (visually similar) location than a
  // confirmation of the path just walked -- refining stride from that would
  // corrupt the estimate rather than improve it, so skip.
  const looksLikeConfusion = state.lastCorrectionDrift !== null && state.lastCorrectionDrift > pdrDistanceSinceLastFix;
  if (looksLikeConfusion || pdrDistanceSinceLastFix < 0.3) return;

  const observedStridePerStep = fixDistance / state.stepsSinceFix;
  const predictedStridePerStep = pdrDistanceSinceLastFix / state.stepsSinceFix;
  const ratio = observedStridePerStep / predictedStridePerStep;

  if (isFinite(ratio) && ratio > 0.4 && ratio < 2.5) {
    const c = state.calibration;
    c.strideScaleFactor = c.strideScaleFactor * 0.7 + ratio * 0.3;
    // Keep it inside physically sane bounds regardless of how the blend lands.
    c.strideScaleFactor = Math.min(1.8, Math.max(0.5, c.strideScaleFactor));
  }
}

function onVprResult(vprFix, latencyMs, trigger) {
  state.framesSent++;
  state.avgLatencyMs = state.avgLatencyMs === 0 ? latencyMs : state.avgLatencyMs * 0.8 + latencyMs * 0.2;

  if (vprFix.tracking && vprFix.confidence > 0.0) {
    state.vprMatches++;

    // Calculate drift between current PDR estimate and VPR ground-truth correction
    if (state.trackingInitialized) {
      const drift = Math.hypot(vprFix.x - state.pos.x, vprFix.y - state.pos.y);
      state.lastCorrectionDrift = drift;
    } else {
      state.lastCorrectionDrift = 0.0;
    }

    refineStrideScaleFactor(vprFix);

    // Auto-calibrate compass offset: aligns device compass to site coordinate
    // frame. Uses the SMOOTHED (circular-mean) recent heading rather than one
    // instantaneous sample, for the same reason as the rolling buffer above.
    if (typeof vprFix.heading === "number") {
      const smoothedRaw =
        state.rawHeadingHistory.length > 0 ? circularMeanDeg(state.rawHeadingHistory) : state.rawHeading;
      state.headingOffset = (vprFix.heading - smoothedRaw + 360) % 360;
      state.hasHeadingCalibration = true;
    }

    state.lastVprFix = { x: vprFix.x, y: vprFix.y, distanceWalkedAtFix: state.distanceWalked };

    // Hard reset / snap position to VPR fix
    state.pos.floor = vprFix.floor;
    state.pos.x = vprFix.x;
    state.pos.y = vprFix.y;
    state.pos.heading = vprFix.heading;
    state.pos.confidence = vprFix.confidence;
    state.pos.tracking = true;
    state.trackingInitialized = true;
    state.stepsSinceFix = 0;
    state.mode = "VPR_LOCKED";

    // Record VPR fix in trajectory
    state.trail.push({
      x: vprFix.x,
      y: vprFix.y,
      heading: vprFix.heading,
      source: "vpr",
      t: Date.now(),
      drift: state.lastCorrectionDrift,
    });

    logEntry(state.pos, latencyMs, trigger, "VPR");
  } else {
    // VPR Miss: The key advantage of the Hybrid design is that PDR continues!
    // We do NOT snap to (0,0) or drop position display.
    state.mode = state.trackingInitialized ? "VPR_MISS" : "STANDBY";

    state.trail.push({
      x: state.pos.x,
      y: state.pos.y,
      heading: state.pos.heading,
      source: "miss",
      t: Date.now(),
    });

    // Log the miss event with tracking: false for VPR server analytics
    const missRecord = {
      floor: state.pos.floor,
      x: state.pos.x,
      y: state.pos.y,
      heading: state.pos.heading,
      confidence: 0.0,
      tracking: false,
    };
    logEntry(missRecord, latencyMs, trigger, "VPR_MISS");
  }

  updateDashboard();
  renderCanvas();
}

// ---------------------------------------------------------------- Pre-session sensor calibration

const CALIBRATION_STILLNESS_MS = 2500;
const CALIBRATION_FIGURE8_MS = 6000;
const CALIBRATION_WALK_EXPECTED_STEPS = 5;

function setCalibrationStep(label) {
  state.calibration.stepLabel = label;
  renderCalibrationUI();
}

async function runCalibrationWizard() {
  const calibrateBtn = document.getElementById("calibrateBtn");
  calibrateBtn.disabled = true;

  // Show feedback immediately — before any async await so the user sees a
  // response to their click even if a system permission dialog is about to appear.
  state.calibration.status = "running";
  state.calibration.stepLabel = "Requesting sensor permission…";
  renderCalibrationUI();

  try {
    // iOS gates devicemotion/deviceorientation behind an explicit, user-gesture
    // permission request -- same call Start Session uses, safe to call twice.
    await requestPermissions();
  } catch (e) {
    setCalibrationStep(`Calibration needs motion sensor permission: ${e.message}`);
    state.calibration.status = "none";
    calibrateBtn.disabled = false;
    return;
  }

  await calibrateStillness();
  await calibrateMagnetometer();
  await calibrateWalk();

  state.calibration.status = "done";
  calibrateBtn.disabled = false;
  calibrateBtn.textContent = "Re-run Calibration";
  renderCalibrationUI();
}

function calibrateStillness() {
  return new Promise((resolve) => {
    setCalibrationStep(`Step 1/3 — Hold the phone still (${(CALIBRATION_STILLNESS_MS / 1000).toFixed(1)}s)...`);

    const accelSamples = [];
    const gyroSamples = [];

    const handler = (event) => {
      const acc = event.accelerationIncludingGravity || event.acceleration;
      if (acc) accelSamples.push(Math.hypot(acc.x || 0, acc.y || 0, acc.z || 0));
      if (event.rotationRate) {
        gyroSamples.push(event.rotationRate.alpha || event.rotationRate.gamma || 0);
      }
    };

    window.addEventListener("devicemotion", handler, true);

    setTimeout(() => {
      window.removeEventListener("devicemotion", handler, true);

      if (accelSamples.length > 0) {
        state.calibration.gravityMagnitude = accelSamples.reduce((a, b) => a + b, 0) / accelSamples.length;
        state.gravityEMA = state.calibration.gravityMagnitude; // seed the live step-detector baseline
      }
      if (gyroSamples.length > 0) {
        state.calibration.gyroBias = gyroSamples.reduce((a, b) => a + b, 0) / gyroSamples.length;
      }
      renderCalibrationUI();
      resolve();
    }, CALIBRATION_STILLNESS_MS);
  });
}

function calibrateMagnetometer() {
  return new Promise(async (resolve) => {
    // ----------------------------------------------------------------
    // Primary path: DeviceOrientationEvent (works on iOS Safari AND
    // Android Chrome over plain HTTP — already permitted by requestPermissions).
    // We collect compass-heading samples during the figure-8 and measure the
    // spread to confirm the sensor is live and responsive.
    //
    // Secondary path (enhancement only): Generic Sensor Magnetometer API for
    // hard/soft-iron correction.  Requires HTTPS + Android Chrome ≥ M67.
    // Silently skipped if unavailable — does not gate the primary flow.
    // ----------------------------------------------------------------

    const headingSamples = [];

    // Collect headings from whichever orientation event the browser fires.
    // webkitCompassHeading  → iOS (0 = North, clockwise, absolute)
    // event.alpha absolute  → Android absolute orientation
    // event.alpha relative  → fallback (relative, still useful for spread)
    const orientHandler = (evt) => {
      const h = computeEulerHeading(evt);
      if (h !== null && !isNaN(h)) headingSamples.push(h);
    };

    window.addEventListener("deviceorientation", orientHandler, true);
    window.addEventListener("deviceorientationabsolute", orientHandler, true);

    setCalibrationStep(
      `Step 2/3 — Slowly rotate the phone in a figure-8 (${(CALIBRATION_FIGURE8_MS / 1000).toFixed(0)}s)…`
    );

    // Optional: also start Generic Sensor Magnetometer for hard/soft-iron correction.
    let magSensor = null;
    const magSamples = [];
    if (typeof Magnetometer !== "undefined" && window.isSecureContext) {
      try {
        // Check permission without throwing on browsers that don't support it.
        try {
          const perm = await navigator.permissions.query({ name: "magnetometer" });
          if (perm.state === "denied") throw new Error("denied");
        } catch (_) {
          // permissions.query may throw if the name isn't recognised — fall through.
        }
        magSensor = new Magnetometer({ frequency: 20 });
        magSensor.addEventListener("reading", () =>
          magSamples.push({ x: magSensor.x, y: magSensor.y, z: magSensor.z })
        );
        magSensor.addEventListener("error", (e) =>
          console.warn("Magnetometer sensor error (non-fatal):", e.error?.name || e)
        );
        magSensor.start();
      } catch (e) {
        console.info(
          `Generic Sensor Magnetometer unavailable (${e.name || e.message}); ` +
            "using heading-spread method only."
        );
        magSensor = null;
      }
    }

    // Wait for the figure-8 window to elapse.
    await sleep(CALIBRATION_FIGURE8_MS);

    // Stop listeners.
    window.removeEventListener("deviceorientation", orientHandler, true);
    window.removeEventListener("deviceorientationabsolute", orientHandler, true);
    if (magSensor) {
      try { magSensor.stop(); } catch (_) {}
    }

    // ---- Evaluate primary (heading-spread) result -------------------------
    if (headingSamples.length < 5) {
      // No orientation events at all — sensor entirely unavailable.
      state.calibration.magSupported = false;
      setCalibrationStep(
        "Step 2/3 — No compass/orientation events received. " +
          "On Android, make sure Location permission is granted. Skipping."
      );
      renderCalibrationUI();
      resolve();
      return;
    }

    // Measure how much the heading spread during the figure-8.
    // A real compass should sweep > 60° if the phone was actually rotated.
    const headingSpreadDeg = circularSpreadDeg(headingSamples);
    state.calibration.magSupported = true;
    state.calibration.magCalibrated = true;

    // ---- Apply Generic Sensor hard/soft-iron correction if available ------
    let calNote = "";
    if (magSamples.length >= 10) {
      const axes = ["x", "y", "z"];
      const ranges = {};
      for (const axis of axes) {
        const mn = Math.min(...magSamples.map((s) => s[axis]));
        const mx = Math.max(...magSamples.map((s) => s[axis]));
        state.calibration.magHardIron[axis] = (mn + mx) / 2;
        ranges[axis] = (mx - mn) / 2 || 1;
      }
      const avgRange = (ranges.x + ranges.y + ranges.z) / 3;
      for (const axis of axes) {
        state.calibration.magSoftIronScale[axis] = avgRange / ranges[axis];
      }

      // Start a live sensor for "Device-Calibrated" compass mode.
      try {
        const liveSensor = new Magnetometer({ frequency: 20 });
        liveSensor.addEventListener("reading", () => {
          state.rawMag = { x: liveSensor.x, y: liveSensor.y, z: liveSensor.z };
        });
        liveSensor.start();
        state.calibration.magSensor = liveSensor;
      } catch (e) {
        console.warn("Could not start live magnetometer after calibration:", e);
      }

      calNote = ` + hard/soft-iron corrected (${magSamples.length} µT samples)`;
    }

    const spreadLabel = headingSpreadDeg > 60
      ? `${headingSpreadDeg.toFixed(0)}° spread — good coverage`
      : `${headingSpreadDeg.toFixed(0)}° spread — rotate more for better calibration`;

    setCalibrationStep(
      `Step 2/3 — Compass calibrated via orientation events: ${spreadLabel}.${calNote}`
    );
    renderCalibrationUI();
    resolve();
  });
}

/**
 * Measure the angular spread (in degrees) of a set of compass readings.
 * Uses circular statistics so 350° → 10° wraps correctly.
 */
function circularSpreadDeg(samples) {
  if (samples.length < 2) return 0;
  const toRad = Math.PI / 180;
  const sinSum = samples.reduce((s, d) => s + Math.sin(d * toRad), 0);
  const cosSum = samples.reduce((s, d) => s + Math.cos(d * toRad), 0);
  const R = Math.hypot(sinSum, cosSum) / samples.length; // mean resultant length [0,1]
  // Circular SD = sqrt(-2 * ln(R)), convert to degrees.  Cap at 180° (full half-sweep).
  return Math.min(180, Math.sqrt(-2 * Math.log(Math.max(R, 1e-9))) / toRad);
}

// Maximum time to wait for the user to complete the walk step before
// auto-finishing (prevents the wizard hanging forever on desktop/no-sensor envs).
const CALIBRATION_WALK_TIMEOUT_MS = 30000;

function calibrateWalk() {
  return new Promise((resolve) => {
    setCalibrationStep(
      `Step 3/3 — Point the phone the way you're about to walk, then walk ${CALIBRATION_WALK_EXPECTED_STEPS} steps ` +
        `straight ahead and tap "Done Walking" (auto-skips in ${CALIBRATION_WALK_TIMEOUT_MS / 1000}s).`
    );

    const headingSamples = [];
    const stepsAtStart = state.totalSteps;

    const orientationHandler = () => headingSamples.push(state.rawHeading);
    window.addEventListener("deviceorientation", orientationHandler, true);
    window.addEventListener("deviceorientationabsolute", orientationHandler, true);

    const doneBtn = document.getElementById("calibWalkDoneBtn");
    doneBtn.style.display = "inline-block";
    doneBtn.disabled = false;

    let finished = false;
    const finish = (autoSkipped) => {
      if (finished) return;
      finished = true;
      clearTimeout(autoFinishTimer);
      window.removeEventListener("deviceorientation", orientationHandler, true);
      window.removeEventListener("deviceorientationabsolute", orientationHandler, true);
      doneBtn.style.display = "none";
      doneBtn.removeEventListener("click", onDoneClick);

      const stepsDetected = state.totalSteps - stepsAtStart;
      state.calibration.walkStepsDetected = stepsDetected;

      if (headingSamples.length > 0) {
        const meanDeg = circularMeanDeg(headingSamples);
        // Seed the rolling buffer with the smoothed calibration-walk heading so
        // the FIRST real VPR fix computes its offset from this averaged value
        // instead of one live noisy sample taken at whatever instant the fix lands.
        state.rawHeadingHistory = [meanDeg];
      }

      if (autoSkipped) {
        setCalibrationStep(`Step 3/3 — Timed out (no motion sensors or no tap detected). Walk step skipped.`);
      } else {
        const detectRatio = stepsDetected / CALIBRATION_WALK_EXPECTED_STEPS;
        let note = "";
        if (detectRatio < 0.7) {
          note = " — fewer steps detected than expected; consider lowering Step Sensitivity.";
        } else if (detectRatio > 1.4) {
          note = " — more steps detected than expected; consider raising Step Sensitivity.";
        }
        setCalibrationStep(`Step 3/3 — Detected ${stepsDetected}/${CALIBRATION_WALK_EXPECTED_STEPS} steps.${note}`);
      }
      renderCalibrationUI();
      resolve();
    };

    const onDoneClick = () => finish(false);
    doneBtn.addEventListener("click", onDoneClick);

    // Auto-finish after timeout so the wizard never hangs on desktop / no-sensor browsers
    const autoFinishTimer = setTimeout(() => finish(true), CALIBRATION_WALK_TIMEOUT_MS);
  });
}

function renderCalibrationUI() {
  const statusEl = document.getElementById("calibStatus");
  const c = state.calibration;

  if (statusEl) {
    const label = c.stepLabel || "Not calibrated yet.";
    statusEl.textContent = label;
    // Highlight the status box during active calibration
    if (c.status === "running") {
      statusEl.style.color = "#60a5fa";  // blue-400 — active
    } else if (c.status === "done") {
      statusEl.style.color = "#4ade80";  // green-400 — done
    } else {
      statusEl.style.color = "";         // reset to CSS default
    }
  }

  const resultsEl = document.getElementById("calibResults");
  if (!resultsEl) return;
  if (c.status === "done" || c.status === "running") {
    resultsEl.style.display = "grid";
    document.getElementById("calGyroBias").textContent = `${c.gyroBias.toFixed(2)}°/s`;
    document.getElementById("calGravity").textContent = `${c.gravityMagnitude.toFixed(2)} m/s²`;
    document.getElementById("calMag").textContent =
      c.magSupported === null ? "—" : c.magSupported ? "Calibrated" : "Unsupported";
    document.getElementById("calSteps").textContent = `${c.walkStepsDetected}/${CALIBRATION_WALK_EXPECTED_STEPS}`;
    document.getElementById("calStride").textContent = `×${c.strideScaleFactor.toFixed(2)}`;
  } else {
    resultsEl.style.display = "none";
  }
}

// ---------------------------------------------------------------- Permissions & Capture Loop

async function requestPermissions() {
  // iOS Safari requires DeviceMotionEvent & DeviceOrientationEvent permission from click handler
  if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
    const motionPerm = await DeviceMotionEvent.requestPermission();
    if (motionPerm !== "granted") {
      throw new Error("Device motion sensor permission denied");
    }
  }

  if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
    const orientPerm = await DeviceOrientationEvent.requestPermission();
    if (orientPerm !== "granted") {
      console.warn("Device orientation permission denied; falling back to relative gyro");
    }
  }

  window.addEventListener("devicemotion", onDeviceMotion, true);
  window.addEventListener("deviceorientation", onDeviceOrientation, true);
  window.addEventListener("deviceorientationabsolute", onDeviceOrientation, true);
}

async function startCapture() {
  await requestPermissions();
  state.stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "environment" },
  });
  state.video.srcObject = state.stream;
  await state.video.play();

  state.capturing = true;
  state.mode = state.trackingInitialized ? "PDR_CARRYING" : "STANDBY";

  document.getElementById("startBtn").disabled = true;
  document.getElementById("stopBtn").disabled = false;
  document.getElementById("downloadBtn").disabled = false;

  updateDashboard();
  captureLoop();
}

function stopCapture() {
  state.capturing = false;
  if (state.stream) {
    state.stream.getTracks().forEach((t) => t.stop());
  }
  window.removeEventListener("devicemotion", onDeviceMotion, true);
  window.removeEventListener("deviceorientation", onDeviceOrientation, true);
  window.removeEventListener("deviceorientationabsolute", onDeviceOrientation, true);

  document.getElementById("startBtn").disabled = false;
  document.getElementById("stopBtn").disabled = true;
  document.getElementById("downloadBtn").disabled = state.log.length === 0;

  state.mode = "STANDBY";
  updateDashboard();
}

async function captureLoop() {
  while (state.capturing) {
    const cfg = getSettings();
    const now = performance.now();
    const sinceLast = now - state.lastCaptureTime;
    const movedEnough = state.cumulativeMotion > cfg.motionThreshold;
    const mustSend = sinceLast >= cfg.maxIntervalMs;
    const canSend = sinceLast >= cfg.minIntervalMs;

    if (!state.sending && canSend && (movedEnough || mustSend)) {
      const trigger = movedEnough ? "motion" : "timeout";
      state.cumulativeMotion = 0;
      state.lastCaptureTime = performance.now();
      state.sending = true;

      captureAndSend(trigger, cfg).finally(() => {
        state.sending = false;
      });
    }
    await sleep(50);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------- Frame Grab & Send

function grabFrameBlob(maxDimension) {
  const vw = state.video.videoWidth,
    vh = state.video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);
  const scale = Math.min(1, maxDimension / Math.max(vw, vh));
  state.canvas.width = Math.round(vw * scale);
  state.canvas.height = Math.round(vh * scale);
  const ctx = state.canvas.getContext("2d");
  ctx.drawImage(state.video, 0, 0, state.canvas.width, state.canvas.height);
  return new Promise((resolve) => state.canvas.toBlob(resolve, "image/jpeg", 0.75));
}

async function captureAndSend(trigger, cfg) {
  const blob = await grabFrameBlob(cfg.maxDimension);
  if (!blob) return;

  const form = new FormData();
  form.append("image", blob, "frame.jpg");

  const t0 = performance.now();
  try {
    const res = await fetch(`/localize?device_id=${state.deviceId}`, {
      method: "POST",
      body: form,
    });
    const latencyMs = performance.now() - t0;

    if (!res.ok) {
      renderError(`Server error ${res.status}`);
      return;
    }
    const pos = await res.json();
    onVprResult(pos, latencyMs, trigger);
  } catch (e) {
    renderError(e.message);
  }
}

// ---------------------------------------------------------------- Dashboard & UI Updates

function updateDashboard() {
  const modeBadge = document.getElementById("statusModeBadge");
  const modeText = document.getElementById("statusModeText");
  const stepBadge = document.getElementById("stepCounterBadge");
  const driftBadge = document.getElementById("driftBadge");

  // Mode badge styling
  modeBadge.className = "status-mode";
  if (state.mode === "VPR_LOCKED") {
    modeBadge.classList.add("mode-vpr");
    modeText.textContent = "VPR LOCKED";
  } else if (state.mode === "PDR_CARRYING") {
    modeBadge.classList.add("mode-pdr");
    modeText.textContent = `PDR CARRYING (+${state.stepsSinceFix} steps)`;
  } else if (state.mode === "VPR_MISS") {
    modeBadge.classList.add("mode-miss");
    modeText.textContent = `VPR MISS (PDR ACTIVE)`;
  } else {
    modeText.textContent = state.capturing ? "SEARCHING..." : "STANDBY";
  }

  // Header badges
  stepBadge.textContent = `${state.totalSteps} steps`;
  if (state.lastCorrectionDrift !== null) {
    driftBadge.textContent = `Drift: ${state.lastCorrectionDrift.toFixed(2)}m`;
  } else {
    driftBadge.textContent = "Drift: —";
  }

  // KPI Grid
  document.getElementById("valFloor").textContent = state.pos.floor ?? "—";
  document.getElementById("valCoords").textContent = `${state.pos.x.toFixed(2)}, ${state.pos.y.toFixed(2)} m`;
  document.getElementById("valHeading").textContent = `${Math.round(state.pos.heading)}°`;
  document.getElementById("valConfidence").textContent = state.pos.confidence.toFixed(2);
  document.getElementById("valSteps").textContent = `${state.stepsSinceFix} / ${state.totalSteps}`;
  document.getElementById("valDistance").textContent = `${state.distanceWalked.toFixed(1)} m`;

  const matchRate = state.framesSent > 0 ? ((state.vprMatches / state.framesSent) * 100).toFixed(0) + "%" : "—";
  document.getElementById("valMatchRate").textContent = matchRate;
  document.getElementById("valLatency").textContent = state.avgLatencyMs > 0 ? `${Math.round(state.avgLatencyMs)} ms` : "—";

  // Raw text diagnostic output
  renderPosition(state.pos);
}

function renderPosition(pos) {
  const el = document.getElementById("posOut");
  if (!el) return;
  el.textContent =
    `[Hybrid VPR+PDR] mode=${state.mode}\n` +
    `floor=${pos.floor}  x=${pos.x.toFixed(2)}  y=${pos.y.toFixed(2)}  heading=${pos.heading.toFixed(0)}°\n` +
    `confidence=${pos.confidence.toFixed(2)}  tracking=${pos.tracking}  steps_since_fix=${state.stepsSinceFix}\n` +
    `avg_latency=${state.avgLatencyMs.toFixed(0)}ms  frames=${state.framesSent}  matches=${state.vprMatches}  drift=${state.lastCorrectionDrift ? state.lastCorrectionDrift.toFixed(2) + "m" : "none"}\n` +
    `stride_scale=×${state.calibration.strideScaleFactor.toFixed(2)}  gyro_bias=${state.calibration.gyroBias.toFixed(2)}°/s`;
}

function renderError(msg) {
  const el = document.getElementById("posOut");
  if (el) el.textContent = "ERROR: " + msg;
}

// ---------------------------------------------------------------- 2D Trajectory Canvas

function initCanvas() {
  state.trailCanvas = document.getElementById("trailCanvas");
  if (!state.trailCanvas) return;
  state.trailCtx = state.trailCanvas.getContext("2d");

  // Set crisp canvas resolution for high-DPI screens
  const dpr = window.devicePixelRatio || 1;
  const rect = state.trailCanvas.getBoundingClientRect();
  state.trailCanvas.width = rect.width * dpr;
  state.trailCanvas.height = 260 * dpr;
  state.trailCtx.scale(dpr, dpr);

  renderCanvas();
}

function renderCanvas() {
  const canvas = state.trailCanvas;
  const ctx = state.trailCtx;
  if (!canvas || !ctx) return;

  const width = canvas.clientWidth || 460;
  const height = canvas.clientHeight || 260;

  ctx.clearRect(0, 0, width, height);

  // Background
  ctx.fillStyle = "#0b0f19";
  ctx.fillRect(0, 0, width, height);

  // Compute bounding box of trail + origin + current position
  let minX = -5,
    maxX = 5,
    minY = -5,
    maxY = 5;
  if (state.trail.length > 0) {
    minX = Math.min(0, state.pos.x);
    maxX = Math.max(0, state.pos.x);
    minY = Math.min(0, state.pos.y);
    maxY = Math.max(0, state.pos.y);

    for (const pt of state.trail) {
      if (pt.x < minX) minX = pt.x;
      if (pt.x > maxX) maxX = pt.x;
      if (pt.y < minY) minY = pt.y;
      if (pt.y > maxY) maxY = pt.y;
    }
  }

  // Add margin around bounding box
  const marginMeters = 4.0;
  minX -= marginMeters;
  maxX += marginMeters;
  minY -= marginMeters;
  maxY += marginMeters;

  const spanX = Math.max(10, maxX - minX);
  const spanY = Math.max(10, maxY - minY);

  // Uniform scale to preserve aspect ratio
  const scale = Math.min((width - 40) / spanX, (height - 40) / spanY);
  const originScreenX = width / 2 - ((minX + maxX) / 2) * scale;
  const originScreenY = height / 2 + ((minY + maxY) / 2) * scale;

  // Function to project world (meters) to canvas pixels
  // Note: Y increases North (+Y), canvas Y increases downwards
  function toScreen(wx, wy) {
    return {
      x: originScreenX + wx * scale,
      y: originScreenY - wy * scale,
    };
  }

  // 1. Draw Grid lines (every 2m or 5m)
  const gridStep = spanX > 40 ? 10 : spanX > 20 ? 5 : 2;
  ctx.strokeStyle = "rgba(255, 255, 255, 0.06)";
  ctx.lineWidth = 1;
  ctx.font = "9px ui-monospace, monospace";
  ctx.fillStyle = "rgba(255, 255, 255, 0.25)";

  const startGX = Math.floor(minX / gridStep) * gridStep;
  const endGX = Math.ceil(maxX / gridStep) * gridStep;
  for (let gx = startGX; gx <= endGX; gx += gridStep) {
    const p1 = toScreen(gx, minY);
    const p2 = toScreen(gx, maxY);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    ctx.fillText(`${gx}m`, p1.x + 2, height - 4);
  }

  const startGY = Math.floor(minY / gridStep) * gridStep;
  const endGY = Math.ceil(maxY / gridStep) * gridStep;
  for (let gy = startGY; gy <= endGY; gy += gridStep) {
    const p1 = toScreen(minX, gy);
    const p2 = toScreen(maxX, gy);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    ctx.fillText(`${gy}m`, 4, p1.y - 2);
  }

  // 2. Draw Facility Origin (0,0) marker
  const pOrigin = toScreen(0, 0);
  ctx.strokeStyle = "rgba(255, 255, 255, 0.3)";
  ctx.setLineDash([2, 2]);
  ctx.beginPath();
  ctx.arc(pOrigin.x, pOrigin.y, 4, 0, Math.PI * 2);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillText("(0,0)", pOrigin.x + 6, pOrigin.y - 4);

  // 3. Draw Continuous PDR Trajectory Path
  if (state.trail.length > 1) {
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();

    let started = false;
    for (const pt of state.trail) {
      if (pt.source === "miss") continue;
      const sp = toScreen(pt.x, pt.y);
      if (!started) {
        ctx.moveTo(sp.x, sp.y);
        started = true;
      } else {
        ctx.lineTo(sp.x, sp.y);
      }
    }
    ctx.stroke();
  }

  // 4. Draw VPR Fix and Miss Markers along the trail
  for (const pt of state.trail) {
    const sp = toScreen(pt.x, pt.y);
    if (pt.source === "vpr") {
      // Emerald VPR fix circle with halo
      ctx.fillStyle = "rgba(16, 185, 129, 0.25)";
      ctx.beginPath();
      ctx.arc(sp.x, sp.y, 7, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#10b981";
      ctx.beginPath();
      ctx.arc(sp.x, sp.y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    } else if (pt.source === "miss") {
      // Amber hollow dot for VPR miss
      ctx.strokeStyle = "#f59e0b";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(sp.x, sp.y, 3, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  // 5. Draw Current Position & Heading Direction Cone
  const curScreen = toScreen(state.pos.x, state.pos.y);
  const hdgRad = (state.pos.heading * Math.PI) / 180;

  // Heading pointer / orientation cone
  const coneLength = 22;
  const coneSpread = 0.45; // radians
  const tipX = curScreen.x + coneLength * Math.sin(hdgRad);
  const tipY = curScreen.y - coneLength * Math.cos(hdgRad);

  const leftX = curScreen.x + coneLength * 0.55 * Math.sin(hdgRad - coneSpread);
  const leftY = curScreen.y - coneLength * 0.55 * Math.cos(hdgRad - coneSpread);

  const rightX = curScreen.x + coneLength * 0.55 * Math.sin(hdgRad + coneSpread);
  const rightY = curScreen.y - coneLength * 0.55 * Math.cos(hdgRad + coneSpread);

  // Vision cone
  const grad = ctx.createRadialGradient(curScreen.x, curScreen.y, 2, curScreen.x, curScreen.y, coneLength);
  grad.addColorStop(0, "rgba(56, 189, 248, 0.45)");
  grad.addColorStop(1, "rgba(56, 189, 248, 0.0)");

  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.moveTo(curScreen.x, curScreen.y);
  ctx.lineTo(leftX, leftY);
  ctx.lineTo(tipX, tipY);
  ctx.lineTo(rightX, rightY);
  ctx.closePath();
  ctx.fill();

  // Pointer triangle
  ctx.fillStyle = "#38bdf8";
  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(leftX, leftY);
  ctx.lineTo(rightX, rightY);
  ctx.closePath();
  ctx.fill();

  // Current position core circle
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.arc(curScreen.x, curScreen.y, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "#0284c7";
  ctx.lineWidth = 2;
  ctx.stroke();
}

// ---------------------------------------------------------------- Logging & CSV Export

function logEntry(pos, latencyMs, trigger, source = "VPR") {
  const entry = {
    t: new Date().toISOString(),
    floor: pos.floor,
    x: pos.x,
    y: pos.y,
    heading: pos.heading,
    confidence: pos.confidence,
    tracking: pos.tracking,
    latencyMs: Math.round(latencyMs),
    trigger: trigger,
    source: source,
    stepCount: state.totalSteps,
    stepsSinceFix: state.stepsSinceFix,
    driftM: state.lastCorrectionDrift !== null ? +state.lastCorrectionDrift.toFixed(2) : "",
  };

  state.log.push(entry);
  if (state.log.length > 600) state.log.shift();

  renderLogTable();
}

function renderLogTable() {
  const rows = state.log.slice(-30).reverse();
  const el = document.getElementById("logTable");
  const countEl = document.getElementById("logCountLabel");
  if (countEl) countEl.textContent = `${state.log.length} events`;
  if (!el) return;

  el.innerHTML =
    "<table><tr><th>t</th><th>src</th><th>x</th><th>y</th><th>hdg</th><th>conf</th><th>steps</th><th>drift</th><th>ms</th></tr>" +
    rows
      .map((r) => {
        let srcClass = "src-vpr";
        if (r.source === "PDR") srcClass = "src-pdr";
        if (r.source === "VPR_MISS") srcClass = "src-miss";

        return (
          `<tr><td>${r.t.slice(11, 19)}</td>` +
          `<td><span class="badge-src ${srcClass}">${r.source}</span></td>` +
          `<td>${r.x.toFixed(2)}</td><td>${r.y.toFixed(2)}</td>` +
          `<td>${Math.round(r.heading)}°</td><td>${r.confidence.toFixed(2)}</td>` +
          `<td>${r.stepsSinceFix}</td><td>${r.driftM !== "" ? r.driftM + "m" : "—"}</td>` +
          `<td>${r.latencyMs}</td></tr>`
        );
      })
      .join("") +
    "</table>";
}

function downloadLogCsv() {
  // Backward-compatible header: first 9 columns match scripts/analyze_live_capture_log.py
  const header = "timestamp,floor,x,y,heading,confidence,tracking,latency_ms,trigger,source,step_count,steps_since_fix,drift_m\n";
  const body = state.log
    .map(
      (r) =>
        `${r.t},${r.floor},${r.x},${r.y},${r.heading},${r.confidence},${r.tracking},` +
        `${r.latencyMs},${r.trigger},${r.source},${r.stepCount},${r.stepsSinceFix},${r.driftM}`
    )
    .join("\n");

  // Calibration summary appended as trailing comment lines -- ignored by
  // analyze_live_capture_log.py's CSV parsing, useful for a human comparing runs.
  const c = state.calibration;
  const calibNotes =
    `\n# calibration_status=${c.status}` +
    ` gyro_bias_deg_s=${c.gyroBias.toFixed(3)}` +
    ` gravity_mag=${c.gravityMagnitude.toFixed(3)}` +
    ` mag_supported=${c.magSupported}` +
    ` walk_steps_detected=${c.walkStepsDetected}/${CALIBRATION_WALK_EXPECTED_STEPS}` +
    ` stride_scale_factor=${c.strideScaleFactor.toFixed(3)}\n`;

  const blob = new Blob([header + body + calibNotes], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `hybrid_live_capture_${state.deviceId}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------- Initialization & Controls

window.addEventListener("load", () => {
  state.video = document.getElementById("video");
  state.canvas = document.getElementById("canvas");

  initCanvas();
  renderCalibrationUI();

  document.getElementById("startBtn").addEventListener("click", () => {
    startCapture().catch((e) => renderError(e.message));
  });

  document.getElementById("stopBtn").addEventListener("click", stopCapture);
  document.getElementById("downloadBtn").addEventListener("click", downloadLogCsv);

  const calibrateBtn = document.getElementById("calibrateBtn");
  if (calibrateBtn) {
    calibrateBtn.addEventListener("click", () => {
      runCalibrationWizard().catch((e) => setCalibrationStep(`Calibration error: ${e.message}`));
    });
  }

  // Manual Step Simulation (for testing on desktop/laptop without motion sensors)
  const simStepBtn = document.getElementById("stepSimBtn");
  if (simStepBtn) {
    simStepBtn.addEventListener("click", () => {
      const cfg = getSettings();
      processStep(cfg.stepLength * state.calibration.strideScaleFactor);
    });
  }

  // Set Origin / Reset track
  const setOriginBtn = document.getElementById("setOriginBtn");
  if (setOriginBtn) {
    setOriginBtn.addEventListener("click", () => {
      state.pos.x = 0.0;
      state.pos.y = 0.0;
      state.trackingInitialized = true;
      state.pos.tracking = true;
      state.pos.confidence = 0.7;
      state.stepsSinceFix = 0;
      state.mode = "PDR_CARRYING";
      state.trail.push({ x: 0, y: 0, heading: state.pos.heading, source: "pdr", t: Date.now() });
      updateDashboard();
      renderCanvas();
    });
  }

  const clearTrackBtn = document.getElementById("clearTrackBtn");
  if (clearTrackBtn) {
    clearTrackBtn.addEventListener("click", () => {
      state.trail = [];
      renderCanvas();
    });
  }

  const recenterBtn = document.getElementById("recenterBtn");
  if (recenterBtn) {
    recenterBtn.addEventListener("click", renderCanvas);
  }

  // Redraw on window resize
  window.addEventListener("resize", () => {
    initCanvas();
  });

  // Keyboard shortcut for testing (Spacebar or 's' triggers simulated step)
  window.addEventListener("keydown", (e) => {
    if (e.target && e.target.tagName === "INPUT") return;
    if (e.key === "s" || e.key === "S" || e.code === "Space") {
      if (e.code === "Space") e.preventDefault();
      const cfg = getSettings();
      processStep(cfg.stepLength * state.calibration.strideScaleFactor);
    }
  });

  updateDashboard();
});
