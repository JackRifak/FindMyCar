/**
 * JS bridge for Capacitor ArVio (ARCore) 6-DOF poses + camera JPEG capture.
 * Falls back gracefully in plain browsers (available=false).
 */
(function (global) {
  const EMPTY = {
    x: 0, y: 0, z: 0,
    qw: 1, qx: 0, qy: 0, qz: 0,
    headingDeg: 0,
    tracking: "stopped",
    timestamp: 0,
  };

  function nativePlugin() {
    try {
      const cap = global.Capacitor;
      if (!cap || !cap.Plugins) return null;
      return cap.Plugins.ArVio || null;
    } catch (_) {
      return null;
    }
  }

  async function isAvailable() {
    const p = nativePlugin();
    if (!p || typeof p.isAvailable !== "function") return false;
    try {
      const res = await p.isAvailable();
      return !!res.available;
    } catch (_) {
      return false;
    }
  }

  async function start() {
    const p = nativePlugin();
    if (!p) return { started: false, available: false };
    return p.start();
  }

  async function stop() {
    const p = nativePlugin();
    if (!p) return { stopped: true };
    return p.stop();
  }

  async function getPose() {
    const p = nativePlugin();
    if (!p) return { ...EMPTY };
    try {
      return await p.getPose();
    } catch (_) {
      return { ...EMPTY, tracking: "lost" };
    }
  }

  async function captureFrame() {
    const p = nativePlugin();
    if (!p || typeof p.captureFrame !== "function") return null;
    try {
      return await p.captureFrame();
    } catch (_) {
      return null;
    }
  }

  async function getPreview() {
    const p = nativePlugin();
    if (!p || typeof p.getPreview !== "function") return null;
    try {
      return await p.getPreview();
    } catch (_) {
      return null;
    }
  }

  global.ArVio = { isAvailable, start, stop, getPose, captureFrame, getPreview, EMPTY };
})(typeof window !== "undefined" ? window : globalThis);
