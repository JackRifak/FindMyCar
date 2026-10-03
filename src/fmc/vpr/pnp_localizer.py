"""6-DOF pose from 2D-to-3D matching + PnP RANSAC.

Loads the mapped landmark database (positions + ORB descriptors) produced by
ContinuousMapper.finalize_map, matches a live query frame against those 3D
points, and recovers camera pose via cv2.solvePnPRansac.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from fmc.config import SiteConfig

logger = logging.getLogger("fmc.vpr.pnp")

MIN_MAP_LANDMARKS = 80
MIN_2D3D_MATCHES = 16
MIN_PNP_INLIERS = 12
RATIO_TEST = 0.78
RANSAC_REPROJ_PX = 4.0


@dataclass
class Pose6Dof:
    """Camera pose in facility frame (x,y floor meters, z height, heading deg)."""
    x: float
    y: float
    z: float
    heading: float
    # OpenCV world→camera
    rvec: np.ndarray
    tvec: np.ndarray
    num_matches: int
    num_inliers: int
    inlier_ratio: float
    confidence: float


class MapLandmarkIndex:
    """In-memory 3D landmark index for PnP localization."""

    def __init__(self, positions: np.ndarray, descriptors: np.ndarray, ids: np.ndarray):
        self.positions = positions.astype(np.float64)  # (N,3) facility x,y,height
        self.descriptors = descriptors  # (N,32) uint8 ORB
        self.ids = ids
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.detector = cv2.ORB_create(nfeatures=4000)

    @classmethod
    def from_arrays(
        cls, pos: np.ndarray, desc: np.ndarray, ids: np.ndarray, source: str = ""
    ) -> Optional["MapLandmarkIndex"]:
        if len(pos) < MIN_MAP_LANDMARKS:
            logger.warning(
                "[PnP] Map DB too small (%s < %s landmarks) [%s]",
                len(pos), MIN_MAP_LANDMARKS, source,
            )
            return None
        logger.info("[PnP] Loaded map DB: %s landmarks from %s", len(pos), source)
        return cls(pos, desc, ids)

    @classmethod
    def load_npz(cls, path: Path) -> Optional["MapLandmarkIndex"]:
        if not path.exists():
            return None
        try:
            data = np.load(path)
            return cls.from_arrays(
                data["positions"], data["descriptors"], data["ids"], source=str(path)
            )
        except (OSError, KeyError, ValueError) as e:
            logger.warning("[PnP] Failed loading %s: %s", path, e)
            return None

    @staticmethod
    def camera_matrix(width: int, height: int) -> np.ndarray:
        f = max(width, height) * 0.85
        cx, cy = width / 2.0, height / 2.0
        return np.array([[f, 0.0, cx], [0.0, f, cy], [0.0, 0.0, 1.0]], dtype=np.float64)

    def localize(self, query_bgr: np.ndarray) -> Optional[Pose6Dof]:
        if query_bgr is None or query_bgr.size == 0:
            return None
        h, w = query_bgr.shape[:2]
        gray = cv2.cvtColor(query_bgr, cv2.COLOR_BGR2GRAY) if query_bgr.ndim == 3 else query_bgr

        kpts, desc = self.detector.detectAndCompute(gray, None)
        n_feat = 0 if kpts is None else len(kpts)
        if desc is None or n_feat < MIN_2D3D_MATCHES:
            logger.info("[PnP] fail: too few query features (%s < %s)", n_feat, MIN_2D3D_MATCHES)
            return None

        raw = self.matcher.knnMatch(desc, self.descriptors, k=2)
        good = []
        for pair in raw:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < RATIO_TEST * n.distance:
                good.append(m)

        if len(good) < MIN_2D3D_MATCHES:
            logger.info(
                "[PnP] fail: too few 2D-3D matches (feat=%s matches=%s < %s)",
                n_feat, len(good), MIN_2D3D_MATCHES,
            )
            return None

        pts_2d = np.array([kpts[m.queryIdx].pt for m in good], dtype=np.float64)
        pts_3d = np.array([self.positions[m.trainIdx] for m in good], dtype=np.float64)

        K = self.camera_matrix(w, h)
        dist = np.zeros(5, dtype=np.float64)

        ok, rvec, tvec, inliers = cv2.solvePnPRansac(
            pts_3d,
            pts_2d,
            K,
            dist,
            iterationsCount=200,
            reprojectionError=RANSAC_REPROJ_PX,
            confidence=0.99,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        n_inl = 0 if inliers is None else len(inliers)
        if not ok or n_inl < MIN_PNP_INLIERS:
            logger.info(
                "[PnP] fail: solvePnPRansac (ok=%s feat=%s matches=%s inliers=%s < %s)",
                ok, n_feat, len(good), n_inl, MIN_PNP_INLIERS,
            )
            return None

        # refine on inliers
        inl = inliers.ravel()
        try:
            ok2, rvec, tvec = cv2.solvePnP(
                pts_3d[inl],
                pts_2d[inl],
                K,
                dist,
                rvec,
                tvec,
                useExtrinsicGuess=True,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
            if not ok2:
                pass
        except cv2.error:
            pass

        R, _ = cv2.Rodrigues(rvec)
        # camera center in world: C = -R^T t
        cam = (-R.T @ tvec.reshape(3, 1)).ravel()
        # camera forward (+Z) in world
        fwd = (R.T @ np.array([0.0, 0.0, 1.0])).ravel()
        # facility heading: atan2(east, north) → degrees [0,360)
        heading = float(np.degrees(np.arctan2(fwd[0], fwd[1])) % 360.0)

        n_inl = int(len(inl))
        ratio = n_inl / max(len(good), 1)
        conf = float(min(1.0, 0.35 + 0.65 * ratio))

        pose = Pose6Dof(
            x=float(cam[0]),
            y=float(cam[1]),
            z=float(cam[2]),
            heading=heading,
            rvec=rvec.reshape(3),
            tvec=tvec.reshape(3),
            num_matches=len(good),
            num_inliers=n_inl,
            inlier_ratio=ratio,
            confidence=conf,
        )
        logger.info(
            "[PnP] Pose OK: (%.2f, %.2f, h=%.2f) heading=%.0f° "
            "inliers=%s/%s conf=%.2f",
            pose.x, pose.y, pose.z, pose.heading,
            pose.num_inliers, pose.num_matches, pose.confidence,
        )
        return pose


def load_map_index(
    site: SiteConfig,
    center_xy: Optional[tuple[float, float]] = None,
    radius_m: float = 30.0,
) -> Optional[MapLandmarkIndex]:
    """Load landmark index — prefer H2GIS, fall back to NPZ."""
    # 1) H2GIS spatial DB
    try:
        from fmc.storage.h2gis_store import load_landmarks

        loaded = load_landmarks(site, center_xy=center_xy, radius_m=radius_m)
        if loaded is not None:
            pos, desc, ids = loaded
            idx = MapLandmarkIndex.from_arrays(pos, desc, ids, source="H2GIS")
            if idx is not None:
                return idx
    except Exception as e:
        logger.warning("[PnP] H2GIS load failed: %s — trying NPZ", e)

    # 2) NPZ fallback
    return MapLandmarkIndex.load_npz(site.index_dir / "map_landmarks.npz")
