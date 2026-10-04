"""Route calculation over a small synthetic walkable graph (not the mock
site's — a hand-built graph here so the test doesn't depend on data files)."""
from __future__ import annotations

import pytest

from fmc.navigation.routing import calculate_route

# A simple T-shaped corridor:
#   (0,0) --- (10,0) --- (20,0)
#                |
#              (10,10)
SEGMENTS = [
    {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0},
    {"x1": 10.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
    {"x1": 10.0, "y1": 0.0, "x2": 10.0, "y2": 10.0},
]


def test_route_along_straight_corridor():
    route = calculate_route(SEGMENTS, start_x=0.0, start_y=0.0, dest_x=20.0, dest_y=0.0)
    assert route is not None
    assert route.waypoints[0] == (0.0, 0.0)
    assert route.waypoints[-1] == (20.0, 0.0)
    assert abs(route.total_distance - 20.0) < 0.01


def test_route_through_junction():
    route = calculate_route(SEGMENTS, start_x=0.0, start_y=0.0, dest_x=10.0, dest_y=10.0)
    assert route is not None
    assert route.waypoints[-1] == (10.0, 10.0)
    # 10 along the corridor + 10 up the branch
    assert abs(route.total_distance - 20.0) < 0.01


def test_destination_off_the_graph_gets_final_hop_appended():
    # Slot sits 2m off the corridor near x=15 -- should snap to (15, 0) then
    # add a final short hop to the actual slot coordinates.
    route = calculate_route(SEGMENTS, start_x=0.0, start_y=0.0, dest_x=15.0, dest_y=2.0)
    assert route is not None
    assert route.waypoints[-1] == (15.0, 2.0)


def test_nearly_touching_endpoints_still_connect():
    # Two segments meant to share a corner at (10, 0), but clicked with
    # realistic human imprecision -- 11cm apart, same magnitude as an
    # actual real-site case that silently failed to route before node
    # snapping was added (see fmc/navigation/graph.py NODE_SNAP_TOLERANCE_M).
    segments = [
        {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0},
        {"x1": 10.08, "y1": 0.08, "x2": 20.0, "y2": 0.0},
    ]
    route = calculate_route(segments, start_x=0.0, start_y=0.0, dest_x=20.0, dest_y=0.0)
    assert route is not None
    assert route.waypoints[-1] == (20.0, 0.0)


def test_endpoints_farther_than_tolerance_do_not_connect():
    # A 1m gap is well outside NODE_SNAP_TOLERANCE_M (0.3m) -- these two
    # segments should NOT be treated as connected, confirming the snap
    # tolerance doesn't just merge everything.
    segments = [
        {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0},
        {"x1": 11.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
    ]
    route = calculate_route(segments, start_x=0.0, start_y=0.0, dest_x=20.0, dest_y=0.0)
    assert route is None


def test_start_and_goal_on_same_segment_route_directly():
    # Real bug found on site_00: start and goal both land on the SAME long
    # segment, a few meters apart and far from either endpoint. Before the
    # fix, attach_point() only wired each new point to its segment's two far
    # endpoints -- with no edge between the two attached points themselves,
    # Dijkstra was forced to route out to the nearer endpoint and back,
    # turning a ~2.6m direct walk into a ~18m detour (24.3m once the final
    # off-graph hop to an actual slot was added, exactly matching the
    # reported bug: 4 waypoints instead of 3, route going "forward and
    # coming back").
    segments = [{"x1": -20.0, "y1": 0.0, "x2": 20.0, "y2": 0.0}]
    route = calculate_route(segments, start_x=-4.408, start_y=0.0, dest_x=-1.809, dest_y=0.0)
    assert route is not None
    assert route.waypoints == [(-4.408, 0.0), (-1.809, 0.0)]
    assert route.total_distance == pytest.approx(2.599, abs=1e-3)


def test_same_segment_direct_edge_still_beats_going_via_a_junction():
    # Guard against a regression where the direct same-segment edge is added
    # but a shorter path via a junction (e.g. a genuine shortcut through a
    # perpendicular corridor) should still win if it's actually shorter.
    # Here the junction detour is deliberately much longer, so direct must win.
    segments = [
        {"x1": -20.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
        {"x1": 20.0, "y1": 0.0, "x2": 20.0, "y2": 100.0},
    ]
    route = calculate_route(segments, start_x=-4.408, start_y=0.0, dest_x=-1.809, dest_y=0.0)
    assert route is not None
    assert route.total_distance == pytest.approx(2.599, abs=1e-3)


def test_multifloor_route_via_elevator():
    from fmc.navigation.routing import calculate_multifloor_route

    floors = {
        "1": [
            {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0},
            {"x1": 10.0, "y1": 0.0, "x2": 10.0, "y2": 10.0},
        ],
        "B1": [
            {"x1": 10.0, "y1": 0.0, "x2": 20.0, "y2": 0.0},
        ],
    }
    connectors = [{
        "connector_id": "elevator_a",
        "type": "elevator",
        "penalty_cost": 15.0,
        "nodes": {
            "1": {"x": 10.0, "y": 0.0},
            "B1": {"x": 10.0, "y": 0.0},
        },
    }]
    route = calculate_multifloor_route(
        floors_segments=floors,
        vertical_connectors=connectors,
        start_floor="1",
        start_x=0.0,
        start_y=0.0,
        dest_floor="B1",
        dest_x=20.0,
        dest_y=0.0,
        dest_slot_id="P2-1",
    )
    assert route is not None
    assert route.start_floor == "1"
    assert route.dest_floor == "B1"
    assert any(leg.floor_transition for leg in route.legs)
    assert route.total_distance == pytest.approx(10.0 + 15.0 + 10.0, abs=0.05)
    # walking legs exist on both floors
    walk_floors = {leg.floor for leg in route.legs if leg.floor is not None}
    assert "1" in walk_floors and "B1" in walk_floors


def test_multifloor_connector_mid_segment_splices():
    # landing not on a declared endpoint must still join the corridor
    from fmc.navigation.routing import calculate_multifloor_route

    floors = {
        "G": [{"x1": 0.0, "y1": 0.0, "x2": 20.0, "y2": 0.0}],
        "B1": [{"x1": 10.0, "y1": -5.0, "x2": 10.0, "y2": 15.0}],
    }
    connectors = [{
        "connector_id": "stairs_mid",
        "type": "stairs",
        "penalty_cost": 8.0,
        "nodes": {
            "G": {"x": 10.0, "y": 0.0},
            "B1": {"x": 10.0, "y": 5.0},
        },
    }]
    route = calculate_multifloor_route(
        floors_segments=floors,
        vertical_connectors=connectors,
        start_floor="G",
        start_x=0.0,
        start_y=0.0,
        dest_floor="B1",
        dest_x=10.0,
        dest_y=15.0,
        dest_slot_id="S1",
    )
    assert route is not None
    assert any(leg.floor_transition for leg in route.legs)
    # vertical weight = penalty + planar offset between landings (5 m here)
    assert route.total_distance == pytest.approx(10.0 + 8.0 + 5.0 + 10.0, abs=0.1)
