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


_BASEMENT = re.compile(r"^(?:B|P|LB|SB|BASEMENT)\s*-?(\d+)$", re.I)
_GROUND = {"G", "GF", "GR", "GROUND", "L", "LOBBY", "0", "L0"}
_UPPER = re.compile(r"^(?:L|F|LEVEL|FLOOR)?\s*(\d+)$", re.I)


def floor_level(floor) -> Optional[int]:
    """Physical level from a floor label: B2 → -2, G → 0, 1/L1/F1 → 1. None if unknown."""
    f = normalize_floor_id(floor).strip()
    m = _BASEMENT.match(f)
    if m:
        return -int(m.group(1))
    if f.upper() in _GROUND:
        return 0
    m = _UPPER.match(f)
    if m:
        return int(m.group(1))
    return None


def floors_bottom_up(floors: Iterable) -> list[str]:
    """Floors ordered physically, lowest first (B2, B1, G, 1, 2…). Labels with no known
    level keep their given order and go above the known ones."""
    ids = [normalize_floor_id(f) for f in floors]
    seen: list[str] = []
    for f in ids:
        if f not in seen:
            seen.append(f)
    def key(item):
        i, f = item
        lvl = floor_level(f)
        return (0, lvl, i) if lvl is not None else (1, 0, i)
    return [f for _, f in sorted(enumerate(seen), key=key)]


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
