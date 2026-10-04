"""VIO tracker interface + ARCore/6-DOF integration.

DeadReckoningStub remains for browser-only PDR. TrueVIOTracker consumes
client ARCore SixDofPose streams and locks them into the facility frame
after each VPR/PnP fix (SE(3) yaw+translation align on the floor plane,
preserving VIO height).
"""
from __future__ import annotations

import math
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
    z: float = 0.0  # height (meters)


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


def _yaw_from_quat(qw: float, qx: float, qy: float, qz: float) -> float:
    """Yaw degrees from quaternion (Y-up)."""
    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    return math.degrees(math.atan2(siny_cosp, cosy_cosp)) % 360.0


class TrueVIOTracker(VIOTracker):
    """ARCore/6-DOF VIO → facility frame.

    After each VPR/PnP fix, stores an SE(2)-on-floor offset (rotation+translation)
    that maps the local VIO XZ path into facility (x,y). Height passes through
    from VIO Y with a vertical offset locked at the last fix.
    """

    def __init__(self):
        self._facility_x = 0.0
        self._facility_y = 0.0
        self._facility_z = 0.0
        self._facility_heading = 0.0

        self._offset_x = 0.0
        self._offset_y = 0.0
        self._offset_z = 0.0
        self._offset_heading = 0.0
        self._aligned = False

        self._last_vio_x = 0.0
        self._last_vio_y = 0.0
        self._last_vio_z = 0.0
        self._last_vio_heading = 0.0

        self._tracking_status = "initializing"

    def reset(self, x: float, y: float, heading: float, z: float | None = None) -> None:
        """Called when a VPR/PnP match provides a new absolute facility position."""
        self._facility_x = float(x)
        self._facility_y = float(y)
        self._facility_heading = float(heading) % 360.0
        if z is not None:
            self._facility_z = float(z)

        # SE(2) offset: rotate VIO floor (x,z) → facility (x,y)
        # VIO local: x≈east, z≈north (after client Z flip); facility: x east, y north
        self._offset_heading = (self._facility_heading - self._last_vio_heading) % 360.0
        rad = math.radians(self._offset_heading)
        c, s = math.cos(rad), math.sin(rad)
        # rotated last VIO floor point
        rx = self._last_vio_x * c - self._last_vio_z * s
        ry = self._last_vio_x * s + self._last_vio_z * c
        self._offset_x = self._facility_x - rx
        self._offset_y = self._facility_y - ry
        self._offset_z = self._facility_z - self._last_vio_y
        self._aligned = True

    def update(self, frame, imu_sample: ImuSample) -> Pose:
        """IMU-only fallback — hold last facility pose."""
        return Pose(
            self._facility_x,
            self._facility_y,
            self._facility_heading,
            0.0,
            self._tracking_status,
            z=self._facility_z,
        )

    def update_from_6dof(self, pose: SixDofPose) -> Pose:
        """Process a 6-DOF pose from the VIO engine (ARCore)."""
        self._tracking_status = pose.tracking_status or "tracking"

        self._last_vio_x = float(pose.x)
        self._last_vio_y = float(pose.y)
        self._last_vio_z = float(pose.z)
        self._last_vio_heading = _yaw_from_quat(pose.qw, pose.qx, pose.qy, pose.qz)

        if not self._aligned:
            # before first VPR fix — report local VIO as-is (not facility)
            self._facility_x = self._last_vio_x
            self._facility_y = self._last_vio_z  # map VIO z → facility y
            self._facility_z = self._last_vio_y
            self._facility_heading = self._last_vio_heading
            return Pose(
                x=self._facility_x,
                y=self._facility_y,
                heading=self._facility_heading,
                velocity=0.0,
                tracking_status=self._tracking_status,
                z=self._facility_z,
            )

        rad = math.radians(self._offset_heading)
        c, s = math.cos(rad), math.sin(rad)
        rx = self._last_vio_x * c - self._last_vio_z * s
        ry = self._last_vio_x * s + self._last_vio_z * c
        self._facility_x = self._offset_x + rx
        self._facility_y = self._offset_y + ry
        self._facility_z = self._offset_z + self._last_vio_y
        self._facility_heading = (self._last_vio_heading + self._offset_heading) % 360.0

        return Pose(
            x=self._facility_x,
            y=self._facility_y,
            heading=self._facility_heading,
            velocity=0.0,
            tracking_status=self._tracking_status,
            z=self._facility_z,
        )
