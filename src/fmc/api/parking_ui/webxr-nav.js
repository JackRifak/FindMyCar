/**
 * WebXR immersive-ar floor arrows for parking-ui.
 * Path is built from facility waypoints (not turn/dist only) so bends match the route.
 * Exposes window.ParkingWebXr for classic app.js.
 */
import * as THREE from "https://unpkg.com/three@0.186.1/build/three.module.js";

const LIME = 0xc9d64f;
const ARROW_N = 8;
const ARROW_GAP = 0.65;
const FIRST_GAP = 0.7;
const ARRIVAL_M = 2.0;
/** how far past the corner the turn glyph stays visible */
const TURN_MARK_AFTER_M = 1.6;

function limeMat(emissive = 0.85) {
  return new THREE.MeshStandardMaterial({
    color: LIME,
    emissive: LIME,
    emissiveIntensity: emissive,
    metalness: 0.05,
    roughness: 0.28,
    side: THREE.DoubleSide,
  });
}

function addFloorGlow(g, scale) {
  const glow = new THREE.Mesh(
    new THREE.CircleGeometry(0.28 * scale, 28),
    new THREE.MeshBasicMaterial({
      color: LIME,
      transparent: true,
      opacity: 0.22,
      depthWrite: false,
    }),
  );
  glow.rotation.x = -Math.PI / 2;
  glow.position.y = 0.003;
  g.add(glow);
}

/**
 * Camera-style V chevron (matches SVG: M8 62 55 12l47 50).
 * Tip along local -Z after rotateX(-90).
 */
function makeFloorChevron(scale = 1) {
  const g = new THREE.Group();
  const shape = new THREE.Shape();
  const w = 0.075;
  shape.moveTo(0, 0.36);
  shape.lineTo(0.42, -0.28);
  shape.lineTo(0.42 - w * 1.5, -0.28 - w * 0.9);
  shape.lineTo(0, 0.36 - w * 2.8);
  shape.lineTo(-0.42 + w * 1.5, -0.28 - w * 0.9);
  shape.lineTo(-0.42, -0.28);
  shape.closePath();

  const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.012, bevelEnabled: false });
  geo.rotateX(-Math.PI / 2);
  geo.translate(0, 0.016, 0);
  const mesh = new THREE.Mesh(geo, limeMat(0.85));
  mesh.scale.setScalar(scale);
  g.add(mesh);
  addFloorGlow(g, scale);
  g.userData.kind = "straight";
  return g;
}

/**
 * L turn glyph — stem along approach (-Z), tip exits left (-X) or right (+X).
 * Shown only at/after the turn (banner: "Turn left · 4 m" → straight 4 m, then this).
 */
function makeFloorTurn(kind = "left", scale = 1) {
  const left = kind === "left";
  const s = left ? -1 : 1;
  const g = new THREE.Group();
  const shape = new THREE.Shape();
  shape.moveTo(-0.09 * s, -0.3);
  shape.lineTo(0.09 * s, -0.3);
  shape.lineTo(0.09 * s, 0.05);
  shape.lineTo(0.3 * s, 0.05);
  shape.lineTo(0.3 * s, 0.18);
  shape.lineTo(0.5 * s, 0);
  shape.lineTo(0.3 * s, -0.18);
  shape.lineTo(0.3 * s, -0.05);
  shape.lineTo(-0.09 * s, -0.05);
  shape.closePath();

  const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.014, bevelEnabled: false });
  geo.rotateX(-Math.PI / 2);
  geo.translate(0, 0.018, 0);
  const mesh = new THREE.Mesh(geo, limeMat(0.95));
  mesh.scale.setScalar(scale);
  g.add(mesh);
  addFloorGlow(g, scale * 1.1);
  g.userData.kind = left ? "left" : "right";
  return g;
}

function aimOnFloor(obj, dir) {
  const d = dir.clone();
  d.y = 0;
  if (d.lengthSq() < 1e-10) return;
  d.normalize();
  // tip was 180° opposite travel — flip so chevrons read <<<<< along the route
  d.negate();
  obj.up.set(0, 1, 0);
  obj.lookAt(obj.position.x + d.x, obj.position.y, obj.position.z + d.z);
}

/** same rules as parking-ui banner: +turn = left */
export function nextManeuverFromLegs(legs, traveledDistance = 0) {
  const list = legs || [];
  let along = 0;
  for (let i = 0; i < list.length; i++) {
    const leg = list[i];
    const abs = Math.abs(leg.turn || 0);
    if (i > 0 && abs >= 30 && traveledDistance <= along + 1.5) {
      const kind = abs >= 150 ? "uturn" : leg.turn > 0 ? "left" : "right";
      return {
        kind,
        label:
          kind === "uturn" ? "Turn around" : kind === "left" ? "Turn left" : "Turn right",
        atM: along,
        distanceM: Math.max(0, along - traveledDistance),
        isArrival: false,
      };
    }
    along += leg.dist || 0;
  }
  const remain = Math.max(0, along - traveledDistance);
  if (remain < ARRIVAL_M) {
    return { kind: "arrive", label: "You've arrived", atM: along, distanceM: 0, isArrival: true };
  }
  return {
    kind: "straight",
    label: "Continue straight",
    atM: along,
    distanceM: remain,
    isArrival: false,
  };
}

/** facility heading: atan2(dx, dy) — 0° = +Y, 90° = +X (same as PnP) */
function turnDeg(ax, ay, bx, by, cx, cy) {
  const dx1 = bx - ax;
  const dy1 = by - ay;
  const dx2 = cx - bx;
  const dy2 = cy - by;
  const cross = dx1 * dy2 - dy1 * dx2;
  const dot = dx1 * dx2 + dy1 * dy2;
  let d = (Math.atan2(cross, dot) * 180) / Math.PI;
  while (d > 180) d -= 360;
  while (d < -180) d += 360;
  return d;
}

/** facility waypoints → turn/dist legs (+turn = left, matches HUD cross-product) */
export function legsFromWaypoints(waypoints) {
  const wps = (waypoints || []).map((w) => [Number(w[0]), Number(w[1])]);
  if (wps.length < 2) return [{ turn: 0, dist: 8 }];
  const legs = [];
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i];
    const b = wps[i + 1];
    const dist = Math.max(0.5, Math.hypot(b[0] - a[0], b[1] - a[1]));
    let turn = 0;
    if (i > 0) {
      const p = wps[i - 1];
      turn = Math.round(turnDeg(p[0], p[1], a[0], a[1], b[0], b[1]));
    }
    legs.push({ turn, dist });
  }
  return legs;
}

/** trim route to remaining path from closest point to user */
export function remainingWaypoints(waypoints, userX, userY) {
  const wps = (waypoints || []).map((w) => [Number(w[0]), Number(w[1])]);
  if (wps.length < 2) return wps;

  let bestD = Infinity;
  let bestI = 0;
  let bestT = 0;
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i];
    const b = wps[i + 1];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy || 1e-12;
    let t = ((userX - a[0]) * dx + (userY - a[1]) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const px = a[0] + dx * t;
    const py = a[1] + dy * t;
    const d = Math.hypot(userX - px, userY - py);
    if (d < bestD) {
      bestD = d;
      bestI = i;
      bestT = t;
    }
  }

  const a = wps[bestI];
  const b = wps[bestI + 1];
  const start = [a[0] + (b[0] - a[0]) * bestT, a[1] + (b[1] - a[1]) * bestT];
  // skip to next vertex if already almost at the end of this segment
  const rest = bestT > 0.92 ? wps.slice(bestI + 1) : wps.slice(bestI + 1);
  const out = [start, ...rest];
  // ensure at least 2 points
  if (out.length < 2) out.push(wps[wps.length - 1]);
  return out;
}

/**
 * map facility polyline into XR xz using PnP pose.
 * - anchor: user (x,y) → XR origin
 * - rotate: facility heading → phone forward at calibrate
 * PnP heading: 0° = +Y, 90° = +X (atan2(dx, dy))
 */
export function pathFromWaypoints(waypoints, origin, initialFwd, facilityPose = null) {
  let wps = (waypoints || []).map((w) => [Number(w[0]), Number(w[1])]);
  if (wps.length < 2) return [origin.clone()];

  const pose = facilityPose || {};
  const hasPose = Number.isFinite(pose.x) && Number.isFinite(pose.y);

  if (hasPose) {
    wps = remainingWaypoints(wps, pose.x, pose.y);
  }

  const fwd = initialFwd.clone();
  fwd.y = 0;
  if (fwd.lengthSq() < 1e-8) fwd.set(0, 0, -1);
  else fwd.normalize();
  const xrAngle = Math.atan2(fwd.x, -fwd.z);

  // align route-ahead to phone forward (arrows go down the aisle you're facing).
  // fall back to PnP heading only if the next segment is tiny.
  const dx0 = wps[1][0] - wps[0][0];
  const dy0 = wps[1][1] - wps[0][1];
  const segLen = Math.hypot(dx0, dy0);
  let facAngle;
  if (segLen >= 0.35) {
    facAngle = Math.atan2(dx0, dy0);
  } else if (Number.isFinite(pose.heading)) {
    facAngle = (Number(pose.heading) * Math.PI) / 180;
  } else {
    facAngle = segLen < 1e-6 ? 0 : Math.atan2(dx0, dy0);
  }

  // facility forward → XR forward: rot = fac - xr
  const rot = facAngle - xrAngle;
  const cos = Math.cos(rot);
  const sin = Math.sin(rot);

  const ox = hasPose ? pose.x : wps[0][0];
  const oy = hasPose ? pose.y : wps[0][1];

  return wps.map(([fx, fy]) => {
    const lx = fx - ox;
    const ly = fy - oy;
    const x2 = lx * cos - ly * sin;
    const y2 = lx * sin + ly * cos;
    return new THREE.Vector3(origin.x + x2, origin.y, origin.z - y2);
  });
}

function polyLen(pts) {
  let n = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    n += Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].z - pts[i].z);
  }
  return n;
}

function closestOnPath(pts, x, z) {
  if (pts.length < 2) {
    return {
      progressM: 0,
      point: pts[0]?.clone() ?? new THREE.Vector3(),
      dir: new THREE.Vector3(0, 0, -1),
      segIdx: 0,
    };
  }
  let bestD = Infinity;
  let bestProg = 0;
  let bestPt = pts[0].clone();
  let bestDir = new THREE.Vector3(0, 0, -1);
  let bestSeg = 0;
  let acc = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dz = b.z - a.z;
    const len2 = dx * dx + dz * dz || 1e-12;
    const len = Math.sqrt(len2);
    let t = ((x - a.x) * dx + (z - a.z) * dz) / len2;
    t = Math.max(0, Math.min(1, t));
    const px = a.x + dx * t;
    const pz = a.z + dz * t;
    const d = Math.hypot(x - px, z - pz);
    if (d < bestD) {
      bestD = d;
      bestProg = acc + len * t;
      bestPt.set(px, a.y, pz);
      bestDir.set(dx / len, 0, dz / len);
      bestSeg = i;
    }
    acc += len;
  }
  return { progressM: bestProg, point: bestPt, dir: bestDir, segIdx: bestSeg };
}

function pointAt(pts, progressM) {
  if (pts.length < 2) {
    return { pos: pts[0]?.clone() ?? new THREE.Vector3(), dir: new THREE.Vector3(0, 0, -1) };
  }
  let left = Math.max(0, progressM);
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dz = b.z - a.z;
    const len = Math.hypot(dx, dz) || 1e-6;
    if (left <= len) {
      const t = left / len;
      return {
        pos: new THREE.Vector3(a.x + dx * t, a.y, a.z + dz * t),
        dir: new THREE.Vector3(dx / len, 0, dz / len),
      };
    }
    left -= len;
  }
  const a = pts[pts.length - 2];
  const b = pts[pts.length - 1];
  const dx = b.x - a.x;
  const dz = b.z - a.z;
  const len = Math.hypot(dx, dz) || 1;
  return { pos: b.clone(), dir: new THREE.Vector3(dx / len, 0, dz / len) };
}

function sampleAhead(pts, fromM, count, spacing, firstGap = 0.75) {
  const out = [];
  for (let i = 0; i < count; i++) {
    const m0 = fromM + firstGap + i * spacing;
    const m1 = m0 + Math.min(spacing, 0.55);
    const a = pointAt(pts, m0);
    const b = pointAt(pts, m1);
    const dir = new THREE.Vector3(b.pos.x - a.pos.x, 0, b.pos.z - a.pos.z);
    if (dir.lengthSq() < 1e-10) dir.copy(a.dir);
    else dir.normalize();
    out.push({ pos: a.pos, dir });
  }
  return out;
}

export async function canUseWebXr() {
  if (!window.isSecureContext) return false;
  const xr = navigator.xr;
  if (!xr?.isSessionSupported) return false;
  try {
    return await xr.isSessionSupported("immersive-ar");
  } catch {
    return false;
  }
}

export class WebXrNav {
  constructor(opts) {
    this.opts = opts;
    this.renderer = new THREE.WebGLRenderer({
      canvas: opts.canvas,
      alpha: true,
      antialias: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.renderer.setSize(window.innerWidth, window.innerHeight, false);
    this.renderer.xr.enabled = true;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera();
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x333333, 1.0));
    const sun = new THREE.DirectionalLight(0xffffff, 0.85);
    sun.position.set(1, 5, 2);
    this.scene.add(sun);

    this.reticle = new THREE.Mesh(
      new THREE.RingGeometry(0.07, 0.1, 40).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: LIME, transparent: true, opacity: 0.85 }),
    );
    this.reticle.matrixAutoUpdate = false;
    this.reticle.visible = false;
    this.scene.add(this.reticle);

    // each slot: straight V + left/right L (swap by distance to next turn)
    this.arrowSlots = [];
    for (let i = 0; i < ARROW_N; i++) {
      const scale = Math.max(0.75, i === 0 ? 1.25 : 1 - i * 0.05);
      const slot = {
        straight: makeFloorChevron(scale),
        left: makeFloorTurn("left", scale * 1.08),
        right: makeFloorTurn("right", scale * 1.08),
      };
      for (const m of Object.values(slot)) {
        m.visible = false;
        this.scene.add(m);
      }
      this.arrowSlots.push(slot);
    }
    // large corner marker exactly on the turn
    this.cornerMark = {
      left: makeFloorTurn("left", 1.55),
      right: makeFloorTurn("right", 1.55),
    };
    for (const m of Object.values(this.cornerMark)) {
      m.visible = false;
      this.scene.add(m);
    }

    this.refSpace = null;
    this.hitSrc = null;
    this.session = null;
    this.path = [];
    this.pathLen = 0;
    this.pathLine = null;
    this.calibrated = false;
    this.progressM = 0;
    this.maxProgressM = 0;
    this.stepIdx = 0;
    this.arrived = false;
    this.floorY = null;
    this.tmp = new THREE.Vector3();
    this.mat = new THREE.Matrix4();
    this.q = new THREE.Quaternion();
    this.fwd = new THREE.Vector3();
    this.pulseT = 0;
    this.legs = opts.legs || legsFromWaypoints(opts.waypoints || []);
    // raw camera for PnP/VPR during floor change (chrome android)
    this.capturePending = null;
    this._capBusy = false;
    this._capFb = null;
    this._capCanvas = null;
    this._capSrcCanvas = null;
    this.hasCameraAccess = false;
  }

  async start() {
    const xr = navigator.xr;
    if (!xr) {
      this.opts.handlers?.onErr?.("unsupported");
      return false;
    }

    let session;
    try {
      session = await xr.requestSession("immersive-ar", {
        requiredFeatures: ["hit-test", "dom-overlay"],
        optionalFeatures: ["local-floor", "anchors", "camera-access"],
        domOverlay: { root: this.opts.overlayRoot },
      });
    } catch {
      try {
        session = await xr.requestSession("immersive-ar", {
          requiredFeatures: ["hit-test"],
          optionalFeatures: ["local-floor", "dom-overlay", "camera-access"],
          domOverlay: { root: this.opts.overlayRoot },
        });
      } catch {
        this.opts.handlers?.onErr?.("denied");
        return false;
      }
    }

    this.session = session;
    this.hasCameraAccess = Boolean(
      session.enabledFeatures?.has?.("camera-access")
      || (Array.isArray(session.enabledFeatures) && session.enabledFeatures.includes("camera-access")),
    );
    this.renderer.xr.setReferenceSpaceType("local-floor");
    await this.renderer.xr.setSession(session);

    try {
      this.refSpace = await session.requestReferenceSpace("local-floor");
    } catch {
      this.refSpace = await session.requestReferenceSpace("local");
    }

    const viewerSpace = await session.requestReferenceSpace("viewer");
    if (session.requestHitTestSource) {
      this.hitSrc = (await session.requestHitTestSource({ space: viewerSpace })) ?? null;
    }

    session.addEventListener("end", () => this.onSessionEnd());
    this.renderer.setAnimationLoop((t, frame) => this.onFrame(t, frame));
    this.opts.handlers?.onStep?.(0);
    return true;
  }

  stop() {
    const s = this.session;
    this.session = null;
    if (s) void s.end().catch(() => {});
    else this.cleanup();
  }

  onSessionEnd() {
    this.cleanup();
    this.opts.handlers?.onEnd?.();
  }

  cleanup() {
    this.renderer.setAnimationLoop(null);
    this.hitSrc?.cancel();
    this.hitSrc = null;
    this.refSpace = null;
    this.session = null;
    this.calibrated = false;
    this.path = [];
    this.hasCameraAccess = false;
    this._capBusy = false;
    if (this.capturePending) {
      clearTimeout(this.capturePending.timer);
      this.capturePending.reject?.(new Error("xr ended"));
      this.capturePending = null;
    }
    const gl = this.renderer?.getContext?.();
    if (gl && this._capFb) {
      try { gl.deleteFramebuffer(this._capFb); } catch (_) { /* ignore */ }
    }
    this._capFb = null;
    this._capCanvas = null;
    if (this.pathLine) {
      this.scene.remove(this.pathLine);
      this.pathLine.geometry.dispose();
      this.pathLine = null;
    }
  }

  /** jpeg blob from XR camera — for /localize (PnP) while AR owns the cam */
  grabFrameBlob({ maxDim = 1280, quality = 0.8 } = {}) {
    if (!this.session) {
      return Promise.reject(new Error("no xr session"));
    }
    if (this.capturePending) {
      return Promise.reject(new Error("capture busy"));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.capturePending?.timer === timer) {
          this.capturePending = null;
          this._capBusy = false;
          reject(new Error("xr camera capture timeout"));
        }
      }, 1800);
      this.capturePending = { resolve, reject, maxDim, quality, timer };
    });
  }

  _finishCapture(blob) {
    const pending = this.capturePending;
    if (!pending) {
      this._capBusy = false;
      return;
    }
    clearTimeout(pending.timer);
    this.capturePending = null;
    this._capBusy = false;
    if (blob) pending.resolve(blob);
    else pending.reject(new Error("xr camera frame empty"));
  }

  /** sync gl read → async jpeg; returns false if camera not ready this frame */
  _tryCameraCapture(pose) {
    if (!this.capturePending || this._capBusy || !pose?.views?.length) return false;
    const gl = this.renderer.getContext();
    if (!gl || typeof XRWebGLBinding === "undefined") return false;

    let binding = null;
    try {
      binding = this.renderer.xr.getBinding?.() || new XRWebGLBinding(this.session, gl);
    } catch {
      return false;
    }

    let cam = null;
    let tex = null;
    for (const view of pose.views) {
      if (!view.camera) continue;
      try {
        tex = binding.getCameraImage(view.camera);
      } catch {
        tex = null;
      }
      if (tex) {
        cam = view.camera;
        break;
      }
    }
    // no camera-access grant yet — keep waiting until timeout
    if (!cam || !tex) return false;

    const w = cam.width | 0;
    const h = cam.height | 0;
    if (w < 8 || h < 8) return false;

    const prevFb = gl.getParameter(gl.FRAMEBUFFER_BINDING);
    const prevTex = gl.getParameter(gl.TEXTURE_BINDING_2D);
    if (!this._capFb) this._capFb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._capFb);
    gl.framebufferTexture2D(
      gl.FRAMEBUFFER,
      gl.COLOR_ATTACHMENT0,
      gl.TEXTURE_2D,
      tex,
      0,
    );
    const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
    if (status !== gl.FRAMEBUFFER_COMPLETE) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, prevFb);
      gl.bindTexture(gl.TEXTURE_2D, prevTex);
      return false;
    }

    const pixels = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    gl.bindFramebuffer(gl.FRAMEBUFFER, prevFb);
    gl.bindTexture(gl.TEXTURE_2D, prevTex);

    // gl origin is bottom-left — flip for PnP
    const rgba = new Uint8ClampedArray(w * h * 4);
    const row = w * 4;
    for (let y = 0; y < h; y += 1) {
      const src = (h - 1 - y) * row;
      rgba.set(pixels.subarray(src, src + row), y * row);
    }

    const pending = this.capturePending;
    const { maxDim, quality } = pending;
    this._capBusy = true;
    const scale = Math.min(1, maxDim / Math.max(w, h));
    const dw = Math.max(1, Math.round(w * scale));
    const dh = Math.max(1, Math.round(h * scale));
    if (!this._capCanvas) this._capCanvas = document.createElement("canvas");
    if (!this._capSrcCanvas) this._capSrcCanvas = document.createElement("canvas");
    const canvas = this._capCanvas;
    const srcCanvas = this._capSrcCanvas;
    srcCanvas.width = w;
    srcCanvas.height = h;
    srcCanvas.getContext("2d").putImageData(new ImageData(rgba, w, h), 0, 0);
    canvas.width = dw;
    canvas.height = dh;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      this._finishCapture(null);
      return true;
    }
    ctx.drawImage(srcCanvas, 0, 0, dw, dh);
    canvas.toBlob((blob) => this._finishCapture(blob), "image/jpeg", quality);
    return true;
  }

  /**
   * floor-forward = direction from head toward the hit-test reticle (where camera looks).
   * using raw -Z when pitched at the floor often flips 180° behind the user.
   */
  floorForward(pos, orient, hitPos) {
    if (hitPos) {
      const dx = hitPos.x - pos.x;
      const dz = hitPos.z - pos.z;
      if (dx * dx + dz * dz > 0.01) {
        return new THREE.Vector3(dx, 0, dz).normalize();
      }
    }
    this.q.set(orient.x, orient.y, orient.z, orient.w);
    // view dir flattened
    const view = new THREE.Vector3(0, 0, -1).applyQuaternion(this.q);
    const flat = new THREE.Vector3(view.x, 0, view.z);
    if (flat.lengthSq() > 0.04) return flat.normalize();
    // straight-down fallback: phone top projected on floor
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(this.q);
    up.y = 0;
    if (up.lengthSq() > 1e-8) return up.normalize();
    return new THREE.Vector3(0, 0, -1);
  }

  calibrate(pos, orient, hitPos = null) {
    this.fwd.copy(this.floorForward(pos, orient, hitPos));

    const origin = pos.clone();
    if (this.floorY != null) origin.y = this.floorY;

    const wps = this.opts.waypoints;
    if (wps?.length >= 2) {
      this.path = pathFromWaypoints(wps, origin, this.fwd, this.opts.facilityPose || null);
    } else {
      this.path = pathFromLegs(this.legs, origin, this.fwd);
    }
    this.pathLen = polyLen(this.path) || this.opts.distanceM || 1;
    this.calibrated = true;
    this.progressM = 0;
    this.maxProgressM = 0;
    // rebuild legs for HUD from remaining polyline
    if (wps?.length >= 2 && this.opts.facilityPose) {
      const rem = remainingWaypoints(wps, this.opts.facilityPose.x, this.opts.facilityPose.y);
      this.legs = legsFromWaypoints(rem);
      this.opts.handlers?.onStep?.(0);
    }
    this.drawPathRibbon();
  }

  drawPathRibbon() {
    if (this.pathLine) {
      this.scene.remove(this.pathLine);
      this.pathLine.geometry.dispose();
    }
    if (this.path.length < 2) return;
    const y = (this.floorY ?? this.path[0].y) + 0.012;
    const pts = this.path.map((p) => new THREE.Vector3(p.x, y, p.z));
    this.pathLine = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: LIME, transparent: true, opacity: 0.55 }),
    );
    this.scene.add(this.pathLine);
  }

  onFrame(t, frame) {
    if (!frame || !this.refSpace || !this.session) return;
    this.pulseT = t * 0.001;

    const pose = frame.getViewerPose(this.refSpace);
    if (!pose) {
      this.opts.handlers?.onTracking?.(false);
      this.renderer.render(this.scene, this.camera);
      return;
    }
    this.opts.handlers?.onTracking?.(true);

    // snag passthrough frame for PnP while elevator / floor detect is active
    if (this.capturePending && !this._capBusy) {
      this._tryCameraCapture(pose);
    }

    const pos = pose.transform.position;
    this.tmp.set(pos.x, pos.y, pos.z);

    let hitPose = null;
    let hitPos = null;
    if (this.hitSrc) {
      const hits = frame.getHitTestResults(this.hitSrc);
      if (hits.length) hitPose = hits[0].getPose(this.refSpace) ?? null;
    }
    if (hitPose) {
      this.mat.fromArray(hitPose.transform.matrix);
      this.reticle.visible = true;
      this.reticle.matrix.copy(this.mat);
      this.floorY = hitPose.transform.position.y;
      hitPos = new THREE.Vector3(
        hitPose.transform.position.x,
        hitPose.transform.position.y,
        hitPose.transform.position.z,
      );
    } else {
      this.reticle.visible = false;
    }

    if (!this.calibrated) {
      if (this.floorY == null || !hitPos) {
        this.renderer.render(this.scene, this.camera);
        return;
      }
      // aim path toward the floor spot you're looking at (not behind you)
      this.calibrate(this.tmp, pose.transform.orientation, hitPos);
    }

    const closest = closestOnPath(this.path, this.tmp.x, this.tmp.z);
    this.progressM = closest.progressM;
    if (this.progressM > this.maxProgressM) this.maxProgressM = this.progressM;

    const remain = Math.max(0, this.pathLen - this.progressM);
    this.opts.handlers?.onWalk?.(this.progressM, remain);
    this.bumpSteps(this.maxProgressM);
    this.placeArrows(remain);
    this.renderer.render(this.scene, this.camera);
  }

  hideSlot(slot) {
    slot.straight.visible = false;
    slot.left.visible = false;
    slot.right.visible = false;
  }

  showSlot(slot, kind, pos, dir, scale) {
    this.hideSlot(slot);
    const key = kind === "left" || kind === "right" ? kind : "straight";
    const mesh = slot[key];
    mesh.visible = true;
    mesh.position.copy(pos);
    aimOnFloor(mesh, dir);
    mesh.scale.setScalar(scale);
  }

  /**
   * Banner "Turn left · 4 m" → straight chevrons for those 4 m,
   * then left L-marks at/after the corner (not left marks during the approach).
   */
  placeArrows(remain) {
    const y = (this.floorY ?? this.path[0]?.y ?? 0) + 0.02;
    const samples = sampleAhead(this.path, this.progressM, ARROW_N, ARROW_GAP, FIRST_GAP);
    const pulse = 1 + Math.sin(this.pulseT * 4) * 0.04;
    const mnv = nextManeuverFromLegs(this.legs, this.progressM);
    const isTurn = mnv.kind === "left" || mnv.kind === "right";
    const turnAt = mnv.atM;

    for (const m of Object.values(this.cornerMark)) m.visible = false;
    if (isTurn && turnAt >= this.progressM - 0.4 && mnv.distanceM < 40) {
      const at = pointAt(this.path, turnAt);
      const approach = pointAt(this.path, Math.max(0, turnAt - 0.45));
      const mark = this.cornerMark[mnv.kind];
      mark.visible = true;
      mark.position.set(at.pos.x, y + 0.012, at.pos.z);
      aimOnFloor(mark, approach.dir);
      mark.scale.setScalar(1.4 + pulse * 0.1);
    }

    for (let i = 0; i < ARROW_N; i++) {
      const slot = this.arrowSlots[i];
      const s = samples[i];
      const along = FIRST_GAP + i * ARROW_GAP;
      const sampleM = this.progressM + along;
      if (!s || remain < along * 0.35) {
        this.hideSlot(slot);
        continue;
      }
      const end = pointAt(this.path, this.pathLen);
      const distToEnd = Math.hypot(s.pos.x - end.pos.x, s.pos.z - end.pos.z);
      if (distToEnd < 0.15 && i > 0) {
        this.hideSlot(slot);
        continue;
      }

      // before turn → straight; at/just after turn → left/right; further → straight again
      let kind = "straight";
      let dir = s.dir;
      if (isTurn) {
        if (sampleM < turnAt - 0.35) {
          kind = "straight";
        } else if (sampleM <= turnAt + TURN_MARK_AFTER_M) {
          kind = mnv.kind;
          // L glyph oriented by approach into the corner
          dir = pointAt(this.path, Math.max(0, turnAt - 0.45)).dir;
        } else {
          kind = "straight";
        }
      }

      const scale = (i === 0 ? 1.15 : 1) * pulse * Math.max(0.5, 1 - i * 0.07);
      this.showSlot(slot, kind, new THREE.Vector3(s.pos.x, y, s.pos.z), dir, scale);
    }
  }

  bumpSteps(progressM) {
    if (this.arrived) return;
    const legs = this.legs;
    let end = 0;
    let next = this.stepIdx;
    const hyst = 1.2;
    for (let i = 0; i < legs.length; i++) {
      end += legs[i].dist;
      if (progressM < end - hyst) {
        next = i;
        break;
      }
      next = i;
    }
    if (next !== this.stepIdx) {
      this.stepIdx = next;
      this.opts.handlers?.onStep?.(next);
      navigator.vibrate?.(30);
    }
    if (Math.max(0, this.pathLen - progressM) <= ARRIVAL_M) {
      this.arrived = true;
      this.opts.handlers?.onArrived?.();
      navigator.vibrate?.([80, 60, 80]);
    }
  }

  dispose() {
    this.stop();
    this.renderer.dispose();
  }
}

/** turn/dist fallback when no polyline */
function pathFromLegs(legs, origin, initialFwd) {
  const fwd = initialFwd.clone();
  fwd.y = 0;
  if (fwd.lengthSq() < 1e-8) fwd.set(0, 0, -1);
  else fwd.normalize();
  let yaw = Math.atan2(fwd.x, -fwd.z);
  let x = origin.x;
  let z = origin.z;
  const pts = [new THREE.Vector3(x, origin.y, z)];
  for (const leg of legs) {
    // +turn = left (facility CCW); yaw+= is right when facing -Z → subtract
    yaw -= THREE.MathUtils.degToRad(leg.turn || 0);
    const fx = Math.sin(yaw);
    const fz = -Math.cos(yaw);
    x += fx * (leg.dist || 0);
    z += fz * (leg.dist || 0);
    pts.push(new THREE.Vector3(x, origin.y, z));
  }
  return pts;
}

window.ParkingWebXr = {
  canUseWebXr,
  WebXrNav,
  legsFromWaypoints,
  pathFromWaypoints,
  remainingWaypoints,
  nextManeuverFromLegs,
  turnDeg,
};
