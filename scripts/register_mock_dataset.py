"""Register every image in data/mock_site/raw/manifest.csv into the dataset.

Run after scripts/generate_mock_images.py:
    python scripts/register_mock_dataset.py
Then build the index:
    python -m fmc.dataset.build_index --site mock_site
"""
from __future__ import annotations

import csv
from pathlib import Path

from fmc.config import load_site_config
from fmc.dataset.capture import register_capture

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    site = load_site_config("mock_site")
    manifest_path = site.raw_dir / "manifest.csv"

    with open(manifest_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            register_capture(
                site=site,
                src_image_path=site.raw_dir / row["filename"],
                floor=int(row["floor"]),
                zone=row["zone"],
                sequence=int(row["sequence"]),
                orientation=int(row["orientation"]),
                x=float(row["x"]),
                y=float(row["y"]),
                device="mock_generator",
            )
            count += 1

    print(f"Registered {count} images into {site.dataset_jsonl_path}")


if __name__ == "__main__":
    main()
