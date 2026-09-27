"""End-to-end VPR pipeline: query image -> verified position estimate.

Implements the flow from project brief Section 6:
  frame -> preprocessing -> embedding -> vector search -> Top-K candidates
  -> geometric verification -> best matching location -> estimated position
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np

from fmc.config import SiteConfig
from fmc.dataset.schema import ReferenceImage
from fmc.vpr.embedder import get_embedder
from fmc.vpr.geometric_verification import verify
from fmc.vpr.search import VectorIndex

logger = logging.getLogger("fmc.vpr")


@dataclass
class VPRResult:
    matched: bool
    record: ReferenceImage | None
    similarity: float
    inlier_ratio: float


class VPRPipeline:
    def __init__(self, site: SiteConfig):
        self.site = site
        self.embedder = get_embedder()
        self.index = VectorIndex.load(site)
        logger.info(f"VPRPipeline initialized with {len(self.index.ids)} index records")

    def localize(self, query_image: np.ndarray) -> VPRResult:
        t0 = time.perf_counter()
        query_embedding = self.embedder.embed(query_image)
        t_embed = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        candidates = self.index.query(query_embedding)
        t_search = (time.perf_counter() - t1) * 1000

        logger.debug(
            f"VPR query: embed={t_embed:.1f}ms, search={t_search:.1f}ms -> retrieved {len(candidates)} candidates"
        )

        for i, candidate in enumerate(candidates):
            candidate_img_path = self.site.processed_dir / candidate.record.processed_path
            candidate_img = cv2.imread(str(candidate_img_path))
            if candidate_img is None:
                logger.warning(f"Candidate image not found on disk: {candidate_img_path}")
                continue

            t_ver0 = time.perf_counter()
            verification = verify(query_image, candidate_img)
            t_ver = (time.perf_counter() - t_ver0) * 1000

            logger.debug(
                f"Candidate #{i+1} [{candidate.record.image_id}]: sim={candidate.similarity:.3f}, "
                f"matches={verification.num_matches}, inliers={verification.num_inliers}, "
                f"ratio={verification.inlier_ratio:.3f}, pass={verification.is_match} ({t_ver:.1f}ms)"
            )

            if verification.is_match:
                logger.info(
                    f"VPR match verified: {candidate.record.image_id} at ({candidate.record.x:.2f}, {candidate.record.y:.2f}) "
                    f"floor={candidate.record.floor} (sim={candidate.similarity:.3f}, "
                    f"inliers={verification.num_inliers}/{verification.num_matches}, ratio={verification.inlier_ratio:.3f})"
                )
                return VPRResult(
                    matched=True,
                    record=candidate.record,
                    similarity=candidate.similarity,
                    inlier_ratio=verification.inlier_ratio,
                )

        logger.debug(f"VPR: no candidate survived geometric verification out of {len(candidates)} candidates")
        return VPRResult(matched=False, record=None, similarity=0.0, inlier_ratio=0.0)

