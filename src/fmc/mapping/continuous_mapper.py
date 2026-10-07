"""Continuous 3D Mapping Engine.

Ingests a stream of keyframes (images + local VIO poses) and periodic manual
georeferenced tags (Ground Control Points) from a mobile client during a continuous walk.
It extracts features, tracks them across frames, triangulates 3D landmarks, 
and aligns the local 3D map into the global facility coordinate system.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from fmc.vio.tracker import SixDofPose
from fmc.vpr.camera_intrinsics import Intrinsics, camera_matrix

logger = logging.getLogger("fmc.mapping")

# landmarks/tags loaded from a prior finalize (survive new sessions + server reload)
COMMITTED_SESSION = -1


@dataclass
class GeoreferencedTag:
    """A user-tapped ground control point on the facility map."""
    timestamp: float
    facility_x: float
    facility_y: float
    facility_floor: str = "1"
    session_id: int = 0


@dataclass
class Keyframe:
    """A camera frame with its extracted features and VIO pose."""
    frame_id: int
    timestamp: float
    vio_pose: SixDofPose
    keypoints: np.ndarray  # Shape (N, 2)
    descriptors: np.ndarray
    heading_deg: float = 0.0
    # integrated map path (client PDR / ARCore)
    map_x: float = 0.0
    map_y: float = 0.0  # height
    map_z: float = 0.0
    # OpenCV-cam → world (X right, Y up, Z fwd); None → yaw-only fallback
    R_wc: Optional[np.ndarray] = None
    session_id: int = 0
    floor: str = "1"
    # 3x3 pinhole K for this image (real intrinsics when the client sent them)
    K: Optional[np.ndarray] = None
    # local keypoint index -> global landmark id
    landmark_ids: Dict[int, int] = field(default_factory=dict) 


@dataclass
class Landmark3D:
    """A triangulated 3D feature in the map."""
    landmark_id: int
    # The optimized 3D position in the facility frame
    position: np.ndarray  # Shape (3,)
    # Which keyframes observe this landmark (frame_id -> keypoint_index)
    observations: Dict[int, int]
    # RGB color (0-255) sampled from camera frame
    color: Tuple[int, int, int] = (52, 199, 89)
    # ORB descriptor for 2D-to-3D localization (PnP)
    descriptor: Optional[np.ndarray] = None
    session_id: int = 0
    floor: str = "1"


class ContinuousMapper:
    def __init__(self):
        self.detector = cv2.ORB_create(nfeatures=3000)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.reset()

    def reset(self) -> None:
        """Wipe all sessions — full clear."""
        self.keyframes: List[Keyframe] = []
        self.user_tags: List[GeoreferencedTag] = []
        self.landmarks: Dict[int, Landmark3D] = {}
        self._next_landmark_id = 0
        self._next_frame_id = 0
        self._session_id = 0
        self._aligned_sessions: set = set()
        self.global_transform = np.eye(4)
        self._is_aligned = False
        self._disk_hydrated = False
        self._vo_x = 0.0
        self._vo_z = 0.0
        self._map_floor = "1"
        self.last_new_landmarks = 0
        self.last_detected_keypoints: List[List[float]] = []
        self.last_tracked_keypoints: List[List[float]] = []
        logger.info("[Mapper] Full reset — all sessions cleared.")

    def set_map_floor(self, floor) -> None:
        """Active floor label for new keyframes / landmarks (1, G, B1, …)."""
        from fmc.floors import normalize_floor_id
        self._map_floor = normalize_floor_id(floor)
        logger.info("[Mapper] Active map floor set to %s", self._map_floor)

    def _next_free_session_id(self) -> int:
        ids = [self._session_id]
        ids.extend(t.session_id for t in self.user_tags)
        ids.extend(lm.session_id for lm in self.landmarks.values())
        ids.extend(kf.session_id for kf in self.keyframes)
        ids.extend(self._aligned_sessions)
        return max(ids) + 1 if ids else 0

    def hydrate_from_disk(self, site) -> int:
        """Merge previously finalized landmarks + tags from disk into memory.

        Prevents new-session / finalize from wiping PLY + H2GIS when the
        in-memory mapper was empty (server reload, new process, etc.).
        """
        index_dir = Path(site.index_dir)
        added = 0

        # --- landmarks: H2GIS first, then NPZ ---
        positions = descriptors = ids = colors = floors_arr = None
        aligned_disk = False
        try:
            from fmc.storage.h2gis_store import load_landmarks

            loaded = load_landmarks(site)
            if loaded is not None:
                positions, descriptors, ids, floors_arr = loaded
                aligned_disk = True
        except Exception as e:
            logger.warning("[Mapper] H2GIS hydrate failed: %s", e)

        npz_path = index_dir / "map_landmarks.npz"
        if positions is None and npz_path.exists():
            try:
                # own export; floor labels are an object array → needs allow_pickle
                data = np.load(npz_path, allow_pickle=True)
                positions = data["positions"]
                descriptors = data["descriptors"]
                ids = data["ids"]
                colors = data["colors"] if "colors" in data.files else None
                floors_arr = data["floors"] if "floors" in data.files else None
                if "aligned" in data.files:
                    aligned_disk = bool(np.asarray(data["aligned"]).reshape(-1)[0])
                else:
                    aligned_disk = True
            except (OSError, ValueError, KeyError) as e:
                logger.warning("[Mapper] NPZ hydrate failed: %s", e)

        if positions is not None and len(positions) > 0:
            if colors is None:
                colors = np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(positions), 1))
            from fmc.floors import coerce_floor_array, normalize_floor_id
            if floors_arr is None:
                floor_labels = ["1"] * len(positions)
            else:
                floor_labels = coerce_floor_array(floors_arr, n=len(positions))
            for i in range(len(positions)):
                lid = int(ids[i])
                if lid in self.landmarks:
                    continue
                desc = descriptors[i].astype(np.uint8).copy() if descriptors is not None else None
                col = tuple(int(c) for c in colors[i][:3])
                self.landmarks[lid] = Landmark3D(
                    landmark_id=lid,
                    position=positions[i].astype(np.float64).copy(),
                    observations={},
                    color=col,
                    descriptor=desc,
                    session_id=COMMITTED_SESSION,
                    floor=normalize_floor_id(floor_labels[i]),
                )
                added += 1
            if aligned_disk:
                self._aligned_sessions.add(COMMITTED_SESSION)
                self._is_aligned = True
            self._next_landmark_id = max(self._next_landmark_id, int(ids.max()) + 1)

        # --- tags.json ---
        tags_path = index_dir / "tags.json"
        if tags_path.exists():
            try:
                with open(tags_path, "r", encoding="utf-8") as f:
                    disk_tags = json.load(f)
                existing_ts = {(t.timestamp, t.facility_x, t.facility_y) for t in self.user_tags}
                for raw in disk_tags:
                    ts = float(raw.get("timestamp", 0.0))
                    fx = float(raw.get("facility_x", raw.get("x", 0.0)))
                    fy = float(raw.get("facility_y", raw.get("y", 0.0)))
                    key = (ts, fx, fy)
                    if key in existing_ts:
                        continue
                    sid = int(raw.get("session_id", COMMITTED_SESSION))
                    if raw.get("aligned", False):
                        self._aligned_sessions.add(sid)
                        self._is_aligned = True
                    self.user_tags.append(
                        GeoreferencedTag(
                            timestamp=ts,
                            facility_x=fx,
                            facility_y=fy,
                            facility_floor=str(raw.get("floor", "1")),
                            session_id=sid,
                        )
                    )
                    existing_ts.add(key)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
                logger.warning("[Mapper] tags.json hydrate failed: %s", e)

        self._disk_hydrated = True
        if added:
            logger.info(
                "[Mapper] Hydrated %s landmarks from disk (total=%s, tags=%s, aligned_sessions=%s)",
                added, len(self.landmarks), len(self.user_tags), sorted(self._aligned_sessions),
            )
        return added

    def begin_session(self, site=None) -> Dict:
        """Start a new disconnected walk; keep prior sessions' landmarks/tags."""
        if site is not None:
            self.hydrate_from_disk(site)
        prev = self._session_id
        n_prev_lm = sum(1 for lm in self.landmarks.values() if lm.session_id == prev)
        n_prev_tags = sum(1 for t in self.user_tags if t.session_id == prev)
        self._session_id = self._next_free_session_id()
        self._vo_x = 0.0
        self._vo_z = 0.0
        self.last_new_landmarks = 0
        self.last_detected_keypoints = []
        self.last_tracked_keypoints = []
        logger.info(
            "[Mapper] New session %s (kept session %s: %s landmarks, %s tags; total map=%s)",
            self._session_id, prev, n_prev_lm, n_prev_tags, len(self.landmarks),
        )
        return {
            "session_id": self._session_id,
            "prev_session_id": prev,
            "total_landmarks": len(self.landmarks),
            "total_tags": len(self.user_tags),
            "aligned_sessions": sorted(self._aligned_sessions),
        }

    def add_keyframe(
        self,
        image: np.ndarray,
        timestamp: float,
        vio_pose: SixDofPose,
        heading_deg: Optional[float] = None,
        intrinsics: Optional[Intrinsics] = None,
    ) -> int:
        """Process a new incoming frame from the client."""
        # 1. Extract Features
        kpts_cv, desc = self.detector.detectAndCompute(image, None)
        
        if kpts_cv is None or len(kpts_cv) == 0:
            logger.warning(f"[Mapper] No ORB features detected in incoming image (shape={image.shape}).")
            self.last_new_landmarks = 0
            self.last_detected_keypoints = []
            self.last_tracked_keypoints = []
            return -1
            
        kpts_np = np.array([kp.pt for kp in kpts_cv], dtype=np.float32)
        # Store top detected keypoints for real-time live feed overlay (up to 150 points)
        self.last_detected_keypoints = [[round(float(pt[0]), 1), round(float(pt[1]), 1)] for pt in kpts_np[:150]]
        self.last_tracked_keypoints = []

        yaw = float(heading_deg) if heading_deg is not None else self._yaw_from_pose(vio_pose)
        R_wc = self._R_wc_from_pose(vio_pose, yaw)

        # seed path from client PDR / ARCore (may stay 0 on WebView — visual path fills in later)
        frame = Keyframe(
            frame_id=self._next_frame_id,
            timestamp=timestamp,
            vio_pose=vio_pose,
            keypoints=kpts_np,
            descriptors=desc,
            heading_deg=yaw,
            map_x=float(vio_pose.x),
            map_y=float(vio_pose.y),
            map_z=float(vio_pose.z),
            R_wc=R_wc,
            session_id=self._session_id,
            floor=str(getattr(self, "_map_floor", "1")),
            K=camera_matrix(image.shape[1], image.shape[0], intrinsics),
            landmark_ids={}
        )
        
        logger.info(
            f"[Mapper] Keyframe #{frame.frame_id} sess={self._session_id} floor={frame.floor}: "
            f"extracted {len(kpts_cv)} ORB features | "
            f"VIO: ({vio_pose.x:.2f}, {vio_pose.y:.2f}, {vio_pose.z:.2f}) "
            f"Q: [{vio_pose.qw:.2f}, {vio_pose.qx:.2f}, {vio_pose.qy:.2f}, {vio_pose.qz:.2f}] "
            f"6dof={self._pose_has_6dof(vio_pose)} "
            f"K={'real' if intrinsics else 'guess'}(f={frame.K[0, 0]:.0f})"
        )
        
        # 2. Match only within the same session (separated walks must not link)
        self.last_new_landmarks = 0
        prev = self.keyframes[-1] if self.keyframes else None
        if prev is not None and prev.session_id == frame.session_id:
            self.last_new_landmarks = self._track_and_triangulate(prev, frame, image)
        else:
            self._vo_x = frame.map_x
            self._vo_z = frame.map_z
            logger.info(
                "[Mapper] Session %s reference keyframe stored. Awaiting next frame to triangulate.",
                self._session_id,
            )
            
        self.keyframes.append(frame)
        self._next_frame_id += 1
        
        return frame.frame_id

    def add_tag(self, timestamp: float, x: float, y: float, floor) -> None:
        """Add a manual ground-truth anchor from the user."""
        from fmc.floors import normalize_floor_id
        floor = normalize_floor_id(floor)
        self._map_floor = floor
        tag = GeoreferencedTag(timestamp, x, y, floor, session_id=self._session_id)
        self.user_tags.append(tag)
        # stamp recent same-session landmarks/keyframes with this floor
        for kf in self.keyframes:
            if kf.session_id == self._session_id:
                kf.floor = floor
        for lm in self.landmarks.values():
            if lm.session_id == self._session_id:
                lm.floor = floor
        logger.info(
            "Added georeferenced tag at (%.2f, %.2f) floor=%s sess=%s",
            x, y, floor, self._session_id,
        )

    def _yaw_from_pose(self, pose: SixDofPose) -> float:
        """Extract compass yaw (deg) from quaternion; fall back to 0."""
        from scipy.spatial.transform import Rotation
        try:
            yaw, _, _ = Rotation.from_quat(
                [pose.qx, pose.qy, pose.qz, pose.qw]
            ).as_euler("YXZ", degrees=True)
            return float(yaw)
        except ValueError:
            return 0.0

    @staticmethod
    def _pose_has_6dof(pose: SixDofPose) -> bool:
        """True when client sent a real ARCore/VIO quat (not identity PDR stub)."""
        off = abs(float(pose.qw) - 1.0) + abs(float(pose.qx)) + abs(float(pose.qy)) + abs(float(pose.qz))
        return off > 0.02 or abs(float(pose.y)) > 0.05

    def _yaw_R(self, heading_deg: float) -> np.ndarray:
        """Yaw-only world rotation: forward=(sin h, 0, cos h), matching client PDR."""
        h = np.deg2rad(heading_deg)
        c, s = np.cos(h), np.sin(h)
        return np.array([
            [c, 0.0, s],
            [0.0, 1.0, 0.0],
            [-s, 0.0, c],
        ], dtype=np.float64)

    def _R_wc_from_pose(self, pose: SixDofPose, heading_deg: float) -> np.ndarray:
        """OpenCV camera → world (X right, Y up, Z fwd).

        ARCore quat maps OpenGL cam → AR world; client stores position with Z flipped
        to our world. Compose F @ R_ar @ S with S: OpenCV→OpenGL, F: AR→ours.
        """
        if not self._pose_has_6dof(pose):
            return self._yaw_R(heading_deg)
        from scipy.spatial.transform import Rotation
        try:
            R_ar = Rotation.from_quat(
                [pose.qx, pose.qy, pose.qz, pose.qw]
            ).as_matrix()
        except ValueError:
            return self._yaw_R(heading_deg)
        S = np.diag([1.0, -1.0, -1.0])  # OpenCV → OpenGL cam
        F = np.diag([1.0, 1.0, -1.0])   # AR world → our Z-forward world
        return (F @ R_ar @ S).astype(np.float64)

    def _track_and_triangulate(self, prev_frame: Keyframe, curr_frame: Keyframe, curr_image: Optional[np.ndarray] = None) -> int:
        """Match features between consecutive frames and triangulate into 3D using Visual Odometry."""
        if prev_frame.descriptors is None or curr_frame.descriptors is None:
            logger.warning("[Mapper] Missing descriptors in one of the frames.")
            return 0
            
        # Match features with ratio test
        raw_matches = self.matcher.knnMatch(prev_frame.descriptors, curr_frame.descriptors, k=2)
        good_matches = [m for m, n in raw_matches if m.distance < 0.78 * n.distance]
        
        logger.info(
            f"[Mapper] Matching Frame #{prev_frame.frame_id} (pts={len(prev_frame.keypoints)}) -> "
            f"Frame #{curr_frame.frame_id} (pts={len(curr_frame.keypoints)}): "
            f"{len(good_matches)} good matches (raw: {len(raw_matches)})"
        )
        
        if len(good_matches) < 8:
            logger.warning(f"[Mapper] Insufficient feature matches ({len(good_matches)} < 8). Moving too fast or pointing at untextured area.")
            return 0

        h, w = (curr_image.shape[:2]) if curr_image is not None else (480, 640)
        K = curr_frame.K if curr_frame.K is not None else camera_matrix(w, h)
        K_prev = prev_frame.K if prev_frame.K is not None else K

        pts_prev = []
        pts_curr = []
        match_indices = []
        for m in good_matches:
            pts_prev.append(prev_frame.keypoints[m.queryIdx])
            pts_curr.append(curr_frame.keypoints[m.trainIdx])
            match_indices.append((m.queryIdx, m.trainIdx))
            
        pts_prev_np = np.array(pts_prev, dtype=np.float64)
        pts_curr_np = np.array(pts_curr, dtype=np.float64)
        if not np.allclose(K_prev, K):
            # different camera config/size between frames — re-project prev pixels into curr K
            ones = np.ones((len(pts_prev_np), 1))
            rays = (np.linalg.inv(K_prev) @ np.hstack((pts_prev_np, ones)).T).T
            pts_prev_np = (K @ rays.T).T[:, :2]

        # 1. Recover relative camera pose (R, t) via Essential Matrix with RANSAC
        E, inlier_mask = cv2.findEssentialMat(
            pts_prev_np, pts_curr_np, K,
            method=cv2.RANSAC, prob=0.999, threshold=1.5
        )
        if E is None:
            logger.warning("[Mapper] Essential Matrix estimation failed (E is None).")
            return 0

        num_inliers, R_rel, t_rel, mask_pose = cv2.recoverPose(
            E, pts_prev_np, pts_curr_np, K, mask=inlier_mask
        )
        if num_inliers < 8:
            logger.warning(f"[Mapper] Low inliers after recoverPose ({num_inliers} < 8).")
            return 0

        # 2. Metric baseline: prefer client 6-DOF / PDR delta; fall back to visual flow
        dx = curr_frame.vio_pose.x - prev_frame.vio_pose.x
        dy = curr_frame.vio_pose.y - prev_frame.vio_pose.y
        dz = curr_frame.vio_pose.z - prev_frame.vio_pose.z
        vio_baseline = float(np.linalg.norm([dx, dy, dz]))

        inlier_idx = mask_pose.ravel() > 0
        flow = np.linalg.norm(pts_curr_np[inlier_idx] - pts_prev_np[inlier_idx], axis=1)
        med_flow = float(np.median(flow)) if len(flow) else 0.0
        dt = abs(float(curr_frame.timestamp) - float(prev_frame.timestamp))
        use_6dof = self._pose_has_6dof(curr_frame.vio_pose)

        if vio_baseline >= 0.12:
            baseline = vio_baseline
            baseline_src = "vio6dof" if use_6dof else "pdr"
            curr_frame.map_x = float(curr_frame.vio_pose.x)
            curr_frame.map_y = float(curr_frame.vio_pose.y)
            curr_frame.map_z = float(curr_frame.vio_pose.z)
            self._vo_x = curr_frame.map_x
            self._vo_z = curr_frame.map_z
        elif med_flow >= 8.0 and dt >= 0.25:
            # client PDR stuck (common in WebView) but camera clearly moved
            baseline = float(np.clip(1.05 * dt, 0.25, 1.40))
            baseline_src = "visual"
            yaw = np.deg2rad(curr_frame.heading_deg)
            self._vo_x += float(np.sin(yaw) * baseline)
            self._vo_z += float(np.cos(yaw) * baseline)
            curr_frame.map_x = self._vo_x
            curr_frame.map_y = float(prev_frame.map_y)
            curr_frame.map_z = self._vo_z
        else:
            logger.info(
                f"[Mapper] Skip triangulation: baseline={vio_baseline:.3f}m flow={med_flow:.1f}px "
                f"(need VIO>=0.12m or visual flow>=8px)."
            )
            curr_frame.map_x = prev_frame.map_x
            curr_frame.map_y = prev_frame.map_y
            curr_frame.map_z = prev_frame.map_z
            tracked = [
                [round(float(pts_curr_np[i, 0]), 1), round(float(pts_curr_np[i, 1]), 1)]
                for i in range(len(match_indices)) if inlier_idx[i]
            ]
            self.last_tracked_keypoints = tracked[:150]
            return 0

        t_scaled = t_rel * baseline

        logger.info(
            f"[Mapper] Pose recovered: inliers={num_inliers}/{len(good_matches)} | "
            f"baseline={baseline:.2f}m ({baseline_src}) flow={med_flow:.1f}px "
            f"path=({curr_frame.map_x:.1f},{curr_frame.map_y:.1f},{curr_frame.map_z:.1f})"
        )

        # 3. Triangulate points in normalized camera coordinates
        p1_norm = cv2.undistortPoints(np.expand_dims(pts_prev_np, 1), K, None).squeeze().T
        p2_norm = cv2.undistortPoints(np.expand_dims(pts_curr_np, 1), K, None).squeeze().T

        P1 = np.hstack((np.eye(3), np.zeros((3, 1))))
        P2 = np.hstack((R_rel, t_scaled))

        points_4d = cv2.triangulatePoints(P1, P2, p1_norm, p2_norm)
        w_coords = points_4d[3, :]
        valid_w = (np.abs(w_coords) > 1e-6) & (mask_pose.ravel() > 0)
        
        points_3d_local = np.zeros((points_4d.shape[1], 3), dtype=np.float64)
        points_3d_local[valid_w] = (points_4d[:3, valid_w] / w_coords[valid_w]).T

        # 4. Register landmarks in world — full SE3 when ARCore quat present
        if prev_frame.R_wc is not None and use_6dof:
            rot_wc = prev_frame.R_wc
            t_wc = np.array(
                [prev_frame.map_x, prev_frame.map_y, prev_frame.map_z],
                dtype=np.float64,
            )
            se3 = True
        else:
            rot_wc = self._yaw_R(prev_frame.heading_deg)
            t_wc = np.array([prev_frame.map_x, 0.0, prev_frame.map_z], dtype=np.float64)
            se3 = False

        new_points = 0
        for i, (idx_prev, idx_curr) in enumerate(match_indices):
            if not valid_w[i]:
                continue
                
            pt_cam = points_3d_local[i]
            # Depth / height filter for garage-scale mapping
            if pt_cam[2] < 0.3 or pt_cam[2] > 20.0 or np.any(np.isnan(pt_cam)) or np.any(np.isinf(pt_cam)):
                continue

            if se3:
                # R_wc already maps OpenCV cam → world
                pt_world = rot_wc @ pt_cam + t_wc
            else:
                # OpenCV cam (X right, Y down, Z fwd) → body (X right, Y up, Z fwd)
                pt_body = np.array([pt_cam[0], -pt_cam[1], pt_cam[2]], dtype=np.float64)
                if abs(pt_body[1]) > 4.0:
                    continue
                pt_world = rot_wc @ pt_body + t_wc

            if abs(float(pt_world[1])) > 4.5:
                continue

            # Sample RGB color
            pt_color = (52, 199, 89)
            if curr_image is not None:
                vi, ui = int(round(pts_curr_np[i, 1])), int(round(pts_curr_np[i, 0]))
                if 0 <= vi < h and 0 <= ui < w:
                    if len(curr_image.shape) == 3 and curr_image.shape[2] == 3:
                        b, g, r = curr_image[vi, ui]
                        pt_color = (int(r), int(g), int(b))

            # prefer current-frame descriptor for live matching
            desc = None
            if curr_frame.descriptors is not None:
                desc = curr_frame.descriptors[idx_curr].copy()

            if idx_prev in prev_frame.landmark_ids:
                lm_id = prev_frame.landmark_ids[idx_prev]
                curr_frame.landmark_ids[idx_curr] = lm_id
                self.landmarks[lm_id].observations[curr_frame.frame_id] = idx_curr
                if desc is not None:
                    self.landmarks[lm_id].descriptor = desc
            else:
                lm_id = self._next_landmark_id
                self._next_landmark_id += 1
                
                landmark = Landmark3D(
                    landmark_id=lm_id,
                    position=pt_world,
                    observations={
                        prev_frame.frame_id: idx_prev,
                        curr_frame.frame_id: idx_curr
                    },
                    color=pt_color,
                    descriptor=desc,
                    session_id=curr_frame.session_id,
                    floor=str(getattr(curr_frame, "floor", getattr(self, "_map_floor", "1"))),
                )
                self.landmarks[lm_id] = landmark
                prev_frame.landmark_ids[idx_prev] = lm_id
                curr_frame.landmark_ids[idx_curr] = lm_id
                new_points += 1

        # Record tracked inliers for the live camera AR feed (up to 150 points)
        tracked = []
        for i in range(len(match_indices)):
            if valid_w[i]:
                tracked.append([round(float(pts_curr_np[i, 0]), 1), round(float(pts_curr_np[i, 1]), 1)])
        self.last_tracked_keypoints = tracked[:150]

        logger.info(f"[Mapper] Triangulation result: +{new_points} new 3D points ({len(self.landmarks)} total landmarks in map, {len(self.last_tracked_keypoints)} tracked inliers).")
        return new_points

    @staticmethod
    def _seg_similarity(a0: np.ndarray, a1: np.ndarray, b0: np.ndarray, b1: np.ndarray):
        """2-point similarity: map path segment a0→a1 onto facility b0→b1."""
        va = a1 - a0
        vb = b1 - b0
        la = float(np.linalg.norm(va))
        lb = float(np.linalg.norm(vb))
        if la < 1e-3 or lb < 1e-3:
            return None
        scale = lb / la
        ang = float(np.arctan2(vb[1], vb[0]) - np.arctan2(va[1], va[0]))
        c, s = np.cos(ang), np.sin(ang)
        R = scale * np.array([[c, -s], [s, c]], dtype=np.float64)
        t = b0 - R @ a0
        return R, t, scale

    def _align_session(self, session_id: int) -> bool:
        """
        Warp one session's landmarks with per-tag-segment similarities.
        Separated walks each get their own local path → facility fit.
        """
        if session_id in self._aligned_sessions:
            logger.info("[Mapper] Session %s already aligned — skip", session_id)
            return True

        sess_tags = [t for t in self.user_tags if t.session_id == session_id]
        sess_kfs = [kf for kf in self.keyframes if kf.session_id == session_id]
        if len(sess_tags) < 2:
            logger.warning(
                "[Mapper] Session %s has %s tag(s) — need 2+ to align",
                session_id, len(sess_tags),
            )
            return False
        if not sess_kfs:
            logger.warning("[Mapper] Session %s has no keyframes", session_id)
            return False

        vio_pts = []
        tag_pts = []
        for tag in sess_tags:
            closest_kf = min(sess_kfs, key=lambda kf: abs(kf.timestamp - tag.timestamp))
            vio_pts.append([closest_kf.map_x, closest_kf.map_z])
            tag_pts.append([tag.facility_x, tag.facility_y])
            logger.info(
                f"[Mapper] Sess {session_id} tag path="
                f"({closest_kf.map_x:.1f},{closest_kf.map_z:.1f}) "
                f"→ facility=({tag.facility_x:.1f},{tag.facility_y:.1f}) "
                f"dt={abs(closest_kf.timestamp - tag.timestamp):.2f}s"
            )

        src = np.array(vio_pts, dtype=np.float64)
        dst = np.array(tag_pts, dtype=np.float64)

        # keep spatially distinct anchors
        uniq = [0]
        for k in range(1, len(src)):
            if float(np.linalg.norm(src[k] - src[uniq[-1]])) > 0.8 and \
               float(np.linalg.norm(dst[k] - dst[uniq[-1]])) > 0.8:
                uniq.append(k)
        src_u = src[uniq]
        dst_u = dst[uniq]

        if len(src_u) == 1:
            R = np.eye(2, dtype=np.float64)
            t = dst_u[0] - src_u[0]
            segs = [(R, t, 1.0, src_u[0], src_u[0])]
            logger.info(
                f"[Mapper] Sess {session_id} single-tag translation: "
                f"path({src_u[0,0]:.1f},{src_u[0,1]:.1f}) → ({dst_u[0,0]:.1f},{dst_u[0,1]:.1f})"
            )
        else:
            segs = []
            for i in range(len(src_u) - 1):
                sim = self._seg_similarity(src_u[i], src_u[i + 1], dst_u[i], dst_u[i + 1])
                if sim is None:
                    continue
                R, t, scale = sim
                if not (0.05 < scale < 15.0):
                    logger.warning(f"[Mapper] Sess {session_id} seg {i} scale={scale:.2f} skipped")
                    continue
                segs.append((R, t, scale, src_u[i], src_u[i + 1]))
                logger.info(
                    f"[Mapper] Sess {session_id} seg {i}: scale={scale:.2f} "
                    f"path({src_u[i,0]:.1f},{src_u[i,1]:.1f})→({src_u[i+1,0]:.1f},{src_u[i+1,1]:.1f}) "
                    f"fac({dst_u[i,0]:.1f},{dst_u[i,1]:.1f})→({dst_u[i+1,0]:.1f},{dst_u[i+1,1]:.1f})"
                )
            if not segs:
                return False

        def nearest_seg(pt: np.ndarray) -> int:
            best_i, best_d = 0, 1e18
            for i, (_, _, _, a0, a1) in enumerate(segs):
                if np.allclose(a0, a1):
                    d = float(np.linalg.norm(pt - a0))
                else:
                    v = a1 - a0
                    u = float(np.clip(np.dot(pt - a0, v) / np.dot(v, v), 0.0, 1.0))
                    d = float(np.linalg.norm(pt - (a0 + u * v)))
                if d < best_d:
                    best_d, best_i = d, i
            return best_i

        # warp only this session's landmarks
        for lm in self.landmarks.values():
            if lm.session_id != session_id:
                continue
            h = float(lm.position[1])
            pt = np.array([lm.position[0], lm.position[2]], dtype=np.float64)
            R, t, _, _, _ = segs[nearest_seg(pt)]
            mapped = R @ pt + t
            lm.position[0] = float(mapped[0])
            lm.position[1] = float(mapped[1])
            lm.position[2] = h

        # drop outliers for this session only; keep other sessions intact
        fac_poly = dst_u
        keep = {}
        dropped = 0
        for lid, lm in self.landmarks.items():
            if lm.session_id != session_id:
                keep[lid] = lm
                continue
            p = np.array([lm.position[0], lm.position[1]], dtype=np.float64)
            dmin = 1e18
            for i in range(len(fac_poly) - 1):
                a0, a1 = fac_poly[i], fac_poly[i + 1]
                v = a1 - a0
                denom = float(np.dot(v, v)) or 1e-9
                u = float(np.clip(np.dot(p - a0, v) / denom, 0.0, 1.0))
                dmin = min(dmin, float(np.linalg.norm(p - (a0 + u * v))))
            if len(fac_poly) == 1:
                dmin = float(np.linalg.norm(p - fac_poly[0]))
            if dmin <= 8.0 and abs(float(lm.position[2])) <= 4.0:
                keep[lid] = lm
            else:
                dropped += 1
        self.landmarks = keep

        self._aligned_sessions.add(session_id)
        self._is_aligned = True
        n_kept = sum(1 for lm in self.landmarks.values() if lm.session_id == session_id)
        logger.info(
            f"[Mapper] Session {session_id} aligned across {len(segs)} segment(s); "
            f"kept {n_kept} pts (dropped {dropped} outliers). "
            f"Total map={len(self.landmarks)}."
        )
        return True

    def _reattach_orphan_tags(self, max_gap_s: float = 300.0) -> None:
        """A tag whose session has no keyframes can't anchor anything (e.g. dropped right
        after a restart, before the first keyframe opened the walk's session). Move it to
        the unaligned session of the keyframe nearest in time."""
        kf_sessions = {kf.session_id for kf in self.keyframes}
        for tag in self.user_tags:
            if tag.session_id in kf_sessions or tag.session_id in self._aligned_sessions:
                continue
            cands = [
                kf for kf in self.keyframes
                if kf.session_id not in self._aligned_sessions and kf.session_id != COMMITTED_SESSION
            ]
            if not cands:
                continue
            near = min(cands, key=lambda kf: abs(kf.timestamp - tag.timestamp))
            if abs(near.timestamp - tag.timestamp) <= max_gap_s:
                logger.info(
                    "[Mapper] Tag at (%.1f, %.1f) moved from empty session %s to session %s "
                    "(nearest keyframe %.0fs away)",
                    tag.facility_x, tag.facility_y, tag.session_id, near.session_id,
                    abs(near.timestamp - tag.timestamp),
                )
                tag.session_id = near.session_id

    def _unaligned_landmark_ids(self) -> set:
        """Landmarks of walks that were never aligned: still raw ARCore coordinates."""
        if not self._aligned_sessions:
            return set()  # nothing aligned at all → whole map is local VIO frame (legacy)
        ok = set(self._aligned_sessions) | {COMMITTED_SESSION}
        return {lid for lid, lm in self.landmarks.items() if lm.session_id not in ok}

    def finalize_map(self, output_ply_path: Optional[str] = None, site=None) -> Tuple[int, int]:
        """Align each unaligned session (2+ tags) into facility coordinates."""
        if site is not None:
            self.hydrate_from_disk(site)

        if not self.user_tags:
            logger.warning("No georeferenced tags provided. Map will remain in local VIO frame.")
            if output_ply_path and self.landmarks:
                self._export_to_ply(output_ply_path)
                self._export_map_db(Path(output_ply_path).parent, site=site)
            return len(self.keyframes), len(self.landmarks)

        if not self.keyframes and not self.landmarks:
            logger.warning("[Mapper] No keyframes or landmarks — cannot finalize.")
            return 0, 0

        self._reattach_orphan_tags()
        pending = sorted(
            {t.session_id for t in self.user_tags} - self._aligned_sessions - {COMMITTED_SESSION}
        )
        if not pending:
            logger.info(
                "[Mapper] All sessions already aligned (%s landmarks) — re-exporting.",
                len(self.landmarks),
            )
        else:
            for sid in pending:
                n_tags = sum(1 for t in self.user_tags if t.session_id == sid)
                n_lm = sum(1 for lm in self.landmarks.values() if lm.session_id == sid)
                if n_tags < 2:
                    logger.warning(
                        "[Mapper] Session %s skipped (%s tags, %s landmarks) — need 2+ tags",
                        sid, n_tags, n_lm,
                    )
                    continue
                if n_lm == 0:
                    logger.warning("[Mapper] Session %s has tags but no landmarks", sid)
                    continue
                ok = self._align_session(sid)
                if not ok:
                    logger.warning("[Mapper] Session %s align failed", sid)

        if output_ply_path:
            out_dir = Path(output_ply_path).parent
            if self.landmarks:
                self._export_to_ply(output_ply_path)
                self._export_map_db(out_dir, site=site)
            else:
                # never wipe an existing cloud just because this finalize had no new points
                logger.warning("[Mapper] No landmarks in memory — leaving on-disk point cloud untouched")
            tags_path = out_dir / "tags.json"
            tags_data = self.get_tags_3d()
            if tags_data:
                try:
                    with open(tags_path, "w", encoding="utf-8") as f:
                        json.dump(tags_data, f, indent=2)
                    logger.info(f"Exported {len(tags_data)} location tags to {tags_path}")
                except OSError as e:
                    logger.warning(f"Failed to export tags.json: {e}")
            
        return len(self.keyframes), len(self.landmarks)

    def get_tags_3d(self) -> List[Dict]:
        """Compute the 3D positions of all user tags in the point cloud coordinate system."""
        results = []
        for idx, tag in enumerate(self.user_tags):
            sess_kfs = [kf for kf in self.keyframes if kf.session_id == tag.session_id]
            closest_kf = (
                min(sess_kfs, key=lambda kf: abs(kf.timestamp - tag.timestamp))
                if sess_kfs else None
            )
            tag_aligned = tag.session_id in self._aligned_sessions

            if tag_aligned:
                # Facility frame: (x,y)=floor meters, z=height
                tag_x = float(tag.facility_x)
                tag_y = float(tag.facility_y)
                tag_z = 0.0
            else:
                # Local map path stored as PLY (x, height, z) = (map_x, 0, map_z)
                if closest_kf:
                    tag_x = float(closest_kf.map_x)
                    tag_y = 0.0
                    tag_z = float(closest_kf.map_z)
                else:
                    tag_x = float(tag.facility_x)
                    tag_y = 0.0
                    tag_z = 0.0
                    
            results.append({
                "id": idx + 1,
                "label": f"Tag #{idx + 1}",
                "type": "user_tag",
                "session_id": int(tag.session_id),
                "timestamp": tag.timestamp,
                "facility_x": float(tag.facility_x),
                "facility_y": float(tag.facility_y),
                "floor": str(tag.facility_floor),
                "aligned": bool(tag_aligned),
                "x": round(tag_x, 3),
                "y": round(tag_y, 3),
                "z": round(tag_z, 3)
            })
        return results

    def drop_landmarks(
        self,
        *,
        floor: Optional[str] = None,
        ids: Optional[set[int]] = None,
        x_min: Optional[float] = None,
        x_max: Optional[float] = None,
        y_min: Optional[float] = None,
        y_max: Optional[float] = None,
        z_min: Optional[float] = None,
        z_max: Optional[float] = None,
    ) -> int:
        """Remove matching landmarks from memory (AND of provided filters)."""
        from fmc.floors import normalize_floor_id

        floor_id = normalize_floor_id(floor) if floor is not None else None
        id_set = {int(i) for i in ids} if ids else None
        if (
            floor_id is None
            and id_set is None
            and x_min is None and x_max is None
            and y_min is None and y_max is None
            and z_min is None and z_max is None
        ):
            return 0

        drop: list[int] = []
        for lid, lm in self.landmarks.items():
            if id_set is not None and int(lid) not in id_set:
                continue
            if floor_id is not None and normalize_floor_id(getattr(lm, "floor", "1")) != floor_id:
                continue
            pt = lm.position
            if x_min is not None and float(pt[0]) < x_min:
                continue
            if x_max is not None and float(pt[0]) > x_max:
                continue
            if y_min is not None and float(pt[1]) < y_min:
                continue
            if y_max is not None and float(pt[1]) > y_max:
                continue
            if z_min is not None and float(pt[2]) < z_min:
                continue
            if z_max is not None and float(pt[2]) > z_max:
                continue
            drop.append(int(lid))
        for lid in drop:
            self.landmarks.pop(lid, None)
        return len(drop)

    def _export_to_ply(self, filepath: str) -> None:
        """Export the 3D landmarks to a standard Polygon File Format (.ply) point cloud with RGB color."""
        skip_ids = self._unaligned_landmark_ids()  # raw-ARCore walks don't belong in the map
        valid_landmarks = [
            lm for lm in self.landmarks.values()
            if lm.landmark_id not in skip_ids
            and not np.any(np.isnan(lm.position)) and not np.any(np.isinf(lm.position))
        ]
        
        if not valid_landmarks:
            logger.warning("No valid 3D points to export.")
            return
            
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            f.write("ply\n")
            f.write("format ascii 1.0\n")
            f.write(f"element vertex {len(valid_landmarks)}\n")
            f.write("property float x\n")
            f.write("property float y\n")
            f.write("property float z\n")
            f.write("property uchar red\n")
            f.write("property uchar green\n")
            f.write("property uchar blue\n")
            f.write("end_header\n")
            for lm in valid_landmarks:
                pt = lm.position
                c = getattr(lm, 'color', (52, 199, 89))
                f.write(f"{pt[0]:.4f} {pt[1]:.4f} {pt[2]:.4f} {int(c[0])} {int(c[1])} {int(c[2])}\n")
        
        logger.info(f"Exported {len(valid_landmarks)} colored 3D points to {filepath}")

    def _export_map_db(self, out_dir: Path, site=None) -> None:
        """Export 3D landmarks + ORB descriptors into H2GIS (primary) and NPZ fallback.

        Merges with any on-disk landmarks not already in memory so a new session
        cannot shrink the feature DB.
        """
        if site is not None:
            self.hydrate_from_disk(site)

        # unaligned walks are raw ARCore coords — never export them as map points
        # (kept in memory: a later finalize with 2+ tags can still align them)
        skip_ids = self._unaligned_landmark_ids()
        if skip_ids:
            logger.warning(
                "[Mapper] Not exporting %s landmarks from unaligned session(s) — drop 2+ tags "
                "in that walk and Finalize again", len(skip_ids),
            )
        by_id: Dict[int, Landmark3D] = {}
        for lm in self.landmarks.values():
            if lm.descriptor is None or lm.landmark_id in skip_ids:
                continue
            if np.any(np.isnan(lm.position)) or np.any(np.isinf(lm.position)):
                continue
            by_id[lm.landmark_id] = lm

        # belt-and-suspenders: pull any disk ids still missing after hydrate
        try:
            from fmc.config import load_site_config
            from fmc.storage.h2gis_store import load_landmarks

            site_obj = site
            if site_obj is None:
                site_obj = load_site_config(out_dir.parent.name)
            loaded = load_landmarks(site_obj)
            if loaded is not None:
                pos, desc, lids, fls = loaded
                for i in range(len(lids)):
                    lid = int(lids[i])
                    if lid in by_id or lid in skip_ids:
                        continue
                    by_id[lid] = Landmark3D(
                        landmark_id=lid,
                        position=pos[i].astype(np.float64).copy(),
                        observations={},
                        descriptor=desc[i].astype(np.uint8).copy(),
                        session_id=COMMITTED_SESSION,
                        floor=str(fls[i]),
                    )
                    # keep in memory too
                    if lid not in self.landmarks:
                        self.landmarks[lid] = by_id[lid]
        except Exception as e:
            logger.warning("[Mapper] merge-from-H2GIS before export failed: %s", e)

        from fmc.floors import coerce_floor_array, normalize_floor_id

        npz_path = out_dir / "map_landmarks.npz"
        if npz_path.exists():
            try:
                data = np.load(npz_path, allow_pickle=True)
                pos, desc, lids = data["positions"], data["descriptors"], data["ids"]
                cols = data["colors"] if "colors" in data.files else None
                fls = data["floors"] if "floors" in data.files else None
                fl_labels = coerce_floor_array(fls, n=len(lids)) if fls is not None else ["1"] * len(lids)
                for i in range(len(lids)):
                    lid = int(lids[i])
                    if lid in by_id or lid in skip_ids:
                        continue
                    col = tuple(int(c) for c in cols[i][:3]) if cols is not None else (52, 199, 89)
                    lm = Landmark3D(
                        landmark_id=lid,
                        position=pos[i].astype(np.float64).copy(),
                        observations={},
                        color=col,
                        descriptor=desc[i].astype(np.uint8).copy(),
                        session_id=COMMITTED_SESSION,
                        floor=normalize_floor_id(fl_labels[i]),
                    )
                    by_id[lid] = lm
                    if lid not in self.landmarks:
                        self.landmarks[lid] = lm
            except OSError as e:
                logger.warning("[Mapper] merge-from-NPZ before export failed: %s", e)

        # infer floor from session tags when still default
        sess_floor: Dict[int, str] = {}
        for tag in self.user_tags:
            sess_floor[tag.session_id] = normalize_floor_id(tag.facility_floor)
        for lm in by_id.values():
            if normalize_floor_id(getattr(lm, "floor", "1")) == "1" and lm.session_id in sess_floor:
                lm.floor = sess_floor[lm.session_id]

        rows = list(by_id.values())
        if not rows:
            logger.warning("[Mapper] No landmarks with descriptors — skip map DB export")
            return

        positions = np.stack([lm.position.astype(np.float32) for lm in rows], axis=0)
        descriptors = np.stack([lm.descriptor.astype(np.uint8) for lm in rows], axis=0)
        ids = np.array([lm.landmark_id for lm in rows], dtype=np.int32)
        colors = np.array([lm.color for lm in rows], dtype=np.uint8)
        floors = np.array(
            [normalize_floor_id(getattr(lm, "floor", "1")) for lm in rows],
            dtype=object,
        )
        aligned = bool(self._aligned_sessions) or bool(getattr(self, "_is_aligned", False))

        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            from fmc.config import load_site_config
            from fmc.storage.h2gis_store import replace_landmarks

            site_obj = site if site is not None else load_site_config(out_dir.parent.name)
            replace_landmarks(
                site_obj, positions, descriptors, ids, colors,
                floors=floors, aligned=aligned,
            )
        except Exception as e:
            logger.warning("[Mapper] H2GIS export failed: %s", e)

        np.savez_compressed(
            npz_path,
            positions=positions,
            descriptors=descriptors,
            ids=ids,
            colors=colors,
            floors=floors,
            aligned=np.array([aligned]),
            frame="facility_xy_height",
        )
        # per-floor archives for partitioned PnP loaders (safe filename from label)
        import re
        for fid in sorted(set(str(f) for f in floors.tolist())):
            mask = np.array([str(f) == fid for f in floors.tolist()])
            if not np.any(mask):
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fid)
            np.savez_compressed(
                out_dir / f"map_landmarks_f{safe}.npz",
                positions=positions[mask],
                descriptors=descriptors[mask],
                ids=ids[mask],
                colors=colors[mask],
                floors=floors[mask],
                aligned=np.array([aligned]),
                frame="facility_xy_height",
            )
        logger.info(
            f"[Mapper] Exported 2D-to-3D map DB: {len(rows)} landmarks "
            f"(H2GIS + {npz_path.name}; floors={sorted(set(str(f) for f in floors.tolist()))})"
        )
