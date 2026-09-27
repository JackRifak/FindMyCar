"""Interactively click points (or point-pairs, for corridor segments) on a
rendered floor-plan image and record their pixel coordinates to a CSV.

Two modes:
  --mode points   (default) each click records one point. Use for control
                  points (georeferencing), capture-location points, or
                  vehicle-slot points.
  --mode segments each PAIR of clicks records one straight walkable
                  segment (click one end, then the other) -- draws a line
                  as you complete each pair. Use for corridor/aisle
                  centerlines feeding fmc/navigation + map matching.

Use this for:
1. Control points for georeferencing (--mode points) -- then manually add
   real_x, real_y columns to the output.
2. Capture-location points (--mode points) -- then manually add
   location_id, floor, zone columns.
3. Walkable-path segments (--mode segments) -- output is ready to use
   directly with scripts/build_site_geometry.py, no manual editing needed.
4. Vehicle-slot points (--mode points) -- then manually add slot_id, zone
   columns, also ready for build_site_geometry.py.

Run:
    python scripts/pick_floorplan_points.py data/site_00/floorplan/floorplan.png --out data/site_00/floorplan/clicked_points.csv [--mode points|segments]

Left-click to add a point (or complete a pair, in segments mode). Press 'u'
to undo the last point/segment. Close the window when done.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image_path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=["points", "segments"], default="points")
    args = parser.parse_args()

    img = plt.imread(args.image_path)
    fig, ax = plt.subplots(figsize=(12, 10))
    ax.imshow(img)
    if args.mode == "points":
        ax.set_title("Left-click to mark a point. 'u' to undo. Close window when done.")
    else:
        ax.set_title("Left-click PAIRS to mark a segment (one end, then the other). 'u' to undo. Close window when done.")

    points: list[tuple[float, float]] = []                         # points mode
    segments: list[tuple[float, float, float, float]] = []         # segments mode
    pending: list[tuple[float, float] | None] = [None]              # segments mode: first click of a pair
    artists = []

    def redraw():
        for artist in artists:
            artist.remove()
        artists.clear()

        if args.mode == "points":
            for i, (x, y) in enumerate(points):
                marker = ax.plot(x, y, "r+", markersize=12, markeredgewidth=2)[0]
                label = ax.annotate(str(i), (x, y), color="red", fontsize=10, xytext=(5, 5), textcoords="offset points")
                artists.extend([marker, label])
        else:
            for i, (x1, y1, x2, y2) in enumerate(segments):
                line = ax.plot([x1, x2], [y1, y2], "g-", linewidth=2)[0]
                marker = ax.plot([x1, x2], [y1, y2], "g+", markersize=10, markeredgewidth=2)[0]
                label = ax.annotate(str(i), ((x1 + x2) / 2, (y1 + y2) / 2), color="green", fontsize=10, xytext=(5, 5), textcoords="offset points")
                artists.extend([line, marker, label])
            if pending[0] is not None:
                marker = ax.plot(pending[0][0], pending[0][1], "y+", markersize=12, markeredgewidth=2)[0]
                artists.append(marker)

        fig.canvas.draw()

    def on_click(event):
        if event.button != 1 or event.xdata is None or event.ydata is None:
            return

        if args.mode == "points":
            points.append((event.xdata, event.ydata))
            print(f"Point {len(points) - 1}: pixel=({event.xdata:.1f}, {event.ydata:.1f})")
        else:
            if pending[0] is None:
                pending[0] = (event.xdata, event.ydata)
                print(f"Segment {len(segments)} start: pixel=({event.xdata:.1f}, {event.ydata:.1f})")
            else:
                segments.append((pending[0][0], pending[0][1], event.xdata, event.ydata))
                print(f"Segment {len(segments) - 1} end: pixel=({event.xdata:.1f}, {event.ydata:.1f})")
                pending[0] = None

        redraw()

    def on_key(event):
        if event.key != "u":
            return
        if args.mode == "points" and points:
            removed = points.pop()
            print(f"Undid point: {removed}")
        elif args.mode == "segments":
            if pending[0] is not None:
                print(f"Undid pending start point: {pending[0]}")
                pending[0] = None
            elif segments:
                removed = segments.pop()
                print(f"Undid segment: {removed}")
        redraw()

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show()

    if args.mode == "segments" and pending[0] is not None:
        print(f"WARNING: discarding an incomplete segment (only one point clicked): {pending[0]}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if args.mode == "points":
            writer.writerow(["index", "pixel_x", "pixel_y"])
            for i, (x, y) in enumerate(points):
                writer.writerow([i, x, y])
            print(f"Saved {len(points)} points -> {args.out}")
        else:
            writer.writerow(["index", "pixel_x1", "pixel_y1", "pixel_x2", "pixel_y2"])
            for i, (x1, y1, x2, y2) in enumerate(segments):
                writer.writerow([i, x1, y1, x2, y2])
            print(f"Saved {len(segments)} segments -> {args.out}")

    if args.mode == "points":
        print("Now edit this CSV to add the columns needed for your next step "
              "(real_x,real_y for control points; location_id,floor,zone for capture "
              "locations; or slot_id,zone for vehicle slots).")
    else:
        print("This is ready to use directly with scripts/build_site_geometry.py "
              "(no manual editing needed).")


if __name__ == "__main__":
    main()
