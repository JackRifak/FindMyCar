"""VIO tracker interface.

INTENTIONALLY NOT IMPLEMENTED YET. Per the project brief (Section 7) and
development order (docs), VIO framework selection is Deliverable 5 —
evaluating OpenVINS, VINS-Fusion, ORB-SLAM3, ARCore, ARKit, and web-compatible
alternatives against smartphone/browser compatibility, accuracy, real-time
performance, licensing, and on-device vs. backend processing.

This module defines the interface the rest of the system (sensor fusion,
position API) will call, so that work can proceed against a stable contract
while the framework evaluation happens independently. A DeadReckoningStub
is provided only so the fusion/map-matching/API layers have something to
run against on the mock site — it is NOT a real VIO implementation and its
drift characteristics are not representative of any real tracker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ImuSample:
    timestamp: float
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]


@dataclass
class Pose:
    x: float
    y: float
    heading: float  # degrees, per docs/01_coordinate_system.md convention
    velocity: float  # m/s
    tracking_status: str  # "tracking" | "lost" | "initializing"


class VIOTracker(ABC):
    @abstractmethod
    def reset(self, x: float, y: float, heading: float) -> None:
        """Re-initialize tracking at a known position (called after VPR relocalisation)."""
        raise NotImplementedError

    @abstractmethod
    def update(self, frame, imu_sample: ImuSample) -> Pose:
        """Feed one camera frame + IMU sample, return the updated pose estimate."""
        raise NotImplementedError


class DeadReckoningStub(VIOTracker):
    """Minimal placeholder: integrates IMU accel/gyro naively. NOT a real VIO
    implementation — no visual tracking, no drift correction. Exists only to
    let fusion/map-matching/API code run end-to-end on the mock site before
    Deliverable 5 selects and integrates a real VIO/SLAM framework."""

    def __init__(self):
        self._x = 0.0
        self._y = 0.0
        self._heading = 0.0
        self._last_ts: float | None = None

    def reset(self, x: float, y: float, heading: float) -> None:
        self._x, self._y, self._heading = x, y, heading
        self._last_ts = None

    def update(self, frame, imu_sample: ImuSample) -> Pose:
        if self._last_ts is not None:
            dt = max(imu_sample.timestamp - self._last_ts, 0.0)
            ax, ay, _ = imu_sample.accel
            # Extremely crude planar integration — placeholder only.
            self._x += ax * dt
            self._y += ay * dt
            self._heading = (self._heading + imu_sample.gyro[2] * dt) % 360
        self._last_ts = imu_sample.timestamp
        return Pose(x=self._x, y=self._y, heading=self._heading, velocity=0.0, tracking_status="tracking")
