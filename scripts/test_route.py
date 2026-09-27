"""Test the full localize -> route chain directly, without running the API
server. Useful for quick iteration while you're still verifying survey data
and walkable geometry.

Two ways to give it a starting position:
  --image <photo>   runs the actual VPR pipeline against a real photo
                     (exercises the same matching step the real app uses)
  --x/--y/--floor    skips VPR, routes from a known position directly
                     (useful for testing routing/geometry in isolation from
                     VPR accuracy)

Run:
    python scripts/test_route.py --site site_00 --image path/to/photo.jpg --slot 87-04C
    python scripts/test_route.py --site site_00 --x 5.37 --y -7.50 --floor 1 --slot 87-04C
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from fmc.config import load_site_config
from fmc.fusion.map_matching import snap_to_walkable
from fmc.navigation.routing import calculate_route
from fmc.vpr.pipeline import VPRPipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--slot", required=True, help="Destination vehicle slot_id")
    parser.add_argument("--image", type=Path, default=None, help="Photo to localize via VPR")
    parser.add_argument("--x", type=float, default=None, help="Known starting x (skips VPR)")
    parser.add_argument("--y", type=float, default=None, help="Known starting y (skips VPR)")
    parser.add_argument("--floor", type=int, default=None, help="Known starting floor (skips VPR)")
    args = parser.parse_args()

    site = load_site_config(args.site)

    if args.image:
        pipeline = VPRPipeline(site)
        query_img = cv2.imread(str(args.image))
        if query_img is None:
            print(f"Could not read {args.image}")
            return
        result = pipeline.localize(query_img)
        if not result.matched:
            print("NO MATCH -- VPR couldn't localize this image. Can't test routing from an unknown position.")
            return
        r = result.record
        floor, x, y = r.floor, r.x, r.y
        print(f"VPR matched: {r.image_id}  floor={floor} x={x} y={y}  "
              f"similarity={result.similarity:.3f} inlier_ratio={result.inlier_ratio:.3f}")
    elif args.x is not None and args.y is not None and args.floor is not None:
        floor, x, y = args.floor, args.x, args.y
        print(f"Using given position: floor={floor} x={x} y={y}")
    else:
        parser.error("Provide either --image, or all of --x/--y/--floor")
        return

    segments = site.walkable_segments(floor)
    if not segments:
        print(f"WARNING: no walkable_segments for floor {floor} in data/{args.site}/config.yaml -- "
              f"did you run scripts/build_site_geometry.py for this floor?")
        return

    snapped_x, snapped_y = snap_to_walkable(x, y, segments)
    print(f"Snapped to walkable path: ({snapped_x:.3f}, {snapped_y:.3f})")

    slot = site.vehicle_slot(args.slot)
    if slot is None:
        print(f"Unknown slot_id: {args.slot}")
        return
    if slot["floor"] != floor:
        print(f"Slot '{args.slot}' is on floor {slot['floor']}, but position is on floor {floor} "
              f"-- cross-floor routing isn't implemented yet.")
        return

    route = calculate_route(segments, start_x=snapped_x, start_y=snapped_y, dest_x=slot["x"], dest_y=slot["y"])
    if route is None:
        print(f"NO ROUTE FOUND to slot '{args.slot}' -- the walkable graph likely has a "
              f"disconnected cluster between the start position and this slot.")
        return

    print(f"\nRoute to slot '{args.slot}' ({slot['x']}, {slot['y']}):")
    for i, (wx, wy) in enumerate(route.waypoints):
        print(f"  {i}: ({wx:.3f}, {wy:.3f})")
    print(f"Total distance: {route.total_distance:.2f} m")


if __name__ == "__main__":
    main()
