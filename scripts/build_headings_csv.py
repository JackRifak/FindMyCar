"""Turn a simple ordered list of compass headings per location into the
(location_id, filename, heading_degrees) headings CSV that
ingest_survey_locations.py's --headings expects.

Use this when you wrote down headings in the order photos were taken during
each rotation sweep (e.g. "shot 1: 10 deg, shot 2: 48 deg, ...") rather than
tying a specific value to a specific filename. It matches your ordered list
against the photos in the SAME capture-order logic ingest_survey_locations.py
uses (fmc.dataset.photo_ordering: EXIF timestamp, falling back to filename
sort) -- so the pairing lines up correctly with what actually gets ingested.

Input CSV columns: location_id, headings
  `headings` is a semicolon-separated list of degrees, one per photo, IN THE
  ORDER YOU CAPTURED THEM for that location (i.e. the order you wrote them
  down while rotating in place).

Example input:
    location_id,headings
    loc_01,0;36;75;110;158;200;240;280;320;350
    loc_02,10;50;95;140;185;230;275;320

Run:
    python scripts/build_headings_csv.py ordered_headings.csv /path/to/survey_photos --out data/site_00/floorplan/headings.csv

Then pass the output to ingestion:
    python scripts/ingest_survey_locations.py /path/to/survey_photos data/site_00/index/locations.csv --site site_00 --headings data/site_00/floorplan/headings.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from fmc.dataset.photo_ordering import ordered_photos


def normalize_headings_rows(rows):
    normalized = []
    for row in rows:
        normalized_row = {}
        for key, value in row.items():
            normalized_key = (key or "").lstrip("\ufeff").strip()
            normalized_row[normalized_key] = value
        normalized.append(normalized_row)
    return normalized


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ordered_headings_csv", type=Path)
    parser.add_argument("survey_root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    with open(args.ordered_headings_csv, "r", encoding="utf-8") as f:
        rows = normalize_headings_rows(list(csv.DictReader(f)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(args.out, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f)
        writer.writerow(["location_id", "filename", "heading_degrees"])

        for row in rows:
            location_id = row["location_id"]
            headings = [h.strip() for h in row["headings"].split(";") if h.strip()]
            location_dir = args.survey_root / location_id
            if not location_dir.is_dir():
                print(f"WARNING: no photo folder found for '{location_id}' at {location_dir}, skipping")
                continue

            photos = ordered_photos(location_dir)
            if len(photos) != len(headings):
                print(
                    f"WARNING: '{location_id}' has {len(photos)} photos but {len(headings)} "
                    f"headings listed -- skipping this location, fix the mismatch first "
                    f"(check for extra/missing photos, or a miscounted heading list)."
                )
                continue

            print(f"Matching '{location_id}' in capture order:")
            for photo_path, heading in zip(photos, headings):
                print(f"  {photo_path.name} -> {heading} deg")
                writer.writerow([location_id, photo_path.name, heading])
                written += 1

    print(f"\nWrote {written} filename->heading rows -> {args.out}")
    print("Double-check the printed pairing above against your notes before trusting it --")
    print("if capture order doesn't match what you wrote down, the pairing will be wrong")
    print("even though every row looks individually valid.")


if __name__ == "__main__":
    main()
