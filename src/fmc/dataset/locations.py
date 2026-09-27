"""locations.csv read/write -- the manifest of capture-location coordinates
(location_id, floor, zone, x, y) that ties survey photos to a floor plan.
Shared by scripts/compute_location_coords.py (CLI) and fmc.webtools.server
(the browser location-photo editor) so both read/write the identical format.
"""
from __future__ import annotations

import csv
from pathlib import Path

LOCATIONS_CSV_FIELDNAMES = ["location_id", "floor", "zone", "x", "y"]


def load_locations_csv(path: Path) -> list[dict]:
    """Rows as {"location_id": str, "floor": int, "zone": str, "x": float, "y": float}.
    Returns [] if the file doesn't exist. utf-8-sig transparently handles a
    leading BOM (common after editing the file in Excel/LibreOffice)."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [
            {
                "location_id": row["location_id"].strip(),
                "floor": int(row["floor"]),
                "zone": row["zone"].strip(),
                "x": float(row["x"]),
                "y": float(row["y"]),
            }
            for row in csv.DictReader(f)
        ]


def save_locations_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOCATIONS_CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in LOCATIONS_CSV_FIELDNAMES})


def upsert_location(path: Path, location_id: str, floor: int, zone: str, x: float, y: float) -> dict:
    """Add a new location, or update floor/zone/x/y if location_id already
    exists (lets a user re-click roughly the same spot, or deliberately
    reposition a location, without erroring). Returns the resulting row."""
    rows = load_locations_csv(path)
    for row in rows:
        if row["location_id"] == location_id:
            row.update({"floor": floor, "zone": zone, "x": round(x, 3), "y": round(y, 3)})
            save_locations_csv(path, rows)
            return row
    new_row = {"location_id": location_id, "floor": floor, "zone": zone, "x": round(x, 3), "y": round(y, 3)}
    rows.append(new_row)
    save_locations_csv(path, rows)
    return new_row


def delete_location(path: Path, location_id: str) -> None:
    rows = load_locations_csv(path)
    rows = [r for r in rows if r["location_id"] != location_id]
    save_locations_csv(path, rows)


def get_location(path: Path, location_id: str) -> dict | None:
    for row in load_locations_csv(path):
        if row["location_id"] == location_id:
            return row
    return None
