"""Shared photo-ordering logic: how to determine capture order for the
photos within a single survey location, when no explicit pairing is given.

Order priority: EXIF DateTimeOriginal timestamp (if every photo in the
folder has one), falling back to filename sort. Used both by
ingest_survey_locations.py (for its evenly-spaced heading fallback) and by
scripts/build_headings_csv.py (to line up a simple ordered list of written-
down headings against the actual files) -- both MUST use the same ordering,
or a heading ends up paired with the wrong photo.
"""
from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_EXIF_DATETIME_TAG = next((k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"), None)


def _exif_datetime(path: Path) -> str | None:
    try:
        with Image.open(path) as img:
            exif = img._getexif()
            if exif and _EXIF_DATETIME_TAG in exif:
                return exif[_EXIF_DATETIME_TAG]
    except Exception:
        pass
    return None


def ordered_photos(location_dir: Path) -> list[Path]:
    """Photos in a location folder, in best-guess capture order."""
    photos = [p for p in location_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    dated = [(p, _exif_datetime(p)) for p in photos]
    if photos and all(dt is not None for _, dt in dated):
        dated.sort(key=lambda pair: pair[1])
        return [p for p, _ in dated]
    # Fall back to filename sort if EXIF timestamps are missing/inconsistent --
    # relies on your capture app naming files in the order they were taken.
    return sorted(photos, key=lambda p: p.name)
