"""Persistence and image-quality metrics for live localization diagnostics."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import cv2
import numpy as np

_WRITE_LOCK = threading.Lock()


def image_quality_metrics(image: np.ndarray) -> dict[str, float | int]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "laplacian_variance": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "brightness_mean": float(gray.mean()),
        "brightness_std": float(gray.std()),
        "brightness_p05": float(np.percentile(gray, 5)),
        "brightness_p95": float(np.percentile(gray, 95)),
    }


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")


def persist_live_miss(
    site_data_dir: Path,
    image: np.ndarray,
    metadata: dict,
    sample_limit: int,
) -> dict:
    """Append every miss record and persist image pixels only up to sample_limit."""
    diagnostics_dir = site_data_dir / "diagnostics" / "live_misses"
    frame_dir = diagnostics_dir / "frames"
    frame_id = uuid.uuid4().hex
    saved_frame = None

    with _WRITE_LOCK:
        if sample_limit > 0:
            frame_dir.mkdir(parents=True, exist_ok=True)
            existing_samples = sum(1 for _ in frame_dir.glob("*.jpg"))
            if existing_samples < sample_limit:
                success, encoded = cv2.imencode(
                    ".jpg",
                    image,
                    [cv2.IMWRITE_JPEG_QUALITY, 95],
                )
                if success:
                    frame_path = frame_dir / f"{frame_id}.jpg"
                    frame_path.write_bytes(encoded.tobytes())
                    saved_frame = str(frame_path.relative_to(site_data_dir))

        record = {
            **metadata,
            "sample_id": frame_id,
            "frame_path": saved_frame,
        }
        log_path = diagnostics_dir / "misses.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
    return record


def append_client_event(site_data_dir: Path, event: dict) -> None:
    _append_jsonl(site_data_dir / "diagnostics" / "client_events.jsonl", event)