import numpy as np
import pytest

from fmc.vpr.embedders.fusion_embedder import combine_descriptors


def test_combine_descriptors_normalizes_each_component_and_full_vector():
    first = np.array([3.0, 4.0], dtype=np.float32)
    second = np.array([0.0, -2.0, 0.0], dtype=np.float32)

    combined = combine_descriptors(first, second)

    assert combined.shape == (5,)
    assert np.isfinite(combined).all()
    assert np.linalg.norm(combined) == pytest.approx(1.0)
    assert np.linalg.norm(combined[:2]) == pytest.approx(1 / np.sqrt(2))
    assert np.linalg.norm(combined[2:]) == pytest.approx(1 / np.sqrt(2))


@pytest.mark.parametrize(
    "first, second",
    [
        (np.array([np.nan], dtype=np.float32), np.array([1.0], dtype=np.float32)),
        (np.zeros(2, dtype=np.float32), np.ones(2, dtype=np.float32)),
    ],
)
def test_combine_descriptors_rejects_invalid_inputs(first, second):
    with pytest.raises(ValueError):
        combine_descriptors(first, second)