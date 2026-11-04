"""Interactive web tool for floor-plan georeferencing and site geometry
editing -- browser front end for the workflow documented in
docs/04_survey_ingestion_workflow.md.

This is deliberately a thin HTTP layer over the SAME modules the CLI
scripts use (fmc.georeference, fmc.config, fmc.navigation, fmc.vpr) --
every file this reads or writes (per-floor control_points.csv /
transform.json / floorplan.png under floorplan/<floor>/, plus
config.yaml and locations.csv) is byte-for-byte the same format the CLI
scripts produce and consume. You can start a site with the CLI tools and
finish it in the browser, or vice versa, interchangeably.

Run:
    uvicorn fmc.webtools.server:app --reload --port 8001
Then open http://127.0.0.1:8001/

The VPRPipeline is cached per site but keyed on a fingerprint of the site's index files
(map DB pointer, landmark npz files, embeddings, dataset), so a re-ingest / Finalize gets a
fresh pipeline on the next query -- the stale-index bug the position API once had can't
happen here. Rebuilding it per request cost ~20 s per Localize (all 305k landmarks, then
every floor again from H2GIS).
"""
from __future__ import annotations

import csv
import math
import re
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fmc.config import DATA_ROOT, SiteConfig, get_or_create_floor, load_site_config, rename_floor, save_site_config
from fmc.floors import (
    floorplan_dir,
    normalize_floor_id,
    resolve_floorplan_asset,
)
from fmc.dataset.build_index import build_index
from fmc.dataset.capture import next_sequence_for_site, register_capture
from fmc.dataset.locations import delete_location, get_location, load_locations_csv, upsert_location
from fmc.dataset.schema import delete_record, load_records, records_for_location, save_records
from fmc.georeference import (
    ControlPoint,
    FloorPlanTransform,
    fit_transform,
    implied_scale,
    load_control_points_csv,
    save_control_points_csv,
    transform_residuals,
)
from fmc.navigation.routing import calculate_multifloor_route, calculate_route, join_route_to_position
from fmc.pdf_render import render_pdf_page
from fmc.vpr.pipeline import VPRPipeline

app = FastAPI(title="Find My Car — Floor Plan Editor")

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png"}
# F{floorToken}_Z{zone}_{seq}_{heading} — floor token is free-form (1, G, B1, …)
_IMAGE_ID_HEADING_SUFFIX = re.compile(r"^(F[^_]+_Z.+_\d{5})_\d{3}$")


# ---------------------------------------------------------------- helpers

def _site_dir_exists(site_id: str) -> bool:
    return (DATA_ROOT / site_id).is_dir()


def get_site_or_404(site_id: str) -> SiteConfig:
    if not _site_dir_exists(site_id):
        raise HTTPException(404, f"Site '{site_id}' not found. Create it first (POST /api/sites).")
    return load_site_config(site_id)


def _locations_csv_path(site: SiteConfig) -> Path:
    return site.index_dir / "locations.csv"


def _prep_floor_assets(site: SiteConfig, floor) -> str:
    return normalize_floor_id(floor)


def _control_points_path(site: SiteConfig, floor, *, for_write: bool = False) -> Path:
    return resolve_floorplan_asset(
        site.data_dir, floor, "control_points.csv", for_write=for_write,
    )


def _transform_path(site: SiteConfig, floor, *, for_write: bool = False) -> Path:
    return resolve_floorplan_asset(
        site.data_dir, floor, "transform.json", for_write=for_write,
    )


def _floorplan_image_path(site: SiteConfig, floor, *, for_write: bool = False) -> Path:
    return resolve_floorplan_asset(
        site.data_dir, floor, "floorplan.png", for_write=for_write,
    )


def _load_transform_or_none(site: SiteConfig, floor) -> FloorPlanTransform | None:
    fid = _prep_floor_assets(site, floor)
    path = _transform_path(site, fid)
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
    # corridor width in metres (segment = centre line); None = default
    width: float | None = None
    # asymmetric band: metres left / right of the line (x1,y1 → x2,y2, map coords)
    width_left: float | None = None
    width_right: float | None = None


class SlotIn(BaseModel):
    px: float
    py: float
    slot_id: str
    zone: str


class KioskIn(BaseModel):
    px: float
    py: float
    kiosk_id: str
    facing_deg: float
    stand_m: float = 0.6
    marker_id: int | None = None


class MarkerIn(BaseModel):
    px: float
    py: float
    marker_id: int
    facing_deg: float
    height_m: float = 1.4
    size_m: float = 0.23


class RouteRequest(BaseModel):
    floor: str = "1"
    start_x: float
    start_y: float
    slot_id: str
    # localized heading (deg) — helps pick the corridor you're in near a junction
    heading: float | None = None


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
    return site.floor_ids()


@app.post("/api/sites/{site_id}/floors/{floor}")
def create_floor(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    fid = normalize_floor_id(floor)
    get_or_create_floor(site, fid)
    save_site_config(site)
    return {"floor": fid}


class FloorRenameIn(BaseModel):
    new_floor: str


@app.patch("/api/sites/{site_id}/floors/{floor}")
def rename_floor_api(site_id: str, floor: str, body: FloorRenameIn):
    site = get_site_or_404(site_id)
    old_id = normalize_floor_id(floor)
    new_id = normalize_floor_id(body.new_floor)
    try:
        rename_floor(site, old_id, new_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # keep per-floor floorplan assets with the renamed label
    old_dir = floorplan_dir(site.data_dir, old_id)
    new_dir = floorplan_dir(site.data_dir, new_id)
    if old_dir.exists() and old_dir != new_dir and not new_dir.exists():
        old_dir.rename(new_dir)
    save_site_config(site)
    return {"floor": new_id, "floors": site.floor_ids()}


# ---------------------------------------------------------------- floor plan (per floor)

@app.post("/api/sites/{site_id}/floors/{floor}/floorplan")
async def upload_floorplan(
    site_id: str,
    floor: str,
    page: int = 0,
    dpi: int = 200,
    file: UploadFile = File(...),
):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    get_or_create_floor(site, fid)
    save_site_config(site)

    pdf_path = resolve_floorplan_asset(site.data_dir, fid, "source.pdf", for_write=True)
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    out_png = _floorplan_image_path(site, fid, for_write=True)
    try:
        width, height = render_pdf_page(pdf_path, page, dpi, out_png)
    except Exception as exc:
        raise HTTPException(400, f"Could not render PDF: {exc}") from exc

    return {"width": width, "height": height, "page": page, "dpi": dpi, "floor": fid}


@app.get("/api/sites/{site_id}/floors/{floor}/floorplan/image")
def get_floorplan_image(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    path = _floorplan_image_path(site, fid)
    if not path.exists():
        raise HTTPException(404, f"No floor plan uploaded yet for floor '{fid}'")
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------- control points / transform (per floor)

@app.get("/api/sites/{site_id}/floors/{floor}/control-points")
def get_control_points(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    points = load_control_points_csv(_control_points_path(site, fid))
    return [{"px": p.px, "py": p.py, "x": p.x, "y": p.y} for p in points]


@app.put("/api/sites/{site_id}/floors/{floor}/control-points")
def put_control_points(site_id: str, floor: str, points: list[ControlPointIn]):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    control_points = [ControlPoint(px=p.px, py=p.py, x=p.x, y=p.y) for p in points]
    save_control_points_csv(_control_points_path(site, fid, for_write=True), control_points)
    return {"saved": len(control_points), "floor": fid}


@app.post("/api/sites/{site_id}/floors/{floor}/control-points/fit")
def fit_control_points(site_id: str, floor: str):
    """Preview-only fit for this floor's control_points.csv."""
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    control_points = load_control_points_csv(_control_points_path(site, fid))
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
        "floor": fid,
        "transform": transform.__dict__,
        "residuals": residuals,
        "max_residual": max(residuals),
        "implied_scale_x": scale_x,
        "implied_scale_y": scale_y,
        "warnings": warnings,
    }


@app.post("/api/sites/{site_id}/floors/{floor}/control-points/fit/save")
def save_transform(site_id: str, floor: str):
    """Persist this floor's transform.json from its control points."""
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    control_points = load_control_points_csv(_control_points_path(site, fid))
    if len(control_points) < 3:
        raise HTTPException(400, f"Need at least 3 control points to fit a transform, have {len(control_points)}")
    transform = fit_transform(control_points)
    transform.save(_transform_path(site, fid, for_write=True))
    return {"saved": True, "floor": fid, "transform": transform.__dict__}


@app.get("/api/sites/{site_id}/floors/{floor}/transform")
def get_transform(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(404, f"No transform saved yet for floor '{fid}'")
    return transform.__dict__


# ---------------------------------------------------------------- overlays (read-only, per floor)

@app.get("/api/sites/{site_id}/floors/{floor}/locations")
def get_locations(site_id: str, floor: str):
    """Ingested capture locations on this floor, in pixel coords for overlay."""
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    path = _locations_csv_path(site)
    if not path.exists():
        return []

    transform = _load_transform_or_none(site, fid)
    result = []
    for row in load_locations_csv(path):
        if normalize_floor_id(row["floor"]) != fid:
            continue
        entry = {
            "location_id": row["location_id"],
            "floor": fid,
            "zone": row["zone"],
            "x": row["x"],
            "y": row["y"],
        }
        if transform is not None:
            px, py = transform.world_to_pixel(row["x"], row["y"])
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
def list_capture_locations(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    want = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, want)
    all_records = load_records(site.dataset_jsonl_path)

    result = []
    for row in load_locations_csv(_locations_csv_path(site)):
        if normalize_floor_id(row["floor"]) != want:
            continue
        photo_count = sum(1 for r in all_records if r.location_id == row["location_id"])
        entry = dict(row, photo_count=photo_count, floor=want)
        if transform is not None:
            entry["px"], entry["py"] = transform.world_to_pixel(row["x"], row["y"])
        result.append(entry)
    return result


@app.post("/api/sites/{site_id}/floors/{floor}/capture-locations")
def create_or_move_capture_location(site_id: str, floor: str, loc: CaptureLocationIn):
    """Create a new capture location, or reposition/re-zone an existing one
    if location_id already exists (upsert -- lets a user re-click roughly
    the same spot without erroring)."""
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(400, f"No transform saved yet for floor '{fid}' — fit & save control points first")
    location_id = loc.location_id.strip()
    if not location_id:
        raise HTTPException(400, "location_id is required")

    x, y = transform.pixel_to_world(loc.px, loc.py)
    row = upsert_location(_locations_csv_path(site), location_id, fid, loc.zone.strip(), x, y)
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
    loc = get_location(_locations_csv_path(site), location_id)
    floor = loc["floor"] if loc else "1"
    transform = _load_transform_or_none(site, floor)
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

    transform = _load_transform_or_none(site, loc["floor"])
    return _photo_json(site_id, record, transform)


@app.patch("/api/sites/{site_id}/capture-locations/{location_id}/photos/{image_id}")
def update_location_photo(site_id: str, location_id: str, image_id: str, body: HeadingUpdate):
    site = get_site_or_404(site_id)
    if not (0 <= body.heading_degrees <= 360):
        raise HTTPException(400, "heading_degrees must be between 0 and 360")
    heading = round(body.heading_degrees) % 360
    records = load_records(site.dataset_jsonl_path)
    record_index = next((i for i, record in enumerate(records) if record.image_id == image_id), None)
    if record_index is None:
        raise HTTPException(404, f"No record with image_id={image_id!r}")

    old_record = records[record_index]
    match = _IMAGE_ID_HEADING_SUFFIX.match(old_record.image_id)
    updated_id = f"{match.group(1)}_{heading:03d}" if match else old_record.image_id
    if any(record.image_id == updated_id and record.image_id != image_id for record in records):
        raise HTTPException(409, f"An image with ID '{updated_id}' already exists")

    old_path = site.processed_dir / old_record.processed_path
    updated_rel_path = old_record.processed_path
    new_path = old_path
    if updated_id != old_record.image_id:
        new_path = old_path.with_name(f"{updated_id}{old_path.suffix}")
        updated_rel_path = str(new_path.relative_to(site.processed_dir))
        if new_path.exists() and new_path != old_path:
            raise HTTPException(409, f"Processed image already exists: {new_path.name}")

    updated_record = old_record.model_copy(
        update={"image_id": updated_id, "orientation": heading, "processed_path": updated_rel_path}
    )
    records[record_index] = updated_record

    moved_file = old_path.exists() and new_path != old_path
    if moved_file:
        old_path.replace(new_path)
    try:
        save_records(site.dataset_jsonl_path, records)
    except OSError:
        if moved_file and new_path.exists():
            new_path.replace(old_path)
        raise

    loc = get_location(_locations_csv_path(site), location_id)
    floor = (loc or {}).get("floor") or updated_record.floor
    transform = _load_transform_or_none(site, floor)
    return _photo_json(site_id, updated_record, transform)


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


@app.post("/api/sites/{site_id}/embeddings/build")
def build_site_embedding_index(site_id: str):
    """Build or rebuild the site's VPR embedding index from ingested photos."""
    site = get_site_or_404(site_id)
    records = load_records(site.dataset_jsonl_path)
    if not records:
        raise HTTPException(400, "No ingested photos available to build embeddings")

    try:
        build_index(site)
    except ImportError as exc:
        raise HTTPException(503, f"Embedding dependencies are unavailable: {exc}") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, f"Could not build embedding index: {exc}") from exc

    with np.load(site.embeddings_path, allow_pickle=True) as index_data:
        return {
            "indexed_images": int(len(index_data["ids"])),
            "embedding_dim": int(index_data["embeddings"].shape[1]),
            "dataset_version": str(index_data["dataset_version"].item()),
        }


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
def get_geometry(site_id: str, floor: str):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    segments = site.walkable_segments(fid)
    floor_entry = next(
        (f for f in site.raw["floors"] if normalize_floor_id(f.get("floor")) == fid),
        None,
    )
    slots = floor_entry.get("vehicle_slots", []) if floor_entry else []

    transform = _load_transform_or_none(site, fid)

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

    marker_out = []
    for m in (floor_entry.get("markers", []) if floor_entry else []):
        entry = dict(m)
        if transform is not None:
            entry["px"], entry["py"] = transform.world_to_pixel(m["x"], m["y"])
        marker_out.append(entry)

    other_ids = sorted({
        int(m.get("marker_id", -1))
        for f in site.raw.get("floors", []) if normalize_floor_id(f.get("floor")) != fid
        for m in f.get("markers") or []
    })
    kiosk_out = []
    for k in (floor_entry.get("kiosks", []) if floor_entry else []):
        entry = dict(k)
        if transform is not None:
            entry["px"], entry["py"] = transform.world_to_pixel(k["x"], k["y"])
        kiosk_out.append(entry)
    other_kiosk_ids = sorted({
        str(k.get("kiosk_id", "")).strip().upper()
        for f in site.raw.get("floors", []) if normalize_floor_id(f.get("floor")) != fid
        for k in f.get("kiosks") or []
    })
    return {
        "segments": seg_out, "slots": slot_out, "markers": marker_out, "other_floor_marker_ids": other_ids,
        "kiosks": kiosk_out, "other_floor_kiosk_ids": other_kiosk_ids,
    }


@app.put("/api/sites/{site_id}/floors/{floor}/segments")
def put_segments(site_id: str, floor: str, segments: list[SegmentIn]):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(400, f"No transform saved yet for floor '{fid}' — fit & save control points first")

    world_segments = []
    for seg in segments:
        x1, y1 = transform.pixel_to_world(seg.px1, seg.py1)
        x2, y2 = transform.pixel_to_world(seg.px2, seg.py2)
        entry = {"x1": round(x1, 3), "y1": round(y1, 3), "x2": round(x2, 3), "y2": round(y2, 3)}
        if seg.width is not None and seg.width > 0:
            entry["width"] = round(float(seg.width), 2)
        for side in ("width_left", "width_right"):
            v = getattr(seg, side)
            if v is not None and v >= 0:
                entry[side] = round(float(v), 2)
        world_segments.append(entry)

    floor_entry = get_or_create_floor(site, fid)
    floor_entry["walkable_segments"] = world_segments
    save_site_config(site)
    return {"saved": len(world_segments)}


@app.put("/api/sites/{site_id}/floors/{floor}/markers")
def put_markers(site_id: str, floor: str, markers: list[MarkerIn]):
    """Lift-lobby ArUco markers of this floor (fmc.vpr.aruco_markers). IDs are unique
    site-wide: lobbies are stacked, so the ID is what tells the floors apart."""
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(400, f"No transform saved yet for floor '{fid}' — fit & save control points first")
    ids = [m.marker_id for m in markers]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, f"Duplicate marker IDs on floor {fid}: {ids}")
    for f in site.raw.get("floors", []):
        if normalize_floor_id(f.get("floor")) == fid:
            continue
        clash = sorted(set(ids) & {int(m.get("marker_id", -1)) for m in f.get("markers") or []})
        if clash:
            raise HTTPException(400, f"Marker ID(s) {clash} already used on floor {f.get('floor')} — IDs must be unique site-wide")
    for m in markers:
        if not (0 <= m.marker_id < 50):
            raise HTTPException(400, f"Marker ID {m.marker_id} out of range 0–49 (DICT_4X4_50)")
        if not (0.05 <= m.size_m <= 1.0) or not (0.3 <= m.height_m <= 3.0):
            raise HTTPException(400, f"Marker {m.marker_id}: size 0.05–1 m and height 0.3–3 m")

    out = []
    for m in markers:
        x, y = transform.pixel_to_world(m.px, m.py)
        out.append({
            "marker_id": int(m.marker_id), "x": round(x, 3), "y": round(y, 3),
            "height_m": round(float(m.height_m), 3), "facing_deg": round(float(m.facing_deg) % 360.0, 1),
            "size_m": round(float(m.size_m), 4),
        })
    floor_entry = get_or_create_floor(site, fid)
    if out:
        floor_entry["markers"] = out
    else:
        floor_entry.pop("markers", None)
    save_site_config(site)
    return {"saved": len(out)}


@app.put("/api/sites/{site_id}/floors/{floor}/kiosks")
def put_kiosks(site_id: str, floor: str, kiosks: list[KioskIn]):
    """Payment kiosks of this floor (fmc.navigation.kiosks): QR start points. IDs are unique
    site-wide; an optional ArUco marker on the kiosk must be registered on this floor."""
    from fmc.navigation.kiosks import KIOSK_ID_RE, norm_id

    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(400, f"No transform saved yet for floor '{fid}' — fit & save control points first")
    ids = [norm_id(k.kiosk_id) for k in kiosks]
    for kid in ids:
        if not KIOSK_ID_RE.match(kid):
            raise HTTPException(400, f"Kiosk ID '{kid}': 1–32 letters, digits, - or _ (it goes into the QR link)")
    if len(set(ids)) != len(ids):
        raise HTTPException(400, f"Duplicate kiosk IDs on floor {fid}: {ids}")
    floor_entry = get_or_create_floor(site, fid)
    for f in site.raw.get("floors", []):
        if normalize_floor_id(f.get("floor")) == fid:
            continue
        clash = sorted(set(ids) & {norm_id(k.get("kiosk_id")) for k in f.get("kiosks") or []})
        if clash:
            raise HTTPException(400, f"Kiosk ID(s) {clash} already used on floor {f.get('floor')} — IDs must be unique site-wide")
    floor_markers = {int(m.get("marker_id", -1)) for m in floor_entry.get("markers") or []}
    out = []
    for k, kid in zip(kiosks, ids):
        if not (0.2 <= k.stand_m <= 3.0):
            raise HTTPException(400, f"Kiosk {kid}: stand distance 0.2–3 m")
        if k.marker_id is not None and k.marker_id not in floor_markers:
            raise HTTPException(400, f"Kiosk {kid}: marker {k.marker_id} isn't registered on floor {fid} (Markers tab)")
        x, y = transform.pixel_to_world(k.px, k.py)
        entry = {
            "kiosk_id": kid, "x": round(x, 3), "y": round(y, 3),
            "facing_deg": round(float(k.facing_deg) % 360.0, 1), "stand_m": round(float(k.stand_m), 2),
        }
        if k.marker_id is not None:
            entry["marker_id"] = int(k.marker_id)
        out.append(entry)
    if out:
        floor_entry["kiosks"] = out
    else:
        floor_entry.pop("kiosks", None)
    save_site_config(site)
    return {"saved": len(out)}


@app.get("/api/sites/{site_id}/kiosks/{kiosk_id}/qr.svg")
def kiosk_qr_svg(site_id: str, kiosk_id: str, slot: str, base: str):
    """Test QR exactly like the kiosk should show it: <base>/parking/?slot=<slot>&kiosk=<id>.
    base = the address phones use (e.g. https://xyz.ngrok-free.dev)."""
    from urllib.parse import quote, urlsplit

    from fastapi.responses import Response

    from fmc.navigation.kiosks import KIOSK_ID_RE, norm_id, qr_svg

    get_site_or_404(site_id)
    kid = norm_id(kiosk_id)
    parts = urlsplit(base)
    if not KIOSK_ID_RE.match(kid) or parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(400, "kiosk_id and an http(s) base address are required")
    slot = slot.strip()
    if not slot or len(slot) > 32:
        raise HTTPException(400, "slot is required")
    link = f"{parts.scheme}://{parts.netloc}/parking/?slot={quote(slot)}&kiosk={quote(kid)}"
    svg = qr_svg(link, caption=f"Kiosk {kid} · bay {slot}", sub=link)
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/sites/{site_id}/markers/{marker_id}.svg")
def marker_svg(site_id: str, marker_id: int, size_m: float = 0.23, label: str = ""):
    """Printable marker at real size — print at 100 % on matte paper (A3 for 23 cm)."""
    from fastapi.responses import Response

    from fmc.vpr import aruco_markers

    site = get_site_or_404(site_id)
    if not (0 <= marker_id < 50) or not (0.05 <= size_m <= 1.0):
        raise HTTPException(400, "marker_id 0–49, size_m 0.05–1.0")
    svg = aruco_markers.svg_for_print(marker_id, size_m, label, aruco_markers.dictionary_name(site))
    return Response(svg, media_type="image/svg+xml", headers={
        "Content-Disposition": f'inline; filename="marker_{marker_id}_{round(size_m * 1000)}mm.svg"',
    })


@app.put("/api/sites/{site_id}/floors/{floor}/slots")
def put_slots(site_id: str, floor: str, slots: list[SlotIn]):
    site = get_site_or_404(site_id)
    fid = _prep_floor_assets(site, floor)
    transform = _load_transform_or_none(site, fid)
    if transform is None:
        raise HTTPException(400, f"No transform saved yet for floor '{fid}' — fit & save control points first")

    world_slots = []
    for slot in slots:
        x, y = transform.pixel_to_world(slot.px, slot.py)
        world_slots.append({"slot_id": slot.slot_id, "zone": slot.zone, "x": round(x, 3), "y": round(y, 3)})

    floor_entry = get_or_create_floor(site, fid)
    floor_entry["vehicle_slots"] = world_slots
    save_site_config(site)
    return {"saved": len(world_slots)}


# ---------------------------------------------------------------- live testing

# site_id -> (index fingerprint, VPRPipeline)
_pipelines: dict[str, tuple[tuple, VPRPipeline]] = {}
_pipelines_lock = threading.Lock()
# files whose change can't alter what localize() returns
_FP_SKIP_SUFFIXES = (".trace.db", ".ply")
_FP_SKIP_NAMES = {"cabin_colours.json", "tags.json", "locations.csv"}


def _index_fingerprint(site) -> tuple:
    """(name, size, mtime) of the index files, plus the current-DB pointer's content.
    H2GIS .mv.db files are named, not stat'ed: H2 touches them on every open, and a new
    map always lands in a new file + pointer swap."""
    d = Path(site.index_dir)
    if not d.exists():
        return ()
    fp = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.name in _FP_SKIP_NAMES or f.name.endswith(_FP_SKIP_SUFFIXES):
            continue
        if f.name.endswith(".mv.db"):
            fp.append((f.name,))
            continue
        st = f.stat()
        entry = (f.name, st.st_size, st.st_mtime_ns)
        if f.name.endswith(".current"):
            entry += (f.read_text(encoding="utf-8", errors="replace").strip(),)
        fp.append(entry)
    return tuple(fp)


def _pipeline_for(site) -> VPRPipeline:
    """cached pipeline, rebuilt when the site's index files change"""
    fp = _index_fingerprint(site)
    with _pipelines_lock:
        hit = _pipelines.get(site.site_id)
        if hit and hit[0] == fp:
            return hit[1]
        pipe = VPRPipeline(site)
        _pipelines[site.site_id] = (fp, pipe)
        return pipe


# 3D map keyframes come from ~1280x720 ARCore frames; full-res gallery photos (4032x3024)
# put ORB features at ~3x the map's scale and PnP match far fewer of them
QUERY_MAX_DIM = 1280

# A camera-app photo is wider than the parking app's video frame: a 26 mm-equivalent main
# camera is f ≈ 0.72 × long side, the server's default guess is 0.85 (made for video
# frames). ~18% focal error and RANSAC rejects the correct matches. So use the photo's
# EXIF focal length, and without EXIF retry once with the typical photo focal.
PHOTO_F_RATIO = 0.72
_EXIF_IFD = 0x8769
_EXIF_FOCAL_35MM = 41989  # FocalLengthIn35mmFilm
_FULL_FRAME_DIAG_MM = 43.27  # 35 mm film diagonal


def _decode_photo(contents: bytes):
    """BGR image upright per EXIF orientation, plus the 35 mm-equivalent focal (or None)."""
    try:
        import io
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(contents))
        f35 = None
        try:
            v = img.getexif().get_ifd(_EXIF_IFD).get(_EXIF_FOCAL_35MM)
            f35 = float(v) if v else None
        except Exception:
            f35 = None
        img = ImageOps.exif_transpose(img).convert("RGB")
        return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR), f35
    except Exception:
        frame = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
        return frame, None


def _photo_intrinsics(w: int, h: int, f_px: float):
    from fmc.vpr.camera_intrinsics import Intrinsics

    return Intrinsics(fx=f_px, fy=f_px, cx=w / 2.0, cy=h / 2.0, width=w, height=h)


@app.post("/api/sites/{site_id}/query")
async def query_position(site_id: str, file: UploadFile = File(...), floor: str | None = Form(None)):
    """Upload a real photo and see where VPR thinks it is, overlaid on the
    map -- the same fmc.vpr.pipeline used by the position API and
    scripts/query_position.py, just visualized here."""
    site = get_site_or_404(site_id)
    if not site.embeddings_path.exists():
        raise HTTPException(400, "No index built yet for this site (fmc.dataset.build_index)")

    contents = await file.read()
    frame, f35 = _decode_photo(contents)
    if frame is None:
        raise HTTPException(400, "Could not decode uploaded image")

    src_h, src_w = frame.shape[:2]
    scale = QUERY_MAX_DIM / max(src_w, src_h)
    if scale < 1.0:
        frame = cv2.resize(frame, (round(src_w * scale), round(src_h * scale)), interpolation=cv2.INTER_AREA)
    h, w = frame.shape[:2]

    prior = normalize_floor_id(floor) if floor else None

    # focal candidates, best first: EXIF → default guess (video-like frames) → photo guess
    long_side = max(w, h)
    if f35:
        focals = [(f35 * math.hypot(w, h) / _FULL_FRAME_DIAG_MM, f"exif {f35:g} mm-eq")]
    else:
        from fmc.vpr.camera_intrinsics import GUESS_F_RATIO

        focals = [
            (GUESS_F_RATIO * long_side, f"guess {GUESS_F_RATIO}×long side"),
            (PHOTO_F_RATIO * long_side, f"photo guess {PHOTO_F_RATIO}×long side"),
        ]
    used = {"focal_px": None, "focal_source": None, "tries": 0}

    def _run():
        # cached per site, rebuilt on index change -- see module docstring
        pipeline = _pipeline_for(site)
        if pipeline.map_index is None and not any(
            pipeline._index_for_floor(f) is not None for f in site.floor_ids()
        ):
            return None
        res = None
        for f_px, src in focals:
            used.update(focal_px=round(f_px, 1), focal_source=src, tries=used["tries"] + 1)
            # editor floor first (prior only -- other mapped floors are still tried)
            res = pipeline.localize(frame, prior_floor=prior, intrinsics=_photo_intrinsics(w, h, f_px))
            if res.matched:
                break
        return res

    # off the event loop: this server also hosts the parking API (/floor-plan mount)
    t0 = time.perf_counter()
    result = await run_in_threadpool(_run)
    if result is None:
        raise HTTPException(400, "No 3D map yet -- map the floor in the mapping app and Finalize first")
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    image_info = {
        "source_size": [src_w, src_h], "query_size": [w, h], "elapsed_ms": elapsed_ms,
        "focal_px": used["focal_px"], "focal_source": used["focal_source"], "focal_tries": used["tries"],
    }
    if not result.matched:
        return {
            "matched": False,
            "reason": (
                "PnP found no pose (too few 2D-3D matches against the 3D map). "
                "Image VPR fallback is disabled, so there is no coarse match either. "
                "Try a photo of a mapped area with pillars, signs or bay numbers in view."
            ),
            **image_info,
        }

    r = result.record
    pose = result.pose_6dof
    response = {
        "matched": True,
        "method": result.method,
        "image_id": r.image_id,
        "floor": r.floor,
        "zone": r.zone,
        "x": r.x,
        "y": r.y,
        "heading": float(pose.heading) if pose else float(r.orientation),
        "num_inliers": int(pose.num_inliers) if pose and pose.num_inliers is not None else None,
        "num_matches": int(pose.num_matches) if pose and pose.num_matches is not None else None,
        "similarity": result.similarity,
        "inlier_ratio": result.inlier_ratio,
        **image_info,
    }
    transform = _load_transform_or_none(site, r.floor)
    if transform is not None:
        response["px"], response["py"] = transform.world_to_pixel(r.x, r.y)
    return response


@app.post("/api/sites/{site_id}/route")
def get_route(site_id: str, req: RouteRequest):
    """Route from a localized position to a vehicle slot -- the SAME steps as the parking
    API's /route (fmc.api.server.get_route), so the editor shows what the phone will:
      1. match the position into its corridor BAND (not onto the centre line)
      2. route from that corridor's centre line, across floors via vertical_connectors
      3. lane start: parallel to the corridor from where you stand up to the first corner,
         then straight / L onto the next segment (join_route_to_position)
    Waypoints come back in world metres and, per leg, in that leg's floor-plan pixels."""
    from fmc.fusion.map_matching import match_to_walkable

    site = get_site_or_404(site_id)
    floor = normalize_floor_id(req.floor)
    segments = site.walkable_segments(floor)
    if not segments:
        raise HTTPException(400, f"No walkable_segments for floor {floor}")

    slot = site.vehicle_slot(req.slot_id)
    if slot is None:
        raise HTTPException(404, f"Unknown slot_id: {req.slot_id}")
    dest_floor = normalize_floor_id(slot["floor"])

    # 1. inside the corridor band — unchanged if already in it; route from its centre line
    center: list = []
    user_x, user_y, _ = match_to_walkable(
        req.start_x, req.start_y, segments, heading_deg=req.heading, center_out=center,
    )
    start_x, start_y = center[0] if center else (user_x, user_y)

    # 2. same routing choice as /route
    floors_segments = {
        normalize_floor_id(f["floor"]): list(f.get("walkable_segments") or [])
        for f in site.raw.get("floors", [])
    }
    connectors = site.vertical_connectors()
    if dest_floor == floor and not connectors:
        route = calculate_route(segments, start_x=start_x, start_y=start_y, dest_x=slot["x"], dest_y=slot["y"])
    else:
        route = calculate_multifloor_route(
            floors_segments=floors_segments,
            vertical_connectors=connectors,
            start_floor=floor,
            start_x=start_x,
            start_y=start_y,
            dest_floor=dest_floor,
            dest_x=slot["x"],
            dest_y=slot["y"],
            dest_slot_id=req.slot_id,
        )
    if route is None:
        if dest_floor != floor and not connectors:
            raise HTTPException(400, f"Cross-floor route {floor}→{dest_floor} needs vertical_connectors in config.yaml")
        raise HTTPException(404, "No route found -- the walkable graph may be disconnected between these points")

    # 3. lane start from where the user actually stands
    lane_off, lane_mode = join_route_to_position(route, floor, user_x, user_y)

    transforms: dict = {}

    def to_px(fl, pts):
        fl = normalize_floor_id(fl)
        if fl not in transforms:
            transforms[fl] = _load_transform_or_none(site, fl)
        t = transforms[fl]
        return [t.world_to_pixel(x, y) for x, y in pts] if t is not None else None

    legs = []
    for leg in route.legs or []:
        lf = normalize_floor_id(leg.floor) if leg.floor is not None else floor
        wps = [(float(x), float(y)) for x, y in leg.waypoints]
        legs.append({
            "floor": lf,
            "instruction": leg.instruction,
            "floor_transition": leg.floor_transition,
            "distance": float(leg.distance),
            "waypoints": wps,
            "waypoints_px": None if leg.floor_transition else to_px(lf, wps),
        })
    # start-floor walk (older clients draw only this)
    start_wps = [p for lg in legs if lg["floor"] == floor and not lg["floor_transition"] for p in lg["waypoints"]]
    if not legs:
        start_wps = [(float(x), float(y)) for x, y in route.waypoints]

    return {
        "floor": floor,
        "dest_floor": dest_floor,
        "start": {"x": user_x, "y": user_y, "moved_m": round(math.hypot(user_x - req.start_x, user_y - req.start_y), 2)},
        "lane_mode": lane_mode,
        "lane_offset_m": round(float(lane_off), 2),
        "legs": legs,
        "waypoints": route.waypoints,
        "waypoints_px": to_px(floor, start_wps),
        "total_distance": route.total_distance,
    }


# ---------------------------------------------------------------- static frontend
# Mounted last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
