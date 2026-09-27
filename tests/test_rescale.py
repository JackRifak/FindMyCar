"""rescale_* functions mutate real project data files in place -- worth
testing in isolation before trusting them against actual survey data."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from fmc.dataset.rescale import rescale_dataset_jsonl, rescale_locations_csv, rescale_transform
from fmc.georeference import FloorPlanTransform


def test_rescale_dataset_jsonl(tmp_path: Path):
    path = tmp_path / "dataset.jsonl"
    records = [
        {"image_id": "A", "x": 1000.0, "y": 2000.0, "floor": 1},
        {"image_id": "B", "x": 500.0, "y": -1500.0, "floor": 1},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    count = rescale_dataset_jsonl(path, 0.001)
    assert count == 2

    with open(path, "r", encoding="utf-8") as f:
        result = [json.loads(line) for line in f]
    assert result[0]["x"] == 1.0
    assert result[0]["y"] == 2.0
    assert result[1]["x"] == 0.5
    assert result[1]["y"] == -1.5
    assert result[0]["image_id"] == "A"  # non-coordinate fields untouched


def test_rescale_dataset_jsonl_missing_file_returns_zero(tmp_path: Path):
    assert rescale_dataset_jsonl(tmp_path / "missing.jsonl", 0.001) == 0


def test_rescale_locations_csv(tmp_path: Path):
    path = tmp_path / "locations.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["location_id", "floor", "zone", "x", "y"])
        writer.writerow(["loc_01", "1", "Zone_A", "1000.0", "2000.0"])

    count = rescale_locations_csv(path, 0.001)
    assert count == 1

    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert float(rows[0]["x"]) == 1.0
    assert float(rows[0]["y"]) == 2.0
    assert rows[0]["location_id"] == "loc_01"


def test_rescale_transform(tmp_path: Path):
    path = tmp_path / "transform.json"
    FloorPlanTransform(a=1.0, b=0.0, c=0.0, d=1.0, tx=1000.0, ty=2000.0).save(path)

    assert rescale_transform(path, 0.001) is True

    rescaled = FloorPlanTransform.load(path)
    assert rescaled.a == 0.001
    assert rescaled.tx == 1.0
    assert rescaled.ty == 2.0


def test_rescale_transform_missing_file_returns_false(tmp_path: Path):
    assert rescale_transform(tmp_path / "missing.json", 0.001) is False
