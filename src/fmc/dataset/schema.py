"""Reference image metadata schema.

Mirrors the metadata fields defined in the project brief (Section 5) and
docs/03_dataset_spec.md. Embeddings and local feature descriptors are
deliberately NOT part of this record — see the dataset spec for why.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class CameraInfo(BaseModel):
    device: str = "unknown"
    focal_length_mm: float | None = None
    resolution: str = "1280x720"


class ReferenceImage(BaseModel):
    image_id: str
    floor: int
    zone: str
    x: float
    y: float
    orientation: int = Field(ge=0, le=360)
    timestamp: str
    camera_information: CameraInfo = CameraInfo()
    processed_path: str  # relative path under the site's processed/ dir
    location_id: str = ""  # which capture location this photo belongs to;
    # default "" for backward compatibility with records written before this
    # field existed (CLI-ingested records predating the web editor's
    # location-photo feature) -- those can still be queried by floor/zone/x/y,
    # just not directly by location_id.


def append_record(dataset_jsonl_path: Path, record: ReferenceImage) -> None:
    dataset_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dataset_jsonl_path, "a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")


def load_records(dataset_jsonl_path: Path) -> list[ReferenceImage]:
    if not dataset_jsonl_path.exists():
        return []
    records = []
    with open(dataset_jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(ReferenceImage(**json.loads(line)))
    return records


def save_records(dataset_jsonl_path: Path, records: list[ReferenceImage]) -> None:
    """Overwrite the whole file with `records`, in order. Used for edits
    (heading update, photo delete) rather than the append-only path --
    dataset sizes here are small (tens to low hundreds of images), so a
    full read-modify-write is simpler and safer than partial in-place edits."""
    dataset_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dataset_jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json() + "\n")


def records_for_location(dataset_jsonl_path: Path, location_id: str) -> list[ReferenceImage]:
    return [r for r in load_records(dataset_jsonl_path) if r.location_id == location_id]


def update_record_heading(dataset_jsonl_path: Path, image_id: str, heading_degrees: int) -> ReferenceImage:
    """Update one record's orientation in place. Raises ValueError if
    image_id isn't found."""
    records = load_records(dataset_jsonl_path)
    for i, r in enumerate(records):
        if r.image_id == image_id:
            updated = r.model_copy(update={"orientation": heading_degrees})
            records[i] = updated
            save_records(dataset_jsonl_path, records)
            return updated
    raise ValueError(f"No record with image_id={image_id!r}")


def delete_record(dataset_jsonl_path: Path, image_id: str) -> ReferenceImage:
    """Remove one record. Raises ValueError if image_id isn't found. Does
    NOT delete the underlying image file -- callers that also want that
    should remove site.processed_dir / record.processed_path themselves."""
    records = load_records(dataset_jsonl_path)
    for i, r in enumerate(records):
        if r.image_id == image_id:
            removed = records.pop(i)
            save_records(dataset_jsonl_path, records)
            return removed
    raise ValueError(f"No record with image_id={image_id!r}")
