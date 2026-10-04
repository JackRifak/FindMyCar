"""Floor identity helpers — labels are user-assigned strings (1, G, B1, …)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional


def normalize_floor_id(floor) -> str:
    """Canonical floor id for config / DB / routing keys."""
    if floor is None:
        return "1"
    if isinstance(floor, bool):
        return "1"
    if isinstance(floor, int):
        return str(floor)
    s = str(floor).strip()
    return s if s else "1"


def floor_token(floor) -> str:
    """Filesystem-safe token for per-floor asset dirs (1, G, B1, …)."""
    tok = re.sub(r"[^A-Za-z0-9_-]+", "_", normalize_floor_id(floor))
    return tok or "1"


def floorplan_dir(site_data_dir: Path, floor) -> Path:
    """Per-floor floorplan asset directory: data/<site>/floorplan/<token>/."""
    return Path(site_data_dir) / "floorplan" / floor_token(floor)


def resolve_floorplan_asset(
    site_data_dir: Path,
    floor,
    name: str,
    *,
    for_write: bool = False,
) -> Path:
    """Path to a per-floor floorplan asset (no shared site-level fallback)."""
    per = floorplan_dir(site_data_dir, floor) / name
    if for_write:
        per.parent.mkdir(parents=True, exist_ok=True)
    return per


def floors_equal(a, b) -> bool:
    return normalize_floor_id(a) == normalize_floor_id(b)


def adjacent_floors(ordered: Iterable, current) -> list[str]:
    """Neighbors of `current` in the site's declared floor order (not ±1 arithmetic)."""
    ids = [normalize_floor_id(f) for f in ordered]
    cur = normalize_floor_id(current)
    if cur not in ids:
        return []
    i = ids.index(cur)
    out = []
    if i > 0:
        out.append(ids[i - 1])
    if i + 1 < len(ids):
        out.append(ids[i + 1])
    return out


def coerce_floor_array(values, n: Optional[int] = None) -> list[str]:
    """Normalize a list/array of floor labels to python strs."""
    if values is None:
        if n is None:
            return []
        return ["1"] * int(n)
    out = [normalize_floor_id(v) for v in list(values)]
    if n is not None and len(out) < n:
        out.extend(["1"] * (n - len(out)))
    return out[:n] if n is not None else out
