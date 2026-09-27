"""Rescale x/y coordinates already written to disk by a constant factor.

Exists for one-off unit-correction fixes -- e.g. control points were
supplied in millimeters instead of meters (docs/01_coordinate_system.md
specifies meters), and existing dataset.jsonl / locations.csv /
transform.json need correcting after the fact rather than re-surveying
from scratch.

Deliberately does NOT touch embeddings.npz: fmc.vpr.search re-reads
dataset.jsonl fresh on every pipeline load, so correcting metadata alone is
sufficient -- no need to re-run the (potentially slow) embedding step.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from fmc.georeference import FloorPlanTransform


def rescale_dataset_jsonl(path: Path, factor: float) -> int:
    """Multiply x and y in every record of a dataset.jsonl by `factor`.
    Returns the number of records rewritten, or 0 if the file doesn't exist."""
    if not path.exists():
        return 0
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            record["x"] = record["x"] * factor
            record["y"] = record["y"] * factor
            records.append(record)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    return len(records)


def rescale_locations_csv(path: Path, factor: float) -> int:
    """Multiply x and y in every row of a locations.csv by `factor`.
    Returns the number of rows rewritten, or 0 if the file doesn't exist."""
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["x"] = round(float(row["x"]) * factor, 6)
        row["y"] = round(float(row["y"]) * factor, 6)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["location_id", "floor", "zone", "x", "y"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def rescale_transform(path: Path, factor: float) -> bool:
    """Rescale a saved FloorPlanTransform in place so future
    pixel_to_world() calls output the corrected unit too.
    Returns False if the file doesn't exist."""
    if not path.exists():
        return False
    transform = FloorPlanTransform.load(path)
    FloorPlanTransform(
        a=transform.a * factor, b=transform.b * factor,
        c=transform.c * factor, d=transform.d * factor,
        tx=transform.tx * factor, ty=transform.ty * factor,
    ).save(path)
    return True
