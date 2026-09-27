"""load_headings_manifest is pure CSV parsing -- verify the (location_id,
filename) -> heading_degrees lookup behaves as ingest_survey_locations.py
expects. exif_compass_heading isn't unit-tested here since it requires a
real image with embedded EXIF GPS data to exercise meaningfully."""
from __future__ import annotations

import csv
from pathlib import Path

from fmc.dataset.heading import load_headings_manifest


def test_loads_and_keys_by_location_and_filename(tmp_path: Path):
    csv_path = tmp_path / "headings.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["location_id", "filename", "heading_degrees"])
        writer.writerow(["loc_01", "IMG_0001.jpg", "0"])
        writer.writerow(["loc_01", "IMG_0002.jpg", "37.5"])
        writer.writerow(["loc_02", "IMG_0001.jpg", "180"])  # same filename, different location

    manifest = load_headings_manifest(csv_path)

    assert manifest[("loc_01", "IMG_0001.jpg")] == 0.0
    assert manifest[("loc_01", "IMG_0002.jpg")] == 37.5
    assert manifest[("loc_02", "IMG_0001.jpg")] == 180.0
    assert len(manifest) == 3


def test_missing_path_returns_empty_dict(tmp_path: Path):
    assert load_headings_manifest(None) == {}
    assert load_headings_manifest(tmp_path / "does_not_exist.csv") == {}
