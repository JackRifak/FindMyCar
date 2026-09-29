"""LoFTR: detector-free dense matching (Sun et al., CVPR 2021), via kornia.

Detector-free matchers tend to do best on low-texture indoor scenes (blank
walls, repetitive flooring) where sparse detectors like ORB/SuperPoint
struggle to find enough keypoints -- exactly the parking-garage scenario
here. Trade-off: no explicit keypoint-detection step means higher latency,
which matters given this project's tight (~2-3s) end-to-end latency budget
-- check benchmark_verifiers.py's latency numbers before adopting this.

Requires: pip install kornia torch
"""
from __future__ import annotations

import ssl
import time

import cv2
import numpy as np

from fmc.config import RANSAC_REPROJ_THRESHOLD
from fmc.vpr.verifiers.base import BenchmarkVerificationResult, BenchmarkVerifier

try:
    import certifi
except ImportError:  # pragma: no cover - optional dependency; degrade gracefully
    certifi = None

_MODEL_CACHE: dict[str, object] = {}


def _ensure_certifi_https_context() -> None:
    """Patch urllib's default HTTPS context to use certifi's CA bundle.

    Some Windows environments ship with a broken or incomplete certificate
    store; `torch.hub.load_state_dict_from_url()` hits that path when it tries
    to download the LoFTR checkpoint. Using certifi avoids the SSL failure
    without requiring the user to manually fix their OS trust store.
    """
    if certifi is None:
        return

    cafile = certifi.where()

    def _patched_create_default_https_context(*args, **kwargs):
        ctx = ssl.create_default_context(*args, cafile=cafile, **kwargs)
        return ctx

    ssl._create_default_https_context = _patched_create_default_https_context


class LoFTRVerifier(BenchmarkVerifier):
    def __init__(
        self,
        pretrained: str = "indoor_new",
        min_match_count: int = 8,
        inlier_ratio_threshold: float = 0.35,
        confidence_threshold: float = 0.5,
        device: str = "cpu",
        max_side: int = 640,
    ):
        try:
            import torch
            from kornia.feature import LoFTR
        except ImportError as e:
            raise ImportError("LoFTRVerifier requires kornia and torch: pip install kornia torch") from e

        self._torch = torch
        self.device = device
        self.min_match_count = min_match_count
        self.inlier_ratio_threshold = inlier_ratio_threshold
        self.confidence_threshold = confidence_threshold
        self.max_side = max_side
        # "indoor_new" (ScanNet-trained) is the closer domain match of the
        # two weight sets kornia ships for a parking garage; "outdoor" is
        # also available if you want that comparison too.
        self.name = f"loftr_{pretrained}"

        _ensure_certifi_https_context()

        cache_key = f"{pretrained}:{device}"
        if cache_key not in _MODEL_CACHE:
            _MODEL_CACHE[cache_key] = LoFTR(pretrained=pretrained).eval().to(device)
        self._matcher = _MODEL_CACHE[cache_key]

    def _load(self, image: np.ndarray):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        scale = self.max_side / max(h, w)
        if scale < 1.0:
            gray = cv2.resize(gray, (int(w * scale), int(h * scale)))
        tensor = self._torch.from_numpy(gray / 255.0).float()[None, None].to(self.device)
        return tensor

    def verify(self, query_image: np.ndarray, candidate_image: np.ndarray) -> BenchmarkVerificationResult:
        t0 = time.perf_counter()
        img0 = self._load(query_image)
        img1 = self._load(candidate_image)

        with self._torch.no_grad():
            correspondences = self._matcher({"image0": img0, "image1": img1})

        confidence = correspondences["confidence"].cpu().numpy()
        keep = confidence >= self.confidence_threshold
        mkpts0 = correspondences["keypoints0"].cpu().numpy()[keep]
        mkpts1 = correspondences["keypoints1"].cpu().numpy()[keep]
        num_matches = len(mkpts0)

        if num_matches < self.min_match_count:
            latency_ms = (time.perf_counter() - t0) * 1000
            return BenchmarkVerificationResult(False, 0.0, num_matches, 0, latency_ms)

        _, mask = cv2.findHomography(mkpts0, mkpts1, cv2.RANSAC, RANSAC_REPROJ_THRESHOLD)
        latency_ms = (time.perf_counter() - t0) * 1000
        if mask is None:
            return BenchmarkVerificationResult(False, 0.0, num_matches, 0, latency_ms)

        num_inliers = int(mask.sum())
        inlier_ratio = num_inliers / num_matches
        return BenchmarkVerificationResult(
            is_match=inlier_ratio >= self.inlier_ratio_threshold,
            inlier_ratio=inlier_ratio,
            num_matches=num_matches,
            num_inliers=num_inliers,
            latency_ms=latency_ms,
        )
