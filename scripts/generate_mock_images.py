"""Generate synthetic placeholder 'landmark' images for the mock site.

Substitutes for a real site survey (see docs/02_site_survey_spec.md, "Mock-site
substitute" section) so the dataset/VPR/geometric-verification pipeline can be
exercised end-to-end before real capture data exists. Each landmark in
data/mock_site/config.yaml gets a distinguishable synthetic image (different
shape/color/pattern) written to data/mock_site/raw/, plus a manifest CSV that
scripts/register_mock_dataset.py consumes.

Run: python scripts/generate_mock_images.py
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

import cv2
import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = PROJECT_ROOT / "data" / "mock_site"

SHAPES = ["circle", "rectangle", "triangle", "cross", "diamond"]
COLORS = [
    (60, 20, 200),   # red-ish (BGR)
    (200, 130, 20),  # blue-ish
    (30, 160, 60),   # green-ish
    (20, 200, 200),  # yellow-ish
    (180, 40, 180),  # purple-ish
]


def _draw_landmark(shape: str, color: tuple, size: int = 480, seed: int = 0) -> np.ndarray:
    rng = random.Random(seed)
    img = np.full((size, size, 3), 235, dtype=np.uint8)  # light gray "floor/wall" background

    # A few faint background lines so ORB has generic texture to match on too.
    for _ in range(6):
        p1 = (rng.randint(0, size), rng.randint(0, size))
        p2 = (rng.randint(0, size), rng.randint(0, size))
        cv2.line(img, p1, p2, (210, 210, 210), 1)

    c = (size // 2, size // 2)
    r = size // 4
    if shape == "circle":
        cv2.circle(img, c, r, color, -1)
    elif shape == "rectangle":
        cv2.rectangle(img, (c[0] - r, c[1] - r), (c[0] + r, c[1] + r), color, -1)
    elif shape == "triangle":
        pts = np.array([[c[0], c[1] - r], [c[0] - r, c[1] + r], [c[0] + r, c[1] + r]])
        cv2.fillPoly(img, [pts], color)
    elif shape == "cross":
        cv2.rectangle(img, (c[0] - r, c[1] - r // 3), (c[0] + r, c[1] + r // 3), color, -1)
        cv2.rectangle(img, (c[0] - r // 3, c[1] - r), (c[0] + r // 3, c[1] + r), color, -1)
    elif shape == "diamond":
        pts = np.array([[c[0], c[1] - r], [c[0] + r, c[1]], [c[0], c[1] + r], [c[0] - r, c[1]]])
        cv2.fillPoly(img, [pts], color)

    return img


def main():
    with open(SITE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    raw_dir = SITE_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir / "manifest.csv"

    rows = []
    seq = 1
    for floor_cfg in config["floors"]:
        floor = floor_cfg["floor"]
        for i, landmark in enumerate(floor_cfg["landmarks"]):
            shape = SHAPES[i % len(SHAPES)]
            color = COLORS[i % len(COLORS)]
            # Generate one image per landmark per orientation (0/90/180/270),
            # matching the multi-orientation capture guidance in the survey spec.
            for orientation in (0, 90, 180, 270):
                img = _draw_landmark(shape, color, seed=i * 10 + orientation)
                filename = f"{landmark['id']}_{orientation:03d}.jpg"
                cv2.imwrite(str(raw_dir / filename), img)
                rows.append({
                    "filename": filename,
                    "floor": floor,
                    "zone": landmark["zone"],
                    "sequence": seq,
                    "orientation": orientation,
                    "x": landmark["x"],
                    "y": landmark["y"],
                })
                seq += 1

    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "floor", "zone", "sequence", "orientation", "x", "y"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Generated {len(rows)} mock images -> {raw_dir}")
    print(f"Manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
