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
class SixDofPose:
    """A 6-DOF pose typically provided by a native VIO system (ARCore/ARKit/WebXR)."""
    timestamp: float
    x: float
    y: float
    z: float
    # Quaternions
    qw: float
    qx: float
    qy: float
    qz: float
    tracking_status: str


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


class TrueVIOTracker(VIOTracker):
    """Integrates real 6-DOF VIO poses (e.g., from ARCore/ARKit via WebXR or Native Bridge).
    Handles aligning the local VIO coordinate frame to the global facility frame."""
    
    def __init__(self):
        self._facility_x = 0.0
        self._facility_y = 0.0
        self._facility_heading = 0.0
        
        # We need to store the offset between the VIO local frame and facility frame
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._offset_heading = 0.0
        
        self._last_vio_x = 0.0
        self._last_vio_y = 0.0
        self._last_vio_heading = 0.0
        
        self._tracking_status = "initializing"

    def reset(self, x: float, y: float, heading: float) -> None:
        """Called when a VPR match provides a new absolute facility position."""
        self._facility_x = x
        self._facility_y = y
        self._facility_heading = heading
        
        # Calculate the new offset relative to the most recent VIO pose
        self._offset_x = self._facility_x - self._last_vio_x
        self._offset_y = self._facility_y - self._last_vio_y
        self._offset_heading = (self._facility_heading - self._last_vio_heading) % 360

    def update(self, frame, imu_sample: ImuSample) -> Pose:
        """Fallback for IMU-only updates if needed, but TrueVIO typically expects 6DOF."""
        return Pose(self._facility_x, self._facility_y, self._facility_heading, 0.0, self._tracking_status)

    def update_from_6dof(self, pose: SixDofPose) -> Pose:
        """Process a 6-DOF pose from the VIO engine."""
        import math
        
        self._tracking_status = pose.tracking_status
        
        # Extract yaw from quaternion (simplified for 2D floor plan tracking)
        # yaw = atan2(2*(qw*qz + qx*qy), 1 - 2*(qy^2 + qz^2))
        siny_cosp = 2 * (pose.qw * pose.qz + pose.qx * pose.qy)
        cosy_cosp = 1 - 2 * (pose.qy * pose.qy + pose.qz * pose.qz)
        yaw_rad = math.atan2(siny_cosp, cosy_cosp)
        
        # Local VIO 2D translation and heading
        self._last_vio_x = pose.x
        self._last_vio_y = pose.y
        self._last_vio_heading = math.degrees(yaw_rad) % 360
        
        # Apply transformation to facility frame
        # (This is a simplified pure-translation+rotation 2D mapping)
        # Ideally, this applies a full SE(2) transformation.
        dx = self._last_vio_x
        dy = self._last_vio_y
        
        # Rotate by offset heading and translate
        rad = math.radians(self._offset_heading)
        cos_rad = math.cos(rad)
        sin_rad = math.sin(rad)
        
        self._facility_x = self._offset_x + (dx * cos_rad - dy * sin_rad)
        self._facility_y = self._offset_y + (dx * sin_rad + dy * cos_rad)
        self._facility_heading = (self._last_vio_heading + self._offset_heading) % 360
        
        return Pose(
            x=self._facility_x,
            y=self._facility_y,
            heading=self._facility_heading,
            velocity=0.0,  # Could differentiate poses over time
            tracking_status=self._tracking_status
        )
