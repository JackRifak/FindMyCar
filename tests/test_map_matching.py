"""Corridor-aware matching: segments are centre lines of corridors with a width."""
import math

from fmc.fusion.map_matching import DEFAULT_CORRIDOR_WIDTH_M, match_to_walkable
from fmc.navigation.graph import split_at_junctions

# 8 m wide corridor running north along x=0, a 3 m one branching east at y=10
SEGS_T = [
    {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 20.0, "width": 8.0},
    {"x1": 0.0, "y1": 10.0, "x2": 15.0, "y2": 10.0, "width": 3.0},
]
# two parallel 3 m aisles 4 m apart (pillar/car row between them)
SEGS_PARALLEL = [
    {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 30.0},
    {"x1": 4.0, "y1": 0.0, "x2": 4.0, "y2": 30.0},
]


def _walk(segs, fixes):
    key = None
    out = []
    for x, y, h in fixes:
        centre: list = []
        sx, sy, key = match_to_walkable(x, y, segs, heading_deg=h, prev_key=key, center_out=centre)
        out.append(((round(sx, 2), round(sy, 2)), (round(centre[0][0], 2), round(centre[0][1], 2))))
    return out


def test_inside_wide_corridor_position_is_kept():
    # 3.5 m off the centre of the 8 m corridor: that's where you are — don't move you
    sx, sy, _ = match_to_walkable(3.5, 4.0, SEGS_T, heading_deg=0.0)
    assert (round(sx, 6), round(sy, 6)) == (3.5, 4.0)


def test_outside_band_pulled_to_edge_not_centre():
    # 6 m off an 8 m corridor's centre (inside the car row) → its edge at 4 m, not 0
    sx, sy, _ = match_to_walkable(-6.0, 4.0, SEGS_T, heading_deg=0.0)
    assert math.isclose(sx, -4.0, abs_tol=1e-6) and math.isclose(sy, 4.0, abs_tol=1e-6)


def test_default_width_when_unset():
    half = DEFAULT_CORRIDOR_WIDTH_M / 2
    sx, _, _ = match_to_walkable(half - 0.2, 5.0, SEGS_PARALLEL[:1])
    assert math.isclose(sx, half - 0.2)  # inside default band → kept
    sx, _, _ = match_to_walkable(half + 0.8, 5.0, SEGS_PARALLEL[:1])
    assert math.isclose(sx, half)  # outside → band edge


def test_route_start_stays_on_your_corridor_near_junction():
    # walking north on the east side of the wide corridor, just below the junction:
    # the branch's centre line (y=10) is nearer than the wide corridor's (x=0)
    fixes = [(3.0, 6.0, 0), (3.2, 8.6, 2), (2.8, 9.4, 358), (3.4, 9.0, 0)]
    for pos, centre in _walk(SEGS_T, fixes):
        assert centre[0] == 0.0, (pos, centre)  # route starts on the corridor you're in


def test_parallel_aisles_no_flip_flop():
    # walking the x=0 aisle; fixes wander up to 1.9 m toward the other aisle
    fixes = [(0.2, 2, 0), (1.9, 4, 2), (0.6, 6, 358), (1.8, 8, 1), (0.9, 10, 0)]
    for _, centre in _walk(SEGS_PARALLEL, fixes):
        assert centre[0] == 0.0


def test_switches_when_clearly_in_other_corridor():
    fixes = [(0.5, 8.0, 0), (2.0, 9.8, 60), (6.0, 10.3, 90), (9.0, 10.2, 90)]
    pos, centre = _walk(SEGS_T, fixes)[-1]
    assert centre == (9.0, 10.0) and pos == (9.0, 10.2)


def test_split_keeps_width():
    pieces = split_at_junctions(SEGS_T)
    assert len(pieces) == 3
    assert sorted(p.get("width") for p in pieces) == [3.0, 8.0, 8.0]


def test_asymmetric_band_each_side_independent():
    # line runs north (x=0); 6 m to the LEFT (west, x<0), only 1 m to the RIGHT (east)
    segs = [{"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 20.0, "width_left": 6.0, "width_right": 1.0}]
    sx, _, _ = match_to_walkable(-5.0, 5.0, segs)
    assert math.isclose(sx, -5.0)  # 5 m west: inside the wide side → kept
    sx, _, _ = match_to_walkable(2.5, 5.0, segs)
    assert math.isclose(sx, 1.0)  # 2.5 m east: past the narrow side → its edge at 1 m
    sx, _, _ = match_to_walkable(-8.0, 5.0, segs)
    assert math.isclose(sx, -6.0)  # past the wide side → its edge at 6 m


def test_side_overrides_symmetric_width_and_survives_split():
    segs = [
        {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 20.0, "width": 8.0, "width_right": 0.5},
        {"x1": 0.0, "y1": 10.0, "x2": 15.0, "y2": 10.0},
    ]
    sx, _, _ = match_to_walkable(3.0, 4.0, segs, heading_deg=0.0)
    assert math.isclose(sx, 0.5)  # right side overridden to 0.5 m
    sx, _, _ = match_to_walkable(-3.5, 4.0, segs, heading_deg=0.0)
    assert math.isclose(sx, -3.5)  # left still width/2 = 4 m
    pieces = [p for p in split_at_junctions(segs) if p["x1"] == p["x2"] == 0.0]
    assert len(pieces) == 2 and all(p.get("width_right") == 0.5 for p in pieces)
