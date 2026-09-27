"""Apply a fitted floor-plan transform to picked location pixel-coordinates,
producing the locations.csv manifest expected by ingest_survey_locations.py.

Input CSV must have columns: location_id, pixel_x, pixel_y, floor, zone
(location_id/floor/zone filled in manually after picking points with
scripts/pick_floorplan_points.py).

Run:
    python scripts/compute_location_coords.py data/mock_site/floorplan/transform.json data/mock_site/floorplan/locations_pixels.csv --out data/mock_site/index/locations.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from fmc.dataset.locations import save_locations_csv
from fmc.georeference import FloorPlanTransform


def normalize_location_rows(rows):
    normalized = []
    for row in rows:
        normalized_row = {}
        for key, value in row.items():
            normalized_key = key.strip() if key is not None else key
            normalized_row[normalized_key] = value
        normalized.append(normalized_row)
    return normalized


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("transform_json", type=Path)
    parser.add_argument("locations_pixels_csv", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    transform = FloorPlanTransform.load(args.transform_json)

    with open(args.locations_pixels_csv, "r", encoding="utf-8") as f:
        rows = normalize_location_rows(list(csv.DictReader(f)))

    output_rows = []
    for row in rows:
        x, y = transform.pixel_to_world(float(row["pixel_x"]), float(row["pixel_y"]))
        output_rows.append({
            "location_id": row["location_id"],
            "floor": row["floor"],
            "zone": row["zone"],
            "x": round(x, 3),
            "y": round(y, 3),
        })
    save_locations_csv(args.out, output_rows)

    print(f"Wrote {len(rows)} location coordinates -> {args.out}")


if __name__ == "__main__":
    main()
