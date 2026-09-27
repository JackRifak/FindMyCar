"""Unit tests for the dataset.jsonl edit helpers (save_records, delete_record,
update_record_heading, records_for_location) added for the web editor's
capture-location/photo feature. Exercised indirectly via
tests/test_webtools_server.py too, but these test the library functions in
isolation, matching the project's convention of testing shared fmc/ modules
directly."""
from __future__ import annotations

from pathlib import Path

import pytest

from fmc.dataset.schema import (
    CameraInfo,
    ReferenceImage,
    append_record,
    delete_record,
    load_records,
    records_for_location,
    save_records,
    update_record_heading,
)


def _make_record(image_id: str, location_id: str = "loc_01", orientation: int = 0) -> ReferenceImage:
    return ReferenceImage(
        image_id=image_id, floor=1, zone="Zone_A", x=1.0, y=2.0, orientation=orientation,
        timestamp="2026-01-01T00:00:00Z", camera_information=CameraInfo(), processed_path=f"{image_id}.jpg",
        location_id=location_id,
    )


def test_old_records_without_location_id_default_to_empty_string(tmp_path: Path):
    # Simulates a record written before the location_id field existed.
    path = tmp_path / "dataset.jsonl"
    path.write_text(
        '{"image_id":"A","floor":1,"zone":"Zone_A","x":1.0,"y":2.0,"orientation":0,'
        '"timestamp":"2026-01-01T00:00:00Z","camera_information":{"device":"unknown",'
        '"focal_length_mm":null,"resolution":"1280x720"},"processed_path":"A.jpg"}\n',
        encoding="utf-8",
    )
    records = load_records(path)
    assert records[0].location_id == ""


def test_save_records_overwrites_whole_file(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A"))
    append_record(path, _make_record("B"))

    save_records(path, [_make_record("C")])

    records = load_records(path)
    assert [r.image_id for r in records] == ["C"]


def test_records_for_location_filters_correctly(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A", location_id="loc_01"))
    append_record(path, _make_record("B", location_id="loc_02"))
    append_record(path, _make_record("C", location_id="loc_01"))

    loc_01_records = records_for_location(path, "loc_01")
    assert {r.image_id for r in loc_01_records} == {"A", "C"}


def test_update_record_heading(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A", orientation=10))
    append_record(path, _make_record("B", orientation=20))

    updated = update_record_heading(path, "A", 270)
    assert updated.orientation == 270

    records = {r.image_id: r for r in load_records(path)}
    assert records["A"].orientation == 270
    assert records["B"].orientation == 20  # untouched


def test_update_record_heading_missing_image_id_raises(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A"))
    with pytest.raises(ValueError):
        update_record_heading(path, "does_not_exist", 90)


def test_delete_record(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A"))
    append_record(path, _make_record("B"))

    removed = delete_record(path, "A")
    assert removed.image_id == "A"

    records = load_records(path)
    assert [r.image_id for r in records] == ["B"]


def test_delete_record_missing_image_id_raises(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    append_record(path, _make_record("A"))
    with pytest.raises(ValueError):
        delete_record(path, "does_not_exist")
