"""Snap a raw fused position onto the nearest walkable path segment.

Prevents nonsensical positions (e.g. "inside a wall") per project brief
Section 18. Baseline: nearest-point-on-segment over the site's walkable
segment list (loaded from data/<site>/config.yaml). Good enough for a
simple corridor graph; revisit with a proper graph/grid representation
if the real facility's walkable area is complex (multiple parallel aisles,
etc.).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    # corridor width (m) around this line; None → DEFAULT_CORRIDOR_WIDTH_M (symmetric)
    width: float | None = None
    # optional asymmetric band: metres to the LEFT / RIGHT of the line, looking from
    # (x1, y1) toward (x2, y2) in map coords (y up). Override `width` when set.
    width_left: float | None = None
    width_right: float | None = None


# walkable segments are corridor CENTRE lines; corridors are bands up to ~8 m wide.
# Snapping onto the centre line would move someone walking along the side by up to half
# the width — a real position error. Inside the band the position is kept as is.
DEFAULT_CORRIDOR_WIDTH_M = 3.0


def corridor_half_width(seg: Segment) -> float:
    w = seg.width if seg.width is not None and seg.width > 0 else DEFAULT_CORRIDOR_WIDTH_M
    return float(w) / 2.0


def corridor_sides(seg: Segment) -> tuple[float, float]:
    """(left, right) extent of the corridor band from the segment line, in metres."""
    half = corridor_half_width(seg)
    left = seg.width_left if seg.width_left is not None and seg.width_left >= 0 else half
    right = seg.width_right if seg.width_right is not None and seg.width_right >= 0 else half
    return float(left), float(right)


def _band_place(x: float, y: float, seg: Segment) -> tuple[float, float, float, float, float]:
    """Point (x, y) against seg's band → (bx, by, outside_m, cx, cy):
    position kept inside the band, else pulled to the nearest band edge; (cx, cy) is the
    nearest point on the segment line."""
    cx, cy, _ = _nearest_point_on_segment(x, y, seg)
    dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        d = math.hypot(x - cx, y - cy)
        half = max(corridor_sides(seg))
        if d <= half:
            return x, y, 0.0, cx, cy
        k = half / d
        return cx + (x - cx) * k, cy + (y - cy) * k, d - half, cx, cy
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux  # left normal (map coords, y up)
    vx, vy = x - cx, y - cy
    lateral = vx * nx + vy * ny  # + = left of the line
    along = vx * ux + vy * uy  # non-zero only beyond the segment's ends
    left, right = corridor_sides(seg)
    clamped = max(-right, min(left, lateral))
    outside = math.hypot(along, lateral - clamped)
    if outside <= 1e-9:
        return x, y, 0.0, cx, cy
    return cx + nx * clamped, cy + ny * clamped, outside, cx, cy


def _nearest_point_on_segment(px: float, py: float, seg: Segment) -> tuple[float, float, float]:
    dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        nx, ny = seg.x1, seg.y1
    else:
        t = ((px - seg.x1) * dx + (py - seg.y1) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        nx, ny = seg.x1 + t * dx, seg.y1 + t * dy
    dist = math.hypot(px - nx, py - ny)
    return nx, ny, dist


# --- sticky, direction-aware matching ------------------------------------------------
# Plain nearest-segment snapping flips between corridors near junctions / close parallel
# aisles with every noisy fix (±0.5–1.4 m), and the route then starts on a corridor you're
# not in — guidance cuts across pillars/cars to reach it.
SWITCH_PENALTY_M = 1.2  # another segment must be this much closer to take over
HEADING_PENALTY_M = 0.6  # camera facing across a corridor's axis (weak prior: you may look sideways)
HEADING_PENALTY_FROM_DEG = 50.0


def _seg_key(seg: Segment) -> tuple:
    """Direction-independent identity of a segment (rounded endpoints)."""
    a = (round(seg.x1, 2), round(seg.y1, 2))
    b = (round(seg.x2, 2), round(seg.y2, 2))
    return (a, b) if a <= b else (b, a)


def _axis_diff_deg(heading_deg: float, seg: Segment) -> float:
    """Angle between a compass heading (0° = +Y) and the segment's axis, in [0, 90]."""
    seg_deg = math.degrees(math.atan2(seg.x2 - seg.x1, seg.y2 - seg.y1))
    d = abs((heading_deg - seg_deg + 180.0) % 360.0 - 180.0)
    return min(d, 180.0 - d)


def match_to_walkable(
    x: float,
    y: float,
    segments: list[dict],
    heading_deg: float | None = None,
    prev_key: tuple | None = None,
    center_out: list | None = None,
) -> tuple[float, float, tuple | None]:
    """Place (x, y) inside the walkable corridors, preferring the corridor you were already
    in and corridors running the way you're facing. Returns (x, y, segment_key) — pass
    the key back as prev_key on the next fix.

    Inside a corridor's band (centre line ± width/2) the position is returned unchanged;
    outside every band (e.g. inside a pillar/car row) it is pulled to the nearest band edge.

    Segments are split at T/X-junctions first, so "staying on your segment" means your
    stretch of corridor, not the whole corridor through a junction.

    center_out: if a list is given, the matched corridor's centre-line point is appended
    (route from there — the nearest centre line near a junction can be the wrong corridor).
    """
    if not segments:
        return x, y, None
    from fmc.navigation.graph import split_at_junctions

    best = None
    for seg_dict in split_at_junctions(segments):
        seg = Segment(**seg_dict)
        # inside the band → kept; outside → pulled to the band edge, not the line
        bx, by, outside, nx, ny = _band_place(x, y, seg)
        dist = math.hypot(x - nx, y - ny)
        key = _seg_key(seg)
        # distance outside the band decides; centre distance only breaks ties (overlapping
        # bands at junctions) so being anywhere inside a corridor costs ~nothing
        cost = outside + 0.05 * dist
        if prev_key is not None and key != prev_key:
            cost += SWITCH_PENALTY_M
        if heading_deg is not None and math.isfinite(heading_deg):
            if _axis_diff_deg(heading_deg, seg) > HEADING_PENALTY_FROM_DEG:
                cost += HEADING_PENALTY_M
        if best is None or cost < best[3]:
            best = (bx, by, key, cost, nx, ny)
    if center_out is not None:
        center_out.append((best[4], best[5]))
    return best[0], best[1], best[2]


def snap_to_walkable(x: float, y: float, segments: list[dict]) -> tuple[float, float]:
    """Return the (x, y) on the walkable graph nearest to the raw estimate."""
    if not segments:
        return x, y

    best = None
    for seg_dict in segments:
        seg = Segment(**seg_dict)
        nx, ny, dist = _nearest_point_on_segment(x, y, seg)
        if best is None or dist < best[2]:
            best = (nx, ny, dist)

    return best[0], best[1]
