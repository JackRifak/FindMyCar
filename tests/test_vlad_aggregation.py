import numpy as np
import pytest
import torch

from fmc.vpr.embedders.netvlad_embedder import aggregate_vlad, fit_vlad_centroids


def test_vlad_aggregation_handles_signed_descriptors():
    descriptors = np.array(
        [[1.0, -2.0, 0.5], [-1.0, 0.25, 1.5], [0.5, -0.75, -1.0]],
        dtype=np.float32,
    )
    centers = fit_vlad_centroids([descriptors], num_clusters=2)
    result = aggregate_vlad(torch.from_numpy(descriptors), torch.from_numpy(centers))

    assert result.shape == (6,)
    assert torch.isfinite(result).all()
    assert torch.linalg.vector_norm(result).item() == pytest.approx(1.0)