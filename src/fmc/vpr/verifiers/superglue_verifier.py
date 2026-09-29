"""SuperPoint + original SuperGlue (Sarlin et al., CVPR 2020).

Unlike LightGlue, SuperGlue's official weights/code aren't pip-installable
-- they ship only as a research-licensed git repo. To use this verifier:

  1. From the project root:
     git clone https://github.com/magicleap/SuperGluePretrainedNetwork \
       third_party/SuperGluePretrainedNetwork
  2. pip install torch opencv-python

If you'd rather skip that setup step, lightglue_verifier.py
(superpoint_lightglue) is the maintained, faster successor from the same
research group and is what most new projects use instead of the original
SuperGlue repo -- benchmark that one first unless you specifically need
SuperGlue itself for the comparison.
"""
from __future__ import annotations

import sys
import time

import cv2
import numpy as np

from fmc.config import PROJECT_ROOT, RANSAC_REPROJ_THRESHOLD
from fmc.vpr.verifiers.base import BenchmarkVerificationResult, BenchmarkVerifier

_MODEL_CACHE: dict[str, object] = {}


class SuperGlueVerifier(BenchmarkVerifier):
    def __init__(
        self,
        min_match_count: int = 8,
        inlier_ratio_threshold: float = 0.35,
        weights: str = "indoor",
        device: str = "cpu",
        max_keypoints: int = 512,
    ):
        repo_path = PROJECT_ROOT / "third_party" / "SuperGluePretrainedNetwork"
        if not repo_path.exists():
            raise ImportError(
                f"SuperGlueVerifier needs the official repo cloned at {repo_path}. Run:\n"
                f"  git clone https://github.com/magicleap/SuperGluePretrainedNetwork {repo_path}"
            )
        if str(repo_path) not in sys.path:
            sys.path.insert(0, str(repo_path))
        try:
            import torch
            from models.matching import Matching
        except ImportError as e:
            raise ImportError(
                "SuperGlueVerifier requires torch, plus the cloned SuperGlue repo on path: "
                "pip install torch"
            ) from e

        self._torch = torch
        self.device = device
        self.min_match_count = min_match_count
        self.inlier_ratio_threshold = inlier_ratio_threshold
        self.max_keypoints = max_keypoints
        # "indoor" weights suit this project's parking-garage use case far
        # better than the "outdoor" weights SuperGlue also ships.
        self.name = f"superpoint_superglue_{weights}"

        cache_key = f"{weights}:{device}:{max_keypoints}"
        if cache_key not in _MODEL_CACHE:
            config = {
                "superpoint": {"max_keypoints": max_keypoints},
                "superglue": {"weights": weights},
            }
            _MODEL_CACHE[cache_key] = Matching(config).eval().to(device)
        self._matching = _MODEL_CACHE[cache_key]

    def _load(self, image: np.ndarray):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        tensor = self._torch.from_numpy(gray / 255.0).float()[None, None].to(self.device)
        return tensor

    def verify(self, query_image: np.ndarray, candidate_image: np.ndarray) -> BenchmarkVerificationResult:
        t0 = time.perf_counter()
        img0 = self._load(query_image)
        img1 = self._load(candidate_image)

        with self._torch.no_grad():
            pred = self._matching({"image0": img0, "image1": img1})
        pred = {k: v[0].cpu().numpy() for k, v in pred.items()}
        kpts0, kpts1 = pred["keypoints0"], pred["keypoints1"]
        matches = pred["matches0"]  # -1 for unmatched keypoints
        valid = matches > -1
        num_matches = int(valid.sum())

        if num_matches < self.min_match_count:
            latency_ms = (time.perf_counter() - t0) * 1000
            return BenchmarkVerificationResult(False, 0.0, num_matches, 0, latency_ms)

        mkpts0 = kpts0[valid]
        mkpts1 = kpts1[matches[valid]]

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
