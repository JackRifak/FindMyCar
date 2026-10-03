"""Backfill ORB descriptors onto an existing point cloud using survey photos.

The continuous-mapping PLY has XYZ+RGB but no descriptors (walk was before
descriptor export). Survey images in processed/ have facility (x,y,heading).

For each survey photo we project nearby cloud points into the camera and
assign the nearest ORB descriptor — then write H2GIS + NPZ for PnP.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from fmc.config import load_site_config
from fmc.dataset.schema import load_records
from fmc.storage.h2gis_store import delete_site_db, replace_landmarks
from fmc.vpr.pnp_localizer import MapLandmarkIndex


EYE_HEIGHT = 1.55  # meters
NEAR_XY = 12.0     # only consider cloud pts within this of camera
MAX_DEPTH = 18.0
MATCH_PX = 6.0     # max pixel distance ORB ↔ projected 3D


def load_ply_xyz_rgb(path: Path) -> tuple[np.ndarray, np.ndarray]:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    idx = next(i for i, ln in enumerate(text) if ln.strip() == "end_header")
    pts, cols = [], []
    for ln in text[idx + 1 :]:
        parts = ln.split()
        if len(parts) < 3:
            continue
        pts.append([float(parts[0]), float(parts[1]), float(parts[2])])
        if len(parts) >= 6:
            cols.append([int(parts[3]), int(parts[4]), int(parts[5])])
        else:
            cols.append([52, 199, 89])
    return np.asarray(pts, dtype=np.float64), np.asarray(cols, dtype=np.uint8)


def yaw_R_world_to_cam(heading_deg: float) -> np.ndarray:
    """World (X east, Y north, Z up) → OpenCV cam (X right, Y down, Z fwd)."""
    h = np.deg2rad(heading_deg)
    # camera axes in world
    forward = np.array([np.sin(h), np.cos(h), 0.0], dtype=np.float64)
    right = np.array([np.cos(h), -np.sin(h), 0.0], dtype=np.float64)
    down = np.cross(forward, right)  # ≈ [0,0,-1]
    # rows = axes (world → camera)
    return np.stack([right, down, forward], axis=0)


def assign_descriptors(
    cloud: np.ndarray,
    colors: np.ndarray,
    site,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    records = load_records(site.dataset_jsonl_path)
    if not records:
        raise RuntimeError(f"no survey records at {site.dataset_jsonl_path}")

    orb = cv2.ORB_create(nfeatures=4000)
    # best descriptor per cloud index (by pixel residual)
    best_desc: dict[int, tuple[float, np.ndarray]] = {}

    n_imgs = 0
    for rec in records:
        img_path = site.processed_dir / rec.processed_path
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        h, w = img.shape[:2]
        kpts, desc = orb.detectAndCompute(img, None)
        if desc is None or len(kpts) < 20:
            continue

        kxy = np.array([kp.pt for kp in kpts], dtype=np.float64)
        K = MapLandmarkIndex.camera_matrix(w, h)
        R = yaw_R_world_to_cam(float(rec.orientation))
        C = np.array([rec.x, rec.y, EYE_HEIGHT], dtype=np.float64)

        # nearby cloud points
        dxy = np.hypot(cloud[:, 0] - rec.x, cloud[:, 1] - rec.y)
        near = np.where(dxy < NEAR_XY)[0]
        if len(near) < 10:
            continue

        pts = cloud[near]
        Xc = (R @ (pts - C).T).T
        depth = Xc[:, 2]
        valid = (depth > 0.4) & (depth < MAX_DEPTH)
        if valid.sum() < 5:
            continue

        idxs = near[valid]
        Xc = Xc[valid]
        uvw = (K @ Xc.T).T
        uv = uvw[:, :2] / uvw[:, 2:3]
        in_frame = (
            (uv[:, 0] >= 0) & (uv[:, 0] < w) &
            (uv[:, 1] >= 0) & (uv[:, 1] < h)
        )
        if in_frame.sum() < 5:
            continue

        idxs = idxs[in_frame]
        uv = uv[in_frame]

        # nearest ORB keypoint for each projection
        # brute force in chunks
        for i, ci in enumerate(idxs):
            d = np.hypot(kxy[:, 0] - uv[i, 0], kxy[:, 1] - uv[i, 1])
            j = int(np.argmin(d))
            if d[j] > MATCH_PX:
                continue
            prev = best_desc.get(int(ci))
            if prev is None or d[j] < prev[0]:
                best_desc[int(ci)] = (float(d[j]), desc[j].copy())

        n_imgs += 1

    if len(best_desc) < 80:
        raise RuntimeError(
            f"only assigned descriptors to {len(best_desc)} points "
            f"(from {n_imgs} images) — need >= 80"
        )

    ids = np.array(sorted(best_desc.keys()), dtype=np.int32)
    positions = cloud[ids].astype(np.float32)
    descriptors = np.stack([best_desc[int(i)][1] for i in ids], axis=0)
    cols = colors[ids]
    print(f"Assigned descriptors to {len(ids)} / {len(cloud)} cloud points "
          f"using {n_imgs} survey images.")
    return positions, descriptors, ids, cols


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site_00")
    args = ap.parse_args()

    site = load_site_config(args.site)
    ply = site.index_dir / "pointcloud.ply"
    if not ply.exists():
        raise SystemExit(f"missing {ply}")

    print(f"Loading {ply} ...")
    cloud, colors = load_ply_xyz_rgb(ply)
    print(f"Cloud: {len(cloud)} points")

    positions, descriptors, ids, cols = assign_descriptors(cloud, colors, site)

    # write H2GIS
    delete_site_db(site)
    replace_landmarks(site, positions, descriptors, ids, cols, aligned=True)

    # write NPZ fallback
    npz = site.index_dir / "map_landmarks.npz"
    np.savez_compressed(
        npz,
        positions=positions,
        descriptors=descriptors,
        ids=ids,
        colors=cols,
        aligned=np.array([True]),
        frame="facility_xy_height",
        source="survey_backfill",
    )
    print(f"Wrote H2GIS map_h2gis + {npz}")
    print("Reload /localize (or POST /admin/reload) to pick up the map DB.")


if __name__ == "__main__":
    main()
