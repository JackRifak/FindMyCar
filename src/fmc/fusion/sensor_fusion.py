"""Combine VPR global fixes with VIO continuous tracking into one position.

Implements the VPR -> VIO -> VPR -> VIO relocalisation loop from the brief
(Section 8): VIO tracks continuously; whenever a fresh VPR match arrives,
it resets/corrects the VIO estimate rather than being blended with it
(a hard reset is the simplest correct behavior for a first version —
revisit with a proper filter (EKF/particle filter) once real drift
characteristics from a real VIO tracker are known).
"""
from __future__ import annotations

from dataclasses import dataclass

from fmc.vio.tracker import Pose, VIOTracker
from fmc.vpr.pipeline import VPRResult


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

    def last_position(self) -> tuple[int, float, float] | None:
        """Most recent known (floor, x, y), or None if no fix yet.
        Used by the navigation/routing layer, which needs a starting point."""
        if self._last_x is None or self._last_y is None:
            return None
        return self.floor, self._last_x, self._last_y

    def on_vpr_result(self, vpr_result: VPRResult) -> FusedPosition | None:
        if not vpr_result.matched or vpr_result.record is None:
            return None
        self.floor = vpr_result.record.floor
        self.vio_tracker.reset(
            x=vpr_result.record.x,
            y=vpr_result.record.y,
            heading=float(vpr_result.record.orientation),
        )
        self._last_confidence = vpr_result.inlier_ratio
        self._last_x, self._last_y = vpr_result.record.x, vpr_result.record.y
        return FusedPosition(
            floor=self.floor,
            x=vpr_result.record.x,
            y=vpr_result.record.y,
            heading=float(vpr_result.record.orientation),
            confidence=self._last_confidence,
            tracking_status="tracking",
        )

    def on_vio_pose(self, pose: Pose) -> FusedPosition:
        # Confidence decays between VPR fixes since VIO alone accumulates drift.
        self._last_confidence = max(self._last_confidence * 0.98, 0.1)
        self._last_x, self._last_y = pose.x, pose.y
        return FusedPosition(
            floor=self.floor,
            x=pose.x,
            y=pose.y,
            heading=pose.heading,
            confidence=self._last_confidence,
            tracking_status=pose.tracking_status,
        )
