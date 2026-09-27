"""fit_transform should exactly recover a known affine mapping from
noise-free synthetic control points, including cases a pure
rotation+scale (similarity) transform couldn't represent."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

from fmc.georeference import ControlPoint, fit_transform, transform_residuals


def _load_compute_location_coords_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "compute_location_coords.py"
    spec = importlib.util.spec_from_file_location("compute_location_coords", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_recovers_pure_scale_and_translation():
    # 1 pixel = 0.05 meters, origin at pixel (100, 100)
    def px_to_world(px, py):
        return (px - 100) * 0.05, (py - 100) * 0.05

    control_points = [
        ControlPoint(px=100, py=100, x=0.0, y=0.0),
        ControlPoint(px=300, py=100, x=10.0, y=0.0),
        ControlPoint(px=100, py=300, x=0.0, y=10.0),
    ]
    transform = fit_transform(control_points)

    x, y = transform.pixel_to_world(500, 500)
    expected_x, expected_y = px_to_world(500, 500)
    assert abs(x - expected_x) < 1e-6
    assert abs(y - expected_y) < 1e-6
    assert max(transform_residuals(transform, control_points)) < 1e-6


def test_recovers_rotation_and_reflection():
    # +pixel_x maps to +world_y and +pixel_y maps to +world_x -- a 90-degree
    # rotation plus a reflection, which only a full affine fit can represent
    # (not a pure rotation+uniform-scale similarity transform).
    control_points = [
        ControlPoint(px=0, py=0, x=0.0, y=0.0),
        ControlPoint(px=10, py=0, x=0.0, y=10.0),
        ControlPoint(px=0, py=10, x=10.0, y=0.0),
    ]
    transform = fit_transform(control_points)

    x, y = transform.pixel_to_world(5, 5)
    assert abs(x - 5.0) < 1e-6
    assert abs(y - 5.0) < 1e-6
    assert max(transform_residuals(transform, control_points)) < 1e-6


def test_requires_at_least_three_points():
    import pytest
    with pytest.raises(ValueError):
        fit_transform([ControlPoint(px=0, py=0, x=0, y=0), ControlPoint(px=1, py=0, x=1, y=0)])


def test_implied_scale_catches_millimeter_mistake():
    from fmc.georeference import implied_scale

    # Correct: 1 pixel = 0.05 meters
    correct_points = [
        ControlPoint(px=100, py=100, x=0.0, y=0.0),
        ControlPoint(px=300, py=100, x=10.0, y=0.0),
        ControlPoint(px=100, py=300, x=0.0, y=10.0),
    ]
    correct_scale_x, correct_scale_y = implied_scale(fit_transform(correct_points))
    assert 0.001 < correct_scale_x < 2.0
    assert 0.001 < correct_scale_y < 2.0

    # Same points, but real_x/real_y mistakenly given in millimeters (1000x too large) --
    # residuals alone would look perfect for this 3-point fit (it's exact either way),
    # but implied_scale should clearly flag it as unreasonable.
    mm_points = [
        ControlPoint(px=100, py=100, x=0.0, y=0.0),
        ControlPoint(px=300, py=100, x=10000.0, y=0.0),
        ControlPoint(px=100, py=300, x=0.0, y=10000.0),
    ]
    mm_scale_x, mm_scale_y = implied_scale(fit_transform(mm_points))
    assert mm_scale_x > 2.0
    assert mm_scale_y > 2.0


def test_world_to_pixel_is_inverse_of_pixel_to_world():
    control_points = [
        ControlPoint(px=100, py=100, x=0.0, y=0.0),
        ControlPoint(px=300, py=140, x=10.0, y=1.0),
        ControlPoint(px=120, py=340, x=0.5, y=11.0),
    ]
    transform = fit_transform(control_points)

    for px, py in [(0, 0), (500, 500), (123.4, 987.6), (-50, 300)]:
        x, y = transform.pixel_to_world(px, py)
        back_px, back_py = transform.world_to_pixel(x, y)
        assert abs(back_px - px) < 1e-6
        assert abs(back_py - py) < 1e-6


def test_location_rows_ignore_header_whitespace():
    module = _load_compute_location_coords_module()
    rows = [
        {"location_id": "P5", "pixel_x": "10", "pixel_y": "20", "floor": "1", "zone ": "B"},
    ]

    normalized = module.normalize_location_rows(rows)

    assert normalized == [{"location_id": "P5", "pixel_x": "10", "pixel_y": "20", "floor": "1", "zone": "B"}]


def test_build_headings_rows_strip_utf8_bom_and_spaces():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "build_headings_csv.py"
    spec = importlib.util.spec_from_file_location("build_headings_csv", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    rows = [{"\ufefflocation_id": "P5", " headings ": "159;198;268"}]
    normalized = module.normalize_headings_rows(rows)

    assert normalized == [{"location_id": "P5", "headings": "159;198;268"}]
