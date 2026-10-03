"""Second-stage geometric verification of VPR candidates.

Embedding similarity alone is not trusted (per project brief Section 6) —
two different areas of a parking garage can look very similar. This module
verifies a candidate match using local feature matching + RANSAC homography,
rejecting candidates whose inlier ratio is too low.

BASELINE implementation: ORB + BFMatcher + RANSAC homography. This is the
"cheap and available everywhere" choice to get the pipeline working.
Deliverable 5 should benchmark this baseline against SuperPoint+SuperGlue,
LightGlue, and LoFTR for accuracy/speed trade-offs before committing.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from fmc.config import (
    GEOMETRIC_INLIER_RATIO_THRESHOLD,
    ORB_MIN_MATCH_COUNT,
    RANSAC_REPROJ_THRESHOLD,
)

_orb = cv2.ORB_create(nfeatures=5000)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)


@dataclass
class VerificationResult:
    is_match: bool
    inlier_ratio: float
    num_matches: int
    num_inliers: int
    inlier_spread_fraction: float = 0.0


@dataclass
class VerificationDetails:
    result: VerificationResult
    query_keypoints: list
    candidate_keypoints: list
    matches: list
    inlier_mask: np.ndarray | None


def verify_with_details(
    query_image: np.ndarray,
    candidate_image: np.ndarray,
    min_inlier_spread_fraction: float = 0.0,
    ratio_test_threshold: float = 0.75,
    min_match_count: int = ORB_MIN_MATCH_COUNT,
) -> VerificationDetails:
    kp1, des1 = _orb.detectAndCompute(query_image, None)
    kp2, des2 = _orb.detectAndCompute(candidate_image, None)

    if des1 is None or des2 is None or len(kp1) < min_match_count or len(kp2) < min_match_count:
        result = VerificationResult(is_match=False, inlier_ratio=0.0, num_matches=0, num_inliers=0)
        return VerificationDetails(result, kp1, kp2, [], None)

    raw_matches = _matcher.knnMatch(des1, des2, k=2)
    # Lowe's ratio test
    good = [m for m, n in raw_matches if m.distance < ratio_test_threshold * n.distance]

    if len(good) < min_match_count:
        result = VerificationResult(is_match=False, inlier_ratio=0.0, num_matches=len(good), num_inliers=0)
        return VerificationDetails(result, kp1, kp2, good, None)

    src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    _, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, RANSAC_REPROJ_THRESHOLD)
    if mask is None:
        result = VerificationResult(is_match=False, inlier_ratio=0.0, num_matches=len(good), num_inliers=0)
        return VerificationDetails(result, kp1, kp2, good, None)

    num_inliers = int(mask.sum())
    inlier_ratio = num_inliers / len(good)
    inlier_mask = mask.ravel().astype(bool)
    if num_inliers:
        inlier_src = src_pts.reshape(-1, 2)[inlier_mask]
        inlier_dst = dst_pts.reshape(-1, 2)[inlier_mask]
        query_height, query_width = query_image.shape[:2]
        candidate_height, candidate_width = candidate_image.shape[:2]
        spread_components = (
            np.ptp(inlier_src[:, 0]) / max(query_width - 1, 1),
            np.ptp(inlier_src[:, 1]) / max(query_height - 1, 1),
            np.ptp(inlier_dst[:, 0]) / max(candidate_width - 1, 1),
            np.ptp(inlier_dst[:, 1]) / max(candidate_height - 1, 1),
        )
        inlier_spread_fraction = float(min(spread_components))
    else:
        inlier_spread_fraction = 0.0

    result = VerificationResult(
        is_match=(
            inlier_ratio >= GEOMETRIC_INLIER_RATIO_THRESHOLD
            and inlier_spread_fraction >= min_inlier_spread_fraction
        ),
        inlier_ratio=inlier_ratio,
        num_matches=len(good),
        num_inliers=num_inliers,
        inlier_spread_fraction=inlier_spread_fraction,
    )
    return VerificationDetails(result, kp1, kp2, good, inlier_mask)


def verify(
    query_image: np.ndarray,
    candidate_image: np.ndarray,
    min_inlier_spread_fraction: float = 0.0,
    ratio_test_threshold: float = 0.75,
    min_match_count: int = ORB_MIN_MATCH_COUNT,
) -> VerificationResult:
    return verify_with_details(
        query_image,
        candidate_image,
        min_inlier_spread_fraction=min_inlier_spread_fraction,
        ratio_test_threshold=ratio_test_threshold,
        min_match_count=min_match_count,
    ).result
