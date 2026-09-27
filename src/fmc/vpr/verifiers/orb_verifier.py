"""Wraps the current production baseline (fmc.vpr.geometric_verification.verify)
so it shows up in every benchmark run alongside the learned matchers."""
from __future__ import annotations

import time

import numpy as np

from fmc.vpr.geometric_verification import verify as orb_verify
from fmc.vpr.verifiers.base import BenchmarkVerificationResult, BenchmarkVerifier


class ORBVerifier(BenchmarkVerifier):
    def __init__(self):
        self.name = "orb_ransac"

    def verify(self, query_image: np.ndarray, candidate_image: np.ndarray) -> BenchmarkVerificationResult:
        t0 = time.perf_counter()
        result = orb_verify(query_image, candidate_image)
        latency_ms = (time.perf_counter() - t0) * 1000
        return BenchmarkVerificationResult(
            is_match=result.is_match,
            inlier_ratio=result.inlier_ratio,
            num_matches=result.num_matches,
            num_inliers=result.num_inliers,
            latency_ms=latency_ms,
        )
