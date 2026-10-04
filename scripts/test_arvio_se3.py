"""Smoke checks for 6-DOF AR/VIO wiring (no ARCore device required)."""
from __future__ import annotations

import numpy as np

from fmc.fusion.sensor_fusion import PositionFuser
from fmc.mapping.continuous_mapper import ContinuousMapper
from fmc.vio.tracker import SixDofPose, TrueVIOTracker


def test_true_vio_lock():
    t = TrueVIOTracker()
    t.update_from_6dof(SixDofPose(0, 0.0, 1.5, 0.0, 1, 0, 0, 0, "tracking"))
    t.reset(10.0, 20.0, 90.0, z=1.5)
    out = t.update_from_6dof(SixDofPose(1, 1.0, 1.5, 0.0, 1, 0, 0, 0, "tracking"))
    # +1m in VIO +X with 90° lock → +1m facility +Y
    assert abs(out.x - 10.0) < 1e-6
    assert abs(out.y - 21.0) < 1e-6
    assert abs(out.z - 1.5) < 1e-6


def test_mapper_6dof_helpers():
    m = ContinuousMapper()
    identity = SixDofPose(0, 0, 0, 0, 1, 0, 0, 0, "tracking")
    real = SixDofPose(0, 0, 1.2, 0, 0.9, 0.1, 0.0, 0.1, "tracking")
    assert not m._pose_has_6dof(identity)
    assert m._pose_has_6dof(real)
    R = m._R_wc_from_pose(real, 0.0)
    assert R.shape == (3, 3)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-5)


def test_fuser_live_marker():
    f = PositionFuser(TrueVIOTracker(), floor=1)
    tracker = f.vio_tracker
    assert isinstance(tracker, TrueVIOTracker)
    tracker.update_from_6dof(SixDofPose(0, 0, 1.4, 0, 1, 0, 0, 0, "tracking"))
    tracker.reset(5.0, 6.0, 0.0, z=1.4)
    f._last_x, f._last_y = 5.0, 6.0
    f.mark_live()
    fused = f.on_six_dof(SixDofPose(1, 0.2, 1.4, 0.5, 1, 0, 0, 0, "tracking"))
    assert f.is_live()
    assert fused.tracking_status == "tracking"
    f.end_live()
    assert not f.is_live()


if __name__ == "__main__":
    test_true_vio_lock()
    test_mapper_6dof_helpers()
    test_fuser_live_marker()
    print("test_arvio_se3: OK")
