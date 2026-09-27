"""Common interface for benchmarkable geometric verification methods."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class BenchmarkVerificationResult:
    is_match: bool
    inlier_ratio: float
    num_matches: int
    num_inliers: int
    latency_ms: float


class BenchmarkVerifier(ABC):
    name: str

    @abstractmethod
    def verify(self, query_image: np.ndarray, candidate_image: np.ndarray) -> BenchmarkVerificationResult:
        raise NotImplementedError
