/**
 * Route drift detection: compares where the user is (and which way they are walking) with
 * the planned path, continuously, and decides when to re-route.
 *
 * Pure module, facility frame (metres, heading 0° = +y, 90° = +x) — no three.js, so the AR
 * view, turn-by-turn mode and Node tests can all use it.
 *
 *   const d = new DriftDetector(waypoints);          // the path being shown
 *   const { status, event } = d.update({ x, y, t }); // every frame / position update
 *   event?.type === "reroute"  → fetch a new route from (event.x, event.y), then
 *   d.setRoute(newWaypoints, t)   or   d.rerouteFailed(t)
 *
 * Deviation kinds (status.kind / event.kind):
 *   overshoot          kept going past the turn            distanceM = how far past it
 *   early_turn         left the corridor before the turn   distanceM = how far before it
 *   parallel           walking alongside, another aisle    distanceM = sideways offset
 *   passed_destination went past the end of the path       distanceM = how far past it
 *   wrong_way          on the path but walking back down it distanceM = how far back
 *   off_route          off the path, none of the above     distanceM = distance to path
 *
 * States (status.state): on → suspect → rerouting → (new route) on.
 * suspect is shown to the user straight away and grows as they keep going; a re-route
 * needs the deviation to be confirmed (hysteresis, walked distance, moving away, settled
 * tracking) so tracking noise and position jumps never re-route.
 */

export const DRIFT_DEFAULTS = {
  // corridors are 6–7 m wide and the path is their centre line: off route = outside the
  // corridor's half-width plus a margin for tracking error
  offM: 4.0,
  clearM: 2.5, // back within this → on route again (hysteresis band clearM..offM)
  confirmWalkM: 1.5, // walk this far while off route (and moving away) before re-routing
  confirmMs: 1000,
  hardOffM: 7.0, // this far off → re-route after only hardWalkM
  hardWalkM: 0.6,
  returnEpsM: 0.4, // distance to the path shrank this much over the last ~1.5 m → coming back
  jumpMps: 3.0, // faster than a run between samples → tracking jump, not walking
  jumpMinM: 0.8,
  settleMs: 2000, // after a jump / new route / hold: watch, don't act
  cooldownMs: 5000, // min time between re-routes
  sampleM: 0.2, // trail spacing
  headingWindowM: 2.0, // walked distance used for the walking direction
  turnMinDeg: 30, // smaller bends aren't turns
  earlyMinM: 1.0, // left the path at least this far before the corner → early turn
  parallelDeg: 30, // walking within this of the corridor axis while off it → parallel aisle
  wrongWayM: 6.0, // walked back down the path this far → "turn around"
  wrongWayRerouteM: 15.0, // …and still going → re-route (the other way may be shorter now)
  windowBackM: 6, // progress search window around the current position on the path
  windowAheadM: 12,
  repeatWindowMs: 90000, // this many re-routes in this window → suggest re-localizing
  repeatN: 3,
};

const DEG = 180 / Math.PI;

function axisDiffDeg(a, b) {
  let d = ((a - b) % 180 + 180) % 180;
  if (d > 90) d = 180 - d;
  return d;
}

/** route polyline with cumulative lengths, turns, and windowed projection */
class Route {
  constructor(wps, turnMinDeg) {
    const pts = [];
    for (const w of wps || []) {
      const p = [Number(w[0]), Number(w[1])];
      if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) continue;
      const last = pts[pts.length - 1];
      if (last && Math.hypot(p[0] - last[0], p[1] - last[1]) < 0.05) continue;
      pts.push(p);
    }
    this.pts = pts;
    this.cum = [0];
    this.dir = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const dx = pts[i + 1][0] - pts[i][0];
      const dy = pts[i + 1][1] - pts[i][1];
      const len = Math.hypot(dx, dy);
      this.cum.push(this.cum[i] + len);
      this.dir.push([dx / len, dy / len]);
    }
    this.total = this.cum[this.cum.length - 1] || 0;
    // corners: vertices where the path bends by turnMinDeg or more (+ = left)
    this.turns = [];
    for (let i = 1; i < pts.length - 1; i++) {
      const u = this.dir[i - 1];
      const v = this.dir[i];
      const deg = Math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1]) * DEG;
      if (Math.abs(deg) >= turnMinDeg) {
        this.turns.push({ i, s: this.cum[i], x: pts[i][0], y: pts[i][1], inDir: u, outDir: v, deg });
      }
    }
    if (pts.length >= 2) {
      const n = pts.length - 1;
      this.end = { i: n, s: this.total, x: pts[n][0], y: pts[n][1], inDir: this.dir[n - 1], outDir: null, deg: 0, isEnd: true };
    } else {
      this.end = null;
    }
  }

  get ok() {
    return this.pts.length >= 2 && this.total > 0.1;
  }

  /** nearest point on the part of the path between arc lengths s0..s1 */
  project(x, y, s0 = 0, s1 = Infinity) {
    let best = null;
    for (let i = 0; i < this.dir.length; i++) {
      const a0 = this.cum[i];
      const a1 = this.cum[i + 1];
      if (a1 < s0 || a0 > s1) continue;
      const [ax, ay] = this.pts[i];
      const [ux, uy] = this.dir[i];
      const len = a1 - a0;
      let t = (x - ax) * ux + (y - ay) * uy;
      t = Math.max(Math.max(0, s0 - a0), Math.min(Math.min(len, s1 - a0), t));
      const px = ax + ux * t;
      const py = ay + uy * t;
      const d = Math.hypot(x - px, y - py);
      if (!best || d < best.d) best = { d, s: a0 + t, seg: i, x: px, y: py };
    }
    return best;
  }

  segAt(s) {
    for (let i = 0; i < this.dir.length; i++) if (s <= this.cum[i + 1]) return i;
    return Math.max(0, this.dir.length - 1);
  }

  pointAt(s) {
    const i = this.segAt(s);
    const t = Math.max(0, Math.min(this.cum[i + 1] - this.cum[i], s - this.cum[i]));
    return { x: this.pts[i][0] + this.dir[i][0] * t, y: this.pts[i][1] + this.dir[i][1] * t, seg: i };
  }

  /** first corner after arc length s (or the end of the path) */
  nextTurn(s) {
    return this.turns.find((tn) => tn.s > s) || this.end;
  }

  /** last corner at or before s */
  prevTurn(s) {
    let out = null;
    for (const tn of this.turns) if (tn.s <= s) out = tn;
    return out;
  }
}

export class DriftDetector {
  constructor(waypoints = [], opts = {}) {
    this.cfg = { ...DRIFT_DEFAULTS, ...opts };
    this.reroutes = [];
    this.lastRerouteAt = -Infinity;
    this.setRoute(waypoints, null);
  }

  /** new path to follow (route start or after a re-route); t = now (ms) */
  setRoute(waypoints, t = null) {
    this.route = new Route(waypoints, this.cfg.turnMinDeg);
    this.s = 0;
    this.maxS = 0;
    this.off = null;
    this.state = "on";
    this.kind = null;
    this.distanceM = 0;
    this.offM = 0;
    this.lastEvent = null;
    if (t != null) this.settleUntil = t + this.cfg.settleMs;
    else this.settleUntil = -Infinity;
    // keep the trail: the walking direction is still valid across a re-route
    this.trail = this.trail || [];
    this.lastSample = this.lastSample || null;
    this.status = this.makeStatus(null, null);
  }

  /** re-route request failed: stay off route, retry after the cooldown */
  rerouteFailed(t) {
    if (this.state === "rerouting") this.state = "suspect";
    this.lastRerouteAt = t;
  }

  /** don't act for ms (tracking degraded, alignment being corrected) */
  hold(t, ms = this.cfg.settleMs) {
    this.settleUntil = Math.max(this.settleUntil, t + ms);
    this.lastSample = null; // the next sample is no step from the held one
  }

  /** position jumped (tracking relocalised / alignment snapped): forget the in-progress judgement */
  jump(t) {
    this.off = null;
    if (this.state !== "rerouting") this.state = "on";
    this.kind = null;
    this.trail = [];
    this.settleUntil = Math.max(this.settleUntil, t + this.cfg.settleMs);
  }

  /** walking direction (unit vector) from the last headingWindowM of the trail, or null */
  heading() {
    const tr = this.trail;
    if (tr.length < 2) return null;
    const cur = tr[tr.length - 1];
    let walked = 0;
    for (let i = tr.length - 2; i >= 0; i--) {
      walked += Math.hypot(tr[i + 1].x - tr[i].x, tr[i + 1].y - tr[i].y);
      if (walked >= this.cfg.headingWindowM) {
        const dx = cur.x - tr[i].x;
        const dy = cur.y - tr[i].y;
        const len = Math.hypot(dx, dy);
        // turning on the spot / zigzag: no clear direction
        if (len < 0.6 * walked) return null;
        return [dx / len, dy / len];
      }
    }
    return null;
  }

  /** distance to the path ~1.5 m walked ago (for "moving away vs coming back") */
  distAgo(back = 1.5) {
    const tr = this.trail;
    let walked = 0;
    for (let i = tr.length - 2; i >= 0; i--) {
      walked += Math.hypot(tr[i + 1].x - tr[i].x, tr[i + 1].y - tr[i].y);
      if (walked >= back) return tr[i].d;
    }
    return null;
  }

  /**
   * one position sample {x, y, t(ms)}. Returns { status, event } — event is set once when a
   * re-route is due ({type:"reroute", kind, distanceM, offM, x, y, heading, …}).
   */
  update({ x, y, t }) {
    const c = this.cfg;
    const r = this.route;
    if (!r.ok || !Number.isFinite(x) || !Number.isFinite(y)) return { status: this.status, event: null };

    // --- position jump: not walking, don't learn from it
    const last = this.lastSample;
    if (last) {
      const jumpM = Math.hypot(x - last.x, y - last.y);
      const dt = Math.max(1, t - last.t);
      if (jumpM >= c.jumpMinM && (jumpM / dt) * 1000 > c.jumpMps) {
        this.jump(t);
        this.lastEvent = { type: "jump", m: jumpM };
      }
    }
    this.lastSample = { x, y, t };

    // --- where on the path: windowed (so a parallel stretch of the same route that doubles
    // back is never mistaken for "here"), plus the whole path (shortcuts, being lost)
    const win = r.project(x, y, this.s - c.windowBackM, this.s + c.windowAheadM);
    const glob = r.project(x, y);
    let near = win;
    if (glob.d < c.clearM && win.d > c.offM && glob.s > this.s) {
      // cut across onto a later part of the path (through a row of bays): fine, skip ahead
      near = glob;
      this.s = glob.s;
      this.off = null;
      if (this.state === "suspect") this.state = "on";
    } else if (win.d <= c.offM && !this.off) {
      this.s = win.s;
    }
    const dist = Math.min(win.d, glob.d);
    if (glob.d < win.d) near = glob;
    this.maxS = Math.max(this.maxS, this.s);

    // trail (with distance to the path, for "moving away"). Walked distance counts trail
    // steps only — per-frame tracking jitter while standing still never adds up to walking
    let step = 0;
    const tl = this.trail[this.trail.length - 1];
    const gap = tl ? Math.hypot(x - tl.x, y - tl.y) : 0;
    if (!tl || gap >= c.sampleM) {
      step = gap;
      this.trail.push({ x, y, t, d: dist });
      if (this.trail.length > 120) this.trail.shift();
    }
    const h = this.heading();

    let event = null;
    const settled = t >= this.settleUntil;

    // --- off / on with hysteresis
    if (dist > c.offM) {
      if (!this.off) {
        const dep = r.pointAt(this.s);
        this.off = { since: t, depS: this.s, depX: dep.x, depY: dep.y, walked: 0 };
      } else {
        this.off.walked += step;
      }
    } else if (dist < c.clearM && this.off) {
      this.lastEvent = { type: "returned", kind: this.kind, distanceM: this.distanceM };
      this.off = null;
    }

    let kind = null;
    let distanceM = 0;
    let turn = null;
    if (this.off) {
      ({ kind, distanceM, turn } = this.classify(x, y, h, dist));
    } else {
      // on the path — but walking back down it?
      const back = this.maxS - this.s;
      const rd = r.dir[r.segAt(this.s)];
      const against = h && rd && h[0] * rd[0] + h[1] * rd[1] < -0.6;
      if (against && back >= c.wrongWayM) {
        kind = "wrong_way";
        distanceM = back;
      } else if (this.kind === "wrong_way" && against && back >= c.wrongWayM - 1) {
        kind = "wrong_way"; // hysteresis
        distanceM = back;
      }
    }

    if (this.state !== "rerouting") this.state = kind ? "suspect" : "on";
    this.kind = kind;
    this.distanceM = distanceM;
    this.offM = this.off ? dist : 0;

    // --- confirm → re-route
    if (this.state === "suspect" && settled) {
      const cool = t - this.lastRerouteAt >= c.cooldownMs;
      let confirm = false;
      if (kind === "wrong_way") {
        confirm = distanceM >= c.wrongWayRerouteM;
      } else if (this.off) {
        const ago = this.distAgo();
        const returning = ago != null && ago - dist > c.returnEpsM;
        const walkedEnough = this.off.walked >= c.confirmWalkM && t - this.off.since >= c.confirmMs;
        const hard = dist >= c.hardOffM && this.off.walked >= c.hardWalkM;
        confirm = !returning && (walkedEnough || hard);
      }
      if (confirm && cool) {
        this.state = "rerouting";
        this.lastRerouteAt = t;
        this.reroutes = this.reroutes.filter((rt) => t - rt < c.repeatWindowMs);
        this.reroutes.push(t);
        event = {
          type: "reroute",
          kind,
          distanceM,
          offM: this.offM,
          x,
          y,
          heading: h ? ((Math.atan2(h[0], h[1]) * DEG) + 360) % 360 : null,
          departure: this.off ? { s: this.off.depS, x: this.off.depX, y: this.off.depY } : null,
          turn: turn ? { x: turn.x, y: turn.y, s: turn.s, deg: turn.deg, isEnd: Boolean(turn.isEnd) } : null,
          walkedOffM: this.off ? this.off.walked : 0,
          // several re-routes in a short time: the position (alignment) is probably wrong
          suggestRelocalize: this.reroutes.length >= c.repeatN,
        };
        this.lastEvent = event;
      }
    }

    this.status = this.makeStatus(near, h);
    return { status: this.status, event };
  }

  /** what kind of deviation, and how big (only called while off the path) */
  classify(x, y, h, dist) {
    const c = this.cfg;
    const r = this.route;
    const dep = this.off.depS;
    const hDeg = h ? Math.atan2(h[0], h[1]) * DEG : null;

    // 1) past a corner (or the end) that was at/just before where you left the path:
    // you're ahead of it along the corridor you were in → overshoot
    const cands = [];
    // the corner you were at (nearest path point can sit just before it, or a bit round it)
    const prev = r.prevTurn(dep + 1.0);
    if (prev) cands.push(prev);
    const nxt = r.nextTurn(dep);
    if (nxt && nxt !== prev) cands.push(nxt);
    for (const tn of cands) {
      const [ux, uy] = tn.inDir;
      const rx = x - tn.x;
      const ry = y - tn.y;
      const a = rx * ux + ry * uy; // along the incoming corridor, past the corner
      const lat = Math.abs(rx * uy - ry * ux);
      if (a > 0.5 && a >= lat) {
        return { kind: tn.isEnd ? "passed_destination" : "overshoot", distanceM: a, turn: tn };
      }
    }

    // 2) walked away backwards from where the path starts (ignored a "turn around" start)
    {
      const [ux0, uy0] = r.dir[0];
      const [sx, sy] = r.pts[0];
      const behind = -((x - sx) * ux0 + (y - sy) * uy0);
      if (dep < 1.0 && behind > 0.5 && behind >= Math.abs((x - sx) * uy0 - (y - sy) * ux0)) {
        return { kind: "wrong_way", distanceM: Math.hypot(x - sx, y - sy), turn: nxt };
      }
    }

    // 3) left the corridor before the corner
    const seg = r.segAt(dep);
    const [ux, uy] = r.dir[seg];
    const [ax, ay] = r.pts[seg];
    const lat = Math.abs((x - ax) * uy - (y - ay) * ux);
    const segDeg = Math.atan2(ux, uy) * DEG;
    if (hDeg != null && lat > c.offM && axisDiffDeg(hDeg, segDeg) <= c.parallelDeg) {
      // in the next aisle, walking the same way as the path
      return { kind: "parallel", distanceM: lat, turn: nxt };
    }
    if (nxt && !nxt.isEnd && nxt.s - dep >= c.earlyMinM) {
      return { kind: "early_turn", distanceM: nxt.s - dep, turn: nxt, side: lat };
    }
    if (nxt?.isEnd && nxt.s - dep >= c.earlyMinM) {
      return { kind: "early_turn", distanceM: nxt.s - dep, turn: nxt };
    }
    return { kind: "off_route", distanceM: dist, turn: nxt };
  }

  makeStatus(near, h) {
    const r = this.route;
    // segment to emphasise: off the path → the stretch you'd rejoin; on it → the stretch
    // you're walking, up to the next corner
    let focus = null;
    if (r.ok) {
      if (this.off && near) {
        const i = near.seg;
        focus = { s0: r.cum[i], s1: r.cum[i + 1], rejoin: { x: near.x, y: near.y, s: near.s } };
      } else {
        const tn = r.nextTurn(this.s);
        focus = { s0: this.s, s1: tn ? tn.s : r.total, rejoin: null };
      }
    }
    return {
      state: this.state,
      kind: this.kind,
      distanceM: this.distanceM,
      offM: this.offM,
      s: this.s,
      maxS: this.maxS,
      total: r.total,
      heading: h ? ((Math.atan2(h[0], h[1]) * DEG) + 360) % 360 : null,
      walkedOffM: this.off ? this.off.walked : 0,
      focus,
    };
  }

  /** facility points of the path between arc lengths s0..s1 (for drawing a stretch of it) */
  slice(s0, s1) {
    const r = this.route;
    if (!r.ok || s1 <= s0) return [];
    const a = r.pointAt(s0);
    const b = r.pointAt(s1);
    const out = [[a.x, a.y]];
    for (let i = a.seg + 1; i <= b.seg; i++) out.push([r.pts[i][0], r.pts[i][1]]);
    out.push([b.x, b.y]);
    return out;
  }
}

if (typeof window !== "undefined") window.FmcDriftDetector = DriftDetector;
