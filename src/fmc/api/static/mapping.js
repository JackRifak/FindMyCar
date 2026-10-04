const video = document.getElementById('video');
const arPreview = document.getElementById('ar-preview');
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
let arPreviewUrl = null;
let arPreviewTimer = null;
let arPreviewBusy = false;
const statusEl = document.getElementById('status');

const btnStart = document.getElementById('btn-start');
const btnStop = document.getElementById('btn-stop');
const btnNewWalk = document.getElementById('btn-new-walk');
const btnClearMap = document.getElementById('btn-clear-map');
const btnShowTag = document.getElementById('btn-show-tag');
const tagPanel = document.getElementById('map-modal');
const btnSubmitTag = document.getElementById('btn-submit-tag');
const btnCancelTag = document.getElementById('btn-cancel-tag');
const btnFinalize = document.getElementById('btn-finalize');
const btnView3d = document.getElementById('btn-view-3d');
const fpCanvas = document.getElementById('floorplan-canvas');
const fpCtx = fpCanvas.getContext('2d');
const mapMarker = document.getElementById('map-marker');
const mapContainer = document.getElementById('map-container');

// AR & Telemetry Elements
const trackingBadge = document.getElementById('tracking-badge');
const featuresBadge = document.getElementById('features-badge');
const poseHud = document.getElementById('pose-hud');
const hudPos = document.getElementById('hud-pos');
const hudRot = document.getElementById('hud-rot');
const serverUrlInput = document.getElementById('server-url');
const btnSaveServer = document.getElementById('btn-save-server');
const btnToggleFeatures = document.getElementById('btn-toggle-features');

// Real-Time Feature Overlay Elements
const featCanvas = document.getElementById('features-canvas');
const featCtx = featCanvas ? featCanvas.getContext('2d') : null;
let showFeaturesOverlay = true;
let activeFeatures = {
    detected: [],
    tracked: [],
    timestamp: 0,
    imageWidth: 1280,
    imageHeight: 720
};

if (btnToggleFeatures) {
    btnToggleFeatures.onclick = () => {
        showFeaturesOverlay = !showFeaturesOverlay;
        btnToggleFeatures.innerText = showFeaturesOverlay ? "Features: ON" : "Features: OFF";
        btnToggleFeatures.style.color = showFeaturesOverlay ? "#34c759" : "#888";
        btnToggleFeatures.style.borderColor = showFeaturesOverlay ? "#34c759" : "#555";
        if (featuresBadge) {
            featuresBadge.style.display = showFeaturesOverlay && (activeFeatures.detected.length || activeFeatures.tracked.length) ? 'inline-block' : 'none';
        }
    };
}

// 3D Viewer Elements
const viewerModal = document.getElementById('viewer-modal');
const viewerContainer = document.getElementById('viewer-container');
const btnCloseViewer = document.getElementById('btn-close-viewer');
const viewerStats = document.getElementById('viewer-stats');

// Live Debug Log Elements
const debugLogEntries = document.getElementById('debug-log-entries');
const btnClearLog = document.getElementById('btn-clear-log');
if (btnClearLog) {
    btnClearLog.onclick = () => {
        if (debugLogEntries) debugLogEntries.innerHTML = '<div style="color: #666;">Log cleared.</div>';
    };
}

function logDebug(message, level = 'info') {
    const timeStr = new Date().toLocaleTimeString();
    console.log(`[FMC ${timeStr}] ${message}`);
    if (debugLogEntries) {
        const item = document.createElement('div');
        const color = level === 'error' ? '#ff453a' : level === 'success' ? '#32d74b' : level === 'warn' ? '#ffd60a' : '#8e8e93';
        item.style.color = color;
        item.innerText = `[${timeStr}] ${message}`;
        debugLogEntries.prepend(item);
        while (debugLogEntries.children.length > 40) {
            debugLogEntries.removeChild(debugLogEntries.lastChild);
        }
    }
}

let isMapping = false;
let deviceId = "mapper-" + Math.random().toString(36).substr(2, 6);

// 6-DOF Live Sensor State — ARCore preferred, compass PDR fallback
let currentPos = { x: 0.0, y: 0.0, z: 0.0 };
let currentRot = { qw: 1.0, qx: 0.0, qy: 0.0, qz: 0.0, headingDeg: 0 };
let lastAccelNorm = 9.8;
let lastStepTime = 0;
let lastPdrTs = 0;
let smoothHeading = null;
const STEP_LENGTH = 0.65; // ~0.65m per step (bonus if accel peaks fire)
const WALK_SPEED = 0.85; // m/s — slightly under real walk; over-speed warped prior maps
let arVioActive = false;
let arVioPollTimer = null;
let poseSource = "pdr"; // "arcore" | "pdr"

// WebXR State (optional leftover)
let xrSession = null;
let xrRefSpace = null;
let xrSupported = false;

// Map Picker state
let floorplanImg = new Image();
let transform = null;
let selectedWorldX = null;
let selectedWorldY = null;
let selectedPx = null;
let selectedPy = null;
let placedTags = []; // {x, y, px, py} kept across pause/resume
let surveySpots = []; // {id, px, py} P5–P8 guides on floorplan
let sessionActive = false;

// Backend API URL helper
let apiBase = localStorage.getItem('fmc_server_url') || '';
if (serverUrlInput) {
    serverUrlInput.value = apiBase;
    btnSaveServer.onclick = () => {
        let val = serverUrlInput.value.trim();
        if (val.endsWith('/')) val = val.slice(0, -1);
        localStorage.setItem('fmc_server_url', val);
        apiBase = val;
        statusEl.innerText = "Server URL saved: " + (apiBase || "Default (current host)");
        loadFloorplanAndTransform();
    };
}

function getApiUrl(path) {
    if (!apiBase) return path;
    return apiBase.endsWith('/') ? `${apiBase.slice(0, -1)}${path}` : `${apiBase}${path}`;
}

// Convert DeviceOrientation Euler angles (alpha, beta, gamma) to Quaternion
function eulerToQuaternion(alphaDeg, betaDeg, gammaDeg) {
    const degToRad = Math.PI / 180;
    const _x = (betaDeg || 0) * degToRad;
    const _y = (gammaDeg || 0) * degToRad;
    const _z = (alphaDeg || 0) * degToRad;

    const cX = Math.cos(_x / 2);
    const cY = Math.cos(_y / 2);
    const cZ = Math.cos(_z / 2);
    const sX = Math.sin(_x / 2);
    const sY = Math.sin(_y / 2);
    const sZ = Math.sin(_z / 2);

    return {
        qw: cX * cY * cZ - sX * sY * sZ,
        qx: sX * cY * cZ - cX * sY * sZ,
        qy: cX * sY * cZ + sX * cY * sZ,
        qz: cX * cY * sZ + sX * sY * cZ
    };
}

function updateHUD() {
    if (poseHud) {
        poseHud.style.display = 'block';
        hudPos.innerText = `X: ${currentPos.x.toFixed(2)}m | Y: ${currentPos.y.toFixed(2)}m | Z: ${currentPos.z.toFixed(2)}m`;
        hudRot.innerText = `Heading: ${currentRot.headingDeg.toFixed(0)}° | Q: [${currentRot.qx.toFixed(2)}, ${currentRot.qy.toFixed(2)}, ${currentRot.qz.toFixed(2)}, ${currentRot.qw.toFixed(2)}]`;
    }
}

function stopWebCamera() {
    try {
        if (video && video.srcObject) {
            video.srcObject.getTracks().forEach((t) => t.stop());
            video.srcObject = null;
        }
        if (video) video.style.display = 'none';
    } catch (_) {}
}

function showArPreview(jpegBase64) {
    if (!arPreview || !jpegBase64) return;
    if (arPreviewUrl) URL.revokeObjectURL(arPreviewUrl);
    const bin = atob(jpegBase64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const blob = new Blob([bytes], { type: 'image/jpeg' });
    arPreviewUrl = URL.createObjectURL(blob);
    arPreview.src = arPreviewUrl;
    arPreview.style.display = 'block';
    if (video) video.style.display = 'none';
}

function startArPreviewLoop() {
    if (arPreviewTimer) return;
    arPreviewTimer = setInterval(async () => {
        if (!arVioActive || arPreviewBusy || !window.ArVio || !ArVio.getPreview) return;
        arPreviewBusy = true;
        try {
            const res = await ArVio.getPreview();
            if (res && res.ok && res.jpegBase64) showArPreview(res.jpegBase64);
        } catch (_) { /* keep last frame */ }
        arPreviewBusy = false;
    }, 120);
}

function stopArPreviewLoop() {
    if (arPreviewTimer) {
        clearInterval(arPreviewTimer);
        arPreviewTimer = null;
    }
    arPreviewBusy = false;
}

async function grabArJpegBlob() {
    if (!window.ArVio || typeof ArVio.captureFrame !== 'function') return null;
    const res = await ArVio.captureFrame();
    if (!res || !res.jpegBase64) return null;
    showArPreview(res.jpegBase64);
    const bin = atob(res.jpegBase64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: 'image/jpeg' });
}

async function startArVioIfAvailable() {
    if (!window.ArVio) return false;
    try {
        const ok = await ArVio.isAvailable();
        if (!ok) {
            logDebug("ARCore not available — using PDR", "warn");
            return false;
        }
        // ARCore takes the camera exclusively — release WebView getUserMedia first
        stopWebCamera();
        await ArVio.start();
        arVioActive = true;
        poseSource = "arcore";
        if (arVioPollTimer) clearInterval(arVioPollTimer);
        arVioPollTimer = setInterval(pollArVioPose, 50);
        if (trackingBadge) {
            trackingBadge.innerText = "ARCore: 6-DOF VIO";
            trackingBadge.className = "badge badge-success";
        }
        logDebug("ARCore 6-DOF tracking started (camera via ARCore)", "success");
        startArPreviewLoop();
        return true;
    } catch (err) {
        arVioActive = false;
        poseSource = "pdr";
        logDebug("ARCore start failed: " + (err.message || err) + " — PDR fallback", "warn");
        return false;
    }
}

async function pollArVioPose() {
    if (!arVioActive || !window.ArVio) return;
    try {
        const pose = await ArVio.getPose();
        if (!pose || pose.tracking !== "tracking") {
            if (trackingBadge && pose) {
                trackingBadge.innerText = `ARCore: ${pose.tracking || "lost"}`;
            }
            return;
        }
        currentPos.x = Number(pose.x) || 0;
        currentPos.y = Number(pose.y) || 0;
        currentPos.z = Number(pose.z) || 0;
        currentRot.qw = Number(pose.qw) || 1;
        currentRot.qx = Number(pose.qx) || 0;
        currentRot.qy = Number(pose.qy) || 0;
        currentRot.qz = Number(pose.qz) || 0;
        currentRot.headingDeg = Number(pose.headingDeg) || 0;
        poseSource = "arcore";
        if (trackingBadge) {
            trackingBadge.innerText = "ARCore: tracking";
            trackingBadge.className = "badge badge-success";
        }
        updateHUD();
    } catch (_) { /* keep last pose */ }
}

async function stopArVio() {
    stopArPreviewLoop();
    if (arVioPollTimer) {
        clearInterval(arVioPollTimer);
        arVioPollTimer = null;
    }
    if (arVioActive && window.ArVio) {
        try { await ArVio.stop(); } catch (_) {}
    }
    arVioActive = false;
}

// Initialize Motion Sensors (DeviceOrientation + Accelerometer Step Detection)
function initSensors() {
    // ARCore is started from initCamera(); PDR sensors stay as fallback

    // 1. Gyroscope / Compass Orientation (used when ARCore inactive)
    const onOrientation = (e) => {
        if (arVioActive && poseSource === "arcore") return;
        let heading = e.alpha;
        if (e.webkitCompassHeading !== undefined) {
            heading = e.webkitCompassHeading;
        }
        if (heading === null || heading === undefined) return;

        // unwrap + low-pass — raw compass jumps (±90°) snake the PDR path
        if (smoothHeading === null) {
            smoothHeading = heading;
        } else {
            let d = heading - smoothHeading;
            while (d > 180) d -= 360;
            while (d < -180) d += 360;
            if (Math.abs(d) > 35) d = Math.sign(d) * 35; // cap per-sample turn
            smoothHeading = (smoothHeading + d * 0.35 + 360) % 360;
        }
        currentRot.headingDeg = smoothHeading;
        const q = eulerToQuaternion(smoothHeading, e.beta, e.gamma);
        currentRot.qw = q.qw;
        currentRot.qx = q.qx;
        currentRot.qy = q.qy;
        currentRot.qz = q.qz;
        updateHUD();
    };

    window.addEventListener("deviceorientation", onOrientation, true);
    window.addEventListener("deviceorientationabsolute", onOrientation, true);

    // 2. Accelerometer step peaks (bonus; often silent inside Android WebView)
    window.addEventListener("devicemotion", (e) => {
        if (arVioActive && poseSource === "arcore") return;
        const acc = e.acceleration || e.accelerationIncludingGravity;
        if (!acc) return;
        const norm = Math.hypot(acc.x || 0, acc.y || 0, acc.z || 0);
        const now = Date.now();

        if (norm - lastAccelNorm > 1.2 && (now - lastStepTime > 280)) {
            lastStepTime = now;
            if (isMapping) {
                const rad = (currentRot.headingDeg || 0) * Math.PI / 180;
                currentPos.x += Math.sin(rad) * STEP_LENGTH * 0.35;
                currentPos.z += Math.cos(rad) * STEP_LENGTH * 0.35;
                updateHUD();
            }
        }
        lastAccelNorm = norm * 0.25 + lastAccelNorm * 0.75;
    }, true);

    if (trackingBadge && !arVioActive) {
        trackingBadge.innerText = "PDR: walk-speed + compass";
        trackingBadge.className = "badge badge-success";
    }
    logDebug("Motion sensors initialized (ARCore if native, else PDR)", "success");
}

function advancePdr() {
    // ARCore owns position when tracking — do not integrate walk-speed on top
    if (arVioActive && poseSource === "arcore") return;
    // primary motion model: constant walk speed while mapping is armed
    // tap Stop before standing still / placing a tag
    if (!isMapping) return;
    const now = performance.now();
    if (!lastPdrTs) {
        lastPdrTs = now;
        return;
    }
    const dt = Math.min(1.5, Math.max(0, (now - lastPdrTs) / 1000));
    lastPdrTs = now;
    if (dt < 0.05) return;
    const rad = (currentRot.headingDeg || 0) * Math.PI / 180;
    const dist = WALK_SPEED * dt;
    currentPos.x += Math.sin(rad) * dist;
    currentPos.z += Math.cos(rad) * dist;
    updateHUD();
}

// 1. Initialize Camera and Map Data
async function loadFloorplanAndTransform() {
    try {
        const trRes = await fetch(getApiUrl('/map/transform'));
        if (trRes.ok) {
            transform = await trRes.json();
            logDebug("Loaded floorplan transform matrix", "info");
        } else {
            console.error("Failed to fetch transform:", trRes.status);
            statusEl.innerText = "Transform load failed: " + trRes.status;
            logDebug("Failed to load transform: HTTP " + trRes.status, "warn");
        }
    } catch (e) {
        console.error("Network error fetching transform", e);
        logDebug("Transform fetch error: " + e.message, "warn");
    }

    try {
        const spRes = await fetch(getApiUrl('/map/survey-spots'));
        if (spRes.ok) {
            const data = await spRes.json();
            surveySpots = data.spots || [];
            logDebug(`Survey spots: ${surveySpots.map(s => s.id).join(', ') || 'none'}`, "info");
        } else {
            logDebug("Survey spots load failed: HTTP " + spRes.status, "warn");
        }
    } catch (e) {
        logDebug("Survey spots fetch error: " + e.message, "warn");
    }
    
    floorplanImg.src = getApiUrl('/map/floorplan');
    floorplanImg.onload = () => {
        fpCanvas.width = floorplanImg.width;
        fpCanvas.height = floorplanImg.height;
        drawFloorplanTags();
        logDebug(`Floorplan loaded (${floorplanImg.width}x${floorplanImg.height})`, "info");
    };
}

function drawFloorplanTags() {
    if (!fpCanvas.width || !floorplanImg.complete) return;
    fpCtx.clearRect(0, 0, fpCanvas.width, fpCanvas.height);
    fpCtx.drawImage(floorplanImg, 0, 0);

    // survey GCPs (P5–P8) — guides for where to drop tags
    surveySpots.forEach((s) => {
        fpCtx.beginPath();
        fpCtx.arc(s.px, s.py, 22, 0, Math.PI * 2);
        fpCtx.fillStyle = 'rgba(255, 149, 0, 0.85)';
        fpCtx.fill();
        fpCtx.lineWidth = 4;
        fpCtx.strokeStyle = '#fff';
        fpCtx.stroke();
        fpCtx.fillStyle = '#000';
        fpCtx.font = 'bold 20px sans-serif';
        fpCtx.textAlign = 'center';
        fpCtx.textBaseline = 'middle';
        fpCtx.fillText(s.id, s.px, s.py);
    });

    placedTags.forEach((t, i) => {
        fpCtx.beginPath();
        fpCtx.arc(t.px, t.py, 18, 0, Math.PI * 2);
        fpCtx.fillStyle = '#ff3b30';
        fpCtx.fill();
        fpCtx.lineWidth = 4;
        fpCtx.strokeStyle = '#fff';
        fpCtx.stroke();
        fpCtx.fillStyle = '#fff';
        fpCtx.font = 'bold 22px sans-serif';
        fpCtx.textAlign = 'center';
        fpCtx.textBaseline = 'middle';
        fpCtx.fillText(String(i + 1), t.px, t.py);
    });
}

async function initCamera() {
    await loadFloorplanAndTransform();
    initSensors();
    // ARCore first — owns the camera on native; getUserMedia only as browser fallback
    const arOk = await startArVioIfAvailable();
    if (arOk) {
        statusEl.innerText = "ARCore camera active. Tap 'Start Mapping' and walk.";
        return;
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: {
                facingMode: { ideal: 'environment' },
                width: { ideal: 1280 },
                height: { ideal: 720 }
            },
            audio: false
        });
        video.srcObject = stream;
        video.style.display = 'block';
        if (arPreview) arPreview.style.display = 'none';
        const track = stream.getVideoTracks()[0];
        statusEl.innerText = "Live camera active. Tap 'Start Mapping' and walk.";
        logDebug(`Camera active: ${track.label || 'Environment Camera'}`, "success");
    } catch (err) {
        console.warn("getUserMedia error:", err);
        statusEl.innerText = "Camera notice: " + err.message;
        logDebug("Camera error: " + err.message, "error");
    }
}

let keyframeCount = 0;

async function blobFromVideoFrame() {
    if (!(video.readyState >= video.HAVE_CURRENT_DATA && video.videoWidth > 0)) return null;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
}

async function uploadKeyframeBlob(blob) {
    if (!blob) return;

    keyframeCount++;
    advancePdr();

    const formData = new FormData();
    formData.append("device_id", deviceId);
    formData.append("timestamp", Date.now() / 1000.0);
    formData.append("vio_x", currentPos.x);
    formData.append("vio_y", currentPos.y);
    formData.append("vio_z", currentPos.z);
    formData.append("vio_qw", currentRot.qw);
    formData.append("vio_qx", currentRot.qx);
    formData.append("vio_qy", currentRot.qy);
    formData.append("vio_qz", currentRot.qz);
    formData.append("heading_deg", currentRot.headingDeg || 0);
    formData.append("image", blob, "frame.jpg");

    try {
        logDebug(`[Upload] Sending Keyframe #${keyframeCount} (${(blob.size/1024).toFixed(0)} KB) at (${currentPos.x.toFixed(1)}, ${currentPos.z.toFixed(1)}m) via ${poseSource}...`);
        const res = await fetch(getApiUrl("/map/keyframe"), { method: "POST", body: formData });
        const data = await res.json();

        if (data.detected_features || data.tracked_features) {
            activeFeatures = {
                detected: data.detected_features || [],
                tracked: data.tracked_features || [],
                timestamp: performance.now(),
                imageWidth: data.image_width || 1280,
                imageHeight: data.image_height || 720
            };
            if (featuresBadge && showFeaturesOverlay) {
                featuresBadge.innerText = `${activeFeatures.tracked.length} tracked | ${activeFeatures.detected.length} detected`;
                featuresBadge.style.display = 'inline-block';
            }
        }

        const level = data.new_landmarks > 0 ? "success" : "info";
        logDebug(`[Server] Frame #${keyframeCount} (ID: ${data.frame_id}): +${data.new_landmarks} 3D points (${data.tracked_features ? data.tracked_features.length : 0} inliers) | Map Total: ${data.total_landmarks} points across ${data.total_keyframes} frames`, level);
        statusEl.innerText = `Keyframe #${keyframeCount} | +${data.new_landmarks} 3D pts | Total Map: ${data.total_landmarks} landmarks`;
    } catch (err) {
        console.error(err);
        statusEl.innerText = "Upload failed: " + err.message;
        logDebug(`[Error] Frame #${keyframeCount} upload failed: ${err.message}`, "error");
    }
}

// 2. Continuous Mapping Loop
async function captureAndSendFrame() {
    if (!isMapping) return;

    let blob = null;
    try {
        if (arVioActive && window.ArVio) {
            blob = await grabArJpegBlob();
        } else {
            blob = await blobFromVideoFrame();
        }
    } catch (err) {
        logDebug("Capture failed: " + (err.message || err), "warn");
    }

    if (!blob) {
        if (isMapping) setTimeout(captureAndSendFrame, 300);
        return;
    }

    await uploadKeyframeBlob(blob);
    if (isMapping) setTimeout(captureAndSendFrame, 1000);
}

async function requestMotionPerms() {
    if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') {
        try { await DeviceOrientationEvent.requestPermission(); } catch (e) {}
    }
    if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
        try { await DeviceMotionEvent.requestPermission(); } catch (e) {}
    }
}

function setWalkButtons(running) {
    isMapping = running;
    btnStart.style.display = running ? 'none' : 'block';
    btnStop.style.display = running ? 'block' : 'none';
    btnShowTag.style.display = 'block';
    btnFinalize.style.display = 'block';
    if (btnNewWalk) btnNewWalk.style.display = 'block';
    if (btnClearMap) btnClearMap.style.display = 'block';
    btnStart.innerText = sessionActive ? 'Resume Walk' : 'Start AR Mapping';
}

async function beginWalk(mode) {
    // mode: 'resume' | 'new-session' | 'clear'
    await requestMotionPerms();
    if (mode === 'clear') {
        try {
            await fetch(getApiUrl("/map/reset"), { method: "POST" });
        } catch (err) {
            logDebug("Clear map failed: " + err.message, "warn");
        }
        currentPos = { x: 0.0, y: 0.0, z: 0.0 };
        keyframeCount = 0;
        placedTags = [];
        smoothHeading = null;
        sessionActive = true;
        drawFloorplanTags();
        mapContainer.querySelectorAll('.tag-dot').forEach((el) => el.remove());
        logDebug("Cleared all mapping sessions", "warn");
    } else if (mode === 'new-session') {
        try {
            const res = await fetch(getApiUrl("/map/new-session"), { method: "POST" });
            const data = await res.json().catch(() => ({}));
            logDebug(
                `New session ${data.session_id ?? '?'} — kept map: ${data.total_landmarks ?? 0} landmarks / ${data.total_tags ?? 0} tags (disk+memory)`,
                "success"
            );
            if ((data.total_landmarks ?? 0) === 0) {
                logDebug("Warning: no prior landmarks in memory/disk — finalize of an earlier walk may be missing", "warn");
            }
        } catch (err) {
            logDebug("New session failed: " + err.message, "warn");
        }
        // restart ARCore so local VIO origin is fresh for this separated walk
        if (arVioActive) {
            await stopArVio();
            await startArVioIfAvailable();
        }
        currentPos = { x: 0.0, y: 0.0, z: 0.0 };
        keyframeCount = 0;
        smoothHeading = null;
        sessionActive = true;
        // placedTags kept so P5–P8 drops stay visible across sessions
    } else if (!sessionActive) {
        // first start this page load — do not wipe server map
        sessionActive = true;
        if (!arVioActive) await startArVioIfAvailable();
        currentPos = { x: 0.0, y: 0.0, z: 0.0 };
        keyframeCount = 0;
        smoothHeading = null;
        logDebug("Mapping session started (prior map kept if any)", "info");
    }
    lastPdrTs = 0;
    setWalkButtons(true);
    statusEl.innerText = `Walking… tags: ${placedTags.length}. Drop a tag at each landmark.`;
    captureAndSendFrame();
}

btnStart.onclick = async () => {
    await beginWalk('resume');
};

btnStop.onclick = () => {
    lastPdrTs = 0;
    setWalkButtons(false);
    statusEl.innerText = `Paused. Tags: ${placedTags.length}. Resume Walk, or New Session for a separated walk (keeps map).`;
};

if (btnNewWalk) {
    btnNewWalk.onclick = async () => {
        if (!confirm(
            "Start a new separated session?\n\n" +
            "• Prior map + tags are kept\n" +
            "• PDR restarts at 0 for this walk\n" +
            "• Drop 2+ tags on this session, then Finalize"
        )) return;
        lastPdrTs = 0;
        await beginWalk('new-session');
    };
}

if (btnClearMap) {
    btnClearMap.onclick = async () => {
        if (!confirm("Clear ALL mapping sessions, point cloud, and tags? This cannot be undone.")) return;
        lastPdrTs = 0;
        await beginWalk('clear');
    };
}

btnShowTag.onclick = () => {
    if (!transform) {
        alert("Floorplan or transform not loaded yet.");
        return;
    }
    // freeze pose while picking so the tag stamps the current spot
    lastPdrTs = 0;
    setWalkButtons(false);
    drawFloorplanTags();
    tagPanel.style.display = 'flex';
};

btnCancelTag.onclick = () => {
    tagPanel.style.display = 'none';
};

// Handle canvas click to place marker
fpCanvas.onclick = (e) => {
    const rect = fpCanvas.getBoundingClientRect();
    const scaleX = fpCanvas.width / rect.width;
    const scaleY = fpCanvas.height / rect.height;
    
    const px = (e.clientX - rect.left) * scaleX;
    const py = (e.clientY - rect.top) * scaleY;
    
    mapMarker.style.display = 'block';
    mapMarker.style.left = (e.clientX - rect.left + mapContainer.scrollLeft) + 'px';
    mapMarker.style.top = (e.clientY - rect.top + mapContainer.scrollTop) + 'px';
    
    selectedPx = px;
    selectedPy = py;
    selectedWorldX = transform.a * px + transform.b * py + transform.tx;
    selectedWorldY = transform.c * px + transform.d * py + transform.ty;
    
    btnSubmitTag.disabled = false;
    btnSubmitTag.innerText = `Confirm Location (${selectedWorldX.toFixed(1)}, ${selectedWorldY.toFixed(1)})`;
};

btnSubmitTag.onclick = async () => {
    if (selectedWorldX === null) return;
    
    const floor = 1;
    const payload = { timestamp: Date.now() / 1000.0, x: selectedWorldX, y: selectedWorldY, floor };
    
    try {
        const res = await fetch(getApiUrl("/map/tag"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        placedTags.push({
            x: selectedWorldX,
            y: selectedWorldY,
            px: selectedPx,
            py: selectedPy
        });
        drawFloorplanTags();
        statusEl.innerText = `Tag ${data.tags_recorded} saved. Resume Walk, then drop another tag — keep all of them.`;
        tagPanel.style.display = 'none';
        mapMarker.style.display = 'none';
        btnSubmitTag.disabled = true;
        btnSubmitTag.innerText = "Confirm Location";
        logDebug(`Tag #${data.tags_recorded} recorded`, "success");
        if (data.tags_recorded < 2) {
            alert("Tag 1 saved. Resume Walk to the next landmark, then Drop Tag again.");
        }
    } catch (err) {
        statusEl.innerText = "Failed to submit tag: " + err.message;
    }
};

btnFinalize.onclick = async () => {
    isMapping = false;
    lastPdrTs = 0;
    btnStart.style.display = 'block';
    btnStop.style.display = 'none';
    if (placedTags.length < 2) {
        statusEl.innerText = `Need 2+ tags total (you have ${placedTags.length}). Each session being aligned needs 2+ tags.`;
        alert("Need at least 2 tags. Drop another tag on this session, or finish a prior session first.");
        return;
    }
    if (btnFinalize.disabled) return;
    btnFinalize.disabled = true;
    statusEl.innerText = "Finalizing map… can take 15–30s (H2GIS). Do not tap again.";
    logDebug("[Finalize] Sending optimization request to server...", "info");
    try {
        const res = await fetch(getApiUrl("/map/finalize"), { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = data.detail || data.error || `finalize failed (${res.status})`;
            statusEl.innerText = String(msg);
            alert(String(msg));
            return;
        }
        const tagN = data.tags_recorded || 0;
        const sessN = (data.aligned_sessions || []).length;
        const alignTxt = data.aligned
            ? `aligned (${sessN} session${sessN === 1 ? '' : 's'})`
            : "local path only — each session needs 2+ tags";
        statusEl.innerText = `Map complete! ${data.landmarks_3d} points, ${tagN} tags (${alignTxt}).`;
        logDebug(
            `[Finalize] ${data.landmarks_3d} landmarks | ${tagN} tags | sessions=${JSON.stringify(data.aligned_sessions || [])}`,
            data.aligned ? "success" : "warn"
        );
        if (tagN < 2) {
            alert("Tip: drop 2+ tags along the walk (start + end) so the map can rotate/scale onto the floorplan. One tag only slides the cloud.");
        }
        if (data.landmarks_3d > 0) {
            show3DViewer();
        } else {
            alert("No 3D points were triangulated yet. Walk further while mapping.");
        }
    } catch (err) {
        statusEl.innerText = "Optimization failed: " + err.message;
        logDebug(`[Finalize] Failed: ${err.message}`, "error");
    } finally {
        btnFinalize.disabled = false;
    }
};

btnView3d.onclick = () => {
    show3DViewer();
};

btnCloseViewer.onclick = () => {
    stopPosePolling();
    viewerModal.style.display = 'none';
};

// 4. Robust Three.js Visualization (No Chrome Aw, Snap! Crashes)
let renderer = null;
let scene = null;
let camera = null;
let controls = null;
let currentPoints = null;
let landmarkGroup = null;
let poseGroup = null;
let show3DLandmarks = true;
let viewerFrame = null; // { center, scale, floorY } after PLY load
let posePollTimer = null;
const btnToggle3DLandmarks = document.getElementById('btn-toggle-3d-landmarks');

if (btnToggle3DLandmarks) {
    btnToggle3DLandmarks.onclick = () => {
        show3DLandmarks = !show3DLandmarks;
        btnToggle3DLandmarks.innerText = show3DLandmarks ? "📍 Landmarks: ON" : "📍 Landmarks: OFF";
        btnToggle3DLandmarks.style.background = show3DLandmarks ? "#ff9500" : "#444";
        btnToggle3DLandmarks.style.color = show3DLandmarks ? "#000" : "#ccc";
        if (landmarkGroup) landmarkGroup.visible = show3DLandmarks;
    };
}

function facilityToScene(fx, fy, frame) {
    // aligned PLY: (fx, fy, h) → rotateX(-90°) → (fx, h, -fy), then recenter/scale
    const rx = fx;
    const rz = -fy;
    return {
        x: (rx - frame.center.x) * frame.scale,
        y: frame.floorY,
        z: (rz - frame.center.z) * frame.scale,
    };
}

function stopPosePolling() {
    if (posePollTimer) {
        clearInterval(posePollTimer);
        posePollTimer = null;
    }
}

function startPosePolling() {
    stopPosePolling();
    loadAndRenderLivePoses();
    posePollTimer = setInterval(loadAndRenderLivePoses, 1000);
}

async function loadAndRenderLivePoses() {
    if (!scene || !viewerFrame) return;
    try {
        const res = await fetch(getApiUrl('/map/live-poses?v=' + Date.now()));
        if (!res.ok) return;
        const data = await res.json();
        // server only returns active live sessions
        renderLivePoseMarkers(data.poses || []);
    } catch (e) {
        console.warn("live poses fetch failed", e);
    }
}

function renderLivePoseMarkers(poses) {
    if (!scene || !viewerFrame) return;
    if (poseGroup) scene.remove(poseGroup);
    poseGroup = new THREE.Group();
    scene.add(poseGroup);

    if (!poses.length) {
        if (viewerStats && viewerStats.dataset.base) {
            viewerStats.innerText = viewerStats.dataset.base + ' | you: —';
        }
        return;
    }

    poses.forEach((p) => {
        const pos = facilityToScene(p.x, p.y, viewerFrame);
        const g = new THREE.Group();
        g.position.set(pos.x, viewerFrame.floorY, pos.z);

        // body
        const body = new THREE.Mesh(
            new THREE.CylinderGeometry(0.12, 0.16, 0.9, 16),
            new THREE.MeshBasicMaterial({ color: 0x00e5ff })
        );
        body.position.y = 0.45;
        g.add(body);

        // head
        const head = new THREE.Mesh(
            new THREE.SphereGeometry(0.16, 16, 12),
            new THREE.MeshBasicMaterial({ color: 0xffffff })
        );
        head.position.y = 1.05;
        g.add(head);

        // heading arrow on floor (facility heading: 0=north/+Y → scene -Z)
        const rad = (Number(p.heading) || 0) * Math.PI / 180;
        const fx = Math.sin(rad);
        const fy = Math.cos(rad);
        const dir = new THREE.Vector3(fx, 0, -fy).normalize();
        const arrowLen = 0.85;
        const arrow = new THREE.ArrowHelper(
            dir,
            new THREE.Vector3(0, 0.05, 0),
            arrowLen,
            0x00e5ff,
            0.28,
            0.18
        );
        g.add(arrow);

        // ground ring
        const ring = new THREE.Mesh(
            new THREE.RingGeometry(0.22, 0.38, 32),
            new THREE.MeshBasicMaterial({ color: 0x00e5ff, side: THREE.DoubleSide, transparent: true, opacity: 0.85 })
        );
        ring.rotation.x = -Math.PI / 2;
        ring.position.y = 0.02;
        g.add(ring);

        const shortId = String(p.device_id || 'you').replace(/^parking-mobile-/, 'm-').slice(0, 14);
        const label = createLandmarkLabel(
            `● ${shortId}`,
            `(${p.x.toFixed(1)}, ${p.y.toFixed(1)}) ${p.method || ''}`,
            '#00e5ff'
        );
        label.position.y = 1.45;
        g.add(label);

        poseGroup.add(g);
    });

    if (viewerStats) {
        const base = viewerStats.dataset.base || viewerStats.innerText.split(' | you:')[0];
        viewerStats.dataset.base = base;
        const p0 = poses[0];
        viewerStats.innerText =
            `${base} | you: (${p0.x.toFixed(1)}, ${p0.y.toFixed(1)}) h=${Math.round(p0.heading)}° ${p0.method}`;
    }
}

function createLandmarkLabel(text, subtext, color = '#ff9500') {
    const c = document.createElement('canvas');
    c.width = 256;
    c.height = 80;
    const ctx = c.getContext('2d');

    // Rounded rectangle background badge
    ctx.fillStyle = 'rgba(15, 18, 26, 0.92)';
    ctx.strokeStyle = color;
    ctx.lineWidth = 4;
    
    const x = 5, y = 5, w = 246, h = 70, r = 14;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Primary Text
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 22px -apple-system, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 128, 27);

    // Subtext (Coordinates)
    if (subtext) {
        ctx.fillStyle = color;
        ctx.font = 'bold 15px monospace';
        ctx.fillText(subtext, 128, 52);
    }

    const texture = new THREE.CanvasTexture(c);
    texture.minFilter = THREE.LinearFilter;
    const spriteMaterial = new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true });
    const sprite = new THREE.Sprite(spriteMaterial);
    sprite.scale.set(1.4, 0.44, 1.0);
    return sprite;
}

async function loadAndRender3DLandmarks(center, scale, floorY) {
    if (landmarkGroup && scene) {
        scene.remove(landmarkGroup);
    }
    landmarkGroup = new THREE.Group();
    landmarkGroup.visible = show3DLandmarks;
    if (scene) scene.add(landmarkGroup);

    try {
        const res = await fetch(getApiUrl('/map/tags?v=' + Date.now()));
        if (!res.ok) return;
        const data = await res.json();
        const landmarks = data.all_landmarks || [];
        if (!landmarks.length) return;

        const aligned = data.aligned === true;
        logDebug(
            `[Viewer] Rendering ${landmarks.length} landmarks (aligned=${aligned})...`,
            "info"
        );

        const planePoints = [];
        const commonFloorY = (floorY !== undefined) ? floorY : 0.0;

        landmarks.forEach((lm) => {
            const isUserTag = lm.type === 'user_tag';
            const colorHex = isUserTag ? 0xff3b30 : 0xff9500;
            const colorCss = isUserTag ? '#ff3b30' : '#ff9500';

            // Aligned PLY: (fx, fy, h) → rotateX → (fx, h, -fy)
            // Local PLY:   (mx, h, mz) → rotateX → (mx, mz, -h)
            let rotatedX, rotatedZ, labelXY;
            if (aligned || lm.type === 'facility_landmark') {
                const rawX = lm.facility_x !== undefined ? lm.facility_x : lm.x;
                const rawY = lm.facility_y !== undefined ? lm.facility_y : lm.y;
                rotatedX = rawX;
                rotatedZ = -rawY;
                labelXY = `(${rawX.toFixed(1)}, ${rawY.toFixed(1)})`;
            } else {
                rotatedX = lm.x;
                rotatedZ = lm.z;
                labelXY = `path (${lm.x.toFixed(1)}, ${lm.z.toFixed(1)})`;
            }

            const sceneX = (rotatedX - center.x) * scale;
            const sceneZ = (rotatedZ - center.z) * scale;
            const sceneY = commonFloorY;

            planePoints.push(new THREE.Vector3(sceneX, sceneY + 0.02, sceneZ));

            const pinGroup = new THREE.Group();
            pinGroup.position.set(sceneX, sceneY, sceneZ);

            // 1. Slender vertical pin pole extending UP from the floor plane
            const poleHeight = 1.0;
            const poleGeo = new THREE.CylinderGeometry(0.025, 0.025, poleHeight, 16);
            const poleMat = new THREE.MeshBasicMaterial({ color: colorHex });
            const poleMesh = new THREE.Mesh(poleGeo, poleMat);
            poleMesh.position.y = poleHeight / 2;
            pinGroup.add(poleMesh);

            // 2. Glowing beacon head (Octahedron diamond at top)
            const beaconGeo = new THREE.OctahedronGeometry(0.20, 0);
            const beaconMat = new THREE.MeshBasicMaterial({ color: colorHex });
            const beaconMesh = new THREE.Mesh(beaconGeo, beaconMat);
            beaconMesh.position.y = poleHeight + 0.15;
            pinGroup.add(beaconMesh);

            // 3. Ground target ring lying flat on the floor plane
            const ringGeo = new THREE.RingGeometry(0.08, 0.32, 32);
            const ringMat = new THREE.MeshBasicMaterial({ color: colorHex, side: THREE.DoubleSide });
            const ringMesh = new THREE.Mesh(ringGeo, ringMat);
            ringMesh.rotation.x = Math.PI / 2;
            ringMesh.position.y = 0.01;
            pinGroup.add(ringMesh);

            // 4. Floating 3D billboard label
            const labelText = isUserTag ? `📍 ${lm.label || 'Tag'}` : `📍 ${lm.label || lm.id}`;
            const subText = labelXY;
            const sprite = createLandmarkLabel(labelText, subText, colorCss);
            sprite.position.y = poleHeight + 0.55;
            pinGroup.add(sprite);

            landmarkGroup.add(pinGroup);
        });

        // Add visual alignment guide between landmarks on the same plane
        if (planePoints.length >= 2) {
            const lineGeo = new THREE.BufferGeometry().setFromPoints(planePoints);
            const lineMat = new THREE.LineDashedMaterial({
                color: 0xff9500,
                dashSize: 0.15,
                gapSize: 0.08,
                transparent: true,
                opacity: 0.65
            });
            const line = new THREE.Line(lineGeo, lineMat);
            line.computeLineDistances();
            landmarkGroup.add(line);
        }

        if (viewerStats && landmarks.length > 0) {
            viewerStats.innerText += ` | 📍 ${landmarks.length} Landmarks (Same Plane)`;
            viewerStats.dataset.base = viewerStats.innerText.split(' | you:')[0];
        }
    } catch (e) {
        console.error("Failed loading 3D landmarks:", e);
    }
}

function createCircleTexture() {
    const c = document.createElement('canvas');
    c.width = 32;
    c.height = 32;
    const cctx = c.getContext('2d');
    const grad = cctx.createRadialGradient(16, 16, 0, 16, 16, 15);
    grad.addColorStop(0, 'rgba(255, 255, 255, 1)');
    grad.addColorStop(0.7, 'rgba(255, 255, 255, 0.9)');
    grad.addColorStop(1, 'rgba(255, 255, 255, 0)');
    cctx.fillStyle = grad;
    cctx.beginPath();
    cctx.arc(16, 16, 15, 0, Math.PI * 2);
    cctx.fill();
    return new THREE.CanvasTexture(c);
}

const circleTexture = createCircleTexture();

function show3DViewer() {
    // If WebXR was somehow running, ensure it is ended before opening Three.js
    if (xrSession) {
        xrSession.end().catch(() => {});
        xrSession = null;
    }

    viewerModal.style.display = 'flex';
    if (viewerStats) viewerStats.innerText = "Loading 3D Point Cloud...";

    if (renderer) {
        loadPLY();
        return;
    }

    requestAnimationFrame(() => {
        try {
            scene = new THREE.Scene();
            scene.background = new THREE.Color(0x0a0c10);

            const width = viewerContainer.clientWidth || window.innerWidth;
            const height = viewerContainer.clientHeight || window.innerHeight;

            camera = new THREE.PerspectiveCamera(65, width / height, 0.1, 500);
            camera.position.set(0, 3, 8);

            // Safe WebGLRenderer settings for mobile stability
            renderer = new THREE.WebGLRenderer({
                antialias: false,
                powerPreference: 'default'
            });
            renderer.setSize(width, height);
            renderer.setPixelRatio(1.0); // 1.0 prevents mobile GPU OOM crashes

            // Gracefully handle context loss
            renderer.domElement.addEventListener('webglcontextlost', (e) => {
                e.preventDefault();
                console.warn("WebGL context lost handled gracefully");
            }, false);

            viewerContainer.appendChild(renderer.domElement);

            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.08;

            const gridHelper = new THREE.GridHelper(10, 20, 0x007aff, 0x222233);
            gridHelper.position.y = -0.01;
            scene.add(gridHelper);

            const axesHelper = new THREE.AxesHelper(1.5);
            scene.add(axesHelper);

            window.addEventListener('resize', () => {
                if (viewerModal.style.display === 'flex' && renderer) {
                    const w = viewerContainer.clientWidth || window.innerWidth;
                    const h = viewerContainer.clientHeight || window.innerHeight;
                    camera.aspect = w / h;
                    camera.updateProjectionMatrix();
                    renderer.setSize(w, h);
                }
            });

            function animate() {
                if (viewerModal.style.display === 'flex') {
                    requestAnimationFrame(animate);
                    if (controls) controls.update();
                    if (landmarkGroup && landmarkGroup.children.length > 0) {
                        const time = performance.now() * 0.0025;
                        landmarkGroup.children.forEach((pin, i) => {
                            if (pin.children[1]) {
                                pin.children[1].rotation.y = time + i * 0.8;
                            }
                        });
                    }
                    renderer.render(scene, camera);
                }
            }
            animate();

            loadPLY();
        } catch (err) {
            console.error("Three.js setup error:", err);
            if (viewerStats) viewerStats.innerText = "3D Viewer error: " + err.message;
        }
    });
}

function loadPLY() {
    const loader = new THREE.PLYLoader();
    loader.load(getApiUrl('/map/pointcloud?v=' + Date.now()), function (geometry) {
        try {
            if (currentPoints && scene) scene.remove(currentPoints);
            
            const count = geometry.attributes.position.count;
            if (count === 0) {
                if (viewerStats) viewerStats.innerText = "Point cloud is empty (0 points).";
                return;
            }

            // 1. Orient point cloud so that height axis is Three.js Y (UP)
            // and the floor plane lies horizontally on the Three.js ground plane (X-Z)
            geometry.rotateX(-Math.PI / 2);

            geometry.computeBoundingBox();
            geometry.computeBoundingSphere();
            const sphere = geometry.boundingSphere;
            const center = sphere ? sphere.center.clone() : new THREE.Vector3(0, 0, 0);
            const radius = (sphere && sphere.radius > 0.01) ? sphere.radius : 1.0;

            // Recenter geometry to origin
            geometry.translate(-center.x, -center.y, -center.z);

            // Scale to comfortable ~5m viewport size
            let scale = 1.0;
            if (radius > 12.0 || radius < 0.5) {
                const targetRadius = 5.0;
                scale = targetRadius / radius;
                geometry.scale(scale, scale, scale);
            }

            // Exact horizontal floor elevation
            geometry.computeBoundingBox();
            const floorY = geometry.boundingBox.min.y;

            // Height-based coloring if PLY has no color attribute
            const hasColors = geometry.attributes.color !== undefined;
            if (!hasColors) {
                const minY = geometry.boundingBox.min.y;
                const maxY = geometry.boundingBox.max.y;
                const rangeY = (maxY - minY) || 1.0;
                
                const positions = geometry.attributes.position.array;
                const colors = new Float32Array(count * 3);
                
                for (let i = 0; i < count; i++) {
                    const y = positions[i * 3 + 1];
                    const t = Math.max(0, Math.min(1, (y - minY) / rangeY));
                    
                    const color = new THREE.Color();
                    if (t < 0.5) {
                        color.setRGB(0.1 + 0.2 * (t * 2), 0.7 + 0.3 * (t * 2), 0.9 - 0.4 * (t * 2));
                    } else {
                        color.setRGB(0.3 + 0.6 * ((t - 0.5) * 2), 1.0, 0.5 - 0.5 * ((t - 0.5) * 2));
                    }
                    colors[i * 3] = color.r;
                    colors[i * 3 + 1] = color.g;
                    colors[i * 3 + 2] = color.b;
                }
                geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
            }

            // Crisp round circular particle dots
            const pointSize = Math.max(0.04, Math.min(0.10, 6.0 / Math.sqrt(count)));
            const material = new THREE.PointsMaterial({
                size: pointSize,
                vertexColors: true,
                map: circleTexture,
                transparent: true,
                alphaTest: 0.1,
                sizeAttenuation: true
            });

            currentPoints = new THREE.Points(geometry, material);
            scene.add(currentPoints);

            viewerFrame = { center, scale, floorY };

            // Render all location tag landmarks on the exact same floor plane
            loadAndRender3DLandmarks(center, scale, floorY);

            // Align ground grid with the floor plane
            if (scene) {
                const existingGrid = scene.children.find(c => c.type === 'GridHelper');
                if (existingGrid) {
                    existingGrid.position.y = floorY - 0.01;
                }
            }

            camera.position.set(0, 2.5, 7);
            if (controls) {
                controls.target.set(0, 0, 0);
                controls.update();
            }

            if (viewerStats) {
                const base = `Points: ${count.toLocaleString()} | Structured 3D Cloud | Orbit: Drag | Zoom: Pinch`;
                viewerStats.dataset.base = base;
                viewerStats.innerText = base;
            }
            logDebug(`[Viewer] Point cloud rendered: ${count.toLocaleString()} 3D points`, "success");
            startPosePolling();
        } catch (e) {
            console.error("PLY render error:", e);
            if (viewerStats) viewerStats.innerText = "Render error: " + e.message;
            logDebug(`[Viewer] Render error: ${e.message}`, "error");
        }
    }, undefined, function (error) {
        console.error("Failed to load pointcloud", error);
        if (viewerStats) viewerStats.innerText = "No pointcloud file found on server yet.";
        logDebug("[Viewer] Pointcloud not found or failed to load", "warn");
    });
}

// ---------------------------------------------------------------- AR Feature Overlay Renderer
function renderFeaturesLoop() {
    requestAnimationFrame(renderFeaturesLoop);
    if (!featCanvas || !featCtx) return;

    const rect = featCanvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const targetW = Math.round(rect.width * dpr);
    const targetH = Math.round(rect.height * dpr);
    if (featCanvas.width !== targetW || featCanvas.height !== targetH) {
        featCanvas.width = targetW;
        featCanvas.height = targetH;
    }

    featCtx.clearRect(0, 0, featCanvas.width, featCanvas.height);

    if (!showFeaturesOverlay) return;
    if (!activeFeatures || (!activeFeatures.detected.length && !activeFeatures.tracked.length)) return;

    // Smooth alpha decay over 1.5s until next keyframe refreshes
    const elapsed = (performance.now() - activeFeatures.timestamp) / 1000.0;
    const fade = Math.max(0.15, 1.0 - (elapsed / 1.5));

    const iw = activeFeatures.imageWidth || (video.videoWidth > 0 ? video.videoWidth : 1280);
    const ih = activeFeatures.imageHeight || (video.videoHeight > 0 ? video.videoHeight : 720);
    const cw = featCanvas.width;
    const ch = featCanvas.height;

    // Mathematical projection matching video object-fit: cover
    const scale = Math.max(cw / iw, ch / ih);
    const renderW = iw * scale;
    const renderH = ih * scale;
    const offsetX = (cw - renderW) / 2;
    const offsetY = (ch - renderH) / 2;

    // 1. Draw detected ORB keypoints (subtle glowing cyan dots)
    const detected = activeFeatures.detected;
    featCtx.fillStyle = `rgba(0, 220, 255, ${0.50 * fade})`;
    for (let i = 0; i < detected.length; i++) {
        const pt = detected[i];
        const sx = pt[0] * scale + offsetX;
        const sy = pt[1] * scale + offsetY;
        if (sx < -5 || sx > cw + 5 || sy < -5 || sy > ch + 5) continue;
        featCtx.beginPath();
        featCtx.arc(sx, sy, 2.5 * dpr, 0, Math.PI * 2);
        featCtx.fill();
    }

    // 2. Draw tracked & triangulated inliers (vibrant glowing green AR target reticles)
    const tracked = activeFeatures.tracked;
    for (let i = 0; i < tracked.length; i++) {
        const pt = tracked[i];
        const sx = pt[0] * scale + offsetX;
        const sy = pt[1] * scale + offsetY;
        if (sx < -12 || sx > cw + 12 || sy < -12 || sy > ch + 12) continue;

        // Outer green targeting ring
        featCtx.strokeStyle = `rgba(52, 199, 89, ${0.92 * fade})`;
        featCtx.lineWidth = 1.6 * dpr;
        featCtx.beginPath();
        featCtx.arc(sx, sy, 5.5 * dpr, 0, Math.PI * 2);
        featCtx.stroke();

        // Inner white high-contrast core
        featCtx.fillStyle = `rgba(255, 255, 255, ${fade})`;
        featCtx.beginPath();
        featCtx.arc(sx, sy, 2 * dpr, 0, Math.PI * 2);
        featCtx.fill();

        // AR Crosshair marks
        featCtx.strokeStyle = `rgba(52, 199, 89, ${0.75 * fade})`;
        featCtx.lineWidth = 1.2 * dpr;
        featCtx.beginPath();
        featCtx.moveTo(sx - 8.5 * dpr, sy);
        featCtx.lineTo(sx - 5.5 * dpr, sy);
        featCtx.moveTo(sx + 5.5 * dpr, sy);
        featCtx.lineTo(sx + 8.5 * dpr, sy);
        featCtx.moveTo(sx, sy - 8.5 * dpr);
        featCtx.lineTo(sx, sy - 5.5 * dpr);
        featCtx.moveTo(sx, sy + 5.5 * dpr);
        featCtx.lineTo(sx, sy + 8.5 * dpr);
        featCtx.stroke();
    }
}

// Start feature overlay loop
requestAnimationFrame(renderFeaturesLoop);

// Start camera on load
initCamera();

