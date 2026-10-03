"""Smoke-test 2D-to-3D PnP localization against map_landmarks.npz."""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from fmc.config import load_site_config
from fmc.vpr.pnp_localizer import MapLandmarkIndex


def main(site_name: str, image_path: str | None) -> None:
    site = load_site_config(site_name)
    db = site.index_dir / "map_landmarks.npz"
    if not db.exists():
        print(f"Missing {db}")
        print("Run a mapping walk + Finalize first (exports descriptors with the cloud).")
        return

    idx = MapLandmarkIndex.load(db)
    if idx is None:
        print("Failed to load map index")
        return
    print(f"Loaded {len(idx.positions)} landmarks")

    if image_path:
        img = cv2.imread(image_path)
    else:
        # synthetic: project a few landmarks with a fake camera to verify PnP path
        print("No image provided — skipping live localize (pass --image path.jpg)")
        data = np.load(db)
        print("positions sample:", data["positions"][:3])
        print("descriptors shape:", data["descriptors"].shape)
        return

    if img is None:
        print(f"Could not read {image_path}")
        return

    pose = idx.localize(img)
    if pose is None:
        print("PnP localization failed (not enough inliers)")
        return
    print(
        f"Pose: x={pose.x:.2f} y={pose.y:.2f} z={pose.z:.2f} "
        f"heading={pose.heading:.1f}° inliers={pose.num_inliers}/{pose.num_matches} "
        f"conf={pose.confidence:.2f}"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--site", default="site_00")
    p.add_argument("--image", default=None)
    args = p.parse_args()
    main(args.site, args.image)
