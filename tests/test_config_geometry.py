"""get_or_create_floor / save_site_config round-trip real config.yaml files
-- worth testing before trusting them against a real site's config."""
from __future__ import annotations

from pathlib import Path

import yaml

from fmc.config import SiteConfig, get_or_create_floor, rename_floor, save_site_config
from fmc.dataset.locations import load_locations_csv, save_locations_csv


def _make_site(tmp_path: Path, monkeypatch, site_id: str, raw: dict) -> SiteConfig:
    import fmc.config as config_module
    monkeypatch.setattr(config_module, "DATA_ROOT", tmp_path)
    (tmp_path / site_id).mkdir(parents=True, exist_ok=True)
    return SiteConfig(site_id=site_id, raw=raw)


def test_get_or_create_floor_returns_existing(tmp_path: Path, monkeypatch):
    raw = {"site_id": "s1", "floors": [{"floor": 1, "walkable_segments": ["existing"]}]}
    site = _make_site(tmp_path, monkeypatch, "s1", raw)

    floor = get_or_create_floor(site, 1)
    assert floor["walkable_segments"] == ["existing"]
    assert len(site.raw["floors"]) == 1  # didn't duplicate


def test_get_or_create_floor_creates_new(tmp_path: Path, monkeypatch):
    raw = {"site_id": "s1", "floors": [{"floor": "1"}]}
    site = _make_site(tmp_path, monkeypatch, "s1", raw)

    floor = get_or_create_floor(site, "B1")
    assert floor["floor"] == "B1"
    assert floor["walkable_segments"] == []
    assert len(site.raw["floors"]) == 2


def test_save_site_config_round_trips(tmp_path: Path, monkeypatch):
    raw = {"site_id": "s1", "floors": [{"floor": 1, "walkable_segments": [{"x1": 0, "y1": 0, "x2": 1, "y2": 1}]}]}
    site = _make_site(tmp_path, monkeypatch, "s1", raw)

    save_site_config(site)

    with open(tmp_path / "s1" / "config.yaml", "r", encoding="utf-8") as f:
        reloaded = yaml.safe_load(f)
    assert reloaded == raw


def test_rename_floor_retags_locations_csv(tmp_path: Path, monkeypatch):
    raw = {"site_id": "s1", "floors": [{"floor": "1", "walkable_segments": []}]}
    site = _make_site(tmp_path, monkeypatch, "s1", raw)
    site.index_dir.mkdir(parents=True, exist_ok=True)
    save_locations_csv(
        site.index_dir / "locations.csv",
        [{"location_id": "P5", "floor": "1", "zone": "B", "x": 1.0, "y": 2.0}],
    )

    rename_floor(site, "1", "B1")

    rows = load_locations_csv(site.index_dir / "locations.csv")
    assert rows[0]["floor"] == "B1"
    assert site.floor_ids() == ["B1"]
