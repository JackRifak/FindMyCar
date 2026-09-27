"""Ingest real survey data: one folder of overlapping rotation-sweep photos
per capture location, plus a locations manifest giving each location's
real-world floor/zone/x/y (from the floor-plan georeferencing scripts).

Expected input layout:
    <survey_root>/<location_id>/*.jpg   (or .jpeg / .png)

locations.csv columns: location_id, floor, zone, x, y
(produced by compute_location_coords.py)

Heading assignment, per photo, tried in this priority order (see
fmc/dataset/heading.py):
  1. --headings CSV (location_id, filename, heading_degrees) -- an explicit
     measured/logged compass reading, the most trustworthy source.
  2. EXIF GPSImgDirection, if the capture app/phone recorded one (run
     scripts/check_exif_compass.py first to see if this covers you already).
  3. An evenly-spaced approximation (360 / N degrees apart, in capture
     order) across the location's photo count -- last resort. Capture order
     is taken from EXIF DateTimeOriginal when available, else filename sort.
This matters beyond just metadata quality: a VPR match resets the VIO
tracker's heading to the matched reference image's stored orientation
(fmc/fusion/sensor_fusion.py), so an approximated heading here injects
error into every relocalisation event downstream.

Run:
    python scripts/ingest_survey_locations.py /path/to/survey_photos data/mock_site/index/locations.csv --site mock_site [--headings headings.csv]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from fmc.config import load_site_config
from fmc.dataset.capture import register_capture
from fmc.dataset.heading import exif_compass_heading, load_headings_manifest
from fmc.dataset.photo_ordering import ordered_photos


def load_locations(locations_csv: Path) -> dict[str, dict]:
    with open(locations_csv, "r", encoding="utf-8") as f:
        return {row["location_id"]: row for row in csv.DictReader(f)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("survey_root", type=Path)
    parser.add_argument("locations_csv", type=Path)
    parser.add_argument("--site", default="mock_site")
    parser.add_argument(
        "--headings", type=Path, default=None,
        help="Optional CSV (location_id,filename,heading_degrees) with measured compass "
             "headings. Highest priority; falls back to EXIF GPSImgDirection, then an "
             "evenly-spaced approximation.",
    )
    args = parser.parse_args()

    site = load_site_config(args.site)
    locations = load_locations(args.locations_csv)
    headings_manifest = load_headings_manifest(args.headings)

    sequence = 1
    total_photos = 0
    heading_source_counts = {"manifest": 0, "exif": 0, "approximated": 0}

    for location_dir in sorted(p for p in args.survey_root.iterdir() if p.is_dir()):
        location_id = location_dir.name
        if location_id not in locations:
            print(f"WARNING: no manifest entry for location '{location_id}', skipping")
            continue

        loc = locations[location_id]
        floor, zone = int(loc["floor"]), loc["zone"]
        x, y = float(loc["x"]), float(loc["y"])

        photos = ordered_photos(location_dir)
        if not photos:
            print(f"WARNING: no photos found in {location_dir}, skipping")
            continue

        n = len(photos)
        for i, photo_path in enumerate(photos):
            manifest_heading = headings_manifest.get((location_id, photo_path.name))
            if manifest_heading is not None:
                orientation = round(manifest_heading) % 360
                heading_source_counts["manifest"] += 1
            else:
                exif_heading = exif_compass_heading(photo_path)
                if exif_heading is not None:
                    orientation = round(exif_heading) % 360
                    heading_source_counts["exif"] += 1
                else:
                    orientation = round(i * 360 / n) % 360
                    heading_source_counts["approximated"] += 1

            register_capture(
                site=site, src_image_path=photo_path,
                floor=floor, zone=zone, sequence=sequence, orientation=orientation,
                x=x, y=y, device=f"survey:{location_id}", location_id=location_id,
            )
            sequence += 1
            total_photos += 1

        print(f"Registered {n} photos for location '{location_id}' at ({x}, {y})")

    print(f"Done. Registered {total_photos} photos across {len(locations)} locations.")
    print(
        f"Heading sources used: {heading_source_counts['manifest']} from --headings CSV, "
        f"{heading_source_counts['exif']} from EXIF, "
        f"{heading_source_counts['approximated']} approximated (evenly-spaced)."
    )


if __name__ == "__main__":
    main()
