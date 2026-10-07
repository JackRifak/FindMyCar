"""Project-wide configuration.

Coordinate system, storage paths, and per-site config loading.
See docs/01_coordinate_system.md and docs/03_dataset_spec.md for the conventions
this module implements.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fmc.floors import normalize_floor_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"

# Normalized processed-image target size (short side), per docs/03_dataset_spec.md
PROCESSED_IMAGE_SHORT_SIDE = 720

# Embedding vector dimensionality of the current (placeholder) embedder.
# Real value will change once Deliverable 4 (embedding model benchmark) picks a model.
EMBEDDING_DIM = 64

# VPR search parameters
TOP_K_CANDIDATES = 10

# Geometric verification thresholds (baseline ORB+RANSAC; revisit after
# Deliverable 4/5 benchmarking of SuperPoint/SuperGlue/LightGlue etc.)
ORB_MIN_MATCH_COUNT = 15
RANSAC_REPROJ_THRESHOLD = 5.0
GEOMETRIC_INLIER_RATIO_THRESHOLD = 0.5


@dataclass
class SiteConfig:
    site_id: str
    raw: dict[str, Any]

    @property
    def data_dir(self) -> Path:
        return DATA_ROOT / self.site_id

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def dataset_jsonl_path(self) -> Path:
        return self.index_dir / "dataset.jsonl"

    @property
    def embeddings_path(self) -> Path:
        return self.index_dir / "embeddings.npz"

    def walkable_segments(self, floor) -> list[dict]:
        want = normalize_floor_id(floor)
        for f in self.raw["floors"]:
            if normalize_floor_id(f.get("floor")) == want:
                return f.get("walkable_segments", [])
        return []

    def vehicle_slot(self, slot_id: str) -> dict | None:
        for f in self.raw["floors"]:
            for slot in f.get("vehicle_slots", []):
                if slot["slot_id"] == slot_id:
                    return {**slot, "floor": normalize_floor_id(f.get("floor"))}
        return None

    def floor_ids(self) -> list[str]:
        """Declared floors in config order (user-defined naming & count)."""
        return [normalize_floor_id(f.get("floor")) for f in self.raw.get("floors", [])]

    def floor_ids_bottom_up(self) -> list[str]:
        """Declared floors in physical order, lowest first (B2, B1, G…) — for stacking,
        up/down and adjacency. Config order is only the order they were added."""
        from fmc.floors import floors_bottom_up
        return floors_bottom_up(self.floor_ids())

    def vertical_connectors(self) -> list[dict]:
        """Stairs / elevators / ramps linking floors (see site config.yaml)."""
        raw = self.raw.get("vertical_connectors") or []
        out = []
        for c in raw:
            nodes = c.get("nodes") or {}
            norm_nodes = {
                normalize_floor_id(k): {"x": float(v["x"]), "y": float(v["y"])}
                for k, v in nodes.items()
            }
            if len(norm_nodes) < 2:
                continue
            out.append({
                "connector_id": str(c.get("connector_id", "connector")),
                "type": str(c.get("type", "stairs")),
                "penalty_cost": float(c.get("penalty_cost", 10.0)),
                "nodes": norm_nodes,
            })
        return out


def load_site_config(site_id: str) -> SiteConfig:
    config_path = DATA_ROOT / site_id / "config.yaml"
    if not config_path.exists():
        _write_default_site_config(config_path, site_id)
        print(
            f"No config.yaml found for site '{site_id}' -- created a minimal default "
            f"at {config_path}. Dataset ingestion/VPR work fine with this; fill in "
            f"walkable_segments/vehicle_slots (see docs/01_coordinate_system.md) "
            f"once you're ready for routing/map-matching."
        )
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    # normalize floor labels to strings in-memory
    for entry in raw.get("floors") or []:
        if "floor" in entry:
            entry["floor"] = normalize_floor_id(entry["floor"])
    return SiteConfig(site_id=raw["site_id"], raw=raw)


def _write_default_site_config(config_path: Path, site_id: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    default = {
        "site_id": site_id,
        "floors": [
            {
                "floor": "1",
                "zones": [],
                "walkable_segments": [],
                "landmarks": [],
                "vehicle_slots": [],
            }
        ],
        "stack_m": 4.0,
        "vertical_connectors": [],
    }
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(default, f, sort_keys=False)
    with open(config_path, "a", encoding="utf-8") as f:
        f.write(
            "\n# Floor labels are free-form strings (1, G, B1, B2, …).\n"
            "# Add / rename floors via the mapping UI or config.yaml.\n"
            "# stack_m = vertical gap (meters) between floors in the 3D viewer.\n"
            "# vertical_connectors.nodes keys must match those floor labels.\n"
        )


def save_site_config(site: SiteConfig) -> None:
    """Write a SiteConfig's current `raw` dict back to its config.yaml."""
    config_path = DATA_ROOT / site.site_id / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(site.raw, f, sort_keys=False)


def get_or_create_floor(site: SiteConfig, floor) -> dict:
    """Return the floor dict for `floor`, creating a minimal one if missing."""
    want = normalize_floor_id(floor)
    for floor_entry in site.raw["floors"]:
        if normalize_floor_id(floor_entry.get("floor")) == want:
            floor_entry["floor"] = want
            return floor_entry
    new_floor = {
        "floor": want,
        "zones": [],
        "walkable_segments": [],
        "landmarks": [],
        "vehicle_slots": [],
    }
    site.raw.setdefault("floors", []).append(new_floor)
    return new_floor


def rename_floor(site: SiteConfig, old_floor, new_floor) -> dict:
    """Rename a floor label and update vertical_connector node keys."""
    old_id = normalize_floor_id(old_floor)
    new_id = normalize_floor_id(new_floor)
    if old_id == new_id:
        return get_or_create_floor(site, old_id)
    if new_id in site.floor_ids():
        raise ValueError(f"floor '{new_id}' already exists")
    entry = None
    for floor_entry in site.raw.get("floors", []):
        if normalize_floor_id(floor_entry.get("floor")) == old_id:
            floor_entry["floor"] = new_id
            entry = floor_entry
            break
    if entry is None:
        raise ValueError(f"floor '{old_id}' not found")

    for conn in site.raw.get("vertical_connectors") or []:
        nodes = conn.get("nodes") or {}
        if old_id in nodes or old_floor in nodes or str(old_floor) in nodes:
            # rebuild with normalized keys
            rebuilt = {}
            for k, v in nodes.items():
                kid = normalize_floor_id(k)
                rebuilt[new_id if kid == old_id else kid] = v
            conn["nodes"] = rebuilt

    retag_floor_artifacts(site, old_id, new_id)
    return entry


def retag_floor_artifacts(site: SiteConfig, old_floor, new_floor) -> dict:
    """Rewrite floor labels in locations.csv, dataset.jsonl, and pixel CSVs."""
    from fmc.dataset.locations import load_locations_csv, save_locations_csv
    from fmc.dataset.schema import load_records, save_records
    from fmc.floors import floorplan_dir, normalize_floor_id as _nf

    old_id = _nf(old_floor)
    new_id = _nf(new_floor)
    stats = {"locations": 0, "records": 0, "pixel_csvs": 0}

    loc_path = site.index_dir / "locations.csv"
    if loc_path.exists():
        rows = load_locations_csv(loc_path)
        changed = False
        for row in rows:
            if _nf(row["floor"]) == old_id:
                row["floor"] = new_id
                stats["locations"] += 1
                changed = True
        if changed:
            save_locations_csv(loc_path, rows)

    ds_path = site.dataset_jsonl_path
    if ds_path.exists():
        records = load_records(ds_path)
        out = []
        changed = False
        for rec in records:
            if _nf(rec.floor) == old_id:
                out.append(rec.model_copy(update={"floor": new_id}))
                stats["records"] += 1
                changed = True
            else:
                out.append(rec)
        if changed:
            save_records(ds_path, out)

    # pixel helper CSVs (locations_pixels / segments_pixels / slots_pixels)
    import csv

    candidates = [
        site.data_dir / "floorplan" / "locations_pixels.csv",
        floorplan_dir(site.data_dir, old_id) / "locations_pixels.csv",
        floorplan_dir(site.data_dir, new_id) / "locations_pixels.csv",
    ]
    for path in candidates:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if "floor" not in fieldnames:
                continue
            rows = list(reader)
        n = 0
        for row in rows:
            if _nf(row.get("floor")) == old_id:
                row["floor"] = new_id
                n += 1
        if n:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            stats["pixel_csvs"] += 1

    # H2GIS map landmarks + per-floor npz archives
    stats["map_landmarks"] = 0
    try:
        from fmc.storage.h2gis_store import retag_floor as retag_h2gis

        stats["map_landmarks"] = int(retag_h2gis(site, old_id, new_id) or 0)
    except Exception:
        pass

    from fmc.floors import floor_token

    old_npz = site.index_dir / f"map_landmarks_f{floor_token(old_id)}.npz"
    new_npz = site.index_dir / f"map_landmarks_f{floor_token(new_id)}.npz"
    if old_npz.exists() and not new_npz.exists():
        old_npz.rename(new_npz)
        stats["map_npz"] = 1
    else:
        stats["map_npz"] = 0

    return stats
