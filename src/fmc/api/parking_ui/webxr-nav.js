/**
 * WebXR immersive-ar floor arrows for parking-ui.
 * Path is built from facility waypoints (not turn/dist only) so bends match the route.
 * Exposes window.ParkingWebXr for classic app.js.
 */
import * as THREE from "./three.module.js";

const LIME = 0xc9d64f;
const LIME_SOFT = 0xd4e070;
const LIME_HOT = 0xe8f28a;
const ARROW_N = 8;
const ARROW_GAP = 0.72;
const FIRST_GAP = 0.5;
const ARRIVAL_M = 1.0;
/** how far past the corner turn glyphs stay visible */
const TURN_MARK_AFTER_M = 1.2;
/** corner fillet radius (meters) for smooth path bends */
// small: a fillet cuts r·(√2−1) inside a 90° corner — 1.55 m cut 0.64 m, straight
// through corner pillars; 0.75 m cuts ~0.3 m and still reads as a smooth bend
const CORNER_RADIUS_M = 0.75;
const CORNER_SEGS = 16;
/** destination pin shows once the corridor stop is this close (m) */
const DEST_MARKER_SHOW_M = 15;
const DEST_PIN_H = 1.15;
/** PnP re-alignment: keep this many recent fixes */
const ALIGN_KEEP = 6;
/** fixes this far apart (m) start to define rotation from positions, not PnP heading */
const ALIGN_SPAN_MIN_M = 3;
/** fix further than this from where the current alignment expects → outlier */
const ALIGN_OUTLIER_M = 3.5;
/** destination moves more than this → snap instead of easing */
const ALIGN_SNAP_M = 2.0;
const ALIGN_EASE_MS = 900;

/* --- walking-direction heading fix: car-park corridors are straight, so the direction you
 * walk (ARCore trail) vs the corridor you're on gives the map→AR rotation without vision.
 * Axis-only (mod 180°): walking back after a U-turn, or a route that doubles back on a
 * parallel aisle, never reads as a heading error. --- */
const WALK_SAMPLE_M = 0.25; // trail point spacing
const WALK_WINDOW_M = 6; // straight stretch needed before judging
const WALK_STRAIGHT_RATIO = 0.92; // chord / walked length (lower = curved / turning)
const WALK_MAX_DEV_M = 0.35; // max sideways wobble from the chord
const WALK_SEG_MIN_M = 2; // ignore stubby route segments
const WALK_SEG_NEAR_M = 4; // corridor must be this close to where we think you are…
const WALK_SEG_NEAR_MAX_M = 10; // …plus heading-error drift, up to this
// route lines are corridor CENTRE lines and corridors are up to ~8 m wide: only pull you
// back when you'd be outside even the widest corridor, never toward the centre
const WALK_LANE_M = 4.0;
const WALK_MAX_ERR_DEG = 60; // bigger = probably a different corridor, not a heading error
/** after the heading has settled, bigger = walking at an angle across a wide aisle / round cars */
const WALK_SETTLED_MAX_ERR_DEG = 15;
const WALK_SETTLED_AFTER = 2; // corrections before "settled"
const WALK_AGREE_DEG = 6; // two consecutive stretches must agree this closely to act
const WALK_MIN_ERR_DEG = 2;
const WALK_GAIN = 0.5; // close half the agreed error per correction
const WALK_GAIN_PNP_SPAN = 0.3; // gentler once well-spread strong PnP fixes pin rotation
const WALK_COOLDOWN_M = 3; // walk this far between judgements
const WALK_SNAP_DEG = 12; // ease below, snap above

/* --- PnP heading consensus: several weaker fixes that agree are trusted for rotation only --- */
const HEAD_CONSENSUS_N = 3;
/* --- tracking dropout recovery. ARCore may come back in a shifted/rotated frame (reset
 * event, or just a relocalisation jump) — the old map→AR alignment then draws the route
 * through pillars. Hide arrows and re-align instead of showing a wrong path. --- */
const RECOVER_VERIFY_MS = 800; // dropouts longer than this: re-check alignment
const RECOVER_JUMP_M = 1.0; // position jump beyond walking distance → frame moved
const RECOVER_WALK_MPS = 1.6; // fastest plausible walk while hidden
const STALE_FALLBACK_MS = 8000; // no PnP by then → re-anchor from where you were (approx.)

/** AR start: PnP heading within this of the first corridor's axis snaps onto it */
const START_AXIS_SNAP = (25 * Math.PI) / 180;
const HEAD_CONSENSUS_SPREAD_DEG = 10;
const HEAD_CONSENSUS_MAX_AGE_MS = 25000;

function limeMat(emissive = 0.85, color = LIME) {
  return new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: emissive,
    metalness: 0.12,
    roughness: 0.18,
    side: THREE.DoubleSide,
  });
}

function addFloorGlow(g, scale) {
  const glow = new THREE.Mesh(
    new THREE.CircleGeometry(0.42 * scale, 36),
    new THREE.MeshBasicMaterial({
      color: LIME_SOFT,
      transparent: true,
      opacity: 0.28,
      depthWrite: false,
    }),
  );
  glow.rotation.x = -Math.PI / 2;
  glow.position.y = 0.002;
  g.add(glow);

  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.22 * scale, 0.3 * scale, 40),
    new THREE.MeshBasicMaterial({
      color: LIME_HOT,
      transparent: true,
      opacity: 0.35,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.004;
  g.add(ring);
  g.userData.ring = ring;
}

/**
 * Bold floating chevron — thick tip + soft body + ground halo.
 */
function makeFloorChevron(scale = 1) {
  const g = new THREE.Group();

  // outer soft chevron
  const outer = new THREE.Shape();
  outer.moveTo(0, 0.42);
  outer.quadraticCurveTo(0.16, 0.14, 0.48, -0.28);
  outer.lineTo(0.3, -0.38);
  outer.quadraticCurveTo(0.1, 0.02, 0, 0.18);
  outer.quadraticCurveTo(-0.1, 0.02, -0.3, -0.38);
  outer.lineTo(-0.48, -0.28);
  outer.quadraticCurveTo(-0.16, 0.14, 0, 0.42);
  outer.closePath();

  const outerGeo = new THREE.ExtrudeGeometry(outer, {
    depth: 0.008,
    bevelEnabled: true,
    bevelThickness: 0.005,
    bevelSize: 0.005,
    bevelSegments: 3,
  });
  outerGeo.rotateX(-Math.PI / 2);
  outerGeo.translate(0, 0.012, 0);
  const outerMesh = new THREE.Mesh(
    outerGeo,
    new THREE.MeshBasicMaterial({
      color: LIME_SOFT,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
    }),
  );
  outerMesh.scale.setScalar(scale);
  g.add(outerMesh);

  // crisp inner arrow
  const inner = new THREE.Shape();
  inner.moveTo(0, 0.36);
  inner.quadraticCurveTo(0.12, 0.12, 0.38, -0.22);
  inner.lineTo(0.22, -0.32);
  inner.quadraticCurveTo(0.08, 0.0, 0, 0.14);
  inner.quadraticCurveTo(-0.08, 0.0, -0.22, -0.32);
  inner.lineTo(-0.38, -0.22);
  inner.quadraticCurveTo(-0.12, 0.12, 0, 0.36);
  inner.closePath();

  const innerGeo = new THREE.ExtrudeGeometry(inner, {
    depth: 0.016,
    bevelEnabled: true,
    bevelThickness: 0.006,
    bevelSize: 0.006,
    bevelSegments: 3,
  });
  innerGeo.rotateX(-Math.PI / 2);
  innerGeo.translate(0, 0.02, 0);
  const mesh = new THREE.Mesh(innerGeo, limeMat(1.15, LIME_HOT));
  mesh.scale.setScalar(scale);
  g.add(mesh);

  addFloorGlow(g, scale);
  g.userData.kind = "straight";
  g.userData.body = mesh;
  return g;
}

/**
 * Turn cue = same bold chevron, just larger — no weird L overlays.
 */
function makeFloorTurn(kind = "left", scale = 1) {
  const g = makeFloorChevron(scale);
  g.userData.kind = kind === "left" ? "left" : "right";
  return g;
}

/**
 * pinhole intrinsics of the camera-access image from the view's GL projection matrix
 * (column-major). The image is the view's (screen-cropped) camera feed, flipped to top-down
 * rows before upload, so: fx = P0·w/2, fy = P5·h/2, cx = (1−P8)·w/2, cy = (1+P9)·h/2.
 */
function intrinsicsFromProjection(P, w, h) {
  if (!P || P.length < 16 || !(w > 0) || !(h > 0)) return null;
  const fx = (P[0] * w) / 2;
  const fy = (P[5] * h) / 2;
  const cx = ((1 - P[8]) * w) / 2;
  const cy = ((1 + P[9]) * h) / 2;
  if (![fx, fy, cx, cy].every(Number.isFinite) || fx <= 0 || fy <= 0) return null;
  return { fx, fy, cx, cy, width: w, height: h };
}

/** slot label that always faces the camera */
function makeLabelSprite(text) {
  const c = document.createElement("canvas");
  c.width = 512;
  c.height = 160;
  const g = c.getContext("2d");
  g.beginPath();
  g.roundRect(10, 10, 492, 140, 60);
  g.fillStyle = "rgba(18, 22, 10, 0.82)";
  g.fill();
  g.lineWidth = 6;
  g.strokeStyle = "#e8f28a";
  g.stroke();
  g.fillStyle = "#e8f28a";
  g.font = "bold 76px system-ui, sans-serif";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(text ? "P  " + text : "Your car", 256, 84);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: tex, transparent: true, depthTest: false, depthWrite: false,
  }));
  sprite.scale.set(0.9, 0.28, 1);
  sprite.renderOrder = 10;
  return sprite;
}

/** destination: pulsing floor ring + pole + pin head + slot label + chevron into the bay */
function makeDestMarker(label) {
  const g = new THREE.Group();
  g.visible = false;

  const disc = new THREE.Mesh(
    new THREE.CircleGeometry(0.55, 48),
    new THREE.MeshBasicMaterial({ color: LIME_SOFT, transparent: true, opacity: 0.3, depthWrite: false }),
  );
  disc.rotation.x = -Math.PI / 2;
  g.add(disc);

  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.55, 0.68, 56),
    new THREE.MeshBasicMaterial({
      color: LIME_HOT, transparent: true, opacity: 0.7, depthWrite: false, side: THREE.DoubleSide,
    }),
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.003;
  g.add(ring);

  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, DEST_PIN_H, 12), limeMat(0.6));
  pole.position.y = DEST_PIN_H / 2;
  g.add(pole);

  const pin = new THREE.Mesh(new THREE.SphereGeometry(0.14, 24, 16), limeMat(1.0, LIME_HOT));
  pin.position.y = DEST_PIN_H;
  g.add(pin);

  const text = makeLabelSprite(label);
  text.position.y = DEST_PIN_H + 0.42;
  g.add(text);

  const bayArrow = makeFloorChevron(1.15);
  bayArrow.visible = false;
  g.add(bayArrow);

  g.userData = { ring, pin, label: text, bayArrow };
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
 * trim polyline so it ends in the walking corridor beside the bay — no spur into the
 * bay itself, and no continuing to the next corridor node.
 */
function clampPathToDest(waypoints, destXY) {
  let wps = (waypoints || []).map((w) => [Number(w[0]), Number(w[1])]);
  if (!destXY || wps.length < 2) return wps;
  const dx = Number(destXY[0]);
  const dy = Number(destXY[1]);
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return wps;
  // route ends with an off-graph hop corridor → bay centre; search the corridor only
  const tail = wps[wps.length - 1];
  if (wps.length >= 3 && Math.hypot(tail[0] - dx, tail[1] - dy) < 0.3) {
    wps = wps.slice(0, -1);
  }

  let bestD = Infinity;
  let bestI = 0;
  let bestT = 0;
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i];
    const b = wps[i + 1];
    const sx = b[0] - a[0];
    const sy = b[1] - a[1];
    const len2 = sx * sx + sy * sy || 1e-12;
    let t = ((dx - a[0]) * sx + (dy - a[1]) * sy) / len2;
    t = Math.max(0, Math.min(1, t));
    const px = a[0] + sx * t;
    const py = a[1] + sy * t;
    const d = Math.hypot(dx - px, dy - py);
    if (d < bestD) {
      bestD = d;
      bestI = i;
      bestT = t;
    }
  }

  const a = wps[bestI];
  const b = wps[bestI + 1];
  const cut = [a[0] + (b[0] - a[0]) * bestT, a[1] + (b[1] - a[1]) * bestT];
  const out = [...wps.slice(0, bestI + 1)];
  const last = out[out.length - 1];
  if (!last || Math.hypot(last[0] - cut[0], last[1] - cut[1]) > 0.05) {
    out.push(cut);
  } else {
    out[out.length - 1] = cut;
  }

  return out.length >= 2 ? out : wps;
}

/**
 * map facility polyline into XR xz using PnP pose.
 * - anchor: user (x,y) → XR origin
 * - rotate: facility heading → phone forward at calibrate
 * PnP heading: 0° = +Y, 90° = +X (atan2(dx, dy))
 * destXY: bay coords — clamp path so it stops at this bay (not the next node)
 */
export function pathFromWaypoints(waypoints, origin, initialFwd, facilityPose = null, destXY = null) {
  let wps = (waypoints || []).map((w) => [Number(w[0]), Number(w[1])]);
  if (wps.length < 2) return [origin.clone()];

  const pose = facilityPose || {};
  const hasPose = Number.isFinite(pose.x) && Number.isFinite(pose.y);

  if (hasPose) {
    wps = remainingWaypoints(wps, pose.x, pose.y);
  }

  // stop at this bay — walkable graph often continues ~1 m past to the next node
  if (destXY) {
    wps = clampPathToDest(wps, destXY);
  }

  const fwd = initialFwd.clone();
  fwd.y = 0;
  if (fwd.lengthSq() < 1e-8) fwd.set(0, 0, -1);
  else fwd.normalize();
  const xrAngle = Math.atan2(fwd.x, -fwd.z);

  // which way the phone faces on the map. Use the PnP heading — assuming you face along the
  // route would erase a needed U-turn and draw the route straight ahead (wrong way).
  // Close to the first corridor's axis (either direction) → lock onto that axis exactly.
  const dx0 = wps[1][0] - wps[0][0];
  const dy0 = wps[1][1] - wps[0][1];
  const segLen = Math.hypot(dx0, dy0);
  const segAngle = segLen >= 1e-6 ? Math.atan2(dx0, dy0) : 0;
  let facAngle;
  if (Number.isFinite(pose.heading)) {
    const h = (Number(pose.heading) * Math.PI) / 180;
    const off = Math.abs(angDiff(h, segAngle));
    if (segLen >= 0.35 && off <= START_AXIS_SNAP) facAngle = segAngle; // facing along the route
    else if (segLen >= 0.35 && off >= Math.PI - START_AXIS_SNAP) facAngle = segAngle + Math.PI; // facing back down it
    else facAngle = h;
  } else {
    facAngle = segAngle; // no heading: best guess is you face along the route
  }

  // facility forward → XR forward: rot = fac - xr
  const rot = facAngle - xrAngle;
  const cos = Math.cos(rot);
  const sin = Math.sin(rot);

  const ox = hasPose ? pose.x : wps[0][0];
  const oy = hasPose ? pose.y : wps[0][1];

  // facility (fx, fy) → XR plane p = (x, -z):  p = R(rot)·f + t
  const xf = {
    rot,
    tx: origin.x - (ox * cos - oy * sin),
    ty: -origin.z - (ox * sin + oy * cos),
  };
  // the route runs down the corridor's centre line; corridors are up to ~8 m wide, so you
  // may be well off it. Start the arrows at your feet and merge into the route ahead,
  // instead of starting them on the centre line beside you.
  const corridorWps = wps;
  if (hasPose) wps = mergeFromPosition(wps, pose.x, pose.y);

  const out = wps.map(([fx, fy]) => xfApply(xf, fx, fy, origin.y));
  out.xf = xf;
  out.facWps = wps; // drawn polyline (incl. merge) — rebuilt on PnP re-alignment
  out.corridorWps = corridorWps; // pure corridor centre lines — for the walking-direction check
  return out;
}

const MERGE_MIN_OFF_M = 0.75; // closer than this to the centre line: just use the route
const MERGE_MIN_M = 3;
const MERGE_MAX_M = 8;

/** [you] → point on the route ~2× your sideways offset ahead (3–8 m, never past the first corner) */
function mergeFromPosition(wps, x, y) {
  if (wps.length < 2) return wps;
  const off = Math.hypot(x - wps[0][0], y - wps[0][1]);
  if (off < MERGE_MIN_OFF_M) return wps;
  const want = Math.max(MERGE_MIN_M, Math.min(MERGE_MAX_M, 2 * off));
  const [ax, ay] = wps[0];
  const [bx, by] = wps[1];
  const len = Math.hypot(bx - ax, by - ay);
  if (len <= want) return [[x, y], ...wps.slice(1)]; // merge at the first corner
  const k = want / len;
  return [[x, y], [ax + (bx - ax) * k, ay + (by - ay) * k], ...wps.slice(1)];
}

/** facility point → XR world (floor height y) */
function xfApply(xf, fx, fy, y = 0) {
  const c = Math.cos(xf.rot);
  const s = Math.sin(xf.rot);
  const px = fx * c - fy * s + xf.tx;
  const py = fx * s + fy * c + xf.ty;
  return new THREE.Vector3(px, y, -py);
}

/** signed smallest difference a - b (radians) */
function angDiff(a, b) {
  let d = a - b;
  while (d > Math.PI) d -= 2 * Math.PI;
  while (d < -Math.PI) d += 2 * Math.PI;
  return d;
}

/** axis difference (direction ignored): wrap into [-90°, 90°] */
function axisDiff(a, b) {
  let d = angDiff(a, b);
  if (d > Math.PI / 2) d -= Math.PI;
  if (d < -Math.PI / 2) d += Math.PI;
  return d;
}

/** XR world (x, z) → facility (fx, fy): inverse of xfApply */
function xfInverse(xf, x, z) {
  const c = Math.cos(xf.rot);
  const s = Math.sin(xf.rot);
  const px = x - xf.tx;
  const py = -z - xf.ty;
  return { fx: px * c + py * s, fy: -px * s + py * c };
}

/** transform with rotation `rot` that keeps facility point f on XR point (x, z) */
function xfPivot(rot, f, x, z) {
  const c = Math.cos(rot);
  const s = Math.sin(rot);
  return { rot, tx: x - (f.fx * c - f.fy * s), ty: -z - (f.fx * s + f.fy * c) };
}

/**
 * fit facility→XR rigid transform from PnP fixes {fx, fy, heading°, xr:{x, z, yaw}}.
 * heading pairs pin rotation early; position spread takes over once fixes are metres apart.
 * recent fixes weigh more (ARCore drift).
 */
function fitXf(fixes, prevRot) {
  const n = fixes.length;
  let wSum = 0;
  let fcx = 0;
  let fcy = 0;
  let pcx = 0;
  let pcy = 0;
  let hs = 0;
  let hc = 0;
  let hN = 0;
  fixes.forEach((f, i) => {
    const w = i + 1;
    wSum += w;
    fcx += w * f.fx;
    fcy += w * f.fy;
    pcx += w * f.xr.x;
    pcy += w * -f.xr.z;
    if (f.xr.yaw != null && Number.isFinite(f.heading)) {
      const th = (f.heading * Math.PI) / 180 - f.xr.yaw;
      hs += w * Math.sin(th);
      hc += w * Math.cos(th);
      hN += 1;
    }
  });
  fcx /= wSum; fcy /= wSum; pcx /= wSum; pcy /= wSum;

  let span = 0;
  let sCross = 0;
  let sDot = 0;
  fixes.forEach((f, i) => {
    const w = i + 1;
    const ax = f.fx - fcx;
    const ay = f.fy - fcy;
    const bx = f.xr.x - pcx;
    const by = -f.xr.z - pcy;
    sCross += w * (ax * by - ay * bx);
    sDot += w * (ax * bx + ay * by);
    for (let j = i + 1; j < n; j++) {
      span = Math.max(span, Math.hypot(f.fx - fixes[j].fx, f.fy - fixes[j].fy));
    }
  });

  const posRot = n >= 2 && span >= ALIGN_SPAN_MIN_M ? Math.atan2(sCross, sDot) : null;
  let rot = prevRot;
  if (hN > 0) {
    rot = Math.atan2(hs, hc);
    if (posRot != null) {
      const wPos = Math.min(0.8, (span - ALIGN_SPAN_MIN_M) / 12 + 0.2);
      rot += wPos * angDiff(posRot, rot);
    }
  } else if (posRot != null) {
    rot = posRot;
  }

  const c = Math.cos(rot);
  const s = Math.sin(rot);
  return { rot, tx: pcx - (fcx * c - fcy * s), ty: pcy - (fcx * s + fcy * c), span };
}

/** public helper: facility polyline → XR path with soft corners */
export function smoothPath(pts) {
  return smoothPolylineXZ(pts);
}

function polyLen(pts) {
  let n = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    n += Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].z - pts[i].z);
  }
  return n;
}

/**
 * Fillet sharp polyline corners with quadratic arcs so guidance bends smoothly.
 */
function smoothPolylineXZ(pts, radius = CORNER_RADIUS_M, segs = CORNER_SEGS) {
  if (!pts || pts.length < 3) {
    return (pts || []).map((p) => p.clone());
  }
  const y = pts[0].y;
  const out = [pts[0].clone()];

  for (let i = 1; i < pts.length - 1; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    const c = pts[i + 1];
    const vIn = new THREE.Vector3(a.x - b.x, 0, a.z - b.z);
    const vOut = new THREE.Vector3(c.x - b.x, 0, c.z - b.z);
    const lenIn = vIn.length();
    const lenOut = vOut.length();
    if (lenIn < 0.08 || lenOut < 0.08) {
      out.push(b.clone());
      continue;
    }
    vIn.multiplyScalar(1 / lenIn);
    vOut.multiplyScalar(1 / lenOut);
    const dot = Math.max(-1, Math.min(1, vIn.dot(vOut)));
    const ang = Math.acos(dot);
    // nearly straight — keep vertex
    if (ang > Math.PI - 0.18) {
      out.push(b.clone());
      continue;
    }
    const cut = Math.min(radius, lenIn * 0.42, lenOut * 0.42);
    if (cut < 0.12) {
      out.push(b.clone());
      continue;
    }
    const p0 = new THREE.Vector3(b.x + vIn.x * cut, y, b.z + vIn.z * cut);
    const p2 = new THREE.Vector3(b.x + vOut.x * cut, y, b.z + vOut.z * cut);
    // soften control: pull slightly off the sharp corner for a rounder bend
    const mid = new THREE.Vector3(
      b.x * 0.55 + (p0.x + p2.x) * 0.225,
      y,
      b.z * 0.55 + (p0.z + p2.z) * 0.225,
    );

    const last = out[out.length - 1];
    if (Math.hypot(last.x - p0.x, last.z - p0.z) > 0.04) {
      out.push(p0);
    }
    for (let s = 1; s <= segs; s++) {
      const t = s / segs;
      const u = 1 - t;
      out.push(new THREE.Vector3(
        u * u * p0.x + 2 * u * t * mid.x + t * t * p2.x,
        y,
        u * u * p0.z + 2 * u * t * mid.z + t * t * p2.z,
      ));
    }
  }

  out.push(pts[pts.length - 1].clone());
  return out;
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
    this.pathGlow = null;
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
    // ARCore may re-origin the space after a long tracking loss
    this.refSpace.addEventListener?.("reset", () => {
      this.spaceReset = true;
      // tracking may already be back — handle now rather than at the next dropout
      if (this.calibrated && this.lostAt == null) this.markAlignStale("reset");
      this.opts.handlers?.onReset?.();
    });
    this.renderer.setAnimationLoop((t, frame) => this.onFrame(t, frame));
    this.opts.handlers?.onStep?.(0);
    return true;
  }

  stop() {
    void this.stopAsync();
  }

  /** end XR and resolve once the session is fully gone (camera can be reclaimed) */
  stopAsync() {
    return new Promise((resolve) => {
      const s = this.session;
      if (!s) {
        this.cleanup();
        resolve();
        return;
      }
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        try { this.cleanup(); } catch (_) { /* ignore */ }
        resolve();
      };
      const onEnd = () => {
        try { s.removeEventListener("end", onEnd); } catch (_) { /* ignore */ }
        finish();
      };
      s.addEventListener("end", onEnd);
      this.session = null;
      try {
        this.renderer?.setAnimationLoop?.(null);
      } catch (_) { /* ignore */ }
      // drop XR camera binding before getUserMedia reclaims the device
      try {
        this.renderer?.xr?.setSession?.(null);
      } catch (_) { /* ignore */ }
      s.end().then(finish).catch(finish);
      setTimeout(finish, 2500);
    });
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
      this.pathLine.geometry?.dispose?.();
      this.pathLine.material?.dispose?.();
      this.pathLine = null;
    }
    if (this.pathGlow) {
      this.scene.remove(this.pathGlow);
      this.pathGlow.geometry?.dispose?.();
      this.pathGlow.material?.dispose?.();
      this.pathGlow = null;
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
    // ARCore pose of the exact frame in this jpeg — pairs with its PnP fix for alignment
    if (blob) blob.xrPose = pending.xrPose || null;
    if (blob) blob.xrIntrinsics = pending.intrinsics || null;
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
    let camView = null;
    for (const view of pose.views) {
      if (!view.camera) continue;
      try {
        tex = binding.getCameraImage(view.camera);
      } catch {
        tex = null;
      }
      if (tex) {
        cam = view.camera;
        camView = view;
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
    pending.xrPose = this.alignPoseOf(pose);
    this._capBusy = true;
    const scale = Math.min(1, maxDim / Math.max(w, h));
    const dw = Math.max(1, Math.round(w * scale));
    const dh = Math.max(1, Math.round(h * scale));
    pending.intrinsics = intrinsicsFromProjection(camView?.projectionMatrix, dw, dh);
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

  /** camera xz + yaw (same convention as PnP heading: atan2(x, -z)); yaw null when pointing at the floor */
  alignPoseOf(pose) {
    const p = pose.transform.position;
    const o = pose.transform.orientation;
    const q = new THREE.Quaternion(o.x, o.y, o.z, o.w);
    const view = new THREE.Vector3(0, 0, -1).applyQuaternion(q);
    const flat = Math.hypot(view.x, view.z);
    return {
      x: p.x,
      z: p.z,
      yaw: flat > 0.35 ? Math.atan2(view.x, -view.z) : null,
    };
  }

  /** tracked again after a dropout — was the AR frame kept, or did it move? */
  onTrackingRecovered(pose) {
    const lostMs = performance.now() - this.lostAt;
    this.lostAt = null;
    if (!this.calibrated || !this.xf) {
      this.spaceReset = false;
      return;
    }
    const p = pose.transform.position;
    const jump = this.lastGood ? Math.hypot(p.x - this.lastGood.x, p.z - this.lastGood.z) : 0;
    const plausible = RECOVER_JUMP_M + (lostMs / 1000) * RECOVER_WALK_MPS;
    if (this.spaceReset || jump > plausible) {
      this.markAlignStale(this.spaceReset ? "reset" : `jump_${jump.toFixed(1)}m`, { lostMs, jump });
    } else if (lostMs > RECOVER_VERIFY_MS) {
      // frame probably intact — keep arrows, but re-check with fresh PnP; pre-dropout fixes
      // may belong to a slightly different frame, so don't mix them in
      this.alignFixes = [];
      this.headVotes = [];
      this.alignUrgent = true;
      this.walkSinceFix = WALK_COOLDOWN_M;
      this.opts.handlers?.onAlignVerify?.({ lostMs, jump });
    }
    this.spaceReset = false;
  }

  /** AR frame moved under us: alignment is invalid until a fresh fix (arrows hidden) */
  markAlignStale(reason, info = {}) {
    if (this.alignStale) return;
    this.alignStale = true;
    this.staleSince = performance.now();
    this.alignFixes = [];
    this.headVotes = [];
    this.walkTrail = [];
    this.walkObs = [];
    this.walkCorrections = 0; // frame moved: heading is unsettled again
    this.xfEase = null;
    this.alignUrgent = true;
    this.opts.handlers?.onAlignStale?.({ reason, ...info });
  }

  /** last trustworthy pose: XR position + camera facing on the map (compass, rad) */
  rememberGood(pose) {
    const a = this.alignPoseOf(pose);
    const f = xfInverse(this.xf, a.x, a.z);
    this.lastGood = {
      x: a.x,
      z: a.z,
      f,
      facHeading: a.yaw != null ? this.xf.rot + a.yaw : this.lastGood?.facHeading ?? null,
    };
  }

  /**
   * no PnP after a frame jump: assume you're still roughly where tracking was lost, facing
   * the same way on the map. Approximate — walking + PnP corrections refine it from here.
   */
  reanchorFromLastGood(pose) {
    const g = this.lastGood;
    const yaw = this.alignPoseOf(pose).yaw;
    if (!g || g.facHeading == null || yaw == null) return; // wait for a usable view
    const p = pose.transform.position;
    this.xf = xfPivot(g.facHeading - yaw, g.f, p.x, p.z);
    this.rebuildPath();
    this.alignStale = false;
    this.walkSinceFix = WALK_COOLDOWN_M;
    this.opts.handlers?.onAlignRecovered?.({ source: "last_position" });
  }

  /**
   * where you face vs where the route goes next (deg, + = route is to your right).
   * null while the camera looks straight down (no usable yaw).
   */
  updateFacing(pose) {
    const yaw = this.alignPoseOf(pose).yaw;
    if (yaw == null || !this.path?.length) return;
    const ahead = pointAt(this.path, Math.min(this.pathLen, this.progressM + 1.5)).dir;
    if (ahead.lengthSq() < 1e-8) return;
    const routeYaw = Math.atan2(ahead.x, -ahead.z);
    this.facingRelDeg = (angDiff(routeYaw, yaw) * 180) / Math.PI;
  }

  /** current XR camera position on the floor plane */
  xrPosition() {
    return this.calibrated ? { x: this.tmp.x, z: this.tmp.z } : null;
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
    this.xf = null;
    this.facWps = null;
    this.alignFixes = [];
    this.alignRejects = 0;
    this.xfEase = null;
    this.lastFitSpan = 0;
    this.alignStale = false;
    this.alignUrgent = false;
    this.lostAt = null;
    this.spaceReset = false;
    this.lastGood = null;
    this.walkTrail = [];
    this.walkSinceFix = 0;
    this.walkObs = [];
    this.walkCorrections = 0;
    this.xfPivotXr = { x: origin.x, z: origin.z }; // path is anchored where you started
    this.headVotes = [];
    this.groundY = origin.y;
    if (wps?.length >= 2) {
      const raw = pathFromWaypoints(
        wps,
        origin,
        this.fwd,
        this.opts.facilityPose || null,
        this.opts.destXY || null,
      );
      this.xf = raw.xf || null;
      this.facWps = raw.facWps || null;
      this.corridorWps = raw.corridorWps || raw.facWps || null;
      this.path = raw;
    } else {
      this.path = pathFromLegs(this.legs, origin, this.fwd);
    }
    this.path = smoothPolylineXZ(this.path);
    this.pathLen = polyLen(this.path) || this.opts.distanceM || 1;
    this.placeDestMarker();
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
    // no continuous path line — guidance is arrows only
    if (this.pathLine) {
      this.scene.remove(this.pathLine);
      this.pathLine.geometry?.dispose?.();
      this.pathLine.material?.dispose?.();
      this.pathLine = null;
    }
    if (this.pathGlow) {
      this.scene.remove(this.pathGlow);
      this.pathGlow.geometry?.dispose?.();
      this.pathGlow.material?.dispose?.();
      this.pathGlow = null;
    }
  }

  onFrame(t, frame) {
    if (!frame || !this.refSpace || !this.session) return;
    this.pulseT = t * 0.001;

    const pose = frame.getViewerPose(this.refSpace);
    // only a missing pose is "lost". Chrome flags emulatedPosition briefly and often while
    // ARCore tracking is merely limited (fast motion, plain view) — the position is still
    // usable for drawing, just not trusted for corrections (see `degraded` below).
    if (!pose) {
      if (this.lostAt == null) this.lostAt = performance.now();
      // stale arrows would float off the floor — hide until ARCore relocalizes
      this.scene.visible = false;
      // ARCore may jump on recovery — don't read that as a walked stretch
      if (this.walkTrail?.length) this.walkTrail = [];
      this.opts.handlers?.onTracking?.(false);
      this.renderer.render(this.scene, this.camera);
      return;
    }
    if (this.lostAt != null) this.onTrackingRecovered(pose);
    this.scene.visible = !this.alignStale;
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

    this.stepAlignEase();
    const degraded = Boolean(pose.emulatedPosition);
    if (this.alignStale) {
      // keep tracking progress quietly; fall back to an approximate re-anchor if PnP can't help
      if (!degraded && performance.now() - this.staleSince > STALE_FALLBACK_MS) this.reanchorFromLastGood(pose);
    } else if (degraded) {
      // limited tracking: keep drawing, but don't learn heading/position from these frames
      if (this.walkTrail?.length) this.walkTrail = [];
    } else {
      this.rememberGood(pose);
      if (this.walkTrail) this.trackWalk(this.tmp);
    }
    const closest = closestOnPath(this.path, this.tmp.x, this.tmp.z);
    this.progressM = closest.progressM;
    if (this.progressM > this.maxProgressM) this.maxProgressM = this.progressM;

    const remain = Math.max(0, this.pathLen - this.progressM);
    this.updateFacing(pose);
    this.opts.handlers?.onWalk?.(this.progressM, remain);
    this.bumpSteps(this.maxProgressM);
    this.placeArrows(remain);
    this.animateDestMarker(remain);
    this.renderer.render(this.scene, this.camera);
  }

  /** re-project the facility path with the current transform (after PnP re-alignment) */
  rebuildPath() {
    if (!this.xf || !this.facWps?.length) return;
    const y = this.floorY ?? this.groundY ?? 0;
    this.path = smoothPolylineXZ(this.facWps.map(([fx, fy]) => xfApply(this.xf, fx, fy, y)));
    this.pathLen = polyLen(this.path) || this.pathLen;
    this.placeDestMarker();
  }

  /**
   * PnP fix taken on an XR camera frame: {fx, fy, heading°} in facility coords + xr:{x, z, yaw}
   * = ARCore pose of that same frame. Refits facility→XR and moves the path.
   */
  addAlignFix(fix) {
    if (!this.calibrated || !this.xf || !this.facWps) return { accepted: false, why: "not_ready" };
    const pred = xfApply(this.xf, fix.fx, fix.fy);
    const residual = Math.hypot(pred.x - fix.xr.x, pred.z - fix.xr.z);

    // a single bad PnP must not yank the path — unless it keeps disagreeing (then the old fixes were wrong)
    if (this.alignFixes.length >= 2 && residual > ALIGN_OUTLIER_M) {
      this.alignRejects += 1;
      if (this.alignRejects < 3) return { accepted: false, why: "outlier", residual };
      this.alignFixes = [];
    }
    this.alignRejects = 0;
    this.alignFixes.push(fix);
    if (this.alignFixes.length > ALIGN_KEEP) this.alignFixes.shift();

    const target = fitXf(this.alignFixes, this.xf.rot);
    this.lastFitSpan = target.span || 0;
    this.headVotes = []; // a strong fix supersedes pending weak votes
    const end = this.facWps[this.facWps.length - 1];
    const a = xfApply(this.xf, end[0], end[1]);
    const b = xfApply(target, end[0], end[1]);
    const destShift = Math.hypot(a.x - b.x, a.z - b.z);
    const rotDeg = (angDiff(target.rot, this.xf.rot) * 180) / Math.PI;

    const first = this.alignFixes.length === 1;
    const recovered = this.alignStale;
    if (recovered) {
      this.alignStale = false;
      this.alignUrgent = false;
      this.opts.handlers?.onAlignRecovered?.({ source: "pnp", destShift });
    }
    if (first || destShift > ALIGN_SNAP_M) {
      this.xfEase = null;
      this.xf = { rot: target.rot, tx: target.tx, ty: target.ty };
      this.rebuildPath();
    } else if (destShift > 0.05 || Math.abs(rotDeg) > 0.3) {
      this.xfEase = { from: { ...this.xf }, to: target, t0: performance.now() };
    }
    return {
      accepted: true,
      residual,
      destShift,
      rotDeg,
      snapped: first || destShift > ALIGN_SNAP_M,
      fixes: this.alignFixes.length,
      span: target.span,
    };
  }

  stepAlignEase() {
    const e = this.xfEase;
    if (!e) return;
    const k = Math.min(1, (performance.now() - e.t0) / ALIGN_EASE_MS);
    const s = k * k * (3 - 2 * k);
    this.xf = {
      rot: e.from.rot + angDiff(e.to.rot, e.from.rot) * s,
      tx: e.from.tx + (e.to.tx - e.from.tx) * s,
      ty: e.from.ty + (e.to.ty - e.from.ty) * s,
    };
    this.rebuildPath();
    if (k >= 1) this.xfEase = null;
  }

  /**
   * change only the map→AR rotation, pivoting on where you stand — the arrows at your
   * feet stay put and the far end of the route swings into place.
   */
  rotateAbout(newRot, { ease = true, pivotFac = null } = {}) {
    if (!this.xf) return 0;
    const delta = angDiff(newRot, this.xf.rot);
    const pivot = this.tmp;
    // pivotFac: where you should be on the map (else: where the current transform puts you)
    const f = pivotFac || xfInverse(this.xf, pivot.x, pivot.z);
    const target = xfPivot(newRot, f, pivot.x, pivot.z);
    this.xfPivotXr = { x: pivot.x, z: pivot.z };
    if (ease && Math.abs(delta) < (WALK_SNAP_DEG * Math.PI) / 180) {
      this.xfEase = { from: { ...this.xf }, to: target, t0: performance.now() };
    } else {
      this.xfEase = null;
      this.xf = target;
      this.rebuildPath();
    }
    return (delta * 180) / Math.PI;
  }

  /** ARCore trail for the walking-direction check (XR xz, spaced WALK_SAMPLE_M) */
  trackWalk(pos) {
    const tr = this.walkTrail;
    const last = tr[tr.length - 1];
    if (last && Math.hypot(pos.x - last.x, pos.z - last.z) < WALK_SAMPLE_M) return;
    const step = last ? Math.hypot(pos.x - last.x, pos.z - last.z) : 0;
    tr.push({ x: pos.x, z: pos.z });
    this.walkSinceFix += step;
    // keep a little more than one window
    let len = 0;
    for (let i = tr.length - 1; i > 0; i--) {
      len += Math.hypot(tr[i].x - tr[i - 1].x, tr[i].z - tr[i - 1].z);
      if (len > WALK_WINDOW_M * 1.6) {
        tr.splice(0, i - 1);
        break;
      }
    }
    this.checkWalkHeading();
  }

  /**
   * last WALK_WINDOW_M of walking is straight → its axis should match the route corridor
   * you're on. Mismatch = heading error → rotate. Direction is ignored (U-turn safe).
   */
  checkWalkHeading() {
    if (!this.xf || !this.facWps?.length || this.xfEase || this.arrived) return;
    if (this.walkSinceFix < WALK_COOLDOWN_M) return;
    const tr = this.walkTrail;
    if (tr.length < 3) return;

    // newest stretch of ≥ WALK_WINDOW_M
    let len = 0;
    let i0 = tr.length - 1;
    while (i0 > 0 && len < WALK_WINDOW_M) {
      len += Math.hypot(tr[i0].x - tr[i0 - 1].x, tr[i0].z - tr[i0 - 1].z);
      i0 -= 1;
    }
    if (len < WALK_WINDOW_M) return;
    const a = tr[i0];
    const b = tr[tr.length - 1];
    const cx = b.x - a.x;
    const cz = b.z - a.z;
    const chord = Math.hypot(cx, cz);
    // turning (incl. a U-turn in progress) → not a straight walk, judge nothing
    if (chord / len < WALK_STRAIGHT_RATIO) return;
    for (let i = i0 + 1; i < tr.length - 1; i++) {
      const dev = Math.abs((tr[i].x - a.x) * cz - (tr[i].z - a.z) * cx) / chord;
      if (dev > WALK_MAX_DEV_M) return;
    }

    // walking axis in facility frame under the current transform (math angles)
    const aXr = Math.atan2(-cz, cx); // XR plane p = (x, -z)
    const aFac = aXr - this.xf.rot;
    const mx = (a.x + b.x) / 2;
    const mz = (a.z + b.z) / 2;
    const mid = xfInverse(this.xf, mx, mz);
    // a heading error displaces where we think you are by ~distance-from-pivot × sin(err):
    // widen the "near a corridor" gate by that, or big errors could never be corrected
    const fromPivot = this.xfPivotXr ? Math.hypot(mx - this.xfPivotXr.x, mz - this.xfPivotXr.z) : 0;

    // nearest route corridor whose axis is plausibly this one
    let best = null;
    // corridor centre lines only — the merge leg from your start position is diagonal
    const wps = this.corridorWps || this.facWps;
    for (let i = 0; i < wps.length - 1; i++) {
      const [x1, y1] = wps[i];
      const [x2, y2] = wps[i + 1];
      const sx = x2 - x1;
      const sy = y2 - y1;
      const segLen = Math.hypot(sx, sy);
      if (segLen < WALK_SEG_MIN_M) continue;
      const t = Math.max(0, Math.min(1, ((mid.fx - x1) * sx + (mid.fy - y1) * sy) / (segLen * segLen)));
      const dist = Math.hypot(mid.fx - (x1 + t * sx), mid.fy - (y1 + t * sy));
      const err = axisDiff(aFac, Math.atan2(sy, sx));
      if (Math.abs(err) > (WALK_MAX_ERR_DEG * Math.PI) / 180) continue;
      if (dist > Math.min(WALK_SEG_NEAR_MAX_M, WALK_SEG_NEAR_M + fromPivot * Math.abs(Math.sin(err)))) continue;
      if (!best || dist < best.dist) best = { i, dist, err, x1, y1, sx, sy, segLen };
    }
    if (!best) return;

    const errDeg = (best.err * 180) / Math.PI;
    this.walkSinceFix = 0;
    // once settled, a big one-off "error" is someone weaving across the aisle — ignore it
    const settled = (this.walkCorrections || 0) >= WALK_SETTLED_AFTER;
    if (settled && Math.abs(errDeg) > WALK_SETTLED_MAX_ERR_DEG) {
      this.walkObs = [];
      return;
    }
    // act only when two consecutive straight stretches agree (one stretch can be diagonal)
    const obs = (this.walkObs = [...(this.walkObs || []), best.err].slice(-2));
    if (obs.length < 2 || Math.abs(angDiff(obs[0], obs[1])) > (WALK_AGREE_DEG * Math.PI) / 180) return;
    const agreed = (obs[0] + obs[1]) / 2;
    this.walkObs = [];
    if (Math.abs(agreed) < (WALK_MIN_ERR_DEG * Math.PI) / 180) {
      this.walkCorrections = (this.walkCorrections || 0) + 1; // confirmed already aligned
      return;
    }
    const gain = (this.lastFitSpan || 0) >= 6 ? WALK_GAIN_PNP_SPAN : WALK_GAIN;
    // the heading error also pushed where we think you are off to the side; you've just
    // walked straight down this corridor, so pull that back to within WALK_LANE_M of its line
    const here = xfInverse(this.xf, this.tmp.x, this.tmp.z);
    const t = Math.max(0, Math.min(1,
      ((here.fx - best.x1) * best.sx + (here.fy - best.y1) * best.sy) / (best.segLen * best.segLen)));
    const ox = best.x1 + t * best.sx - here.fx;
    const oy = best.y1 + t * best.sy - here.fy;
    const off = Math.hypot(ox, oy);
    const pull = off > WALK_LANE_M ? (off - WALK_LANE_M) / off : 0;
    const pivotFac = { fx: here.fx + ox * pull, fy: here.fy + oy * pull };
    // aFac = aXr - rot → raising rot by err lines the walk up with the corridor
    const rotDeg = this.rotateAbout(this.xf.rot + gain * agreed, { pivotFac });
    this.walkCorrections = (this.walkCorrections || 0) + 1;
    this.opts.handlers?.onHeadingFix?.({
      source: "walk",
      errDeg: (agreed * 180) / Math.PI,
      rotDeg,
      seg: best.i,
      distM: best.dist,
      straightM: len,
    });
  }

  /**
   * weaker PnP fix (rotation only): each gives rot = heading − camera yaw. Once
   * HEAD_CONSENSUS_N recent ones agree within HEAD_CONSENSUS_SPREAD_DEG, rotate to their median.
   */
  addHeadingVote(fix) {
    if (!this.xf || fix?.xr?.yaw == null || !Number.isFinite(fix.heading)) {
      return { used: false, why: "no_yaw" };
    }
    // well-spread strong fixes already pin rotation better than weak votes
    if (this.alignFixes.length >= 2 && (this.lastFitSpan || 0) >= ALIGN_SPAN_MIN_M) {
      return { used: false, why: "strong_fit" };
    }
    const now = performance.now();
    const th = (fix.heading * Math.PI) / 180 - fix.xr.yaw;
    this.headVotes = this.headVotes.filter((v) => now - v.t < HEAD_CONSENSUS_MAX_AGE_MS);
    this.headVotes.push({ th, t: now });
    if (this.headVotes.length > HEAD_CONSENSUS_N) this.headVotes.shift();
    const n = this.headVotes.length;
    if (n < HEAD_CONSENSUS_N) return { used: false, why: `votes_${n}/${HEAD_CONSENSUS_N}` };

    const mean = Math.atan2(
      this.headVotes.reduce((s, v) => s + Math.sin(v.th), 0),
      this.headVotes.reduce((s, v) => s + Math.cos(v.th), 0),
    );
    const devs = this.headVotes.map((v) => angDiff(v.th, mean));
    const spreadDeg = (Math.max(...devs.map(Math.abs)) * 180) / Math.PI;
    if (spreadDeg > HEAD_CONSENSUS_SPREAD_DEG) {
      return { used: false, why: `spread_${spreadDeg.toFixed(0)}deg` };
    }
    const med = mean + [...devs].sort((p, q) => p - q)[Math.floor(n / 2)];
    this.headVotes = [];
    const rotDeg = this.rotateAbout(med);
    this.walkSinceFix = 0; // let the walk check re-measure from the new heading
    this.opts.handlers?.onHeadingFix?.({ source: "pnp_consensus", rotDeg, spreadDeg, votes: n });
    return { used: true, rotDeg, spreadDeg };
  }

  /** pin at the corridor stop + chevron turning toward the bay */
  placeDestMarker() {
    const dest = this.opts.destXY;
    const bayXr = dest && this.xf ? xfApply(this.xf, Number(dest[0]), Number(dest[1])) : null;
    if (!this.destMarker) {
      this.destMarker = makeDestMarker(this.opts.destLabel || "");
      this.scene.add(this.destMarker);
    }
    const m = this.destMarker;
    if (!this.opts.destXY || !this.path?.length) {
      m.visible = false;
      return;
    }
    const end = this.path[this.path.length - 1];
    m.position.set(end.x, (this.floorY ?? end.y) + 0.03, end.z);
    const { bayArrow } = m.userData;
    bayArrow.visible = false;
    if (bayXr) {
      const toBay = new THREE.Vector3(bayXr.x - end.x, 0, bayXr.z - end.z);
      if (toBay.length() > 0.6) {
        toBay.normalize();
        // just off the pin on the bay side, pointing into the bay
        bayArrow.position.set(toBay.x * 0.95, 0, toBay.z * 0.95);
        // lookAt wants a world target — child of the marker group, so resolve world pos first
        m.updateMatrixWorld(true);
        const w = bayArrow.getWorldPosition(new THREE.Vector3());
        // same flip as aimOnFloor: chevron tip faces -lookAt
        bayArrow.up.set(0, 1, 0);
        bayArrow.lookAt(w.x - toBay.x, w.y, w.z - toBay.z);
        bayArrow.visible = true;
      }
    }
  }

  animateDestMarker(remain) {
    const m = this.destMarker;
    if (!m || !this.opts.destXY) return;
    // only near the stop — a pin far away would float through walls/cars
    m.visible = remain < DEST_MARKER_SHOW_M;
    if (!m.visible) return;
    const { ring, pin, label } = m.userData;
    const p = (Math.sin(this.pulseT * 3.0) + 1) / 2;
    const bob = Math.sin(this.pulseT * 2.4) * 0.05;
    ring.scale.setScalar(1 + p * 0.35);
    ring.material.opacity = 0.75 - p * 0.45;
    pin.position.y = DEST_PIN_H + bob;
    label.position.y = DEST_PIN_H + 0.42 + bob;
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
    const ring = mesh.userData.ring;
    if (ring?.material) {
      ring.material.opacity = 0.22 + Math.max(0, 0.2 * (scale - 0.7));
    }
  }

  /**
   * Chevrons only — follow path tangent through bends (no turn glyph overlays).
   */
  placeArrows(remain) {
    const y = (this.floorY ?? this.path[0]?.y ?? 0) + 0.028;
    const samples = sampleAhead(this.path, this.progressM, ARROW_N, ARROW_GAP, FIRST_GAP);
    const pulse = 1 + Math.sin(this.pulseT * 3.6) * 0.06;

    for (const m of Object.values(this.cornerMark)) m.visible = false;

    for (let i = 0; i < ARROW_N; i++) {
      const slot = this.arrowSlots[i];
      const s = samples[i];
      const along = FIRST_GAP + i * ARROW_GAP;
      if (!s || remain < along * 0.3) {
        this.hideSlot(slot);
        continue;
      }
      const end = pointAt(this.path, this.pathLen);
      const distToEnd = Math.hypot(s.pos.x - end.pos.x, s.pos.z - end.pos.z);
      if (distToEnd < 0.25 && i > 0) {
        this.hideSlot(slot);
        continue;
      }

      // wave: nearer arrows bob + lead the pack
      const wave = 1 + Math.sin(this.pulseT * 4.2 - i * 0.55) * 0.08;
      const fade = Math.max(0.5, 1 - i * 0.07);
      const scale = (i === 0 ? 1.28 : 1.05 - i * 0.03) * pulse * wave * fade;
      const lift = y + Math.max(0, Math.sin(this.pulseT * 4.2 - i * 0.55) * 0.012);
      this.showSlot(
        slot,
        "straight",
        new THREE.Vector3(s.pos.x, lift, s.pos.z),
        s.dir,
        scale,
      );
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
  smoothPath,
};
window.dispatchEvent(new Event("parking-webxr-ready"));
