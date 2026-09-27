"""Floor-plan pixel -> real-world-meters coordinate transform.

Fits a full 2D affine transform (independent x/y scale, rotation, shear,
and reflection) from a small set of control points where BOTH the pixel
location on a rendered floor-plan image AND the real-world (x, y) coordinate
(per docs/01_coordinate_system.md) are known. Needs >=3 non-collinear points;
more points give a more robust least-squares fit and let you sanity-check
quality via residuals.

Affine (not just similarity/rotation+scale) is used deliberately: a raster
render of a PDF plan commonly has image y increasing downward while the
project's world y increases "away from entrance" -- a reflection, which a
pure rotation+scale transform cannot represent. Affine handles that for free.

Typical control points: pick two ends of a dimensioned wall/corridor on the
drawing as pixel coordinates, and assign real-world coordinates to them
based on the marked dimension -- e.g. if a 10m corridor is dimensioned on
the drawing and you're using one end as your coordinate-system origin,
control point A = (pixel of one end, real (0, 0)), control point B =
(pixel of other end, real (10, 0)). A third point off that line resolves
the y-axis direction/scale.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class ControlPoint:
    px: float
    py: float
    x: float
    y: float


@dataclass
class FloorPlanTransform:
    a: float
    b: float
    c: float
    d: float
    tx: float
    ty: float

    def pixel_to_world(self, px: float, py: float) -> tuple[float, float]:
        x = self.a * px + self.b * py + self.tx
        y = self.c * px + self.d * py + self.ty
        return float(x), float(y)

    def world_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """Inverse of pixel_to_world -- world meters back to floor-plan pixel
        coordinates. Used to overlay already-saved geometry (segments,
        vehicle slots, ingested capture locations -- all stored in world
        coordinates) back onto the rendered floor-plan image, e.g. in the
        web editing tool (fmc/webtools)."""
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            raise ValueError("Transform is not invertible (degenerate fit)")
        dx, dy = x - self.tx, y - self.ty
        px = (self.d * dx - self.b * dy) / det
        py = (-self.c * dx + self.a * dy) / det
        return float(px), float(py)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"a": self.a, "b": self.b, "c": self.c, "d": self.d, "tx": self.tx, "ty": self.ty},
                f, indent=2,
            )

    @classmethod
    def load(cls, path: Path) -> "FloorPlanTransform":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


def fit_transform(control_points: list[ControlPoint]) -> FloorPlanTransform:
    """Least-squares fit of a full affine transform from >=3 control points."""
    if len(control_points) < 3:
        raise ValueError("Need at least 3 non-collinear control points to fit an affine transform")

    design = np.array([[cp.px, cp.py, 1.0] for cp in control_points])
    target_x = np.array([cp.x for cp in control_points])
    target_y = np.array([cp.y for cp in control_points])

    (a, b, tx), *_ = np.linalg.lstsq(design, target_x, rcond=None)
    (c, d, ty), *_ = np.linalg.lstsq(design, target_y, rcond=None)

    return FloorPlanTransform(a=float(a), b=float(b), c=float(c), d=float(d), tx=float(tx), ty=float(ty))


def transform_residuals(transform: FloorPlanTransform, control_points: list[ControlPoint]) -> list[float]:
    """Distance (meters) between each control point's known real coordinate
    and what the fitted transform predicts for its pixel coordinate --
    a fit-quality check to run before trusting the transform.

    IMPORTANT: with exactly 3 control points (the minimum), the fit is
    exact and residuals will be ~0 regardless of whether the input units
    were correct -- e.g. entering millimeters instead of meters produces
    a transform that is wrong by a constant factor, but still fits those
    3 points perfectly. Residuals only catch geometric/positional mistakes
    (a mis-clicked point), not unit mistakes. Use implied_scale() as a
    separate check, and prefer 4+ control points when possible so a unit
    or measurement error has a chance to show up as a residual too."""
    residuals = []
    for cp in control_points:
        px, py = transform.pixel_to_world(cp.px, cp.py)
        residuals.append(float(np.hypot(px - cp.x, py - cp.y)))
    return residuals


def implied_scale(transform: FloorPlanTransform) -> tuple[float, float]:
    """Real-world meters represented by one pixel step, along each pixel
    axis (x, y). A sanity check independent of residuals -- see the note
    on transform_residuals() for why residuals alone can't catch a units
    mistake. Compare the returned values to what you'd expect from your
    drawing's rendered DPI and scale; wildly different x/y values, or a
    value far outside roughly 0.001-2 m/pixel for a typical floor plan,
    usually means a mis-entered real_x/real_y control point."""
    scale_x = math.hypot(transform.a, transform.c)
    scale_y = math.hypot(transform.b, transform.d)
    return scale_x, scale_y


def load_control_points_csv(path: Path) -> list[ControlPoint]:
    """Load control points from the same CSV format
    scripts/pick_floorplan_points.py + fit_floorplan_transform.py use
    (columns: pixel_x, pixel_y, real_x, real_y; an index column is ignored
    if present). Returns [] if the file doesn't exist. utf-8-sig handles a
    leading BOM transparently (common after editing the CSV in Excel/
    LibreOffice), same as a plain utf-8 file if no BOM is present."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [
            ControlPoint(px=float(row["pixel_x"]), py=float(row["pixel_y"]), x=float(row["real_x"]), y=float(row["real_y"]))
            for row in csv.DictReader(f)
        ]


def save_control_points_csv(path: Path, control_points: list[ControlPoint]) -> None:
    """Write control points in the same CSV format the CLI tools read/write,
    so files created or edited by the web tool and the CLI scripts are
    fully interchangeable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "pixel_x", "pixel_y", "real_x", "real_y"])
        for i, cp in enumerate(control_points):
            writer.writerow([i, cp.px, cp.py, cp.x, cp.y])
