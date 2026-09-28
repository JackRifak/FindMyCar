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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"

# Normalized processed-image target size (short side), per docs/03_dataset_spec.md
PROCESSED_IMAGE_SHORT_SIDE = 720

# Embedding vector dimensionality of the current (placeholder) embedder.
# Real value will change once Deliverable 4 (embedding model benchmark) picks a model.
EMBEDDING_DIM = 64

# VPR search parameters
TOP_K_CANDIDATES = 5

# Geometric verification thresholds (baseline ORB+RANSAC; revisit after
# Deliverable 4/5 benchmarking of SuperPoint/SuperGlue/LightGlue etc.)
ORB_MIN_MATCH_COUNT = 15
RANSAC_REPROJ_THRESHOLD = 5.0
GEOMETRIC_INLIER_RATIO_THRESHOLD = 0.4


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

    def walkable_segments(self, floor: int) -> list[dict]:
        for f in self.raw["floors"]:
            if f["floor"] == floor:
                return f.get("walkable_segments", [])
        return []

    def vehicle_slot(self, slot_id: str) -> dict | None:
        for f in self.raw["floors"]:
            for slot in f.get("vehicle_slots", []):
                if slot["slot_id"] == slot_id:
                    return {**slot, "floor": f["floor"]}
        return None


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
    return SiteConfig(site_id=raw["site_id"], raw=raw)


def _write_default_site_config(config_path: Path, site_id: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    default = {
        "site_id": site_id,
        "floors": [
            {
                "floor": 1,
                "zones": [],
                "walkable_segments": [],
                "landmarks": [],
                "vehicle_slots": [],
            }
        ],
    }
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(default, f, sort_keys=False)
    with open(config_path, "a", encoding="utf-8") as f:
        f.write(
            "\n# Auto-generated placeholder. Add walkable_segments/vehicle_slots\n"
            "# per docs/01_coordinate_system.md and data/mock_site/config.yaml for\n"
            "# an example, once you need routing (fmc/navigation) or map matching\n"
            "# (fmc/fusion/map_matching.py). Add one entry per real floor if this\n"
            "# site has more than one -- the 'floor' field must match the floor\n"
            "# numbers used in your locations.csv / dataset.jsonl.\n"
        )


def save_site_config(site: SiteConfig) -> None:
    """Write a SiteConfig's current `raw` dict back to its config.yaml.
    Used by tools that populate walkable_segments/vehicle_slots after the
    fact (e.g. scripts/build_site_geometry.py)."""
    config_path = DATA_ROOT / site.site_id / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(site.raw, f, sort_keys=False)


def get_or_create_floor(site: SiteConfig, floor: int) -> dict:
    """Return the floor dict for `floor` from site.raw, creating a minimal
    one (appended to site.raw["floors"]) if it doesn't exist yet. Mutates
    site.raw in place -- call save_site_config(site) afterward to persist."""
    for floor_entry in site.raw["floors"]:
        if floor_entry["floor"] == floor:
            return floor_entry
    new_floor = {"floor": floor, "zones": [], "walkable_segments": [], "landmarks": [], "vehicle_slots": []}
    site.raw["floors"].append(new_floor)
    return new_floor
