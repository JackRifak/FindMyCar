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
