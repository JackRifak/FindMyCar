"""Smoke test: full pipeline (dataset -> index -> VPR -> geometric verification)
runs end-to-end against the mock site and returns a plausible position.

Requires the mock dataset to already be generated/registered/indexed:
    python scripts/generate_mock_images.py
    python scripts/register_mock_dataset.py
    python -m fmc.dataset.build_index --site mock_site
    pytest tests/test_pipeline_smoke.py
"""
from __future__ import annotations

import cv2
import pytest

from fmc.config import load_site_config
from fmc.vpr.pipeline import VPRPipeline


@pytest.fixture(scope="module")
def site():
    return load_site_config("mock_site")


@pytest.fixture(scope="module")
def pipeline(site):
    if not site.embeddings_path.exists():
        pytest.skip("Index not built yet — run the mock dataset scripts first (see module docstring).")
    return VPRPipeline(site)


def test_known_landmark_localizes_correctly(site, pipeline):
    query_path = site.raw_dir / "L1_000.jpg"
    if not query_path.exists():
        pytest.skip("Mock images not generated yet — run scripts/generate_mock_images.py first.")

    query_img = cv2.imread(str(query_path))
    result = pipeline.localize(query_img)

    assert result.matched, "Expected the query image to match its own landmark"
    assert result.record.zone == "Zone_A"
    assert abs(result.record.x - 2.0) < 0.01
    assert abs(result.record.y - 1.0) < 0.01
