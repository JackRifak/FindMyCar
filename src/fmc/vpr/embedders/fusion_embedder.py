"""Concatenate normalized NetVLAD and DINOv2-VLAD descriptors for retrieval."""
from __future__ import annotations

import numpy as np

from fmc.vpr.embedders.base import BenchmarkEmbedder
from fmc.vpr.embedders.dinov2_embedder import DINOv2Embedder
from fmc.vpr.embedders.netvlad_embedder import NetVLADEmbedder


def combine_descriptors(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Concatenate unit-normalized descriptors with equal total weight."""
    first = np.asarray(first, dtype=np.float32).reshape(-1)
    second = np.asarray(second, dtype=np.float32).reshape(-1)
    first_norm = np.linalg.norm(first)
    second_norm = np.linalg.norm(second)
    if not np.isfinite(first).all() or not np.isfinite(second).all():
        raise ValueError("Cannot fuse non-finite descriptors")
    if first_norm == 0 or second_norm == 0:
        raise ValueError("Cannot fuse a zero-norm descriptor")

    combined = np.concatenate((first / first_norm, second / second_norm))
    combined /= np.linalg.norm(combined)
    return combined.astype(np.float32)


class FusionEmbedder(BenchmarkEmbedder):
    """Equal-weight fusion of site-fitted ResNet18 NetVLAD and DINOv2-base VLAD."""

    def __init__(self, device: str = "cpu", num_clusters: int = 64):
        self.device = device
        self.netvlad = NetVLADEmbedder(
            backbone="resnet18",
            device=device,
            num_clusters=num_clusters,
        )
        self.dinov2 = DINOv2Embedder(
            model_name="facebook/dinov2-base",
            device=device,
            num_clusters=num_clusters,
        )
        self.name = "fusion_netvlad_dinov2_base"
        self._dim = self.netvlad.dim + self.dinov2.dim

    @property
    def dim(self) -> int:
        return self._dim

    def fit_clusters(self, sample_images: list[np.ndarray]) -> None:
        """Fit both VLAD vocabularies on the same site-image sample."""
        self.netvlad.fit_clusters(sample_images)
        self.dinov2.fit_clusters(sample_images)

    def embed(self, image: np.ndarray) -> np.ndarray:
        return combine_descriptors(self.netvlad.embed(image), self.dinov2.embed(image))