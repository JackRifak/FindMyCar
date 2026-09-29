"""Per-query z-score late fusion of NetVLAD and DINOv2-VLAD similarities."""
from __future__ import annotations

import time

import numpy as np

from fmc.vpr.embedders.dinov2_embedder import DINOv2Embedder
from fmc.vpr.embedders.netvlad_embedder import NetVLADEmbedder


def zscore_similarity_rows(similarities: np.ndarray) -> np.ndarray:
    """Z-score every query row using candidate scores, excluding its diagonal."""
    similarities = np.asarray(similarities, dtype=np.float64)
    if similarities.ndim != 2 or similarities.shape[0] != similarities.shape[1]:
        raise ValueError("similarities must be a square query-by-candidate matrix")
    if not np.isfinite(similarities).all():
        raise ValueError("similarity matrix contains non-finite values")

    standardized = np.zeros_like(similarities)
    for row_index, row in enumerate(similarities):
        candidate_mask = np.arange(len(row)) != row_index
        candidate_scores = row[candidate_mask]
        mean = candidate_scores.mean()
        std = candidate_scores.std()
        if std > np.finfo(np.float64).eps:
            standardized[row_index, candidate_mask] = (candidate_scores - mean) / std
    np.fill_diagonal(standardized, -np.inf)
    return standardized


def cosine_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Compute cosine similarity for a matrix of image embeddings."""
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.ndim != 2 or not np.isfinite(embeddings).all():
        raise ValueError("embeddings must be a finite 2D matrix")
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normalized = embeddings / np.maximum(norms, np.finfo(np.float32).eps)
    return normalized @ normalized.T


class LateFusionEmbedder:
    """Benchmark ranker that blends per-query z-scored cosine scores equally."""

    name = "late_fusion_zscore_netvlad_dinov2_base"

    def __init__(self, device: str = "cpu", num_clusters: int = 64):
        self.netvlad = NetVLADEmbedder(
            backbone="resnet18", device=device, num_clusters=num_clusters
        )
        self.dinov2 = DINOv2Embedder(
            model_name="facebook/dinov2-base", device=device, num_clusters=num_clusters
        )
        self._dim = self.netvlad.dim + self.dinov2.dim

    @property
    def dim(self) -> int:
        return self._dim

    def fit_clusters(self, sample_images: list[np.ndarray]) -> None:
        self.netvlad.fit_clusters(sample_images)
        self.dinov2.fit_clusters(sample_images)

    def score_matrix(self, images: list[np.ndarray]) -> tuple[np.ndarray, list[float]]:
        """Return equal-weighted z-score fusion and per-image inference times."""
        netvlad_vectors = []
        dinov2_vectors = []
        latencies = []
        for image in images:
            start = time.perf_counter()
            netvlad_vectors.append(self.netvlad.embed(image))
            dinov2_vectors.append(self.dinov2.embed(image))
            latencies.append((time.perf_counter() - start) * 1000)

        netvlad_scores = cosine_similarity_matrix(np.stack(netvlad_vectors))
        dinov2_scores = cosine_similarity_matrix(np.stack(dinov2_vectors))
        netvlad_z = zscore_similarity_rows(netvlad_scores)
        dinov2_z = zscore_similarity_rows(dinov2_scores)
        fused_scores = (netvlad_z + dinov2_z) / 2.0
        np.fill_diagonal(fused_scores, -np.inf)
        return fused_scores, latencies