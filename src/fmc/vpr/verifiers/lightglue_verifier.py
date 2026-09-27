"""SuperPoint keypoints + LightGlue matcher.

LightGlue (Lindenberger et al., ICCV 2023) is the maintained, faster
drop-in successor to SuperGlue from the same research group (same
SuperPoint front-end, a lighter learned matcher) -- use this as the modern,
easy-to-install representative of learned-matcher verification. If you
specifically need the original SuperGlue for comparison, see
superglue_verifier.py (it requires cloning a separate research repo).

Requires: pip install torch "lightglue @ git+https://github.com/cvg/LightGlue.git"
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from fmc.config import RANSAC_REPROJ_THRESHOLD
from fmc.vpr.verifiers.base import BenchmarkVerificationResult, BenchmarkVerifier


def _ensure_lightglue_repo() -> Path:
    project_root = Path(__file__).resolve().parents[4]
    repo_candidates = [
        Path.cwd() / "LightGlue",
        project_root / "LightGlue",
        project_root / ".vendor" / "LightGlue",
    ]

    repo_dir = next((p for p in repo_candidates if p.exists() and (p / ".git").exists()), None)
    if repo_dir is None:
        repo_dir = repo_candidates[1]
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", "https://github.com/cvg/LightGlue.git", str(repo_dir)],
            check=True,
        )

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(repo_dir)],
        check=True,
    )
    return repo_dir

_EXTRACTOR_CACHE: dict[str, object] = {}
_MATCHER_CACHE: dict[str, object] = {}


class LightGlueVerifier(BenchmarkVerifier):
    def __init__(self, min_match_count: int = 15, inlier_ratio_threshold: float = 0.35, device: str = "cpu"):
        try:
            import torch
            from lightglue import LightGlue, SuperPoint
            from lightglue.utils import rbd
        except ImportError:
            _ensure_lightglue_repo()
            try:
                import torch
                from lightglue import LightGlue, SuperPoint
                from lightglue.utils import rbd
            except ImportError as e:
                raise ImportError(
                    "LightGlueVerifier requires the lightglue package and a working clone/install: "
                    'git clone https://github.com/cvg/LightGlue.git && pip install -e LightGlue'
                ) from e

        self._torch = torch
        self._rbd = rbd
        self.device = device
        self.min_match_count = min_match_count
        self.inlier_ratio_threshold = inlier_ratio_threshold
        self.name = "superpoint_lightglue"

        if device not in _EXTRACTOR_CACHE:
            _EXTRACTOR_CACHE[device] = SuperPoint(max_num_keypoints=1024).eval().to(device)
            _MATCHER_CACHE[device] = LightGlue(features="superpoint").eval().to(device)
        self._extractor = _EXTRACTOR_CACHE[device]
        self._matcher = _MATCHER_CACHE[device]

    def _load(self, image: np.ndarray):
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        tensor = self._torch.from_numpy(rgb).float().permute(2, 0, 1) / 255.0
        return tensor.unsqueeze(0).to(self.device)

    def verify(self, query_image: np.ndarray, candidate_image: np.ndarray) -> BenchmarkVerificationResult:
        t0 = time.perf_counter()
        img0 = self._load(query_image)
        img1 = self._load(candidate_image)

        with self._torch.no_grad():
            feats0 = self._extractor.extract(img0)
            feats1 = self._extractor.extract(img1)
            matches01 = self._matcher({"image0": feats0, "image1": feats1})

        feats0, feats1, matches01 = (self._rbd(x) for x in (feats0, feats1, matches01))
        matches = matches01["matches"]  # (M, 2)
        num_matches = int(matches.shape[0])

        if num_matches < self.min_match_count:
            latency_ms = (time.perf_counter() - t0) * 1000
            return BenchmarkVerificationResult(False, 0.0, num_matches, 0, latency_ms)

        kpts0 = feats0["keypoints"][matches[:, 0]].cpu().numpy()
        kpts1 = feats1["keypoints"][matches[:, 1]].cpu().numpy()

        _, mask = cv2.findHomography(kpts0, kpts1, cv2.RANSAC, RANSAC_REPROJ_THRESHOLD)
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
