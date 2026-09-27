"""Image embedding model interface.

PLACEHOLDER IMPLEMENTATION — this is intentionally a cheap, dependency-light
baseline (color histogram) that exists ONLY to make the end-to-end pipeline
runnable now, before Deliverable 4 (embedding model benchmark) picks a real
model. Do not use this for real-world accuracy evaluation.

Deliverable 4 candidates to benchmark against this baseline: CLIP variants,
DINOv2, NetVLAD, and other place-recognition-specific embedding models —
evaluated on Top-1/Top-5 retrieval accuracy, robustness to lighting/occlusion,
embedding size, and on-device vs. backend inference cost.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import cv2
import numpy as np

from fmc.config import EMBEDDING_DIM


class Embedder(ABC):
    @abstractmethod
    def embed(self, image: np.ndarray) -> np.ndarray:
        """Return a fixed-length embedding vector (float32) for a BGR image."""
        raise NotImplementedError


class ColorHistogramEmbedder(Embedder):
    """Baseline placeholder: normalized HSV color histogram.

    Deliberately simple and fast so the pipeline (index build, VPR search,
    geometric verification, fusion) can be exercised end-to-end on the mock
    site without heavy ML dependencies (torch/CLIP etc.) installed yet.
    """

    def __init__(self, bins: tuple[int, int, int] = (8, 4, 2)):
        self.bins = bins
        assert bins[0] * bins[1] * bins[2] == EMBEDDING_DIM, (
            "Histogram bin product must match EMBEDDING_DIM in fmc.config"
        )

    def embed(self, image: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist(
            [hsv], [0, 1, 2], None, list(self.bins),
            [0, 180, 0, 256, 0, 256],
        )
        hist = cv2.normalize(hist, hist).flatten()
        return hist.astype(np.float32)


def get_embedder() -> Embedder:
    """Factory — swap the returned implementation once a real model is chosen."""
    return ColorHistogramEmbedder()
