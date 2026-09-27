"""Minimal position API — the contract the rest of the app (navigation, AR)
consumes, per project brief Section 26. Callers never see VPR/VIO/fusion
internals, only:

{
  "floor": 1, "x": 42.5, "y": 67.2, "heading": 87.5,
  "confidence": 0.93, "tracking": true, "timestamp": 1750000000
}

Run (FMC_SITE_ID is required -- there is no default):
    FMC_SITE_ID=site_00 uvicorn fmc.api.server:app --reload

This is a scaffold: one in-memory session per device_id, VIO stub tracker,
no real camera/IMU streaming yet (that arrives with a real VIO integration
and a mobile client). Good enough to test the fusion/map-matching/routing
contract end-to-end against any site with a built index.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fmc.config import load_site_config
from fmc.fusion.map_matching import snap_to_walkable
from fmc.fusion.sensor_fusion import PositionFuser
from fmc.navigation.routing import calculate_route
from fmc.vio.tracker import DeadReckoningStub
from fmc.vpr.pipeline import VPRPipeline

LOG_LEVEL = os.environ.get("FMC_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fmc.api")

app = FastAPI(title="Find My Car — Position API")

STATIC_DIR = Path(__file__).parent / "static"

_SITE_ID = os.environ.get("FMC_SITE_ID")
if not _SITE_ID:
    sys.exit(
        "FMC_SITE_ID environment variable is required (e.g. FMC_SITE_ID=site_00 "
        "uvicorn fmc.api.server:app --reload) -- there is no default site."
    )
_site = load_site_config(_SITE_ID)
_vpr_pipeline: VPRPipeline | None = None
_sessions: dict[str, PositionFuser] = {}
logger.info(f"Server initialized for site={_SITE_ID} (log_level={LOG_LEVEL})")


def _get_vpr_pipeline() -> VPRPipeline:
    global _vpr_pipeline
    if _vpr_pipeline is None:
        _vpr_pipeline = VPRPipeline(_site)
    return _vpr_pipeline


@app.post("/admin/reload")
def reload_site():
    """Dev convenience: reload the site config and force the VPR index to
    reload from disk on the next request, without restarting the server
    process. Call this after re-ingesting photos, rebuilding the index
    (fmc.dataset.build_index), or editing config.yaml (walkable_segments/
    vehicle_slots via build_site_geometry.py) for the currently running
    site -- none of those changes are picked up automatically, since the
    site config and VPR index are loaded once and cached in memory.
    """
    global _site, _vpr_pipeline
    _site = load_site_config(_SITE_ID)
    _vpr_pipeline = None
    logger.info(f"Site config and VPR pipeline cache reloaded for site={_SITE_ID}")
    return {"status": "reloaded", "site": _SITE_ID}


class PositionResponse(BaseModel):
    floor: int
    x: float
    y: float
    heading: float
    confidence: float
    tracking: bool
    timestamp: float


@app.post("/localize", response_model=PositionResponse)
async def localize(device_id: str, image: UploadFile = File(...)):
    """First fix / relocalisation: submit a camera frame, get a VPR-based position."""
    t0 = time.perf_counter()
    contents = await image.read()
    np_arr = np.frombuffer(contents, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if frame is None:
        logger.error(f"[{device_id}] Failed to decode uploaded image ({len(contents)} bytes)")
        raise HTTPException(status_code=400, detail="Failed to decode image")

    h, w, _ = frame.shape
    logger.info(f"[{device_id}] POST /localize received: frame={w}x{h} ({len(contents)/1024:.1f} KB)")

    pipeline = _get_vpr_pipeline()
    t_vpr0 = time.perf_counter()
    result = pipeline.localize(frame)
    t_vpr_ms = (time.perf_counter() - t_vpr0) * 1000

    fuser = _sessions.setdefault(device_id, PositionFuser(DeadReckoningStub(), floor=1))
    fused = fuser.on_vpr_result(result)

    if fused is None:
        total_ms = (time.perf_counter() - t0) * 1000
        logger.warning(
            f"[{device_id}] VPR MISS (no verified candidate) | "
            f"vpr={t_vpr_ms:.1f}ms | total_server={total_ms:.1f}ms"
        )
        return PositionResponse(
            floor=fuser.floor, x=0.0, y=0.0, heading=0.0,
            confidence=0.0, tracking=False, timestamp=time.time(),
        )

    snapped_x, snapped_y = snap_to_walkable(fused.x, fused.y, _site.walkable_segments(fused.floor))
    snap_drift = float(np.hypot(snapped_x - fused.x, snapped_y - fused.y))
    total_ms = (time.perf_counter() - t0) * 1000

    rec_id = result.record.image_id if result.record else "unknown"
    logger.info(
        f"[{device_id}] VPR MATCH -> photo={rec_id} (floor={fused.floor}) | "
        f"raw=({fused.x:.2f}, {fused.y:.2f}) -> snapped=({snapped_x:.2f}, {snapped_y:.2f}) "
        f"[snap_dist={snap_drift:.2f}m] | heading={fused.heading:.1f}° | conf={fused.confidence:.2f} | "
        f"vpr={t_vpr_ms:.1f}ms | total_server={total_ms:.1f}ms"
    )

    return PositionResponse(
        floor=fused.floor, x=snapped_x, y=snapped_y, heading=fused.heading,
        confidence=fused.confidence, tracking=True, timestamp=time.time(),
    )


@app.get("/position/{device_id}", response_model=PositionResponse)
def get_last_position(device_id: str):
    """Placeholder for continuous VIO-driven updates between VPR fixes.

    Real version streams camera+IMU continuously (Deliverable 5); this
    scaffold just reports whatever the fuser last knew, for contract testing.
    """
    logger.debug(f"[{device_id}] GET /position/{device_id}")
    fuser = _sessions.get(device_id)
    if fuser is None:
        return PositionResponse(floor=1, x=0.0, y=0.0, heading=0.0, confidence=0.0, tracking=False, timestamp=time.time())

    last = fuser.last_position()
    if last is None:
        return PositionResponse(floor=fuser.floor, x=0.0, y=0.0, heading=0.0, confidence=0.0, tracking=False, timestamp=time.time())

    floor, x, y = last
    return PositionResponse(
        floor=floor, x=x, y=y, heading=0.0,
        confidence=fuser._last_confidence, tracking=True, timestamp=time.time(),
    )


class RouteResponse(BaseModel):
    floor: int
    waypoints: list[tuple[float, float]]
    total_distance: float


@app.get("/route/{device_id}", response_model=RouteResponse)
def get_route(device_id: str, slot_id: str):
    """Route from the device's last known position to a vehicle slot.

    Implements project brief Section 10: current position + destination slot
    + walkable map -> ordered waypoints (Dijkstra). Does not yet handle a
    slot on a different floor than the current position (needs stairs/
    elevator/ramp transitions modeled in the walkable graph -- not in the
    mock site yet).
    """
    fuser = _sessions.get(device_id)
    if fuser is None:
        raise HTTPException(status_code=404, detail="No known position for this device — call /localize first")

    last = fuser.last_position()
    if last is None:
        raise HTTPException(status_code=404, detail="No known position for this device — call /localize first")
    floor, x, y = last

    slot = _site.vehicle_slot(slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Unknown slot_id: {slot_id}")
    if slot["floor"] != floor:
        raise HTTPException(status_code=400, detail="Cross-floor routing not yet supported")

    route = calculate_route(
        walkable_segments=_site.walkable_segments(floor),
        start_x=x, start_y=y,
        dest_x=slot["x"], dest_y=slot["y"],
    )
    if route is None:
        raise HTTPException(status_code=404, detail="No walkable route found to that slot")

    return RouteResponse(floor=floor, waypoints=route.waypoints, total_distance=route.total_distance)


# ---------------------------------------------------------------- static test client
# Mounted last so it doesn't shadow the API routes above. Serves the live
# capture test page (docs/06_live_capture_test.md) at "/".
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
