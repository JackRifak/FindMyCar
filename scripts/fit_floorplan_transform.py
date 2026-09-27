"""Fit a floor-plan pixel -> real-world-meters transform from control points
and save it for reuse by compute_location_coords.py.

Input CSV must have columns: pixel_x, pixel_y, real_x, real_y (at least 3
non-collinear points -- see docs/04_survey_ingestion_workflow.md for how to
derive real_x/real_y from a dimensioned drawing).

Run:
    python scripts/fit_floorplan_transform.py data/mock_site/floorplan/control_points.csv --out data/mock_site/floorplan/transform.json
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from fmc.georeference import ControlPoint, fit_transform, implied_scale, transform_residuals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("control_points_csv", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    with open(args.control_points_csv, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    control_points = [
        ControlPoint(px=float(r["pixel_x"]), py=float(r["pixel_y"]), x=float(r["real_x"]), y=float(r["real_y"]))
        for r in rows
    ]

    transform = fit_transform(control_points)
    residuals = transform_residuals(transform, control_points)

    print("Fit residuals per control point (meters -- should be small, e.g. < 0.2m):")
    for cp, res in zip(control_points, residuals):
        print(f"  pixel=({cp.px:.1f},{cp.py:.1f}) real=({cp.x:.2f},{cp.y:.2f}) -> residual={res:.3f}m")
    max_residual = max(residuals)
    print(f"Max residual: {max_residual:.3f}m" + ("  <-- looks off, double-check your points!" if max_residual > 0.5 else ""))

    scale_x, scale_y = implied_scale(transform)
    print(f"\nImplied scale: {scale_x:.4f} m/pixel (x-axis), {scale_y:.4f} m/pixel (y-axis)")
    print(
        "Sanity check this against your drawing -- does that many meters per pixel make "
        "sense given the image size and the facility's real dimensions?"
    )
    if len(control_points) == 3:
        print(
            "NOTE: with exactly 3 control points, residuals above will look perfect (~0) "
            "even if real_x/real_y were entered in the wrong unit (e.g. mm instead of "
            "meters) -- the fit is mathematically exact regardless of the unit used. The "
            "scale check above is your main defense against that; add a 4th point if you "
            "want residuals to also catch it."
        )
    if scale_x > 2.0 or scale_y > 2.0 or scale_x < 0.001 or scale_y < 0.001:
        print(
            "WARNING: implied scale is well outside the typical 0.001-2 m/pixel range for "
            "a floor plan -- double-check real_x/real_y are in METERS, not mm or cm."
        )

    transform.save(args.out)
    print(f"\nSaved transform -> {args.out}")


if __name__ == "__main__":
    main()
