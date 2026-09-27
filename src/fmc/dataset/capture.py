"""Register a captured image into a site's dataset.

Given a raw image plus its surveyed coordinates, this:
1. Normalizes/resizes the image (per docs/03_dataset_spec.md).
2. Copies it into the site's processed/ directory using the naming convention
   in docs/02_site_survey_spec.md.
3. Appends a metadata record to the site's dataset.jsonl.

This does NOT compute embeddings — that happens in a separate index-build step
(see build_index.py) so the embedding model can be swapped independently of
survey/ground-truth data.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import cv2

from fmc.config import PROCESSED_IMAGE_SHORT_SIDE, SiteConfig
from fmc.dataset.schema import CameraInfo, ReferenceImage, append_record, load_records

_IMAGE_ID_SEQUENCE_PATTERN = re.compile(r"^F\d{2}_Z.+_(\d{5})_\d{3}$")


def image_id_for(floor: int, zone: str, sequence: int, orientation: int) -> str:
    return f"F{floor:02d}_Z{zone}_{sequence:05d}_{orientation:03d}"


def next_sequence_for_site(site: SiteConfig) -> int:
    """A safe next sequence number for image_id_for(), found by scanning
    the site's existing image_ids. Batch CLI ingestion (ingest_survey_locations.py)
    tracks its own counter across one run; this is for registering photos
    one at a time (e.g. the web editor's per-photo upload), where each
    request needs to independently avoid colliding with whatever already
    exists."""
    max_seq = 0
    for record in load_records(site.dataset_jsonl_path):
        match = _IMAGE_ID_SEQUENCE_PATTERN.match(record.image_id)
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return max_seq + 1


def _normalize_image(src_path: Path, dst_path: Path) -> None:
    img = cv2.imread(str(src_path))
    if img is None:
        raise ValueError(f"Could not read image: {src_path}")
    h, w = img.shape[:2]
    short_side = min(h, w)
    if short_side != PROCESSED_IMAGE_SHORT_SIDE:
        scale = PROCESSED_IMAGE_SHORT_SIDE / short_side
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst_path), img, [cv2.IMWRITE_JPEG_QUALITY, 90])


def register_capture(
    site: SiteConfig,
    src_image_path: Path,
    floor: int,
    zone: str,
    sequence: int,
    orientation: int,
    x: float,
    y: float,
    device: str = "unknown",
    location_id: str = "",
) -> ReferenceImage:
    image_id = image_id_for(floor, zone, sequence, orientation)
    dst_path = site.processed_dir / f"F{floor}" / f"Z{zone}" / f"{image_id}.jpg"
    _normalize_image(src_image_path, dst_path)
    processed_rel_path = str(dst_path.relative_to(site.processed_dir))

    record = ReferenceImage(
        image_id=image_id,
        floor=floor,
        zone=zone,
        x=x,
        y=y,
        orientation=orientation,
        timestamp=datetime.now(timezone.utc).isoformat(),
        camera_information=CameraInfo(device=device),
        processed_path=processed_rel_path,
        location_id=location_id,
    )
    append_record(site.dataset_jsonl_path, record)
    return record
