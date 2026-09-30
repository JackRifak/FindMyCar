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

from fmc.config import GEOMETRIC_INLIER_RATIO_THRESHOLD, ORB_MIN_MATCH_COUNT, RANSAC_REPROJ_THRESHOLD

_orb = cv2.ORB_create(nfeatures=1000)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)


@dataclass
class VerificationResult:
    is_match: bool
    inlier_ratio: float
    num_matches: int
    num_inliers: int


@dataclass
class VerificationDetails:
    result: VerificationResult
    query_keypoints: list
    candidate_keypoints: list
    matches: list
    inlier_mask: np.ndarray | None


def verify_with_details(query_image: np.ndarray, candidate_image: np.ndarray) -> VerificationDetails:
    kp1, des1 = _orb.detectAndCompute(query_image, None)
    kp2, des2 = _orb.detectAndCompute(candidate_image, None)

    if des1 is None or des2 is None or len(kp1) < ORB_MIN_MATCH_COUNT or len(kp2) < ORB_MIN_MATCH_COUNT:
        result = VerificationResult(is_match=False, inlier_ratio=0.0, num_matches=0, num_inliers=0)
        return VerificationDetails(result, kp1, kp2, [], None)

    raw_matches = _matcher.knnMatch(des1, des2, k=2)
    # Lowe's ratio test
    good = [m for m, n in raw_matches if m.distance < 0.75 * n.distance]

    if len(good) < ORB_MIN_MATCH_COUNT:
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

    result = VerificationResult(
        is_match=inlier_ratio >= GEOMETRIC_INLIER_RATIO_THRESHOLD,
        inlier_ratio=inlier_ratio,
        num_matches=len(good),
        num_inliers=num_inliers,
    )
    return VerificationDetails(result, kp1, kp2, good, mask.ravel().astype(bool))


def verify(query_image: np.ndarray, candidate_image: np.ndarray) -> VerificationResult:
    return verify_with_details(query_image, candidate_image).result
