"""Localization from ArUco markers in the lift lobbies.

Lobbies have bare painted walls, so landmark PnP can't lock there, and lobby colour only
says which floor. A printed ArUco marker of known size at a surveyed pose gives the full
camera pose from ONE frame in a few milliseconds, and its ID gives the floor.

Marker registry: config.yaml, per floor (written by the floor plan editor, Markers mode):

    floors:
    - floor: B1
      markers:
      - marker_id: 1
        x: 15.3          # facility metres, centre of the black square
        y: -17.9
        height_m: 1.4    # centre above the floor
        facing_deg: 180  # direction the marker FACES (into the lobby), 0 = +y, 90 = +x
        size_m: 0.23     # black square edge, measured on the print

Marker IDs must be unique across the site (lobbies are stacked — same spot, every floor).

Frames: facility x, y on the floor, z up; heading 0 = +y, clockwise (as PnP). Marker-local
frame = OpenCV's ArUco convention: x to the viewer's right, y up, z out of the marker.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

logger = logging.getLogger("fmc.vpr.marker")

DEFAULT_DICTIONARY = "DICT_4X4_50"
DEFAULT_SIZE_M = 0.23  # largest that fits A3 with its white margin
DEFAULT_HEIGHT_M = 1.4

# acceptance
MIN_SIDE_PX = 36  # marker edge in the image; smaller = corners too noisy for a pose
MAX_REPROJ_PX = 3.0
MAX_VIEW_ANGLE_DEG = 70  # between the marker normal and the line to the camera
MAX_DIST_M = 8.0
# phone held by a person: camera 0.3–2.5 m above the floor, not pointing at the ceiling/floor
CAM_HEIGHT_RANGE_M = (0.3, 2.5)
MAX_PITCH_DEG = 65
# two IPPE solutions closer than this in reprojection error = the flip is ambiguous.
# Small in the image (far): corner noise ≈ the mirror's error → need a clear 3× margin
# (simulated 23 cm marker: 1.3 let ~1 in 10 mirrored poses through at 2.5–4 m, 40° off).
# Big in the image (close): corners are precise, but lens distortion we don't model adds
# a few px to BOTH solutions — real lobby frames at 0.5–0.8 m had ratios 1.4–2.8 with the
# better one always right. Ratio needed falls from 3.0 (≤ 80 px) to 1.3 (≥ 150 px).
AMBIGUOUS_RATIO = 3.0
AMBIGUOUS_RATIO_NEAR = 1.3
RATIO_FAR_PX, RATIO_NEAR_PX = 80.0, 150.0


def _needed_ratio(side_px: float) -> float:
    t = min(1.0, max(0.0, (side_px - RATIO_FAR_PX) / (RATIO_NEAR_PX - RATIO_FAR_PX)))
    return AMBIGUOUS_RATIO + t * (AMBIGUOUS_RATIO_NEAR - AMBIGUOUS_RATIO)


def _max_reproj(side_px: float) -> float:
    """unmodelled lens distortion grows with the marker's size in the image"""
    return max(MAX_REPROJ_PX, 0.025 * side_px)


@dataclass
class MarkerDef:
    marker_id: int
    floor: str
    x: float
    y: float
    height_m: float
    facing_deg: float
    size_m: float

    def basis(self) -> tuple[np.ndarray, np.ndarray]:
        """(A, c): world = c + A @ local. Columns of A = marker x (right), y (up), z (normal)."""
        f = math.radians(self.facing_deg)
        n = np.array([math.sin(f), math.cos(f), 0.0])  # facing direction
        right = np.array([-math.cos(f), math.sin(f), 0.0])  # viewer's right when facing it
        up = np.array([0.0, 0.0, 1.0])
        A = np.column_stack([right, up, n])
        c = np.array([self.x, self.y, self.height_m])
        return A, c


@dataclass
class MarkerFix:
    marker_id: int
    floor: str
    x: float
    y: float
    z: float  # camera height above the floor
    heading: float
    rvec: np.ndarray  # world→camera, like PnP
    tvec: np.ndarray
    distance_m: float
    view_angle_deg: float
    side_px: float
    reproj_px: float
    ambiguous: bool
    confidence: float
    others: list[int] = field(default_factory=list)  # other ids seen in the frame


_disk_cache: dict[str, tuple[float, list, str]] = {}


def current_markers(site) -> tuple[list[MarkerDef], str]:
    """Markers + dictionary from config.yaml ON DISK (cached on its mtime). The API server
    holds the site config it loaded at startup; markers saved in the editor afterwards
    must still count without a restart."""
    try:
        from fmc.config import load_site_config

        path = site.data_dir / "config.yaml"
        mtime = path.stat().st_mtime
        hit = _disk_cache.get(str(path))
        if hit is None or hit[0] != mtime:
            fresh = load_site_config(site.site_id)
            hit = (mtime, load_markers(fresh), dictionary_name(fresh))
            _disk_cache[str(path)] = hit
            logger.info("[Marker] registry: %s", [(m.marker_id, m.floor) for m in hit[1]] or "none")
        return hit[1], hit[2]
    except Exception as e:  # unreadable mid-save etc. — fall back to what the site holds
        logger.debug("[Marker] disk registry read failed: %s", e)
        return load_markers(site), dictionary_name(site)


def load_markers(site) -> list[MarkerDef]:
    from fmc.floors import normalize_floor_id

    out: list[MarkerDef] = []
    for f in site.raw.get("floors") or []:
        fid = normalize_floor_id(f.get("floor"))
        for m in f.get("markers") or []:
            try:
                out.append(MarkerDef(
                    marker_id=int(m["marker_id"]),
                    floor=fid,
                    x=float(m["x"]),
                    y=float(m["y"]),
                    height_m=float(m.get("height_m", DEFAULT_HEIGHT_M)),
                    facing_deg=float(m.get("facing_deg", 0.0)) % 360.0,
                    size_m=float(m.get("size_m", DEFAULT_SIZE_M)),
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("[Marker] bad marker entry on floor %s: %s (%s)", fid, m, e)
    return out


def dictionary_name(site) -> str:
    return str(site.raw.get("aruco_dictionary") or DEFAULT_DICTIONARY)


_detectors: dict[str, cv2.aruco.ArucoDetector] = {}


def _detector(name: str) -> cv2.aruco.ArucoDetector:
    det = _detectors.get(name)
    if det is None:
        dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        det = cv2.aruco.ArucoDetector(dic, params)
        _detectors[name] = det
    return det


def marker_bits(marker_id: int, name: str = DEFAULT_DICTIONARY) -> np.ndarray:
    """(n+2)×(n+2) cells incl. the black border, 1 = black — for printing."""
    dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
    n = dic.markerSize + 2
    cell = 20
    img = cv2.aruco.generateImageMarker(dic, int(marker_id), n * cell)
    centres = img[cell // 2::cell, cell // 2::cell][:n, :n]
    return (centres < 128).astype(np.uint8)


def _pose_from_solution(rvec, tvec, mk: MarkerDef):
    R, _ = cv2.Rodrigues(rvec)
    A, c = mk.basis()
    cam_l = (-R.T @ tvec.reshape(3, 1)).ravel()  # camera in marker frame
    cam_w = c + A @ cam_l
    axes_w = A @ R.T  # columns: camera x, y, z axes in the world
    fwd = axes_w[:, 2]
    heading = math.degrees(math.atan2(fwd[0], fwd[1])) % 360.0
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, fwd[2]))))
    roll = math.degrees(math.asin(max(-1.0, min(1.0, axes_w[2, 0]))))  # image x tilt
    dist = float(np.linalg.norm(cam_l))
    view = math.degrees(math.acos(max(-1.0, min(1.0, cam_l[2] / max(dist, 1e-9)))))
    R_w = R @ A.T
    t_w = tvec.reshape(3) - R_w @ c
    rvec_w, _ = cv2.Rodrigues(R_w)
    return {
        "cam": cam_w, "heading": heading, "pitch": pitch, "roll": roll, "dist": dist,
        "view": view, "in_front": cam_l[2] > 0.05, "rvec": rvec_w.reshape(3), "tvec": t_w,
    }


def detect(frame_bgr: np.ndarray, site, K: np.ndarray, markers: list[MarkerDef] | None = None,
           dictionary: str | None = None):
    """Best registered marker in the frame → (MarkerFix | None, reason for logging)."""
    markers = load_markers(site) if markers is None else markers
    dictionary = dictionary or dictionary_name(site)
    if not markers or frame_bgr is None:
        return None, "no_markers_registered"
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
    corners, ids, _ = _detector(dictionary).detectMarkers(gray)
    if ids is None or len(ids) == 0:
        return None, "none_seen"
    by_id = {m.marker_id: m for m in markers}
    seen = [int(i) for i in ids.ravel()]
    cands = []
    for cs, mid in zip(corners, seen):
        mk = by_id.get(mid)
        if mk is None:
            continue
        pts = cs.reshape(4, 2).astype(np.float64)
        side = float(np.mean([np.linalg.norm(pts[i] - pts[(i + 1) % 4]) for i in range(4)]))
        cands.append((side, mk, pts))
    if not cands:
        return None, f"unregistered_ids={seen}"
    cands.sort(key=lambda t: -t[0])  # biggest in the image = most precise
    reasons = []
    for side, mk, pts in cands:
        if side < MIN_SIDE_PX:
            reasons.append(f"id{mk.marker_id}:small_{side:.0f}px")
            continue
        s = mk.size_m / 2.0
        obj = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], dtype=np.float64)
        try:
            n_sol, rvecs, tvecs, errs = cv2.solvePnPGeneric(
                obj, pts, K, np.zeros(5), flags=cv2.SOLVEPNP_IPPE_SQUARE,
            )
        except cv2.error as e:
            reasons.append(f"id{mk.marker_id}:solve_{e.code}")
            continue
        sols = []
        for i in range(int(n_sol)):
            p = _pose_from_solution(rvecs[i], tvecs[i], mk)
            p["err"] = float(np.ravel(errs)[i]) if errs is not None else 0.0
            h = p["cam"][2]
            p["plausible"] = (
                p["in_front"]
                and CAM_HEIGHT_RANGE_M[0] <= h <= CAM_HEIGHT_RANGE_M[1]
                and abs(p["pitch"]) <= MAX_PITCH_DEG
            )
            sols.append(p)
        ok = sorted([p for p in sols if p["plausible"]], key=lambda p: p["err"])
        if not ok:
            reasons.append(f"id{mk.marker_id}:implausible")
            continue
        best = ok[0]
        ambiguous = False
        if len(ok) > 1 and ok[1]["err"] < _needed_ratio(side) * max(best["err"], 1e-6):
            d = abs((ok[1]["heading"] - best["heading"] + 180) % 360 - 180)
            if d > 10:
                # left/right mirror (seen from the side, small in the image): both poses are
                # level, nothing tells them apart — a wrong pick is metres + tens of degrees off
                reasons.append(
                    f"id{mk.marker_id}:ambiguous_{best['heading']:.0f}or{ok[1]['heading']:.0f}deg"
                    f"_{side:.0f}px"
                )
                continue
            # up/down mirror (same heading): a hand-held phone keeps its image level
            ambiguous = True
            best = min(ok[:2], key=lambda p: abs(p["roll"]) + 0.5 * abs(p["pitch"]))
        if best["err"] > _max_reproj(side):
            reasons.append(f"id{mk.marker_id}:reproj_{best['err']:.1f}px")
            continue
        if best["view"] > MAX_VIEW_ANGLE_DEG:
            reasons.append(f"id{mk.marker_id}:angle_{best['view']:.0f}deg")
            continue
        if best["dist"] > MAX_DIST_M:
            reasons.append(f"id{mk.marker_id}:far_{best['dist']:.1f}m")
            continue
        conf = 0.95 - 0.04 * max(0.0, best["dist"] - 1.5) - (0.1 if ambiguous else 0.0)
        cam = best["cam"]
        fix = MarkerFix(
            marker_id=mk.marker_id, floor=mk.floor,
            x=float(cam[0]), y=float(cam[1]), z=float(cam[2]), heading=float(best["heading"]),
            rvec=best["rvec"], tvec=best["tvec"],
            distance_m=best["dist"], view_angle_deg=best["view"], side_px=side,
            reproj_px=best["err"], ambiguous=ambiguous,
            confidence=float(max(0.55, min(0.95, conf))),
            others=[m for m in seen if m != mk.marker_id],
        )
        return fix, "ok"
    return None, ",".join(reasons) or "rejected"


def svg_for_print(marker_id: int, size_m: float, label: str = "", name: str = DEFAULT_DICTIONARY) -> str:
    """Printable SVG in real millimetres: print at 100 % (no "fit to page")."""
    bits = marker_bits(marker_id, name)
    n = bits.shape[0]
    side_mm = size_m * 1000.0
    cell = side_mm / n
    quiet = max(cell * 0.6, 8.0)  # white margin the detector needs
    text_h = 16.0
    w = side_mm + 2 * quiet
    h = side_mm + 2 * quiet + text_h
    rects = "".join(
        f'<rect x="{quiet + c * cell:.3f}" y="{quiet + r * cell:.3f}" width="{cell + 0.02:.3f}" '
        f'height="{cell + 0.02:.3f}"/>'
        for r in range(n) for c in range(n) if bits[r, c]
    )
    cap = f"FindMyCar marker {marker_id}" + (f" · {label}" if label else "")
    cap2 = f"black square = {side_mm:.0f} mm — print at 100 %, matte paper"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" '
        f'viewBox="0 0 {w:.3f} {h:.3f}">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        f'<rect x="0.3" y="0.3" width="{w - 0.6:.3f}" height="{h - 0.6:.3f}" fill="none" '
        f'stroke="#999" stroke-width="0.3" stroke-dasharray="3 2"/>'
        f'<g fill="#000">{rects}</g>'
        f'<text x="{w / 2:.2f}" y="{side_mm + 2 * quiet + 5:.2f}" font-family="sans-serif" '
        f'font-size="5" text-anchor="middle">{cap}</text>'
        f'<text x="{w / 2:.2f}" y="{side_mm + 2 * quiet + 11:.2f}" font-family="sans-serif" '
        f'font-size="3.5" fill="#555" text-anchor="middle">{cap2}</text>'
        "</svg>"
    )
