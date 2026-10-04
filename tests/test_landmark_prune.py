"""Selective landmark prune (memory + filter mask)."""
import numpy as np

from fmc.mapping.continuous_mapper import ContinuousMapper, Landmark3D


def _lm(lid, x, y, z, floor="B1"):
    return Landmark3D(
        landmark_id=lid,
        position=np.array([x, y, z], dtype=np.float64),
        observations={},
        descriptor=np.zeros(32, dtype=np.uint8),
        floor=floor,
    )


def test_drop_landmarks_by_floor():
    m = ContinuousMapper()
    m.landmarks = {
        1: _lm(1, 0, 0, 0, "B1"),
        2: _lm(2, 1, 1, 0, "G"),
        3: _lm(3, 2, 2, 0, "B1"),
    }
    n = m.drop_landmarks(floor="B1")
    assert n == 2
    assert set(m.landmarks) == {2}


def test_drop_landmarks_by_bbox_and_floor():
    m = ContinuousMapper()
    m.landmarks = {
        1: _lm(1, 0, 0, 0, "G"),
        2: _lm(2, 5, 5, 0, "G"),
        3: _lm(3, 1, 1, 0, "B1"),
    }
    n = m.drop_landmarks(floor="G", x_min=4, x_max=6, y_min=4, y_max=6)
    assert n == 1
    assert set(m.landmarks) == {1, 3}


def test_drop_landmarks_by_ids():
    m = ContinuousMapper()
    m.landmarks = {
        10: _lm(10, 0, 0, 0),
        11: _lm(11, 1, 1, 0),
        12: _lm(12, 2, 2, 0),
    }
    n = m.drop_landmarks(ids={11, 99})
    assert n == 1
    assert set(m.landmarks) == {10, 12}


def test_drop_landmarks_refuses_empty_filters():
    m = ContinuousMapper()
    m.landmarks = {1: _lm(1, 0, 0, 0)}
    assert m.drop_landmarks() == 0
    assert 1 in m.landmarks
