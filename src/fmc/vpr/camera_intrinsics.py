"""Pinhole camera matrix for mapping + PnP.

Clients send the real intrinsics (ARCore getImageIntrinsics / WebXR projection) for the
exact image they upload. Without them we fall back to a guessed ~61° HFOV, which biases
triangulated depth and PnP position along the viewing direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# f = 0.85 * long side ≈ 61° horizontal FOV — typical phone main camera
GUESS_F_RATIO = 0.85


@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    # image size these intrinsics belong to (pixels)
    width: int
    height: int


def parse_intrinsics(
    fx: float | None,
    fy: float | None,
    cx: float | None,
    cy: float | None,
    width: float | None,
    height: float | None,
) -> Intrinsics | None:
    """Validate client-sent intrinsics; None if missing or implausible."""
    vals = (fx, fy, cx, cy, width, height)
    if any(v is None for v in vals):
        return None
    try:
        fx, fy, cx, cy = float(fx), float(fy), float(cx), float(cy)
        w, h = int(round(float(width))), int(round(float(height)))
    except (TypeError, ValueError):
        return None
    if w < 16 or h < 16 or fx <= 0 or fy <= 0:
        return None
    long_side = max(w, h)
    if not (0.25 * long_side < fx < 3.0 * long_side and 0.25 * long_side < fy < 3.0 * long_side):
        logger.warning("[Intrinsics] rejected implausible focal fx=%.1f fy=%.1f for %dx%d", fx, fy, w, h)
        return None
    if not (0 < cx < w and 0 < cy < h):
        logger.warning("[Intrinsics] rejected principal point (%.1f, %.1f) for %dx%d", cx, cy, w, h)
        return None
    return Intrinsics(fx=fx, fy=fy, cx=cx, cy=cy, width=w, height=h)


def camera_matrix(width: int, height: int, intr: Intrinsics | None = None) -> np.ndarray:
    """3x3 K for a width x height image — real intrinsics (rescaled) when given, else a guess."""
    if intr is not None:
        sx = width / float(intr.width)
        sy = height / float(intr.height)
        # uniform rescale only; a different aspect means a crop we can't model
        if abs(sx - sy) < 0.02:
            return np.array(
                [[intr.fx * sx, 0.0, intr.cx * sx], [0.0, intr.fy * sy, intr.cy * sy], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
        logger.warning(
            "[Intrinsics] size mismatch: intrinsics %dx%d vs image %dx%d — using guess",
            intr.width, intr.height, width, height,
        )
    f = max(width, height) * GUESS_F_RATIO
    return np.array(
        [[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
