import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import cv2
import yaml

from fmc.api.live_diagnostics import image_quality_metrics, persist_live_miss
from fmc.dataset.schema import CameraInfo, ReferenceImage, append_record
from scripts import analyze_live_vpr_diagnostics


def test_image_quality_metrics_reports_resolution_blur_and_brightness():
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    image[:, 60:] = 255

    metrics = image_quality_metrics(image)

    assert metrics["width"] == 120
    assert metrics["height"] == 80
    assert metrics["laplacian_variance"] > 0
    assert metrics["brightness_mean"] == 127.5


def test_persist_live_miss_caps_images_but_keeps_every_jsonl_record(tmp_path):
    frame = np.full((48, 64, 3), 90, dtype=np.uint8)
    metadata = {"site_id": "site_test", "failure_stage": "verification_rejection"}

    first = persist_live_miss(tmp_path, frame, metadata, sample_limit=1)
    second = persist_live_miss(tmp_path, frame, metadata, sample_limit=1)

    assert first["frame_path"] is not None
    assert (tmp_path / first["frame_path"]).is_file()
    assert second["frame_path"] is None
    log_path = tmp_path / "diagnostics" / "live_misses" / "misses.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["sample_id"] != records[1]["sample_id"]


def test_disabled_image_sampling_still_records_miss(tmp_path):
    frame = np.zeros((32, 32, 3), dtype=np.uint8)

    record = persist_live_miss(tmp_path, frame, {"failure_stage": "unknown"}, sample_limit=0)

    assert record["frame_path"] is None
    assert len(list((tmp_path / "diagnostics" / "live_misses").glob("*.jsonl"))) == 1


def test_analyzer_generates_side_by_side_live_miss_report(tmp_path, monkeypatch):
    import fmc.config as config_module

    monkeypatch.setattr(config_module, "DATA_ROOT", tmp_path)
    site_dir = tmp_path / "site_test"
    index_dir = site_dir / "index"
    processed_dir = site_dir / "processed" / "F1" / "A"
    index_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    (site_dir / "config.yaml").write_text(
        yaml.safe_dump({"site_id": "site_test", "floors": [{"floor": 1, "zones": []}]}),
        encoding="utf-8",
    )

    candidate_path = processed_dir / "reference.jpg"
    candidate_image = np.zeros((48, 64, 3), dtype=np.uint8)
    candidate_image[:, 32:] = 220
    assert cv2.imwrite(str(candidate_path), candidate_image)
    record = ReferenceImage(
        image_id="candidate-1",
        floor=1,
        zone="A",
        x=1.0,
        y=2.0,
        orientation=90,
        timestamp=datetime.now(timezone.utc).isoformat(),
        camera_information=CameraInfo(),
        processed_path="F1/A/reference.jpg",
        location_id="loc_1",
    )
    append_record(index_dir / "dataset.jsonl", record)

    query_image = np.full((48, 64, 3), 100, dtype=np.uint8)
    persist_live_miss(
        site_dir,
        query_image,
        {
            "site_id": "site_test",
            "sample_id": "sample-test",
            "received_at": datetime.now(timezone.utc).timestamp(),
            "failure_stage": "verification_rejection",
            "device_id": "test-device",
            "capture_trigger": "motion",
            "ground_truth_location_id": "loc_1",
            "ground_truth_in_top_k": True,
            "uploaded_bytes": 100,
            "client": {"source_width": 64, "source_height": 48, "encoded_width": 64, "encoded_height": 48, "jpeg_quality": 0.8},
            "server_image": image_quality_metrics(query_image),
            "candidates": [{
                "rank": 1,
                "image_id": "candidate-1",
                "similarity": 0.9,
                "location_id": "loc_1",
                "x": 1.0,
                "y": 2.0,
                "timestamp": record.timestamp,
                "num_matches": 20,
                "num_inliers": 5,
                "inlier_ratio": 0.25,
                "passed": False,
            }],
        },
        sample_limit=1,
    )

    monkeypatch.setattr(sys, "argv", ["analyze_live_vpr_diagnostics", "--site", "site_test"])
    analyze_live_vpr_diagnostics.main()

    report_path = site_dir / "diagnostics" / "live_misses" / "report.html"
    report = report_path.read_text(encoding="utf-8")
    assert "Failed live query" in report
    assert "candidate-1" in report
    assert "verification_rejection" in report
    assert "brightness" in report