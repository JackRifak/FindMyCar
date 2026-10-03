"""Combine VPR global fixes with VIO continuous tracking into one position.

Implements the VPR -> VIO -> VPR -> VIO relocalisation loop from the brief
(Section 8): VIO tracks continuously; whenever a fresh VPR match arrives,
it resets/corrects the VIO estimate rather than being blended with it
(a hard reset is the simplest correct behavior for a first version —
revisit with a proper filter (EKF/particle filter) once real drift
characteristics from a real VIO tracker are known).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from fmc.vio.tracker import Pose, VIOTracker
from fmc.vpr.pipeline import VPRResult

# drop stale 3D markers if the client never calls end (tab crash, etc.)
LIVE_POSE_TTL_S = 300.0


@dataclass
class FusedPosition:
    floor: int
    x: float
    y: float
    heading: float
    confidence: float
    tracking_status: str


class PositionFuser:
    def __init__(self, vio_tracker: VIOTracker, floor: int):
        self.vio_tracker = vio_tracker
        self.floor = floor
        self._last_confidence = 0.0
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_heading: float = 0.0
        self._last_z: float | None = None
        self._last_method: str = "none"
        # 3D viewer marker — only while an active localization session is live
        self._live_marker: bool = False
        self._live_ts: float = 0.0

    def last_position(self) -> tuple[int, float, float] | None:
        """Most recent known (floor, x, y), or None if no fix yet.
        Used by the navigation/routing layer, which needs a starting point."""
        if self._last_x is None or self._last_y is None:
            return None
        return self.floor, self._last_x, self._last_y

    def mark_live(self) -> None:
        """Show this device in the 3D live-pose overlay."""
        self._live_marker = True
        self._live_ts = time.time()

    def end_live(self) -> None:
        """Hide 3D marker when the client localization session ends."""
        self._live_marker = False
        self._live_ts = 0.0

    def is_live(self, ttl_s: float = LIVE_POSE_TTL_S) -> bool:
        if not self._live_marker or self._last_x is None:
            return False
        if self._live_ts <= 0:
            return False
        return (time.time() - self._live_ts) <= ttl_s

    def on_vpr_result(self, vpr_result: VPRResult) -> FusedPosition | None:
        if not vpr_result.matched:
            return None

        # prefer 6-DOF PnP pose when available
        z = None
        if vpr_result.pose_6dof is not None:
            pose = vpr_result.pose_6dof
            x, y, heading = pose.x, pose.y, pose.heading
            conf = pose.confidence
            floor = vpr_result.record.floor if vpr_result.record else self.floor
            z = float(pose.z)
        elif vpr_result.record is not None:
            x = vpr_result.record.x
            y = vpr_result.record.y
            heading = float(vpr_result.record.orientation)
            conf = vpr_result.inlier_ratio
            floor = vpr_result.record.floor
        else:
            return None

        self.floor = floor
        self.vio_tracker.reset(x=x, y=y, heading=heading)
        self._last_confidence = conf
        self._last_x, self._last_y = x, y
        self._last_heading = float(heading)
        self._last_z = z
        self._last_method = getattr(vpr_result, "method", None) or "image_vpr"
        self.mark_live()
        return FusedPosition(
            floor=self.floor,
            x=x,
            y=y,
            heading=heading,
            confidence=self._last_confidence,
            tracking_status="tracking",
        )

    def on_vio_pose(self, pose: Pose) -> FusedPosition:
        # Confidence decays between VPR fixes since VIO alone accumulates drift.
        self._last_confidence = max(self._last_confidence * 0.98, 0.1)
        self._last_x, self._last_y = pose.x, pose.y
        self._last_heading = float(pose.heading)
        return FusedPosition(
            floor=self.floor,
            x=pose.x,
            y=pose.y,
            heading=pose.heading,
            confidence=self._last_confidence,
            tracking_status=pose.tracking_status,
        )


class EKFPositionFuser(PositionFuser):
    """An Extended Kalman Filter (EKF) inspired fuser.
    
    Instead of hard-resetting the VIO tracker on every VPR match, this fuser
    smoothly blends the VIO prediction with the VPR measurement using an error-state
    or alpha-beta approach, avoiding jarring teleportation jumps in the UI.
    """
    
    def __init__(self, vio_tracker: VIOTracker, floor: int):
        super().__init__(vio_tracker, floor)
        # Process noise covariance (VIO drift uncertainty)
        self.P = 1.0  # initial variance
        self.Q = 0.05 # drift added per VIO update
        
    def on_vpr_result(self, vpr_result: VPRResult) -> FusedPosition | None:
        if not vpr_result.matched:
            return None

        if vpr_result.pose_6dof is not None:
            z_x, z_y = vpr_result.pose_6dof.x, vpr_result.pose_6dof.y
            z_heading = vpr_result.pose_6dof.heading
            floor = vpr_result.record.floor if vpr_result.record else self.floor
        elif vpr_result.record is not None:
            z_x, z_y = vpr_result.record.x, vpr_result.record.y
            z_heading = float(vpr_result.record.orientation)
            floor = vpr_result.record.floor
        else:
            return None

        self.floor = floor
        
        if self._last_x is None:
            # First initialization, do a hard reset
            super().on_vpr_result(vpr_result)
            self.P = 0.5
            return self._build_fused(z_x, z_y, z_heading, "tracking")
            
        # VPR / PnP measurement
        
        # Measurement uncertainty based on inlier ratio (lower ratio -> higher uncertainty)
        # Cap ratio between 0.1 and 1.0 to avoid divide by zero
        inlier_ratio = max(0.1, min(1.0, vpr_result.inlier_ratio))
        R = 2.0 / inlier_ratio  
        
        # Kalman Gain
        K = self.P / (self.P + R)
        
        # Update State
        new_x = self._last_x + K * (z_x - self._last_x)
        new_y = self._last_y + K * (z_y - self._last_y)
        
        # Heading interpolation (shortest path)
        import math
        diff = (z_heading - self.vio_tracker._facility_heading + 180) % 360 - 180
        new_heading = (self.vio_tracker._facility_heading + K * diff) % 360
        
        # Update uncertainty
        self.P = (1 - K) * self.P
        
        # We push the smoothed state back into the VIO tracker so it acts as the new baseline
        self.vio_tracker.reset(x=new_x, y=new_y, heading=new_heading)
        
        self._last_confidence = max(self._last_confidence, vpr_result.inlier_ratio)
        self._last_x = new_x
        self._last_y = new_y
        self._last_heading = float(new_heading)
        if vpr_result.pose_6dof is not None:
            self._last_z = float(vpr_result.pose_6dof.z)
        self._last_method = getattr(vpr_result, "method", None) or "image_vpr"
        self.mark_live()
        
        return self._build_fused(new_x, new_y, new_heading, "tracking")

    def on_vio_pose(self, pose: Pose) -> FusedPosition:
        # Increase uncertainty as VIO drifts
        self.P += self.Q
        self._last_confidence = max(self._last_confidence * 0.98, 0.1)
        self._last_x = pose.x
        self._last_y = pose.y
        self._last_heading = float(pose.heading)
        return self._build_fused(pose.x, pose.y, pose.heading, pose.tracking_status)
        
    def _build_fused(self, x, y, heading, status):
        return FusedPosition(
            floor=self.floor,
            x=x,
            y=y,
            heading=heading,
            confidence=self._last_confidence,
            tracking_status=status
        )
