"""Interactive web tool for floor-plan georeferencing and site geometry
editing -- browser front end for the workflow documented in
docs/04_survey_ingestion_workflow.md.

This is deliberately a thin HTTP layer over the SAME modules the CLI
scripts use (fmc.georeference, fmc.config, fmc.navigation, fmc.vpr) --
every file this reads or writes (control_points.csv, transform.json,
config.yaml, locations.csv) is byte-for-byte the same format the CLI
scripts produce and consume. You can start a site with the CLI tools and
finish it in the browser, or vice versa, interchangeably.

Run:
    uvicorn fmc.webtools.server:app --reload --port 8001
Then open http://127.0.0.1:8001/

Deliberately NOT caching the VPRPipeline per site (unlike fmc.api.server) --
this is a low-traffic editing/dev tool, not a production hot path, and a
stale cached index after re-ingestion is exactly the bug that bit the
position API earlier in this project. Simplicity over speed here.
"""
from __future__ import annotations

import csv
import math
import shutil
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fmc.config import DATA_ROOT, SiteConfig, get_or_create_floor, load_site_config, save_site_config
from fmc.dataset.capture import next_sequence_for_site, register_capture
from fmc.dataset.locations import delete_location, get_location, load_locations_csv, upsert_location
from fmc.dataset.schema import delete_record, load_records, records_for_location, update_record_heading
from fmc.fusion.map_matching import snap_to_walkable
from fmc.georeference import (
    ControlPoint,
    FloorPlanTransform,
    fit_transform,
    implied_scale,
    load_control_points_csv,
    save_control_points_csv,
    transform_residuals,
)
from fmc.navigation.routing import calculate_route
from fmc.pdf_render import render_pdf_page
from fmc.vpr.pipeline import VPRPipeline

app = FastAPI(title="Find My Car — Floor Plan Editor")

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png"}


# ---------------------------------------------------------------- helpers

def _site_dir_exists(site_id: str) -> bool:
    return (DATA_ROOT / site_id).is_dir()


def get_site_or_404(site_id: str) -> SiteConfig:
    if not _site_dir_exists(site_id):
        raise HTTPException(404, f"Site '{site_id}' not found. Create it first (POST /api/sites).")
    return load_site_config(site_id)


def _control_points_path(site: SiteConfig) -> Path:
    return site.data_dir / "floorplan" / "control_points.csv"


def _transform_path(site: SiteConfig) -> Path:
    return site.data_dir / "floorplan" / "transform.json"


def _floorplan_image_path(site: SiteConfig) -> Path:
    return site.data_dir / "floorplan" / "floorplan.png"


def _locations_csv_path(site: SiteConfig) -> Path:
    return site.index_dir / "locations.csv"


def _load_transform_or_none(site: SiteConfig) -> FloorPlanTransform | None:
    path = _transform_path(site)
    if not path.exists():
        return None
    return FloorPlanTransform.load(path)


# ---------------------------------------------------------------- models

class ControlPointIn(BaseModel):
    px: float
    py: float
    x: float
    y: float


class SegmentIn(BaseModel):
    px1: float
    py1: float
    px2: float
    py2: float


class SlotIn(BaseModel):
    px: float
    py: float
    slot_id: str
    zone: str


class RouteRequest(BaseModel):
    floor: int
    start_x: float
    start_y: float
    slot_id: str


class CaptureLocationIn(BaseModel):
    location_id: str
    zone: str
    px: float
    py: float


class HeadingUpdate(BaseModel):
    heading_degrees: float


# ---------------------------------------------------------------- sites

@app.get("/api/sites")
def list_sites():
    if not DATA_ROOT.exists():
        return []
    return sorted(p.name for p in DATA_ROOT.iterdir() if p.is_dir() and (p / "config.yaml").exists())


@app.post("/api/sites/{site_id}")
def create_site(site_id: str):
    if _site_dir_exists(site_id):
        raise HTTPException(409, f"Site '{site_id}' already exists")
    site = load_site_config(site_id)  # auto-creates a minimal config.yaml
    return {"site_id": site.site_id, "floors": [f["floor"] for f in site.raw["floors"]]}


@app.get("/api/sites/{site_id}/floors")
def list_floors(site_id: str):
    site = get_site_or_404(site_id)
    return sorted(f["floor"] for f in site.raw["floors"])


@app.post("/api/sites/{site_id}/floors/{floor}")
def create_floor(site_id: str, floor: int):
    site = get_site_or_404(site_id)
    get_or_create_floor(site, floor)
    save_site_config(site)
    return {"floor": floor}


# ---------------------------------------------------------------- floor plan

@app.post("/api/sites/{site_id}/floorplan")
async def upload_floorplan(site_id: str, page: int = 0, dpi: int = 200, file: UploadFile = File(...)):
    site = get_site_or_404(site_id)
    pdf_path = site.data_dir / "floorplan" / "source.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        width, height = render_pdf_page(pdf_path, page, dpi, _floorplan_image_path(site))
    except Exception as exc:
        raise HTTPException(400, f"Could not render PDF: {exc}") from exc

    return {"width": width, "height": height, "page": page, "dpi": dpi}


@app.get("/api/sites/{site_id}/floorplan/image")
def get_floorplan_image(site_id: str):
    site = get_site_or_404(site_id)
    path = _floorplan_image_path(site)
    if not path.exists():
        raise HTTPException(404, "No floor plan uploaded yet for this site")
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------- control points / transform

@app.get("/api/sites/{site_id}/control-points")
def get_control_points(site_id: str):
    site = get_site_or_404(site_id)
    points = load_control_points_csv(_control_points_path(site))
    return [{"px": p.px, "py": p.py, "x": p.x, "y": p.y} for p in points]


@app.put("/api/sites/{site_id}/control-points")
def put_control_points(site_id: str, points: list[ControlPointIn]):
    site = get_site_or_404(site_id)
    control_points = [ControlPoint(px=p.px, py=p.py, x=p.x, y=p.y) for p in points]
    save_control_points_csv(_control_points_path(site), control_points)
    return {"saved": len(control_points)}


@app.post("/api/sites/{site_id}/control-points/fit")
def fit_control_points(site_id: str):
    """Preview-only: fits from whatever is currently saved in
    control_points.csv and returns the fit quality, but does NOT persist
    transform.json -- call /control-points/fit/save once you've reviewed
    the residuals and implied scale and are happy with them."""
    site = get_site_or_404(site_id)
    control_points = load_control_points_csv(_control_points_path(site))
    if len(control_points) < 3:
        raise HTTPException(400, f"Need at least 3 control points to fit a transform, have {len(control_points)}")

    transform = fit_transform(control_points)
    residuals = transform_residuals(transform, control_points)
    scale_x, scale_y = implied_scale(transform)

    warnings = []
    if len(control_points) == 3:
        warnings.append(
            "Only 3 control points: residuals will look perfect (~0) even if real_x/real_y "
            "were entered in the wrong unit (e.g. mm instead of meters) -- the fit is exact "
            "regardless of unit with the minimum number of points. Check the implied scale "
            "below carefully, or add a 4th point."
        )
    if max(residuals) > 0.5:
        warnings.append(f"Max residual is {max(residuals):.2f}m -- a point may be mis-clicked or mis-measured.")
    if scale_x > 2.0 or scale_y > 2.0 or scale_x < 0.001 or scale_y < 0.001:
        warnings.append(
            f"Implied scale ({scale_x:.4f}, {scale_y:.4f}) m/pixel is well outside the typical "
            f"0.001-2 m/pixel range for a floor plan -- double check real_x/real_y are in METERS."
        )

    return {
        "transform": transform.__dict__,
        "residuals": residuals,
        "max_residual": max(residuals),
        "implied_scale_x": scale_x,
        "implied_scale_y": scale_y,
        "warnings": warnings,
    }


@app.post("/api/sites/{site_id}/control-points/fit/save")
def save_transform(site_id: str):
    """Re-fits from control_points.csv (the on-disk source of truth,
    guaranteeing the saved transform always matches saved points) and
    persists transform.json."""
    site = get_site_or_404(site_id)
    control_points = load_control_points_csv(_control_points_path(site))
    if len(control_points) < 3:
        raise HTTPException(400, f"Need at least 3 control points to fit a transform, have {len(control_points)}")
    transform = fit_transform(control_points)
    transform.save(_transform_path(site))
    return {"saved": True, "transform": transform.__dict__}


@app.get("/api/sites/{site_id}/transform")
def get_transform(site_id: str):
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    if transform is None:
        raise HTTPException(404, "No transform saved yet for this site")
    return transform.__dict__


# ---------------------------------------------------------------- overlays (read-only)

@app.get("/api/sites/{site_id}/locations")
def get_locations(site_id: str):
    """Already-ingested capture locations, converted to pixel coordinates
    for overlay -- read-only in this tool; capture locations come from
    real survey photos via scripts/ingest_survey_locations.py, not from
    clicking in the browser."""
    site = get_site_or_404(site_id)
    path = _locations_csv_path(site)
    if not path.exists():
        return []

    transform = _load_transform_or_none(site)
    result = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            x, y = float(row["x"]), float(row["y"])
            entry = {"location_id": row["location_id"], "floor": int(row["floor"]), "zone": row["zone"], "x": x, "y": y}
            if transform is not None:
                px, py = transform.world_to_pixel(x, y)
                entry["px"], entry["py"] = px, py
            result.append(entry)
    return result


# ---------------------------------------------------------------- capture locations + photos
# The feature described in docs/05_web_editor.md's roadmap: click a point on
# the floor plan to create/select a capture location, attach multiple
# reference photos to it, and set each photo's heading -- all in the
# browser, no CLI/file-folder step required. Writes the exact same
# locations.csv + dataset.jsonl (with the new location_id field) the CLI
# ingestion scripts produce, so this and scripts/ingest_survey_locations.py
# remain interchangeable.

def _heading_direction_px(transform: FloorPlanTransform, x: float, y: float, heading_degrees: float) -> dict:
    """Unit direction vector in PIXEL space for `heading_degrees` in world
    space, computed via the transform rather than naive pixel-space trig --
    the transform may rotate/reflect between pixel and world axes (see
    fmc/georeference.py), so a world-space heading doesn't map to a fixed
    pixel-space angle without going through it."""
    heading_rad = math.radians(heading_degrees)
    tip_x, tip_y = x + math.sin(heading_rad), y + math.cos(heading_rad)
    base_px, base_py = transform.world_to_pixel(x, y)
    tip_px, tip_py = transform.world_to_pixel(tip_x, tip_y)
    dx, dy = tip_px - base_px, tip_py - base_py
    length = math.hypot(dx, dy) or 1.0
    return {"dx": dx / length, "dy": dy / length}


def _photo_json(site_id: str, record, transform: FloorPlanTransform | None) -> dict:
    entry = {
        "image_id": record.image_id,
        "heading_degrees": record.orientation,
        "photo_url": f"/api/sites/{site_id}/photos/{record.image_id}",
    }
    if transform is not None:
        entry["direction_px"] = _heading_direction_px(transform, record.x, record.y, record.orientation)
    return entry


@app.get("/api/sites/{site_id}/floors/{floor}/capture-locations")
def list_capture_locations(site_id: str, floor: int):
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    all_records = load_records(site.dataset_jsonl_path)

    result = []
    for row in load_locations_csv(_locations_csv_path(site)):
        if row["floor"] != floor:
            continue
        photo_count = sum(1 for r in all_records if r.location_id == row["location_id"])
        entry = dict(row, photo_count=photo_count)
        if transform is not None:
            entry["px"], entry["py"] = transform.world_to_pixel(row["x"], row["y"])
        result.append(entry)
    return result


@app.post("/api/sites/{site_id}/floors/{floor}/capture-locations")
def create_or_move_capture_location(site_id: str, floor: int, loc: CaptureLocationIn):
    """Create a new capture location, or reposition/re-zone an existing one
    if location_id already exists (upsert -- lets a user re-click roughly
    the same spot without erroring)."""
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    if transform is None:
        raise HTTPException(400, "No transform saved yet -- fit and save control points first")
    location_id = loc.location_id.strip()
    if not location_id:
        raise HTTPException(400, "location_id is required")

    x, y = transform.pixel_to_world(loc.px, loc.py)
    row = upsert_location(_locations_csv_path(site), location_id, floor, loc.zone.strip(), x, y)
    row["px"], row["py"] = loc.px, loc.py
    row["photo_count"] = len(records_for_location(site.dataset_jsonl_path, location_id))
    return row


@app.delete("/api/sites/{site_id}/capture-locations/{location_id}")
def delete_capture_location(site_id: str, location_id: str):
    """Removes the location AND all its photos (files + dataset.jsonl
    records). No confirmation step at this layer -- the frontend should
    confirm before calling this."""
    site = get_site_or_404(site_id)
    photos = records_for_location(site.dataset_jsonl_path, location_id)
    for record in photos:
        photo_path = site.processed_dir / record.processed_path
        if photo_path.exists():
            photo_path.unlink()
        delete_record(site.dataset_jsonl_path, record.image_id)
    delete_location(_locations_csv_path(site), location_id)
    return {"deleted": location_id, "photos_removed": len(photos)}


@app.get("/api/sites/{site_id}/capture-locations/{location_id}/photos")
def list_location_photos(site_id: str, location_id: str):
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    photos = records_for_location(site.dataset_jsonl_path, location_id)
    return [_photo_json(site_id, record, transform) for record in photos]


@app.post("/api/sites/{site_id}/capture-locations/{location_id}/photos")
async def add_location_photo(
    site_id: str,
    location_id: str,
    heading_degrees: float = Form(...),
    file: UploadFile = File(...),
):
    site = get_site_or_404(site_id)
    loc = get_location(_locations_csv_path(site), location_id)
    if loc is None:
        raise HTTPException(404, f"Unknown location_id: {location_id} -- create the location first")
    if file.content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
        raise HTTPException(400, f"Unsupported file type '{file.content_type}' -- only JPEG/PNG allowed")
    if not (0 <= heading_degrees <= 360):
        raise HTTPException(400, "heading_degrees must be between 0 and 360")

    # register_capture() reads from a source file PATH (it normalizes/copies
    # rather than accepting raw bytes) -- stage the upload to a temp file first.
    suffix = ".png" if file.content_type == "image/png" else ".jpg"
    tmp_path = site.data_dir / "floorplan" / f"_upload_tmp{suffix}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        record = register_capture(
            site=site,
            src_image_path=tmp_path,
            floor=loc["floor"],
            zone=loc["zone"],
            sequence=next_sequence_for_site(site),
            orientation=round(heading_degrees) % 360,
            x=loc["x"],
            y=loc["y"],
            device=f"webtools:{location_id}",
            location_id=location_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    transform = _load_transform_or_none(site)
    return _photo_json(site_id, record, transform)


@app.patch("/api/sites/{site_id}/capture-locations/{location_id}/photos/{image_id}")
def update_location_photo(site_id: str, location_id: str, image_id: str, body: HeadingUpdate):
    site = get_site_or_404(site_id)
    if not (0 <= body.heading_degrees <= 360):
        raise HTTPException(400, "heading_degrees must be between 0 and 360")
    try:
        record = update_record_heading(site.dataset_jsonl_path, image_id, round(body.heading_degrees) % 360)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    transform = _load_transform_or_none(site)
    return _photo_json(site_id, record, transform)


@app.delete("/api/sites/{site_id}/capture-locations/{location_id}/photos/{image_id}")
def delete_location_photo(site_id: str, location_id: str, image_id: str):
    site = get_site_or_404(site_id)
    try:
        record = delete_record(site.dataset_jsonl_path, image_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    photo_path = site.processed_dir / record.processed_path
    if photo_path.exists():
        photo_path.unlink()
    return {"deleted": image_id}


@app.get("/api/sites/{site_id}/photos/{image_id}")
def get_photo(site_id: str, image_id: str):
    site = get_site_or_404(site_id)
    record = next((r for r in load_records(site.dataset_jsonl_path) if r.image_id == image_id), None)
    if record is None:
        raise HTTPException(404, f"Unknown image_id: {image_id}")
    photo_path = site.processed_dir / record.processed_path
    if not photo_path.exists():
        raise HTTPException(404, "Photo file missing on disk")
    return FileResponse(photo_path, media_type="image/jpeg")


# ---------------------------------------------------------------- geometry (segments + slots)

@app.get("/api/sites/{site_id}/floors/{floor}/geometry")
def get_geometry(site_id: str, floor: int):
    site = get_site_or_404(site_id)
    segments = site.walkable_segments(floor)
    floor_entry = next((f for f in site.raw["floors"] if f["floor"] == floor), None)
    slots = floor_entry.get("vehicle_slots", []) if floor_entry else []

    transform = _load_transform_or_none(site)

    seg_out = []
    for seg in segments:
        entry = dict(seg)
        if transform is not None:
            entry["px1"], entry["py1"] = transform.world_to_pixel(seg["x1"], seg["y1"])
            entry["px2"], entry["py2"] = transform.world_to_pixel(seg["x2"], seg["y2"])
        seg_out.append(entry)

    slot_out = []
    for slot in slots:
        entry = dict(slot)
        if transform is not None:
            entry["px"], entry["py"] = transform.world_to_pixel(slot["x"], slot["y"])
        slot_out.append(entry)

    return {"segments": seg_out, "slots": slot_out}


@app.put("/api/sites/{site_id}/floors/{floor}/segments")
def put_segments(site_id: str, floor: int, segments: list[SegmentIn]):
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    if transform is None:
        raise HTTPException(400, "No transform saved yet -- fit and save control points first")

    world_segments = []
    for seg in segments:
        x1, y1 = transform.pixel_to_world(seg.px1, seg.py1)
        x2, y2 = transform.pixel_to_world(seg.px2, seg.py2)
        world_segments.append({"x1": round(x1, 3), "y1": round(y1, 3), "x2": round(x2, 3), "y2": round(y2, 3)})

    floor_entry = get_or_create_floor(site, floor)
    floor_entry["walkable_segments"] = world_segments
    save_site_config(site)
    return {"saved": len(world_segments)}


@app.put("/api/sites/{site_id}/floors/{floor}/slots")
def put_slots(site_id: str, floor: int, slots: list[SlotIn]):
    site = get_site_or_404(site_id)
    transform = _load_transform_or_none(site)
    if transform is None:
        raise HTTPException(400, "No transform saved yet -- fit and save control points first")

    world_slots = []
    for slot in slots:
        x, y = transform.pixel_to_world(slot.px, slot.py)
        world_slots.append({"slot_id": slot.slot_id, "zone": slot.zone, "x": round(x, 3), "y": round(y, 3)})

    floor_entry = get_or_create_floor(site, floor)
    floor_entry["vehicle_slots"] = world_slots
    save_site_config(site)
    return {"saved": len(world_slots)}


# ---------------------------------------------------------------- live testing

@app.post("/api/sites/{site_id}/query")
async def query_position(site_id: str, file: UploadFile = File(...)):
    """Upload a real photo and see where VPR thinks it is, overlaid on the
    map -- the same fmc.vpr.pipeline used by the position API and
    scripts/query_position.py, just visualized here."""
    site = get_site_or_404(site_id)
    if not site.embeddings_path.exists():
        raise HTTPException(400, "No index built yet for this site (fmc.dataset.build_index)")

    contents = await file.read()
    np_arr = np.frombuffer(contents, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Could not decode uploaded image")

    pipeline = VPRPipeline(site)  # not cached -- see module docstring
    result = pipeline.localize(frame)

    if not result.matched:
        return {"matched": False}

    r = result.record
    response = {
        "matched": True,
        "image_id": r.image_id,
        "floor": r.floor,
        "zone": r.zone,
        "x": r.x,
        "y": r.y,
        "similarity": result.similarity,
        "inlier_ratio": result.inlier_ratio,
    }
    transform = _load_transform_or_none(site)
    if transform is not None:
        response["px"], response["py"] = transform.world_to_pixel(r.x, r.y)
    return response


@app.post("/api/sites/{site_id}/route")
def get_route(site_id: str, req: RouteRequest):
    """Compute a route from a given world position to a vehicle slot and
    return waypoints in both world and pixel coordinates, for drawing on
    the map -- same fmc.navigation.routing + fmc.fusion.map_matching the
    position API uses."""
    site = get_site_or_404(site_id)
    segments = site.walkable_segments(req.floor)
    if not segments:
        raise HTTPException(400, f"No walkable_segments for floor {req.floor}")

    slot = site.vehicle_slot(req.slot_id)
    if slot is None:
        raise HTTPException(404, f"Unknown slot_id: {req.slot_id}")
    if slot["floor"] != req.floor:
        raise HTTPException(400, "Cross-floor routing isn't implemented yet")

    snapped_x, snapped_y = snap_to_walkable(req.start_x, req.start_y, segments)
    route = calculate_route(segments, start_x=snapped_x, start_y=snapped_y, dest_x=slot["x"], dest_y=slot["y"])
    if route is None:
        raise HTTPException(404, "No route found -- the walkable graph may be disconnected between these points")

    transform = _load_transform_or_none(site)
    waypoints_px = None
    if transform is not None:
        waypoints_px = [transform.world_to_pixel(x, y) for x, y in route.waypoints]

    return {
        "waypoints": route.waypoints,
        "waypoints_px": waypoints_px,
        "total_distance": route.total_distance,
    }


# ---------------------------------------------------------------- static frontend
# Mounted last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
