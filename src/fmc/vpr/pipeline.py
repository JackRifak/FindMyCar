"""End-to-end VPR pipeline: query image -> verified position estimate.

Flow:
  1) Optional 6-DOF PnP against mapped 3D landmarks (2D-to-3D + RANSAC)
  2) Fallback: embedding retrieval -> Top-K -> geometric verification -> 2D fix
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import cv2
import numpy as np

from fmc.config import SiteConfig
from fmc.dataset.schema import CameraInfo, ReferenceImage
from fmc.vpr.embedder import get_embedder
from fmc.vpr.geometric_verification import verify
from fmc.vpr.pnp_localizer import Pose6Dof, available_map_floors, load_map_index
from fmc.vpr.search import VectorIndex

logger = logging.getLogger("fmc.vpr")


@dataclass
class CandidateTrace:
    rank: int
    image_id: str
    similarity: float
    location_id: str
    x: float
    y: float
    timestamp: str
    num_matches: int
    num_inliers: int
    inlier_ratio: float
    passed: bool
    image_readable: bool = True


@dataclass
class VPRResult:
    matched: bool
    record: ReferenceImage | None
    similarity: float
    inlier_ratio: float
    candidates: list[CandidateTrace]
    pose_6dof: Pose6Dof | None = None
    method: str = "none"  # "pnp" | "image_vpr" | "none"


class VPRPipeline:
    def __init__(self, site: SiteConfig):
        self.site = site
        self.embedder = get_embedder(site)
        self.index = VectorIndex.load(site)
        self.map_index = load_map_index(site)
        self._floor_indexes: dict[int, object] = {}
        n_map = 0 if self.map_index is None else len(self.map_index.positions)
        logger.info(
            f"VPRPipeline initialized with {len(self.index.ids)} index records, "
            f"{n_map} 3D map landmarks"
        )

    def reload_map_index(self) -> None:
        """Reload H2GIS / NPZ map index after a new mapping finalize."""
        self.map_index = load_map_index(self.site)
        self._floor_indexes: dict[int, object] = {}
        n = 0 if self.map_index is None else len(self.map_index.positions)
        logger.info("[VPR] Reloaded 3D map index (%s landmarks)", n)

    def _index_for_floor(self, floor: str):
        from fmc.floors import normalize_floor_id
        floor = normalize_floor_id(floor)
        cache = getattr(self, "_floor_indexes", None)
        if cache is None:
            self._floor_indexes = {}
            cache = self._floor_indexes
        if floor in cache:
            return cache[floor]
        idx = load_map_index(self.site, floor=floor)
        cache[floor] = idx
        return idx

    def _candidate_floors(self, prior_floor, query_image: np.ndarray) -> list[str]:
        from fmc.floors import adjacent_floors, normalize_floor_id

        mapped = available_map_floors(self.site)
        declared = self.site.floor_ids_bottom_up()  # physical adjacency (B2–B1–G)
        if not mapped and self.map_index is not None:
            mapped = declared or ["1"]

        ordered: list[str] = []
        if prior_floor is not None:
            cur = normalize_floor_id(prior_floor)
            ordered.append(cur)
            for adj in adjacent_floors(declared or mapped, cur):
                if adj in mapped and adj not in ordered:
                    ordered.append(adj)

        # global embedding majority vote for floor when unknown / as fallback
        if prior_floor is None or len(ordered) < 2:
            try:
                emb = self.embedder.embed(query_image)
                hits = self.index.query(emb)
                votes: dict[str, int] = {}
                for hit in hits[:5]:
                    f = normalize_floor_id(getattr(hit.record, "floor", "1") or "1")
                    votes[f] = votes.get(f, 0) + 1
                for f, _ in sorted(votes.items(), key=lambda kv: -kv[1]):
                    if f not in ordered:
                        ordered.append(f)
            except Exception as e:
                logger.debug("[VPR] floor vote skipped: %s", e)

        for f in mapped:
            if f not in ordered:
                ordered.append(f)
        return ordered or ["1"]

    def localize(
        self,
        query_image: np.ndarray,
        prior_floor=None,
        restrict_floor: bool = False,
        intrinsics=None,
    ) -> VPRResult:
        """restrict_floor: only try prior_floor (floor already confirmed, e.g. by cabin colour).
        intrinsics: fmc.vpr.camera_intrinsics.Intrinsics of this exact image (else a guess)."""
        from fmc.floors import normalize_floor_id
        from fmc.vpr.camera_intrinsics import camera_matrix

        h_img, w_img = query_image.shape[:2]
        K = camera_matrix(w_img, h_img, intrinsics) if intrinsics is not None else None

        # --- Stage A: floor-partitioned 2D-to-3D PnP ---
        if restrict_floor and prior_floor is not None:
            floors = [normalize_floor_id(prior_floor)]
        else:
            floors = self._candidate_floors(prior_floor, query_image)
        for floor in floors:
            idx = self._index_for_floor(floor)
            if idx is None:
                continue
            t0 = time.perf_counter()
            pose = idx.localize(query_image, K=K)
            t_pnp = (time.perf_counter() - t0) * 1000
            if pose is None:
                logger.info("[VPR] PnP floor=%s failed (%.1fms)", floor, t_pnp)
                continue
            floor = normalize_floor_id(floor)
            pose.floor = floor
            logger.info(
                "[VPR] PnP localization succeeded in %.1fms "
                "floor=%s at (%.2f, %.2f) heading=%.0f",
                t_pnp, floor, pose.x, pose.y, pose.heading,
            )
            record = ReferenceImage(
                image_id="pnp_fix",
                floor=floor,
                zone=f"floor:{floor}",
                x=pose.x,
                y=pose.y,
                orientation=int(round(pose.heading)) % 360,
                timestamp=datetime.now(timezone.utc).isoformat(),
                camera_information=CameraInfo(),
                processed_path="",
                location_id="pnp",
            )
            return VPRResult(
                matched=True,
                record=record,
                similarity=pose.confidence,
                inlier_ratio=pose.inlier_ratio,
                candidates=[],
                pose_6dof=pose,
                method="pnp",
            )

        # unpartitioned fallback (legacy single-floor maps)
        if self.map_index is not None and not floors:
            t0 = time.perf_counter()
            pose = self.map_index.localize(query_image, K=K)
            t_pnp = (time.perf_counter() - t0) * 1000
            if pose is not None:
                floor = normalize_floor_id(getattr(pose, "floor", prior_floor or "1") or "1")
                pose.floor = floor
                record = ReferenceImage(
                    image_id="pnp_fix",
                    floor=floor,
                    zone=f"floor:{floor}",
                    x=pose.x,
                    y=pose.y,
                    orientation=int(round(pose.heading)) % 360,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    camera_information=CameraInfo(),
                    processed_path="",
                    location_id="pnp",
                )
                return VPRResult(
                    matched=True,
                    record=record,
                    similarity=pose.confidence,
                    inlier_ratio=pose.inlier_ratio,
                    candidates=[],
                    pose_6dof=pose,
                    method="pnp",
                )
            logger.info("[VPR] PnP failed (%.1fms)", t_pnp)

        # --- Stage B: classic image retrieval + geometric verification ---
        # disabled for now: image_vpr returns the reference photo's pose (not the camera's),
        # which is too coarse for AR anchoring/alignment — PnP or nothing.
        # return self._localize_image_vpr(query_image)
        logger.info("[VPR] PnP failed — image_vpr fallback disabled, no fix")
        return VPRResult(
            matched=False,
            record=None,
            similarity=0.0,
            inlier_ratio=0.0,
            candidates=[],
            method="none",
        )

    def _localize_image_vpr(self, query_image: np.ndarray) -> VPRResult:
        t0 = time.perf_counter()
        query_embedding = self.embedder.embed(query_image)
        t_embed = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        candidates = self.index.query(query_embedding)
        t_search = (time.perf_counter() - t1) * 1000

        logger.debug(
            f"VPR query: embed={t_embed:.1f}ms, search={t_search:.1f}ms -> retrieved {len(candidates)} candidates"
        )

        candidate_traces = []
        for i, candidate in enumerate(candidates):
            candidate_img_path = self.site.processed_dir / candidate.record.processed_path
            candidate_img = cv2.imread(str(candidate_img_path))
            if candidate_img is None:
                logger.warning(f"Candidate image not found on disk: {candidate_img_path}")
                candidate_traces.append(
                    CandidateTrace(
                        rank=i + 1,
                        image_id=candidate.image_id,
                        similarity=candidate.similarity,
                        location_id=candidate.record.location_id,
                        x=candidate.record.x,
                        y=candidate.record.y,
                        timestamp=candidate.record.timestamp,
                        num_matches=0,
                        num_inliers=0,
                        inlier_ratio=0.0,
                        passed=False,
                        image_readable=False,
                    )
                )
                continue

            t_ver0 = time.perf_counter()
            verification = verify(query_image, candidate_img)
            t_ver = (time.perf_counter() - t_ver0) * 1000
            candidate_traces.append(
                CandidateTrace(
                    rank=i + 1,
                    image_id=candidate.image_id,
                    similarity=candidate.similarity,
                    location_id=candidate.record.location_id,
                    x=candidate.record.x,
                    y=candidate.record.y,
                    timestamp=candidate.record.timestamp,
                    num_matches=verification.num_matches,
                    num_inliers=verification.num_inliers,
                    inlier_ratio=verification.inlier_ratio,
                    passed=verification.is_match,
                )
            )

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
                    candidates=candidate_traces,
                    method="image_vpr",
                )

        logger.debug(f"VPR: no candidate survived geometric verification out of {len(candidates)} candidates")
        return VPRResult(
            matched=False,
            record=None,
            similarity=0.0,
            inlier_ratio=0.0,
            candidates=candidate_traces,
            method="none",
        )
