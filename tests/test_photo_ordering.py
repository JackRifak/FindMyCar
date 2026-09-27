"""ordered_photos must give a stable, deterministic order that both
ingest_survey_locations.py and build_headings_csv.py agree on -- verify the
filename-sort fallback (no EXIF timestamps present, the common case for
freshly-created test files)."""
from __future__ import annotations

from pathlib import Path

from fmc.dataset.photo_ordering import ordered_photos


def test_falls_back_to_filename_sort_without_exif(tmp_path: Path):
    # Create in a deliberately non-alphabetical creation order to prove
    # sorting (not creation/mtime order) determines the result.
    for name in ["IMG_0003.jpg", "IMG_0001.jpg", "IMG_0002.jpg"]:
        (tmp_path / name).write_bytes(b"not a real jpeg, just needs to exist")

    result = [p.name for p in ordered_photos(tmp_path)]
    assert result == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]


def test_ignores_non_image_files(tmp_path: Path):
    (tmp_path / "IMG_0001.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "manifest.csv").write_bytes(b"x")

    result = [p.name for p in ordered_photos(tmp_path)]
    assert result == ["IMG_0001.jpg"]
