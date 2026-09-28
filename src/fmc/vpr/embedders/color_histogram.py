"""Wraps the current production baseline (fmc.vpr.embedder.ColorHistogramEmbedder)
so it shows up in every benchmark run -- the report should always make clear
how much a real model improves over what's actually deployed today."""
from __future__ import annotations

import numpy as np

from fmc.vpr.embedder import ColorHistogramEmbedder
from fmc.vpr.embedders.base import BenchmarkEmbedder


class ColorHistogramBenchmarkEmbedder(BenchmarkEmbedder):
    def __init__(self, bins: tuple[int, int, int] = (8, 4, 2), device: str | None = None):
        self._impl = ColorHistogramEmbedder(bins=bins)
        self.name = "color_histogram"
        self._dim = bins[0] * bins[1] * bins[2]
        self.device = device

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, image: np.ndarray) -> np.ndarray:
        return self._impl.embed(image)
