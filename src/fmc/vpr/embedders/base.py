"""Common interface for benchmarkable VPR embedding models.

This mirrors fmc.vpr.embedder.Embedder's contract (embed(image) -> vector)
but adds the metadata (name, dim) the benchmark harness needs to report on
multiple models side by side. It's kept separate from the production
Embedder ABC so benchmarking code never has to touch fmc.vpr.pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BenchmarkEmbedder(ABC):
    """name is set by each implementation's __init__ (may encode config,
    e.g. 'clip_ViT-B-32_openai')."""

    name: str

    @abstractmethod
    def embed(self, image: np.ndarray) -> np.ndarray:
        """Return a fixed-length float32 embedding for a BGR uint8 image
        (same convention as fmc.vpr.embedder.Embedder)."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dim(self) -> int:
        raise NotImplementedError
