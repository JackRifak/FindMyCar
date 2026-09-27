"""Unit tests for fmc.dataset.locations (locations.csv read/write), shared
between scripts/compute_location_coords.py and the web editor's
capture-location feature."""
from __future__ import annotations

from pathlib import Path

from fmc.dataset.locations import (
    delete_location,
    get_location,
    load_locations_csv,
    save_locations_csv,
    upsert_location,
)


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "locations.csv"
    rows = [
        {"location_id": "loc_01", "floor": 1, "zone": "Zone_A", "x": 1.5, "y": 2.5},
        {"location_id": "loc_02", "floor": 1, "zone": "Zone_B", "x": -3.0, "y": 4.0},
    ]
    save_locations_csv(path, rows)
    assert load_locations_csv(path) == rows


def test_load_missing_file_returns_empty_list(tmp_path: Path):
    assert load_locations_csv(tmp_path / "missing.csv") == []


def test_upsert_creates_new_location(tmp_path: Path):
    path = tmp_path / "locations.csv"
    row = upsert_location(path, "loc_01", floor=1, zone="Zone_A", x=1.234, y=5.678)
    assert row == {"location_id": "loc_01", "floor": 1, "zone": "Zone_A", "x": 1.234, "y": 5.678}
    assert load_locations_csv(path) == [row]


def test_upsert_moves_existing_location_instead_of_duplicating(tmp_path: Path):
    path = tmp_path / "locations.csv"
    upsert_location(path, "loc_01", floor=1, zone="Zone_A", x=0.0, y=0.0)
    upsert_location(path, "loc_01", floor=1, zone="Zone_B", x=10.0, y=20.0)

    rows = load_locations_csv(path)
    assert len(rows) == 1
    assert rows[0]["zone"] == "Zone_B"
    assert rows[0]["x"] == 10.0


def test_get_location(tmp_path: Path):
    path = tmp_path / "locations.csv"
    upsert_location(path, "loc_01", floor=1, zone="Zone_A", x=1.0, y=2.0)
    assert get_location(path, "loc_01")["zone"] == "Zone_A"
    assert get_location(path, "does_not_exist") is None


def test_delete_location(tmp_path: Path):
    path = tmp_path / "locations.csv"
    upsert_location(path, "loc_01", floor=1, zone="Zone_A", x=1.0, y=2.0)
    upsert_location(path, "loc_02", floor=1, zone="Zone_B", x=3.0, y=4.0)

    delete_location(path, "loc_01")

    rows = load_locations_csv(path)
    assert [r["location_id"] for r in rows] == ["loc_02"]


def test_bom_prefixed_file_reads_correctly(tmp_path: Path):
    # Simulates a file saved by Excel/LibreOffice with a UTF-8 BOM.
    path = tmp_path / "locations.csv"
    path.write_bytes(
        "\ufefflocation_id,floor,zone,x,y\nloc_01,1,Zone_A,1.0,2.0\n".encode("utf-8")
    )
    rows = load_locations_csv(path)
    assert rows == [{"location_id": "loc_01", "floor": 1, "zone": "Zone_A", "x": 1.0, "y": 2.0}]
