"""Convert clicked floor-plan pixel geometry (walkable corridors + vehicle
slots) into real-world coordinates and write them into a site's config.yaml
-- so routing (fmc/navigation) and map matching (fmc/fusion/map_matching.py)
have real geometry instead of the auto-generated empty placeholder.

Prerequisites: a fitted transform.json for this site/floor (see
docs/04_survey_ingestion_workflow.md, steps 1-3).

Walkable segments -- click pairs of points along every pedestrian-navigable
corridor/aisle centerline on the floor plan:
    python scripts/pick_floorplan_points.py data/site_00/floorplan/floorplan.png \\
        --out data/site_00/floorplan/segments_pixels.csv --mode segments
Click continuous pairs to build up a connected path graph -- e.g. click
(corridor start, corridor end), then (that same end, next corridor's end),
and so on, so segments actually share endpoints and form a connected
network. Disconnected segments mean routing can't reach between them.

Vehicle slots -- click one point per parking slot you want routable to:
    python scripts/pick_floorplan_points.py data/site_00/floorplan/floorplan.png \\
        --out data/site_00/floorplan/slots_pixels.csv
Then manually edit that CSV to add slot_id and zone columns.

Run:
    python scripts/build_site_geometry.py --site site_00 --floor 1 \\
        --segments data/site_00/floorplan/segments_pixels.csv \\
        --slots data/site_00/floorplan/slots_pixels.csv

Either --segments or --slots can be omitted if you're only updating one.
Re-running REPLACES that floor's walkable_segments/vehicle_slots entirely
(not additive) -- re-click everything for that floor each time, or edit
config.yaml by hand for small tweaks.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from fmc.config import get_or_create_floor, load_site_config, save_site_config
from fmc.georeference import FloorPlanTransform


def _load_segments(path: Path, transform: FloorPlanTransform) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    segments = []
    for row in rows:
        x1, y1 = transform.pixel_to_world(float(row["pixel_x1"]), float(row["pixel_y1"]))
        x2, y2 = transform.pixel_to_world(float(row["pixel_x2"]), float(row["pixel_y2"]))
        segments.append({"x1": round(x1, 3), "y1": round(y1, 3), "x2": round(x2, 3), "y2": round(y2, 3)})
    return segments


def _load_slots(path: Path, transform: FloorPlanTransform) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    slots = []
    for row in rows:
        x, y = transform.pixel_to_world(float(row["pixel_x"]), float(row["pixel_y"]))
        slots.append({"slot_id": row["slot_id"], "zone": row["zone"], "x": round(x, 3), "y": round(y, 3)})
    return slots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--floor", type=int, required=True)
    parser.add_argument("--segments", type=Path, default=None)
    parser.add_argument("--slots", type=Path, default=None)
    parser.add_argument(
        "--transform", type=Path, default=None,
        help="Defaults to data/<site>/floorplan/transform.json",
    )
    args = parser.parse_args()

    if not args.segments and not args.slots:
        parser.error("Provide at least one of --segments or --slots")

    site = load_site_config(args.site)
    transform_path = args.transform or (site.data_dir / "floorplan" / "transform.json")
    transform = FloorPlanTransform.load(transform_path)

    floor_entry = get_or_create_floor(site, args.floor)

    if args.segments:
        segments = _load_segments(args.segments, transform)
        floor_entry["walkable_segments"] = segments
        print(f"Set {len(segments)} walkable segments for floor {args.floor}")

    if args.slots:
        slots = _load_slots(args.slots, transform)
        floor_entry["vehicle_slots"] = slots
        print(f"Set {len(slots)} vehicle slots for floor {args.floor}")

    save_site_config(site)
    print(f"Saved -> data/{args.site}/config.yaml")


if __name__ == "__main__":
    main()
