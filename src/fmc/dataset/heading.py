"""Compass heading resolution for survey photos.

Three sources, tried in priority order by ingest_survey_locations.py:
1. A manually supplied headings CSV (location_id, filename, heading_degrees)
   -- the most trustworthy since it's an explicit, human-provided value.
2. EXIF GPSImgDirection, when the capture app/phone already recorded it
   (common when location services were enabled during capture -- worth
   checking before doing any manual logging; see
   scripts/check_exif_compass.py).
3. An evenly-spaced approximation across the rotation sweep -- last resort,
   see the docstring in ingest_survey_locations.py for why.
"""
from __future__ import annotations

import csv
from pathlib import Path

from PIL import ExifTags, Image

_GPSINFO_TAG = next((k for k, v in ExifTags.TAGS.items() if v == "GPSInfo"), None)
_GPS_TAGS = ExifTags.GPSTAGS


def exif_compass_heading(path: Path) -> float | None:
    """Return the EXIF GPSImgDirection heading in degrees, or None if absent/unreadable."""
    try:
        with Image.open(path) as img:
            exif = img._getexif()
            if not exif or _GPSINFO_TAG not in exif:
                return None
            gps_named = {_GPS_TAGS.get(k, k): v for k, v in exif[_GPSINFO_TAG].items()}
            direction = gps_named.get("GPSImgDirection")
            return float(direction) if direction is not None else None
    except Exception:
        return None


def load_headings_manifest(path: Path) -> dict[tuple[str, str], float]:
    """Load a (location_id, filename) -> heading_degrees lookup from CSV.
    Returns an empty dict if the path is None or the file doesn't exist."""
    if path is None or not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return {
            (row["location_id"], row["filename"]): float(row["heading_degrees"])
            for row in csv.DictReader(f)
        }
