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

from fmc.config import get_or_create_floor, load_site_config, rename_floor, save_site_config
from fmc.api.live_diagnostics import append_client_event, image_quality_metrics, persist_live_miss
from fmc.dataset.schema import load_records
from fmc.dataset.locations import load_locations_csv
from fmc.floors import normalize_floor_id
from fmc.fusion.map_matching import match_to_walkable
from fmc.fusion.sensor_fusion import PositionFuser
from fmc.navigation.routing import calculate_multifloor_route, calculate_route
from fmc.vio.tracker import SixDofPose, TrueVIOTracker
from fmc.vpr import cabin_colour
from fmc.vpr.camera_intrinsics import parse_intrinsics
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

def _default_floor() -> str:
    ids = _site.floor_ids()
    return ids[0] if ids else "1"


# Global mapper instance for the continuous mapping feature
_global_mapper = ContinuousMapper()
_global_mapper.set_map_floor(_default_floor())
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
    # keep mapper on a floor that still exists after config edits
    cur = normalize_floor_id(getattr(_global_mapper, "_map_floor", None) or _default_floor())
    if cur not in _site.floor_ids():
        _global_mapper.set_map_floor(_default_floor())
    logger.info(f"Site config and VPR pipeline cache reloaded for site={_SITE_ID}")
    return {"status": "reloaded", "site": _SITE_ID}


class PositionResponse(BaseModel):
    floor: str
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
    # unsnapped localizer pose — where the phone really is (AR anchors here, map uses x/y)
    raw_x: float | None = None
    raw_y: float | None = None
    # camera yaw from PnP (0° = +Y, 90° = +X) — AR alignment pairs it with the ARCore pose
    raw_heading: float | None = None
    # lift-lobby colour check (only when the client asked for it)
    cabin_floor: str | None = None
    cabin_score: float | None = None


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
    payload = {"site_id": _SITE_ID, "received_at": time.time(), **event}
    append_client_event(_site.data_dir, payload)
    device_id = event.get("device_id") or "?"
    ev = event.get("event") or "client_event"
    # print every phone event (xr_track_lost, xr_align, landing_seed, …), not just a few
    reason = event.get("reason") or event.get("nav_mode") or event.get("frame_source") or ""
    logger.info(f"[{device_id}] client {ev} {reason}".strip())
    return {"logged": True}


XR_ALIGN_FRAME_KEEP = 20


def _keep_xr_align_frame(frame, result, has_intrinsics: bool) -> None:
    """Rolling sample of AR-camera alignment frames (separate from the capped miss archive)
    so PnP failures on WebXR frames can be inspected."""
    try:
        out = _site.data_dir / "diagnostics" / "xr_align_frames"
        out.mkdir(parents=True, exist_ok=True)
        pose = getattr(result, "pose_6dof", None)
        tag = (
            f"ok_{pose.num_inliers}inl" if result.matched and pose is not None
            else "miss"
        ) + ("_K" if has_intrinsics else "_guessK")
        cv2.imwrite(str(out / f"{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{tag}.jpg"), frame)
        old = sorted(out.glob("*.jpg"))
        for p in old[:-XR_ALIGN_FRAME_KEEP]:
            p.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("xr align frame not saved: %s", e)


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
    prior_floor: str | None = Form(None),
    frame_source: str | None = Form(None),
    nav_mode: str | None = Form(None),
    trigger: str | None = Form(None),
    # "1" right after a lift: classify lobby colour, restrict PnP to that floor
    cabin_check: str | None = Form(None),
    # real intrinsics of the uploaded image (WebXR camera-access frames are a screen crop —
    # the default focal guess is ~15% off for them and PnP RANSAC rejects every match)
    cam_fx: float | None = Form(None),
    cam_fy: float | None = Form(None),
    cam_cx: float | None = Form(None),
    cam_cy: float | None = Form(None),
    cam_width: float | None = Form(None),
    cam_height: float | None = Form(None),
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
    cap_trigger = capture_trigger or trigger or "manual"
    frame_src = frame_source or "unknown"
    nav = nav_mode or "unknown"
    logger.info(
        f"[{device_id}] POST /localize received: frame={w}x{h} ({len(contents)/1024:.1f} KB) "
        f"trigger={cap_trigger} frame_source={frame_src} nav_mode={nav}"
    )

    pipeline = _get_vpr_pipeline()
    fuser = _sessions.setdefault(device_id, PositionFuser(TrueVIOTracker(), floor=_default_floor()))
    floor_prior = (
        normalize_floor_id(prior_floor)
        if prior_floor is not None
        else getattr(fuser, "floor", None)
    )
    cabin = None
    restrict_floor = False
    if cabin_check and cabin_check not in ("0", "false"):
        refs = cabin_colour.load_references(_site.index_dir)
        cabin = cabin_colour.classify(frame, refs)
        logger.info(
            f"[{device_id}] CABIN colour floor={cabin.floor or '-'} score={cabin.score:.2f} "
            f"margin={cabin.margin:.2f} coloured={cabin.colour_frac:.0%} "
            f"(prior={floor_prior}, refs={sorted(refs)})"
        )
        if cabin.floor is not None:
            floor_prior = normalize_floor_id(cabin.floor)
            restrict_floor = True
    intr = parse_intrinsics(cam_fx, cam_fy, cam_cx, cam_cy, cam_width, cam_height)
    if intr is not None:
        logger.info(
            f"[{device_id}] intrinsics fx={intr.fx:.0f} fy={intr.fy:.0f} cx={intr.cx:.0f} cy={intr.cy:.0f} "
            f"for {intr.width}x{intr.height} (guess would be f={max(w, h) * 0.85:.0f})"
        )
    t_vpr0 = time.perf_counter()
    result = pipeline.localize(
        frame, prior_floor=floor_prior, restrict_floor=restrict_floor, intrinsics=intr,
    )
    t_vpr_ms = (time.perf_counter() - t_vpr0) * 1000
    if cap_trigger == "xr_align":
        _keep_xr_align_frame(frame, result, intr is not None)
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
            "capture_trigger": cap_trigger,
            "frame_source": frame_src,
            "nav_mode": nav,
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
            f"frame_source={frame_src} nav_mode={nav} | "
            f"vpr={t_vpr_ms:.1f}ms | total_server={total_ms:.1f}ms"
        )
        return PositionResponse(
            floor=normalize_floor_id(fuser.floor), x=0.0, y=0.0, heading=0.0,
            confidence=0.0, tracking=False, timestamp=time.time(),
            cabin_floor=cabin.floor if cabin else None,
            cabin_score=round(cabin.score, 3) if cabin else None,
        )

    # sticky + direction-aware: don't jump to a neighbouring corridor on a noisy fix
    # (the route would then start there and guide you across pillars/cars to reach it)
    prev_seg = getattr(fuser, "_match_seg", None)
    if getattr(fuser, "_match_floor", None) != fused.floor:
        prev_seg = None
    center: list = []
    snapped_x, snapped_y, fuser._match_seg = match_to_walkable(
        fused.x, fused.y, _site.walkable_segments(fused.floor),
        heading_deg=float(fused.heading), prev_key=prev_seg, center_out=center,
    )
    fuser._match_floor = fused.floor
    # route from the matched corridor's centre line (valid while the position is unchanged)
    fuser._route_start = (
        normalize_floor_id(fused.floor), snapped_x, snapped_y, *center[0],
    ) if center else None
    snap_drift = float(np.hypot(snapped_x - fused.x, snapped_y - fused.y))
    total_ms = (time.perf_counter() - t0) * 1000

    # keep fuser / 3D viewer on the snapped walkable pose
    fuser._last_x, fuser._last_y = snapped_x, snapped_y
    fuser._last_heading = float(fused.heading)
    fuser.mark_live()

    rec_id = result.record.image_id if result.record else "unknown"
    loc_method = result.method or "unknown"
    loc_kind = "pnp" if str(loc_method).lower() == "pnp" else "vpr"
    logger.info(
        f"[{device_id}] LOCALIZE MATCH loc={loc_kind} method={loc_method} photo={rec_id} (floor={fused.floor}) | "
        f"frame_source={frame_src} nav_mode={nav} | "
        f"raw=({fused.x:.2f}, {fused.y:.2f}) -> in_corridor=({snapped_x:.2f}, {snapped_y:.2f}) "
        f"[pulled={snap_drift:.2f}m"
        + (f" off_centre={math.hypot(fused.x - center[0][0], fused.y - center[0][1]):.2f}m" if center else "")
        + f"] | heading={fused.heading:.1f}° | conf={fused.confidence:.2f} | "
        f"vpr={t_vpr_ms:.1f}ms | total_server={total_ms:.1f}ms"
    )

    pose = result.pose_6dof
    return PositionResponse(
        floor=normalize_floor_id(fused.floor),
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
        raw_x=float(pose.x if pose else fused.x),
        raw_y=float(pose.y if pose else fused.y),
        raw_heading=float(pose.heading if pose else fused.heading),
        cabin_floor=cabin.floor if cabin else None,
        cabin_score=round(cabin.score, 3) if cabin else None,
    )


class SeedPoseBody(BaseModel):
    floor: str
    x: float
    y: float
    heading: float = 0.0
    reason: str = "door"


@app.post("/position/{device_id}/seed")
def seed_position(device_id: str, body: SeedPoseBody):
    """Client placed itself without PnP (e.g. lift door after a cabin-colour match) —
    keep server routing on the new floor until the next real fix."""
    fuser = _sessions.setdefault(device_id, PositionFuser(TrueVIOTracker(), floor=_default_floor()))
    floor = normalize_floor_id(body.floor)
    fuser.floor = floor
    fuser._last_x, fuser._last_y = float(body.x), float(body.y)
    fuser._last_heading = float(body.heading)
    fuser._last_method = f"seed_{body.reason}"
    fuser._match_seg = None  # new place — no corridor to stick to
    fuser._route_start = None
    fuser._last_confidence = 0.2
    fuser.mark_live()
    logger.info(
        f"[{device_id}] POSITION SEED reason={body.reason} floor={floor} "
        f"at ({body.x:.2f}, {body.y:.2f}) heading={body.heading:.0f}°"
    )
    return {"status": "ok", "floor": floor, "x": body.x, "y": body.y}


@app.get("/map/cabin-colours")
def get_cabin_colours():
    """Per-floor lift-lobby colour references (for the mapping app)."""
    refs = cabin_colour.load_references(_site.index_dir)
    out = {}
    for f, ref in refs.items():
        info = {k: v for k, v in ref.items() if k != "hist"}
        # nearest other floor — the mapping UI warns when two lobbies look alike
        others = [
            (cabin_colour.similarity(ref["hist"], o["hist"]), of)
            for of, o in refs.items() if of != f
        ]
        if others:
            sim, of = max(others)
            info["closest_floor"] = of
            info["closest_sim"] = round(sim, 3)
            info["too_similar"] = sim >= 1.0 - cabin_colour.MATCH_MARGIN
        out[f] = info
    return {"floors": out, "min_colour_frac": cabin_colour.MIN_COLOUR_FRAC}


@app.post("/map/cabin-colour")
async def add_cabin_colour(floor: str = Form(...), image: UploadFile = File(...)):
    """Average one frame of the lift-lobby walls into this floor's colour reference."""
    contents = await image.read()
    frame = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Failed to decode image")
    fl = normalize_floor_id(floor)
    try:
        ref = cabin_colour.add_reference(_site.index_dir, fl, frame)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # how distinct is this floor from the others now?
    refs = cabin_colour.load_references(_site.index_dir)
    check = cabin_colour.classify(frame, refs)
    logger.info(
        f"[API /map/cabin-colour] floor={fl} samples={ref['samples']} hue~{ref['dominant_hue_deg']}° "
        f"coloured={ref['colour_frac']:.0%} self-check={check.floor} margin={check.margin:.2f}"
    )
    return {
        "status": "ok",
        "floor": fl,
        "samples": ref["samples"],
        "dominant_hue_deg": ref["dominant_hue_deg"],
        "colour_frac": ref["colour_frac"],
        "self_check_floor": check.floor,
        "self_check_margin": round(check.margin, 3),
    }


@app.delete("/map/cabin-colour/{floor}")
def delete_cabin_colour(floor: str):
    refs = cabin_colour.load_references(_site.index_dir)
    removed = refs.pop(normalize_floor_id(floor), None) is not None
    cabin_colour.save_references(_site.index_dir, refs)
    return {"status": "ok", "removed": removed}


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
    fuser = _sessions.setdefault(device_id, PositionFuser(TrueVIOTracker(), floor=_default_floor()))
    # need an absolute fix first — otherwise marker stays off
    if fuser.last_position() is None and not getattr(fuser, "_live_marker", False):
        return PositionResponse(
            floor=normalize_floor_id(fuser.floor), x=0.0, y=0.0, heading=0.0,
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
        floor=normalize_floor_id(fused.floor),
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
        return PositionResponse(
            floor=_default_floor(), x=0.0, y=0.0, heading=0.0,
            confidence=0.0, tracking=False, timestamp=time.time(),
        )

    last = fuser.last_position()
    if last is None:
        return PositionResponse(
            floor=normalize_floor_id(fuser.floor), x=0.0, y=0.0, heading=0.0,
            confidence=0.0, tracking=False, timestamp=time.time(),
        )

    floor, x, y = last
    return PositionResponse(
        floor=normalize_floor_id(floor),
        x=x,
        y=y,
        heading=float(getattr(fuser, "_last_heading", 0.0) or 0.0),
        confidence=fuser._last_confidence,
        tracking=True,
        timestamp=time.time(),
        method=getattr(fuser, "_last_method", "none") or "none",
        z=getattr(fuser, "_last_z", None),
    )


class RouteLegResponse(BaseModel):
    floor: str | None = None
    instruction: str = ""
    waypoints: list[tuple[float, float]] = []
    floor_transition: dict | None = None
    distance: float = 0.0
    # True when waypoints[0] → waypoints[1] is the diagonal "join the walkway" stretch from
    # the user's position (not a corridor line — AR's walking-direction check skips it)
    joined: bool = False


class RouteResponse(BaseModel):
    floor: str
    dest_floor: str | None = None
    waypoints: list[tuple[float, float]]
    total_distance: float
    legs: list[RouteLegResponse] = []


_last_route_sig: dict[str, tuple] = {}


def _log_route(device_id: str, slot_id: str, floor: str, x: float, y: float, dest_floor: str, route) -> None:
    """Log the calculated route — only when it changes for this device + slot (the client
    re-requests it after every fix, every few seconds)."""
    legs = route.legs or []
    # signature at 0.5 m resolution: small position noise doesn't re-log an identical route
    sig = (slot_id, tuple(
        (str(leg.floor), tuple((round(px * 2) / 2, round(py * 2) / 2) for px, py in leg.waypoints),
         (leg.floor_transition or {}).get("connector_id"))
        for leg in legs
    ))
    if _last_route_sig.get(device_id) == sig:
        return
    _last_route_sig[device_id] = sig
    logger.info(
        f"[{device_id}] ROUTE to {slot_id}: {floor} ({x:.2f}, {y:.2f}) -> {dest_floor} | "
        f"{route.total_distance:.1f} m, {len(legs)} leg(s)"
    )
    for i, leg in enumerate(legs, 1):
        tf = leg.floor_transition
        if tf:
            logger.info(
                f"[{device_id}]   leg {i}: {tf.get('type', 'connector')} {tf.get('connector_id', '?')} "
                f"{tf.get('from_floor')} -> {tf.get('to_floor')}"
            )
        else:
            pts = " -> ".join(f"({px:.1f}, {py:.1f})" for px, py in leg.waypoints)
            logger.info(f"[{device_id}]   leg {i}: floor {leg.floor} {float(leg.distance):.1f} m: {pts}")


@app.get("/route/{device_id}", response_model=RouteResponse)
def get_route(device_id: str, slot_id: str):
    """Route from the device's last known position to a vehicle slot.

    Same-floor or cross-floor via vertical_connectors in site config.yaml
    (elevators / stairs / ramps).
    """
    global _site
    # pick up config.yaml edits (connectors / walkables) without full restart
    try:
        _site = load_site_config(_SITE_ID)
    except Exception as e:
        logger.warning("[API /route] config reload failed: %s", e)

    fuser = _sessions.get(device_id)
    if fuser is None:
        raise HTTPException(status_code=404, detail="No known position for this device — call /localize first")

    last = fuser.last_position()
    if last is None:
        raise HTTPException(status_code=404, detail="No known position for this device — call /localize first")
    floor, x, y = last
    floor = normalize_floor_id(floor)
    user_x, user_y = x, y  # where the user actually is (inside the corridor band)
    # start on the corridor the last fix was matched to, not whichever centre line is
    # nearest (near a junction that can be the crossing corridor)
    rs = getattr(fuser, "_route_start", None)
    if rs and rs[0] == floor and math.hypot(rs[1] - x, rs[2] - y) < 0.05:
        x, y = rs[3], rs[4]

    slot = _site.vehicle_slot(slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Unknown slot_id: {slot_id}")
    dest_floor = normalize_floor_id(slot["floor"])

    floors_segments = {
        normalize_floor_id(f["floor"]): list(f.get("walkable_segments") or [])
        for f in _site.raw.get("floors", [])
    }
    connectors = _site.vertical_connectors()

    if dest_floor == floor and not connectors:
        route = calculate_route(
            walkable_segments=_site.walkable_segments(floor),
            start_x=x, start_y=y,
            dest_x=slot["x"], dest_y=slot["y"],
        )
    else:
        route = calculate_multifloor_route(
            floors_segments=floors_segments,
            vertical_connectors=connectors,
            start_floor=floor,
            start_x=x,
            start_y=y,
            dest_floor=dest_floor,
            dest_x=slot["x"],
            dest_y=slot["y"],
            dest_slot_id=slot_id,
        )

    if route is None:
        if dest_floor != floor and not connectors:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cross-floor route {floor}→{dest_floor} needs vertical_connectors "
                    f"in config.yaml (none configured)"
                ),
            )
        start_n = len(floors_segments.get(floor) or [])
        dest_n = len(floors_segments.get(dest_floor) or [])
        raise HTTPException(
            status_code=404,
            detail=(
                f"No walkable route {floor}→{dest_floor} to {slot_id} "
                f"(segments {floor}={start_n}, {dest_floor}={dest_n}, "
                f"connectors={len(connectors)})"
            ),
        )

    # one path from where the user stands: their own lane, parallel to the corridor, up to
    # the first corner, then straight / L onto the next segment — no diagonals
    from fmc.navigation.routing import join_route_to_position
    lane_off, lane_mode = join_route_to_position(route, floor, user_x, user_y)

    legs = [
        RouteLegResponse(
            floor=normalize_floor_id(leg.floor) if leg.floor is not None else None,
            instruction=leg.instruction,
            waypoints=list(leg.waypoints),
            floor_transition=leg.floor_transition,
            distance=float(leg.distance),
            # no diagonal join any more (lane runs parallel to the corridor) — AR needs no
            # special-casing; field kept for older clients
            joined=False,
        )
        for i, leg in enumerate(route.legs or [])
    ]
    _log_route(device_id, slot_id, floor, user_x, user_y, dest_floor, route)
    return RouteResponse(
        floor=floor,
        dest_floor=dest_floor,
        waypoints=route.waypoints,
        total_distance=route.total_distance,
        legs=legs,
    )


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
    floor: str = "1"


class FloorBody(BaseModel):
    floor: str


class FloorRenameBody(BaseModel):
    new_floor: str


class LandmarkPruneBody(BaseModel):
    """Selective delete for 3D landmarks / feature DB (not a full map wipe)."""
    floor: str | None = None
    ids: list[int] | None = None
    x_min: float | None = None
    x_max: float | None = None
    y_min: float | None = None
    y_max: float | None = None
    z_min: float | None = None
    z_max: float | None = None


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
def map_new_session(floor: str | None = None):
    """Start a separated mapping walk; keep prior sessions' landmarks and tags."""
    # reload finalized cloud/H2GIS into memory so the next finalize cannot wipe them
    info = _global_mapper.begin_session(site=_site)
    if floor is not None:
        fid = normalize_floor_id(floor)
        _global_mapper.set_map_floor(fid)
        info["floor"] = fid
    else:
        info["floor"] = normalize_floor_id(getattr(_global_mapper, "_map_floor", _default_floor()))
    logger.info(
        "[API /map/new-session] session=%s floor=%s total_landmarks=%s total_tags=%s",
        info.get("session_id"), info.get("floor"),
        info.get("total_landmarks"), info.get("total_tags"),
    )
    return {"status": "success", "message": "new mapping session", **info}


@app.post("/map/floor")
def map_set_floor(floor: str = Form(...)):
    """Set active floor for subsequent keyframes / tags in the mapping session."""
    fid = normalize_floor_id(floor)
    _global_mapper.set_map_floor(fid)
    return {"status": "success", "floor": fid}


@app.get("/map/floors")
def map_list_floors():
    """Floors declared in site config only (user-assigned names / count)."""
    cfg = list(_site.floor_ids_bottom_up())  # bottom → top (lift up/down + 3D stack)
    map_only: list[str] = []
    try:
        from fmc.vpr.pnp_localizer import available_map_floors
        for f in available_map_floors(_site):
            fid = normalize_floor_id(f)
            if fid not in cfg and fid not in map_only:
                map_only.append(fid)
    except Exception:
        pass
    return {
        "floors": cfg,
        "map_only_floors": map_only,  # orphan map DB labels not in config (not selectable)
        "vertical_connectors": _site.vertical_connectors(),
    }


@app.post("/map/floors")
def map_create_floor(body: FloorBody):
    """Add a user-named floor to the site config (e.g. G, B1, B2)."""
    fid = normalize_floor_id(body.floor)
    if not fid or fid.lower() in ("none", "null"):
        raise HTTPException(status_code=400, detail="invalid floor name")
    get_or_create_floor(_site, fid)
    save_site_config(_site)
    _global_mapper.set_map_floor(fid)
    return {"status": "success", "floor": fid, "floors": _site.floor_ids()}


@app.patch("/map/floors/{old_floor}")
def map_rename_floor(old_floor: str, body: FloorRenameBody):
    """Rename a floor label in config and vertical_connectors."""
    try:
        rename_floor(_site, old_floor, body.new_floor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    save_site_config(_site)
    new_id = normalize_floor_id(body.new_floor)
    if normalize_floor_id(getattr(_global_mapper, "_map_floor", "")) == normalize_floor_id(old_floor):
        _global_mapper.set_map_floor(new_id)
    return {"status": "success", "floor": new_id, "floors": _site.floor_ids()}


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
    for p in _site.index_dir.glob("map_landmarks_f*.npz"):
        try:
            p.unlink()
        except OSError as e:
            logger.warning("[API /map/reset] Could not remove %s: %s", p, e)
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


def _prune_filters_ok(body: LandmarkPruneBody) -> bool:
    return bool(
        body.floor is not None
        or body.ids
        or body.x_min is not None or body.x_max is not None
        or body.y_min is not None or body.y_max is not None
        or body.z_min is not None or body.z_max is not None
    )


def _load_map_arrays():
    """Load (pos, desc, ids, colors, floors, aligned) for prune rewrite."""
    from fmc.floors import coerce_floor_array
    from fmc.storage.h2gis_store import load_landmarks

    npz_path = _site.index_dir / "map_landmarks.npz"
    if npz_path.exists():
        try:
            data = np.load(npz_path, allow_pickle=True)
            pos = np.asarray(data["positions"], dtype=np.float32)
            desc = np.asarray(data["descriptors"], dtype=np.uint8)
            ids = np.asarray(data["ids"], dtype=np.int32)
            colors = (
                np.asarray(data["colors"], dtype=np.uint8)
                if "colors" in data.files
                else np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(ids), 1))
            )
            floors = coerce_floor_array(
                data["floors"] if "floors" in data.files else None, n=len(ids)
            )
            aligned = False
            if "aligned" in data.files:
                try:
                    aligned = bool(np.asarray(data["aligned"]).ravel()[0])
                except Exception:
                    aligned = False
            return pos, desc, ids, colors, np.array(floors, dtype=object), aligned
        except Exception as e:
            logger.warning("[API prune] NPZ load failed: %s", e)

    loaded = None
    try:
        loaded = load_landmarks(_site)
    except Exception as e:
        logger.warning("[API prune] H2GIS load failed: %s", e)
    if loaded is None:
        return None
    pos, desc, ids, floors = loaded
    colors = np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(ids), 1))
    aligned = bool(getattr(_global_mapper, "_is_aligned", False))
    return pos, desc, ids, colors, np.asarray(floors, dtype=object), aligned


def _keep_mask(pos, ids, floors, body: LandmarkPruneBody) -> np.ndarray:
    """True = keep landmark. Filters AND together."""
    n = len(ids)
    drop = np.ones(n, dtype=bool)
    if body.ids:
        id_set = {int(i) for i in body.ids}
        drop &= np.array([int(i) in id_set for i in ids], dtype=bool)
    if body.floor is not None:
        fid = normalize_floor_id(body.floor)
        drop &= np.array(
            [normalize_floor_id(f) == fid for f in floors.tolist()], dtype=bool
        )
    if body.x_min is not None:
        drop &= pos[:, 0] >= float(body.x_min)
    if body.x_max is not None:
        drop &= pos[:, 0] <= float(body.x_max)
    if body.y_min is not None:
        drop &= pos[:, 1] >= float(body.y_min)
    if body.y_max is not None:
        drop &= pos[:, 1] <= float(body.y_max)
    if body.z_min is not None:
        drop &= pos[:, 2] >= float(body.z_min)
    if body.z_max is not None:
        drop &= pos[:, 2] <= float(body.z_max)
    return ~drop


def _write_ply_xyzrgb(path: Path, pos: np.ndarray, colors: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(pos)}\n")
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        f.write("end_header\n")
        for i in range(len(pos)):
            x, y, z = float(pos[i, 0]), float(pos[i, 1]), float(pos[i, 2])
            r, g, b = int(colors[i, 0]), int(colors[i, 1]), int(colors[i, 2])
            f.write(f"{x:.4f} {y:.4f} {z:.4f} {r} {g} {b}\n")


def _clear_landmark_artifacts() -> None:
    for name in ("pointcloud.ply", "map_landmarks.npz"):
        p = _site.index_dir / name
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                logger.warning("[API prune] Could not remove %s: %s", name, e)
    for p in _site.index_dir.glob("map_landmarks_f*.npz"):
        try:
            p.unlink()
        except OSError as e:
            logger.warning("[API prune] Could not remove %s: %s", p, e)
    try:
        from fmc.storage.h2gis_store import delete_site_db
        delete_site_db(_site)
    except Exception as e:
        logger.warning("[API prune] H2GIS wipe failed: %s", e)


def _rewrite_landmark_artifacts(pos, desc, ids, colors, floors, aligned: bool) -> None:
    import re
    from fmc.storage.h2gis_store import replace_landmarks

    out_dir = _site.index_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("map_landmarks_f*.npz"):
        try:
            p.unlink()
        except OSError:
            pass

    replace_landmarks(
        _site, pos, desc, ids, colors,
        floors=floors, aligned=aligned,
    )
    np.savez_compressed(
        out_dir / "map_landmarks.npz",
        positions=pos,
        descriptors=desc,
        ids=ids,
        colors=colors,
        floors=floors,
        aligned=np.array([aligned]),
        frame="facility_xy_height",
    )
    for fid in sorted({str(f) for f in floors.tolist()}):
        mask = np.array([str(f) == fid for f in floors.tolist()])
        if not np.any(mask):
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fid)
        np.savez_compressed(
            out_dir / f"map_landmarks_f{safe}.npz",
            positions=pos[mask],
            descriptors=desc[mask],
            ids=ids[mask],
            colors=colors[mask],
            floors=floors[mask],
            aligned=np.array([aligned]),
            frame="facility_xy_height",
        )
    _write_ply_xyzrgb(out_dir / "pointcloud.ply", pos, colors)


@app.post("/map/landmarks/prune")
def map_landmarks_prune(body: LandmarkPruneBody):
    """Delete landmarks by floor, id list, and/or facility-frame bbox; rewrite feature DB."""
    if not _prune_filters_ok(body):
        raise HTTPException(
            status_code=400,
            detail="pass floor, ids, and/or bbox (x_min/x_max/y_min/y_max/z_min/z_max)",
        )

    arrays = _load_map_arrays()
    if arrays is None:
        # still drop any in-memory matches
        n_mem = _global_mapper.drop_landmarks(
            floor=body.floor,
            ids=set(body.ids) if body.ids else None,
            x_min=body.x_min, x_max=body.x_max,
            y_min=body.y_min, y_max=body.y_max,
            z_min=body.z_min, z_max=body.z_max,
        )
        return {
            "status": "success",
            "deleted": n_mem,
            "remaining": len(_global_mapper.landmarks),
            "message": "no on-disk landmarks",
        }

    pos, desc, ids, colors, floors, aligned = arrays
    keep = _keep_mask(pos, ids, floors, body)
    n_del = int((~keep).sum())
    if n_del == 0:
        return {
            "status": "success",
            "deleted": 0,
            "remaining": int(len(ids)),
            "message": "no landmarks matched filters",
        }

    # memory first so a later finalize cannot resurrect deleted points
    _global_mapper.drop_landmarks(
        floor=body.floor,
        ids=set(body.ids) if body.ids else None,
        x_min=body.x_min, x_max=body.x_max,
        y_min=body.y_min, y_max=body.y_max,
        z_min=body.z_min, z_max=body.z_max,
    )
    # also drop by id from keep mask (covers NPZ-only ids not in memory filters edge cases)
    drop_ids = {int(i) for i in ids[~keep].tolist()}
    for lid in drop_ids:
        _global_mapper.landmarks.pop(lid, None)

    n_keep = int(keep.sum())
    if n_keep == 0:
        _clear_landmark_artifacts()
    else:
        try:
            _rewrite_landmark_artifacts(
                pos[keep], desc[keep], ids[keep], colors[keep], floors[keep], aligned,
            )
        except Exception as e:
            logger.error("[API /map/landmarks/prune] rewrite failed: %s", e)
            raise HTTPException(status_code=500, detail="could not update feature db") from e

    if _vpr_pipeline is not None:
        try:
            _vpr_pipeline.reload_map_index()
        except Exception as e:
            logger.warning("[API /map/landmarks/prune] PnP reload failed: %s", e)

    logger.info(
        "[API /map/landmarks/prune] deleted=%s remaining=%s floor=%s ids=%s",
        n_del, n_keep, body.floor, len(body.ids or []),
    )
    return {
        "status": "success",
        "deleted": n_del,
        "remaining": n_keep,
        "floor": normalize_floor_id(body.floor) if body.floor is not None else None,
    }


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

    _prepare_walk_session("keyframe")

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

def _prepare_walk_session(source: str) -> None:
    """Make sure the current walk records into a fresh, unaligned session.

    Shared by keyframes AND tags: after a restart a tag dropped before the first keyframe
    used to stay in the old session while the walk's keyframes moved to a new one — each
    session then had 1 tag, neither could be aligned, and the walk was exported in raw
    ARCore coordinates.
    """
    # first request after reload: pull prior finalized map so it isn't lost on next finalize
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
                        "[API /map/%s] Auto session=%s after hydrating %s landmarks",
                        source, _global_mapper._session_id, len(_global_mapper.landmarks),
                    )
        except Exception as e:
            logger.warning("[API /map/%s] Disk hydrate failed: %s", source, e)

    # a walk recorded into an already-aligned session is never aligned (finalize skips it)
    # and would be exported in raw ARCore coords — start a fresh session instead
    if _global_mapper._session_id in getattr(_global_mapper, "_aligned_sessions", set()):
        old_sid = _global_mapper._session_id
        _global_mapper._session_id = _global_mapper._next_free_session_id()
        logger.warning(
            "[API /map/%s] Session %s is already aligned — this walk goes to session %s "
            "(drop 2+ tags in this walk, then Finalize)",
            source, old_sid, _global_mapper._session_id,
        )


@app.post("/map/tag")
def map_tag(tag: MappingTag):
    """Add a ground truth physical anchor to the map."""
    logger.info(f"[API /map/tag] Tag received: pos=({tag.x:.2f}, {tag.y:.2f}), floor={tag.floor}, time={tag.timestamp}")
    _prepare_walk_session("tag")
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
                
    # survey spots are map coordinates — the viewer draws everything in map coordinates
    facility_landmarks = []
    if True:
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
                        "floor": normalize_floor_id(r["floor"]),
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
        from fmc.storage.h2gis_store import db_file_for_site
        h2_stem = db_file_for_site(_site)
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
def get_floorplan(floor: str | None = None):
    """Serve the floorplan image for the active / requested floor."""
    from fmc.floors import resolve_floorplan_asset

    fid = normalize_floor_id(
        floor if floor is not None else getattr(_global_mapper, "_map_floor", None) or _default_floor()
    )
    img_path = resolve_floorplan_asset(_site.data_dir, fid, "floorplan.png")
    if not img_path.exists():
        logger.warning(f"[API /map/floorplan] Floorplan not found at {img_path}")
        raise HTTPException(status_code=404, detail=f"Floorplan image not found for floor '{fid}'")
    return FileResponse(img_path)

@app.get("/map/transform")
def get_transform(floor: str | None = None):
    """Serve the floorplan transform matrix for the active / requested floor."""
    from fmc.floors import resolve_floorplan_asset

    fid = normalize_floor_id(
        floor if floor is not None else getattr(_global_mapper, "_map_floor", None) or _default_floor()
    )
    transform_path = resolve_floorplan_asset(_site.data_dir, fid, "transform.json")
    if not transform_path.exists():
        logger.warning(f"[API /map/transform] Transform not found at {transform_path}")
        raise HTTPException(status_code=404, detail=f"Transform not found for floor '{fid}'")
    with open(transform_path, "r") as f:
        return json.load(f)

@app.get("/map/survey-spots")
def get_survey_spots(floor: str | None = None):
    """Pixel coords of survey spots for the tagging floorplan overlay.

    Optional per floor — missing file / no rows returns an empty list (not 404).
    """
    from fmc.floors import resolve_floorplan_asset

    fid = normalize_floor_id(
        floor if floor is not None else getattr(_global_mapper, "_map_floor", None) or _default_floor()
    )
    path = resolve_floorplan_asset(_site.data_dir, fid, "locations_pixels.csv")
    if not path.exists():
        return {"status": "success", "spots": [], "floor": fid}
    spots = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lid = (row.get("location_id") or "").strip()
                if not lid:
                    continue
                spot_floor = normalize_floor_id(row.get("floor") or fid)
                if spot_floor != fid:
                    continue
                spots.append({
                    "id": lid,
                    "px": float(row["pixel_x"]),
                    "py": float(row["pixel_y"]),
                    "floor": spot_floor,
                    "zone": (row.get("zone") or "").strip(),
                })
    except (KeyError, ValueError, OSError) as e:
        logger.warning("[API /map/survey-spots] Failed reading %s: %s", path, e)
        raise HTTPException(status_code=500, detail="Could not load survey spots") from e
    spots.sort(key=lambda s: s["id"])
    return {"status": "success", "spots": spots, "floor": fid}

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


@app.get("/map/cloud-layers")
def get_cloud_layers(max_per_floor: int = 8000):
    """Point cloud split by floor for stacked 3D visualization."""
    from fmc.storage.h2gis_store import load_landmarks

    floor_order = list(_site.floor_ids_bottom_up())  # stack: index 0 = lowest floor
    loaded = None
    try:
        # the viewer shows H2GIS (the map DB); NPZ only if the DB is missing/empty
        loaded = load_landmarks(_site, ignore_stale=True)
    except Exception as e:
        logger.warning("[API /map/cloud-layers] H2GIS load failed: %s", e)
    if loaded is None:
        npz = _site.index_dir / "map_landmarks.npz"
        if npz.exists():
            try:
                data = np.load(npz, allow_pickle=True)  # floor labels: object array
                pos = data["positions"]
                fls = data["floors"] if "floors" in data.files else np.array(["1"] * len(pos))
                lids = data["ids"] if "ids" in data.files else None
                loaded = (pos, None, lids, fls)
            except Exception as e:
                logger.warning("[API /map/cloud-layers] NPZ load failed: %s", e)
    if loaded is None:
        raise HTTPException(status_code=404, detail="No map landmarks for cloud layers")

    positions, _desc, ids_arr, floors_arr = loaded
    from fmc.floors import coerce_floor_array, normalize_floor_id

    labels = coerce_floor_array(floors_arr, n=len(positions))
    # keep config order, then any extras
    for lab in labels:
        fid = normalize_floor_id(lab)
        if fid not in floor_order:
            floor_order.append(fid)

    max_n = max(200, min(int(max_per_floor), 20000))
    layers = []
    total = 0
    for fid in floor_order:
        idxs = [i for i, lab in enumerate(labels) if normalize_floor_id(lab) == fid]
        if len(idxs) > max_n:
            step = max(1, len(idxs) // max_n)
            idxs = idxs[::step][:max_n]
        pts = [[float(positions[i][0]), float(positions[i][1]), float(positions[i][2])] for i in idxs]
        ids = (
            [int(ids_arr[i]) for i in idxs]
            if ids_arr is not None
            else []
        )
        layers.append({"floor": fid, "positions": pts, "ids": ids, "count": len(pts)})
        total += len(pts)

    aligned = False
    tags_file = _site.index_dir / "tags.json"
    if tags_file.exists():
        try:
            with open(tags_file, "r", encoding="utf-8") as f:
                tags = json.load(f)
            aligned = bool(tags and tags[0].get("aligned"))
        except Exception:
            pass
    if not aligned:
        aligned = bool(getattr(_global_mapper, "_is_aligned", False))

    try:
        stack_m = float(_site.raw.get("stack_m", 4.0))
    except (TypeError, ValueError):
        stack_m = 4.0
    stack_m = max(0.5, min(stack_m, 50.0))

    return {
        "status": "success",
        "aligned": aligned,
        "floor_order": floor_order,
        "stack_m": stack_m,
        "total_points": total,
        "layers": layers,
    }


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


@app.get("/floor-plan", include_in_schema=False)
def floor_plan_redirect():
    """Editor page uses relative asset/API paths — needs the trailing slash."""
    return RedirectResponse(url="/floor-plan/", status_code=307)


# floor-plan editor (fmc.webtools) served by this same server on the same port
from fmc.webtools.server import app as floor_plan_app  # noqa: E402

app.mount("/floor-plan", floor_plan_app, name="floor_plan")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
