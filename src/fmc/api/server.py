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

import csv
import logging
import math
import os
import json
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fmc.config import load_site_config
from fmc.api.live_diagnostics import append_client_event, image_quality_metrics, persist_live_miss
from fmc.dataset.schema import load_records
from fmc.dataset.locations import load_locations_csv
from fmc.fusion.map_matching import snap_to_walkable
from fmc.fusion.sensor_fusion import PositionFuser
from fmc.navigation.routing import calculate_route
from fmc.vio.tracker import SixDofPose, TrueVIOTracker
from fmc.vpr.pipeline import VPRPipeline
from fmc.mapping.continuous_mapper import ContinuousMapper

LOG_LEVEL = os.environ.get("FMC_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fmc.api")

app = FastAPI(title="Find My Car — Position API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
PARKING_UI_DIR = Path(__file__).parent / "parking_ui"

_SITE_ID = os.environ.get("FMC_SITE_ID")
if not _SITE_ID:
    sys.exit(
        "FMC_SITE_ID environment variable is required (e.g. FMC_SITE_ID=site_00 "
        "uvicorn fmc.api.server:app --reload) -- there is no default site."
    )
_site = load_site_config(_SITE_ID)
LIVE_MISS_SAMPLE_LIMIT = max(0, int(os.environ.get("FMC_LIVE_MISS_SAMPLE_LIMIT", "100")))
_vpr_pipeline: VPRPipeline | None = None
_sessions: dict[str, PositionFuser] = {}
logger.info(f"Server initialized for site={_SITE_ID} (log_level={LOG_LEVEL})")


def _get_vpr_pipeline() -> VPRPipeline:
    global _vpr_pipeline
    if _vpr_pipeline is None:
        _vpr_pipeline = VPRPipeline(_site)
    return _vpr_pipeline

# Global mapper instance for the continuous mapping feature
_global_mapper = ContinuousMapper()
_finalize_lock = threading.Lock()


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
    # optional 6-DOF fields from PnP localization
    method: str = "none"
    z: float | None = None
    num_inliers: int | None = None
    num_matches: int | None = None


@app.get("/diagnostics/locations")
def diagnostic_locations():
    """Capture-location IDs for optional live-miss ground-truth annotation."""
    unique = {}
    for record in load_records(_site.dataset_jsonl_path):
        if record.location_id:
            unique.setdefault(
                record.location_id,
                {"location_id": record.location_id, "floor": record.floor, "zone": record.zone, "x": record.x, "y": record.y},
            )
    return sorted(unique.values(), key=lambda item: item["location_id"])


@app.post("/diagnostics/client-event")
def diagnostics_client_event(event: dict):
    """Persist client-side capture events such as frames rejected as blurry."""
    append_client_event(_site.data_dir, {"site_id": _SITE_ID, "received_at": time.time(), **event})
    return {"logged": True}


@app.post("/localize", response_model=PositionResponse)
async def localize(
    device_id: str,
    image: UploadFile = File(...),
    ground_truth_location_id: str | None = Form(None),
    capture_trigger: str = Form("manual"),
    motion_score: float | None = Form(None),
    client_laplacian_variance: float | None = Form(None),
    source_width: int | None = Form(None),
    source_height: int | None = Form(None),
    encoded_width: int | None = Form(None),
    encoded_height: int | None = Form(None),
    jpeg_quality: float | None = Form(None),
    screen_orientation: str | None = Form(None),
    max_dimension: int | None = Form(None),
    camera_track_settings: str | None = Form(None),
    # optional ARCore pose at capture time — locks VIO→facility on this fix
    vio_x: float | None = Form(None),
    vio_y: float | None = Form(None),
    vio_z: float | None = Form(None),
    vio_qw: float | None = Form(None),
    vio_qx: float | None = Form(None),
    vio_qy: float | None = Form(None),
    vio_qz: float | None = Form(None),
):
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

    fuser = _sessions.setdefault(device_id, PositionFuser(TrueVIOTracker(), floor=1))
    # seed VIO pose before reset so facility lock is relative to current ARCore frame
    if vio_x is not None and vio_z is not None:
        tracker = fuser.vio_tracker
        if isinstance(tracker, TrueVIOTracker):
            tracker.update_from_6dof(SixDofPose(
                timestamp=time.time(),
                x=float(vio_x),
                y=float(vio_y or 0.0),
                z=float(vio_z),
                qw=float(vio_qw if vio_qw is not None else 1.0),
                qx=float(vio_qx or 0.0),
                qy=float(vio_qy or 0.0),
                qz=float(vio_qz or 0.0),
                tracking_status="tracking",
            ))
    fused = fuser.on_vpr_result(result)

    if fused is None:
        total_ms = (time.perf_counter() - t0) * 1000
        ground_truth_records = [
            record for record in load_records(_site.dataset_jsonl_path)
            if ground_truth_location_id and record.location_id == ground_truth_location_id
        ]
        ground_truth_retrieved = None
        if ground_truth_location_id:
            ground_truth_retrieved = any(
                candidate.location_id == ground_truth_location_id
                or any(math.hypot(candidate.x - record.x, candidate.y - record.y) <= 0.5 for record in ground_truth_records)
                for candidate in result.candidates
            )
        if not result.candidates:
            failure_stage = "no_candidates"
        elif ground_truth_location_id and not ground_truth_retrieved:
            failure_stage = "retrieval_failure"
        elif ground_truth_location_id and ground_truth_retrieved:
            failure_stage = "verification_rejection"
        else:
            failure_stage = "verification_rejected_ground_truth_unknown"
        metadata = {
            "site_id": _SITE_ID,
            "received_at": time.time(),
            "device_id": device_id,
            "capture_trigger": capture_trigger,
            "ground_truth_location_id": ground_truth_location_id or None,
            "ground_truth_in_top_k": ground_truth_retrieved,
            "failure_stage": failure_stage,
            "candidate_count": len(result.candidates),
            "candidates": [candidate.__dict__ for candidate in result.candidates],
            "uploaded_bytes": len(contents),
            "client": {
                "motion_score": motion_score,
                "laplacian_variance": client_laplacian_variance,
                "source_width": source_width,
                "source_height": source_height,
                "encoded_width": encoded_width,
                "encoded_height": encoded_height,
                "jpeg_quality": jpeg_quality,
                "screen_orientation": screen_orientation,
                "max_dimension": max_dimension,
                "camera_track_settings": {},
            },
            "server_image": image_quality_metrics(frame),
            "vpr_latency_ms": t_vpr_ms,
        }
        if camera_track_settings:
            try:
                metadata["client"]["camera_track_settings"] = json.loads(camera_track_settings)
            except json.JSONDecodeError:
                metadata["client"]["camera_track_settings"] = {"parse_error": True}
        try:
            miss_record = persist_live_miss(_site.data_dir, frame, metadata, LIVE_MISS_SAMPLE_LIMIT)
            logger.warning(
                f"[{device_id}] VPR MISS | stage={failure_stage} "
                f"ground_truth_retrieved={ground_truth_retrieved} candidates={len(result.candidates)} "
                f"sample={miss_record['frame_path']}"
            )
        except OSError:
            logger.exception("Could not persist live VPR miss diagnostics")
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

    # keep fuser / 3D viewer on the snapped walkable pose
    fuser._last_x, fuser._last_y = snapped_x, snapped_y
    fuser._last_heading = float(fused.heading)
    fuser.mark_live()

    rec_id = result.record.image_id if result.record else "unknown"
    logger.info(
        f"[{device_id}] LOCALIZE MATCH method={result.method} photo={rec_id} (floor={fused.floor}) | "
        f"raw=({fused.x:.2f}, {fused.y:.2f}) -> snapped=({snapped_x:.2f}, {snapped_y:.2f}) "
        f"[snap_dist={snap_drift:.2f}m] | heading={fused.heading:.1f}° | conf={fused.confidence:.2f} | "
        f"vpr={t_vpr_ms:.1f}ms | total_server={total_ms:.1f}ms"
    )

    pose = result.pose_6dof
    return PositionResponse(
        floor=fused.floor,
        x=snapped_x,
        y=snapped_y,
        heading=fused.heading,
        confidence=fused.confidence,
        tracking=True,
        timestamp=time.time(),
        method=result.method,
        z=pose.z if pose else None,
        num_inliers=pose.num_inliers if pose else None,
        num_matches=pose.num_matches if pose else None,
    )


@app.get("/map/live-poses")
def map_live_poses():
    """Active localization poses only (hidden when the client ends its session)."""
    poses = []
    for did, fuser in _sessions.items():
        if not getattr(fuser, "is_live", lambda: False)():
            if getattr(fuser, "_live_marker", False):
                # ttl expired — clear marker flag
                fuser.end_live()
            continue
        last = fuser.last_position()
        if last is None:
            continue
        floor, x, y = last
        poses.append({
            "device_id": did,
            "floor": floor,
            "x": float(x),
            "y": float(y),
            "z": getattr(fuser, "_last_z", None),
            "heading": float(getattr(fuser, "_last_heading", 0.0) or 0.0),
            "confidence": float(getattr(fuser, "_last_confidence", 0.0) or 0.0),
            "tracking": True,
            "method": getattr(fuser, "_last_method", "none") or "none",
            "live_age_s": round(time.time() - float(getattr(fuser, "_live_ts", 0.0) or 0.0), 1),
        })
    return {"status": "success", "poses": poses}


@app.post("/position/{device_id}/end")
def end_live_position(device_id: str):
    """Client localization session ended — remove 3D live marker for this device."""
    fuser = _sessions.get(device_id)
    if fuser is None:
        return {"status": "ok", "device_id": device_id, "live": False}
    fuser.end_live()
    logger.info("[%s] live localization ended — 3D marker cleared", device_id)
    return {"status": "ok", "device_id": device_id, "live": False}


@app.post("/position/{device_id}/heartbeat")
def heartbeat_live_position(device_id: str):
    """Keep 3D live marker alive while the client is still navigating."""
    fuser = _sessions.get(device_id)
    if fuser is None or not getattr(fuser, "_live_marker", False):
        return {"status": "ok", "device_id": device_id, "live": False}
    fuser.mark_live()
    return {"status": "ok", "device_id": device_id, "live": True}


class TrackPoseBody(BaseModel):
    timestamp: float | None = None
    x: float
    y: float
    z: float = 0.0
    qw: float = 1.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    tracking_status: str = "tracking"


@app.post("/track/{device_id}", response_model=PositionResponse)
def track_six_dof(device_id: str, body: TrackPoseBody):
    """Push ARCore/6-DOF odometry between VPR fixes (live localization)."""
    fuser = _sessions.setdefault(device_id, PositionFuser(TrueVIOTracker(), floor=1))
    # need an absolute fix first — otherwise marker stays off
    if fuser.last_position() is None and not getattr(fuser, "_live_marker", False):
        return PositionResponse(
            floor=fuser.floor, x=0.0, y=0.0, heading=0.0,
            confidence=0.0, tracking=False, timestamp=time.time(),
            method="none",
        )
    six = SixDofPose(
        timestamp=float(body.timestamp or time.time()),
        x=float(body.x),
        y=float(body.y),
        z=float(body.z),
        qw=float(body.qw),
        qx=float(body.qx),
        qy=float(body.qy),
        qz=float(body.qz),
        tracking_status=body.tracking_status or "tracking",
    )
    fused = fuser.on_six_dof(six)
    return PositionResponse(
        floor=fused.floor,
        x=fused.x,
        y=fused.y,
        heading=fused.heading,
        confidence=fused.confidence,
        tracking=fused.tracking_status == "tracking",
        timestamp=time.time(),
        method="vio6dof",
        z=getattr(fuser, "_last_z", None),
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
        floor=floor,
        x=x,
        y=y,
        heading=float(getattr(fuser, "_last_heading", 0.0) or 0.0),
        confidence=fuser._last_confidence,
        tracking=True,
        timestamp=time.time(),
        method=getattr(fuser, "_last_method", "none") or "none",
        z=getattr(fuser, "_last_z", None),
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


@app.get("/slots")
def get_available_slots():
    """Return all configured parking slots for the active site."""
    slots = []
    for floor_entry in _site.raw.get("floors", []):
        for slot in floor_entry.get("vehicle_slots", []):
            slots.append({
                "slot_id": slot["slot_id"],
                "floor": floor_entry["floor"],
                "zone": slot.get("zone"),
                "x": slot["x"],
                "y": slot["y"],
            })
    return {"site_id": _SITE_ID, "slots": slots}


# ---------------------------------------------------------------- Mapping API

class MappingTag(BaseModel):
    timestamp: float
    x: float
    y: float
    floor: int

def _persist_live_tags() -> None:
    tags_path = _site.index_dir / "tags.json"
    tags_data = _global_mapper.get_tags_3d()
    try:
        tags_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tags_path, "w") as f:
            json.dump(tags_data, f, indent=2)
    except OSError as e:
        logger.warning("[API /map/tag] Failed writing tags.json: %s", e)


@app.post("/map/new-session")
def map_new_session():
    """Start a separated mapping walk; keep prior sessions' landmarks and tags."""
    # reload finalized cloud/H2GIS into memory so the next finalize cannot wipe them
    info = _global_mapper.begin_session(site=_site)
    logger.info(
        "[API /map/new-session] session=%s total_landmarks=%s total_tags=%s",
        info.get("session_id"), info.get("total_landmarks"), info.get("total_tags"),
    )
    return {"status": "success", "message": "new mapping session", **info}


@app.post("/map/reset")
def map_reset():
    """Wipe all mapping sessions and on-disk survey artifacts."""
    _global_mapper.reset()
    # drop stale survey files so the viewer / PnP cannot use an old walk
    for name in ("tags.json", "pointcloud.ply", "map_landmarks.npz"):
        p = _site.index_dir / name
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                logger.warning("[API /map/reset] Could not remove %s: %s", name, e)
    try:
        from fmc.storage.h2gis_store import delete_site_db
        delete_site_db(_site)
    except Exception as e:
        logger.warning("[API /map/reset] H2GIS cleanup failed: %s", e)
    if _vpr_pipeline is not None:
        try:
            _vpr_pipeline.reload_map_index()
        except Exception as e:
            logger.warning("[API /map/reset] PnP reload failed: %s", e)
    return {"status": "success", "message": "all mapping sessions cleared"}


@app.post("/map/keyframe")
async def map_keyframe(
    device_id: str = Form(...),
    timestamp: float = Form(...),
    vio_x: float = Form(...),
    vio_y: float = Form(...),
    vio_z: float = Form(...),
    vio_qw: float = Form(...),
    vio_qx: float = Form(...),
    vio_qy: float = Form(...),
    vio_qz: float = Form(...),
    image: UploadFile = File(...),
    heading_deg: float | None = Form(None),
):
    """Ingest a camera frame + VIO pose for 3D map building."""
    contents = await image.read()
    np_arr = np.frombuffer(contents, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    
    if frame is None:
        logger.error(f"[API /map/keyframe] Failed to decode image from device {device_id} ({len(contents)} bytes)")
        raise HTTPException(status_code=400, detail="Failed to decode mapping image")
        
    pose = SixDofPose(
        timestamp=timestamp, x=vio_x, y=vio_y, z=vio_z, 
        qw=vio_qw, qx=vio_qx, qy=vio_qy, qz=vio_qz, tracking_status="tracking"
    )
    
    logger.info(
        f"[API /map/keyframe] Incoming from {device_id}: "
        f"image={frame.shape[1]}x{frame.shape[0]} ({len(contents)/1024:.1f} KB), "
        f"VIO=({vio_x:.2f}, {vio_y:.2f}, {vio_z:.2f}), "
        f"heading={heading_deg if heading_deg is not None else 'n/a'}, "
        f"Q=[{vio_qw:.2f}, {vio_qx:.2f}, {vio_qy:.2f}, {vio_qz:.2f}]"
    )

    # first frame after reload: pull prior finalized map so it isn't lost on next finalize
    if not getattr(_global_mapper, "_disk_hydrated", False):
        try:
            _global_mapper.hydrate_from_disk(_site)
            if _global_mapper.landmarks or _global_mapper.user_tags:
                # don't append new walk onto an already-aligned session id
                from fmc.mapping.continuous_mapper import COMMITTED_SESSION
                aligned = getattr(_global_mapper, "_aligned_sessions", set())
                if _global_mapper._session_id in aligned or COMMITTED_SESSION in aligned:
                    _global_mapper._session_id = _global_mapper._next_free_session_id()
                    logger.info(
                        "[API /map/keyframe] Auto session=%s after hydrating %s landmarks",
                        _global_mapper._session_id, len(_global_mapper.landmarks),
                    )
        except Exception as e:
            logger.warning("[API /map/keyframe] Disk hydrate failed: %s", e)

    frame_id = _global_mapper.add_keyframe(frame, timestamp, pose, heading_deg=heading_deg)
    new_landmarks = getattr(_global_mapper, 'last_new_landmarks', 0)
    total_landmarks = len(_global_mapper.landmarks)
    total_keyframes = len(_global_mapper.keyframes)

    logger.info(
        f"[API /map/keyframe] Ingest complete: Frame #{frame_id} "
        f"(+{new_landmarks} new 3D points | {total_landmarks} total landmarks | {total_keyframes} keyframes)"
    )

    return {
        "status": "success",
        "frame_id": frame_id,
        "new_landmarks": new_landmarks,
        "total_landmarks": total_landmarks,
        "total_keyframes": total_keyframes,
        "image_width": int(frame.shape[1]),
        "image_height": int(frame.shape[0]),
        "detected_features": getattr(_global_mapper, 'last_detected_keypoints', []),
        "tracked_features": getattr(_global_mapper, 'last_tracked_keypoints', [])
    }

@app.post("/map/tag")
def map_tag(tag: MappingTag):
    """Add a ground truth physical anchor to the map."""
    logger.info(f"[API /map/tag] Tag received: pos=({tag.x:.2f}, {tag.y:.2f}), floor={tag.floor}, time={tag.timestamp}")
    _global_mapper.add_tag(tag.timestamp, tag.x, tag.y, tag.floor)
    total_tags = len(_global_mapper.user_tags)
    _persist_live_tags()
    logger.info(f"[API /map/tag] Total tags recorded: {total_tags}")
    return {
        "status": "success",
        "tags_recorded": total_tags,
        "tags": _global_mapper.get_tags_3d(),
    }

@app.get("/map/tags")
def get_map_tags():
    """Retrieve all recorded ground control tags and facility landmarks for 3D visualization."""
    tags = _global_mapper.get_tags_3d()
    aligned = bool(getattr(_global_mapper, "_is_aligned", False))
    
    # If mapper in-memory tags are empty, check disk for finalized tags.json
    if not tags:
        tags_file = _site.index_dir / "tags.json"
        if tags_file.exists():
            try:
                with open(tags_file, "r") as f:
                    tags = json.load(f)
                if tags:
                    aligned = bool(tags[0].get("aligned", False))
            except Exception as e:
                logger.warning(f"[API /map/tags] Failed reading tags.json: {e}")
                
    # Facility spots only make sense once the cloud is in facility coordinates
    facility_landmarks = []
    if aligned:
        locations_file = _site.index_dir / "locations.csv"
        if locations_file.exists():
            try:
                loc_rows = load_locations_csv(locations_file)
                for r in loc_rows:
                    facility_landmarks.append({
                        "id": r["location_id"],
                        "label": f"Spot {r['location_id']}",
                        "type": "facility_landmark",
                        "facility_x": float(r["x"]),
                        "facility_y": float(r["y"]),
                        "floor": int(r["floor"]),
                        "zone": r.get("zone", ""),
                        "x": float(r["x"]),
                        "y": float(r["y"]),
                        "z": 0.0
                    })
            except Exception as e:
                logger.warning(f"[API /map/tags] Failed reading locations.csv: {e}")

    return {
        "status": "success",
        "aligned": aligned,
        "user_tags": tags,
        "facility_landmarks": facility_landmarks,
        "all_landmarks": tags + facility_landmarks
    }

@app.post("/map/finalize")
def map_finalize():
    """Finalize map, optimizing poses against the tags."""
    if not _finalize_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="finalize already running — wait for it to finish (do not tap again)",
        )
    try:
        _global_mapper.hydrate_from_disk(_site)
        n_tags = len(_global_mapper.user_tags)
        aligned = set(getattr(_global_mapper, "_aligned_sessions", set()))
        pending_tags = sum(1 for t in _global_mapper.user_tags if t.session_id not in aligned)
        if n_tags < 2 and not _global_mapper.landmarks:
            logger.warning("[API /map/finalize] Refusing finalize with %s tag(s) — need 2+", n_tags)
            raise HTTPException(
                status_code=400,
                detail=f"need at least 2 tags along the walk (got {n_tags})",
            )
        logger.info(
            f"[API /map/finalize] Finalize requested across {len(_global_mapper.keyframes)} keyframes, "
            f"{n_tags} tags ({pending_tags} on unaligned sessions), "
            f"{len(_global_mapper.landmarks)} landmarks in memory..."
        )
        ply_path = str(_site.index_dir / "pointcloud.ply")
        num_frames, num_landmarks = _global_mapper.finalize_map(
            output_ply_path=ply_path, site=_site,
        )
        aligned = bool(getattr(_global_mapper, "_is_aligned", False))
        npz_db = _site.index_dir / "map_landmarks.npz"
        h2_stem = _site.index_dir / "map_h2gis"
        h2_ready = Path(str(h2_stem) + ".mv.db").exists() or Path(str(h2_stem) + ".db").exists()
        # hot-reload PnP index so /localize can use the new 3D map immediately
        try:
            _get_vpr_pipeline().reload_map_index()
        except Exception as e:
            logger.warning("[API /map/finalize] Could not reload PnP map index: %s", e)
        logger.info(
            f"[API /map/finalize] Finalize completed: {num_frames} frames, {num_landmarks} landmarks, "
            f"aligned={aligned}, h2gis={h2_ready}, npz={npz_db.exists()}"
        )
        aligned_sessions = sorted(getattr(_global_mapper, "_aligned_sessions", set()))
        msg = (
            f"Map aligned ({len(aligned_sessions)} session(s))"
            if aligned
            else "Map exported (not fully aligned — each session needs 2+ tags)"
        )
        return {
            "status": "success",
            "message": msg,
            "keyframes": num_frames,
            "landmarks_3d": num_landmarks,
            "tags_recorded": n_tags,
            "aligned": aligned,
            "session_id": getattr(_global_mapper, "_session_id", 0),
            "aligned_sessions": aligned_sessions,
            "pointcloud_path": ply_path,
            "map_db_path": str(h2_stem) if h2_ready else (str(npz_db) if npz_db.exists() else None),
            "map_db_ready": h2_ready or npz_db.exists(),
            "map_db_backend": "h2gis" if h2_ready else ("npz" if npz_db.exists() else None),
        }
    finally:
        _finalize_lock.release()

@app.get("/map/floorplan")
def get_floorplan():
    """Serve the floorplan image for visual tagging."""
    img_path = _site.data_dir / "floorplan" / "floorplan.png"
    if not img_path.exists():
        logger.warning(f"[API /map/floorplan] Floorplan not found at {img_path}")
        raise HTTPException(status_code=404, detail="Floorplan image not found")
    return FileResponse(img_path)

@app.get("/map/transform")
def get_transform():
    """Serve the floorplan transform matrix to map pixels to meters."""
    transform_path = _site.data_dir / "floorplan" / "transform.json"
    if not transform_path.exists():
        logger.warning(f"[API /map/transform] Transform not found at {transform_path}")
        raise HTTPException(status_code=404, detail="Transform not found")
    with open(transform_path, "r") as f:
        return json.load(f)

@app.get("/map/survey-spots")
def get_survey_spots():
    """Pixel coords of survey spots (P5–P8) for the tagging floorplan overlay."""
    path = _site.data_dir / "floorplan" / "locations_pixels.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Survey spot pixels not found")
    want = {"P5", "P6", "P7", "P8"}
    spots = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lid = (row.get("location_id") or "").strip()
                if lid not in want:
                    continue
                spots.append({
                    "id": lid,
                    "px": float(row["pixel_x"]),
                    "py": float(row["pixel_y"]),
                    "floor": int(float(row.get("floor") or 1)),
                    "zone": (row.get("zone") or "").strip(),
                })
    except (KeyError, ValueError, OSError) as e:
        logger.warning("[API /map/survey-spots] Failed reading %s: %s", path, e)
        raise HTTPException(status_code=500, detail="Could not load survey spots") from e
    spots.sort(key=lambda s: s["id"])
    return {"status": "success", "spots": spots}

@app.get("/map/pointcloud")
def get_pointcloud():
    """Serve the generated 3D point cloud PLY file."""
    ply_path = _site.index_dir / "pointcloud.ply"
    if not ply_path.exists():
        logger.warning(f"[API /map/pointcloud] Pointcloud file not found at {ply_path}")
        raise HTTPException(status_code=404, detail="Point cloud not generated yet")
    size_bytes = ply_path.stat().st_size
    logger.info(f"[API /map/pointcloud] Serving pointcloud.ply ({size_bytes/1024:.1f} KB)")
    return FileResponse(ply_path, media_type="application/octet-stream")


@app.get("/favicon.ico")
def favicon_redirect():
    """Keep the browser from requesting a missing root favicon."""
    return RedirectResponse(url="/parking-ui/favicon.svg")


@app.get("/parking-ui")
def parking_ui_redirect():
    """Support the common browser form without a trailing slash."""
    return RedirectResponse(url="/parking-ui/", status_code=307)


@app.get("/parking")
def parking_redirect():
    """Support the same path without a trailing slash."""
    return RedirectResponse(url="/parking/", status_code=307)


@app.get("/mapping")
def mapping_redirect():
    """Redirect to the static mapping UI."""
    return RedirectResponse(url="/mapping.html", status_code=307)


# ---------------------------------------------------------------- static test client
# Mount the more specific app folders before the root catch-all so they are
# not shadowed by the general static site at "/".
app.mount("/parking-ui", StaticFiles(directory=str(PARKING_UI_DIR), html=True), name="parking_ui")
app.mount("/parking", StaticFiles(directory=str(PARKING_UI_DIR), html=True), name="parking_ui_alt")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
