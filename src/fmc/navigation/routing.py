"""Shortest-path route calculation from current position to a destination.

Single-floor: Dijkstra over walkable segments (navigation/graph.py).
Multi-floor: layered Dijkstra with vertical_connectors (elevators/stairs).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from fmc.navigation.graph import (
    Graph,
    LayeredGraph,
    LayeredNodeId,
    NodeId,
    attach_layered_point,
    attach_point,
    build_graph,
    build_layered_graph,
)


@dataclass
class RouteLeg:
    """One walking leg on a floor, or a vertical transition."""
    floor: Optional[str] = None
    instruction: str = ""
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    floor_transition: Optional[dict[str, Any]] = None
    distance: float = 0.0


@dataclass
class Route:
    waypoints: list[tuple[float, float]]
    total_distance: float
    legs: list[RouteLeg] = field(default_factory=list)
    start_floor: str = "1"
    dest_floor: str = "1"


def _dijkstra(graph: Graph, start: NodeId, goal: NodeId) -> Route | None:
    distances: dict[NodeId, float] = {start: 0.0}
    previous: dict[NodeId, NodeId] = {}
    visited: set[NodeId] = set()
    queue: list[tuple[float, NodeId]] = [(0.0, start)]

    while queue:
        dist, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == goal:
            break
        for neighbor, weight in graph.edges.get(node, {}).items():
            new_dist = dist + weight
            if new_dist < distances.get(neighbor, float("inf")):
                distances[neighbor] = new_dist
                previous[neighbor] = node
                heapq.heappush(queue, (new_dist, neighbor))

    if goal not in distances:
        return None

    path = [goal]
    while path[-1] != start:
        path.append(previous[path[-1]])
    path.reverse()

    return Route(waypoints=list(path), total_distance=distances[goal])


def _dijkstra_layered(
    graph: LayeredGraph, start: LayeredNodeId, goal: LayeredNodeId
) -> tuple[list[LayeredNodeId], float] | None:
    distances: dict[LayeredNodeId, float] = {start: 0.0}
    previous: dict[LayeredNodeId, LayeredNodeId] = {}
    visited: set[LayeredNodeId] = set()
    queue: list[tuple[float, LayeredNodeId]] = [(0.0, start)]

    while queue:
        dist, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == goal:
            break
        for neighbor, weight in graph.edges.get(node, {}).items():
            new_dist = dist + weight
            if new_dist < distances.get(neighbor, float("inf")):
                distances[neighbor] = new_dist
                previous[neighbor] = node
                heapq.heappush(queue, (new_dist, neighbor))

    if goal not in distances:
        return None

    path = [goal]
    while path[-1] != start:
        path.append(previous[path[-1]])
    path.reverse()
    return path, distances[goal]


def _path_length(waypoints: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(waypoints)):
        a, b = waypoints[i - 1], waypoints[i]
        total += math.hypot(a[0] - b[0], a[1] - b[1])
    return total


def _legs_from_layered_path(
    graph: LayeredGraph,
    path: list[LayeredNodeId],
    dest_x: float,
    dest_y: float,
    dest_slot_id: str | None = None,
) -> list[RouteLeg]:
    if not path:
        return []

    legs: list[RouteLeg] = []
    walk_pts: list[tuple[float, float]] = [(path[0][1], path[0][2])]
    walk_floor = path[0][0]

    def flush_walk(instruction: str | None = None) -> None:
        nonlocal walk_pts, walk_floor
        if len(walk_pts) < 1:
            return
        # dedupe consecutive
        cleaned = [walk_pts[0]]
        for p in walk_pts[1:]:
            if math.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) >= 0.05:
                cleaned.append(p)
        if not cleaned:
            return
        dist = _path_length(cleaned)
        instr = instruction or f"Walk on floor {walk_floor}"
        legs.append(RouteLeg(
            floor=walk_floor,
            instruction=instr,
            waypoints=cleaned,
            distance=dist,
        ))
        walk_pts = []

    for i in range(1, len(path)):
        prev, cur = path[i - 1], path[i]
        meta = graph.edge_meta.get((prev, cur)) or graph.edge_meta.get((cur, prev))
        if meta and meta.get("kind") == "vertical":
            flush_walk(f"Walk to {meta.get('connector_id', 'connector')}")
            from fmc.floors import normalize_floor_id
            from_f = normalize_floor_id(meta.get("from_floor", prev[0]))
            to_f = normalize_floor_id(meta.get("to_floor", cur[0]))
            # orient transition in travel direction
            if prev[0] == to_f and cur[0] == from_f:
                from_f, to_f = to_f, from_f
            elif prev[0] != from_f:
                from_f, to_f = normalize_floor_id(prev[0]), normalize_floor_id(cur[0])
            ctype = str(meta.get("type", "stairs"))
            cid = str(meta.get("connector_id", "connector"))
            label = "Elevator" if ctype == "elevator" else ("Ramp" if ctype == "ramp" else "Stairs")
            legs.append(RouteLeg(
                floor=None,
                instruction=f"Take {label} ({cid}) to Floor {to_f}",
                waypoints=[],
                floor_transition={
                    "from_floor": from_f,
                    "to_floor": to_f,
                    "type": ctype,
                    "connector_id": cid,
                    "instruction": f"Take {label} ({cid}) to Floor {to_f}",
                },
                distance=float(graph.edges.get(prev, {}).get(cur, 0.0)),
            ))
            walk_floor = cur[0]
            walk_pts = [(cur[1], cur[2])]
        else:
            if cur[0] != walk_floor:
                flush_walk()
                walk_floor = cur[0]
                walk_pts = [(prev[1], prev[2]), (cur[1], cur[2])]
            else:
                walk_pts.append((cur[1], cur[2]))

    dest_label = dest_slot_id or "destination"
    if walk_pts:
        last = walk_pts[-1]
        if (dest_x, dest_y) != last:
            walk_pts.append((dest_x, dest_y))
        flush_walk(f"Follow path to {dest_label}")
    return legs


def calculate_route(
    walkable_segments: list[dict],
    start_x: float,
    start_y: float,
    dest_x: float,
    dest_y: float,
) -> Route | None:
    """Route on a single floor's walkable graph (backward compatible)."""
    graph = build_graph(walkable_segments)
    start_node, start_seg_idx = attach_point(graph, start_x, start_y, walkable_segments)
    goal_node, goal_seg_idx = attach_point(graph, dest_x, dest_y, walkable_segments)

    if start_seg_idx == goal_seg_idx and start_node != goal_node:
        direct_dist = math.hypot(start_node[0] - goal_node[0], start_node[1] - goal_node[1])
        graph.add_edge(start_node, goal_node, direct_dist)

    route = _dijkstra(graph, start_node, goal_node)
    if route is None:
        return None

    last = route.waypoints[-1]
    if (dest_x, dest_y) != last:
        route.waypoints.append((dest_x, dest_y))
        route.total_distance += math.hypot(dest_x - last[0], dest_y - last[1])

    from fmc.floors import normalize_floor_id
    route.legs = [RouteLeg(
        floor="1",
        instruction="Follow path to destination",
        waypoints=list(route.waypoints),
        distance=route.total_distance,
    )]
    route.start_floor = "1"
    route.dest_floor = "1"
    return route


def calculate_multifloor_route(
    floors_segments: dict,
    vertical_connectors: list[dict],
    start_floor,
    start_x: float,
    start_y: float,
    dest_floor,
    dest_x: float,
    dest_y: float,
    dest_slot_id: str | None = None,
) -> Route | None:
    """Route across one or more floors using vertical connectors."""
    from fmc.floors import normalize_floor_id

    start_floor = normalize_floor_id(start_floor)
    dest_floor = normalize_floor_id(dest_floor)
    # normalize segment map keys
    segs_by_floor = {
        normalize_floor_id(k): v for k, v in (floors_segments or {}).items()
    }

    if start_floor == dest_floor:
        segs = segs_by_floor.get(start_floor) or []
        if not segs:
            return None
        route = calculate_route(segs, start_x, start_y, dest_x, dest_y)
        if route is None:
            return None
        route.start_floor = start_floor
        route.dest_floor = dest_floor
        if route.legs:
            route.legs[0].floor = start_floor
            if dest_slot_id:
                route.legs[0].instruction = f"Follow path to {dest_slot_id}"
        return route

    graph = build_layered_graph(segs_by_floor, vertical_connectors)
    start_segs = segs_by_floor.get(start_floor) or []
    dest_segs = segs_by_floor.get(dest_floor) or []
    if not start_segs or not dest_segs:
        return None

    start_node, start_seg_idx = attach_layered_point(
        graph, start_floor, start_x, start_y, start_segs
    )
    goal_node, goal_seg_idx = attach_layered_point(
        graph, dest_floor, dest_x, dest_y, dest_segs
    )

    if (
        start_floor == dest_floor
        and start_seg_idx == goal_seg_idx
        and start_node != goal_node
    ):
        direct = math.hypot(start_node[1] - goal_node[1], start_node[2] - goal_node[2])
        graph.add_edge(start_node, goal_node, direct)

    found = _dijkstra_layered(graph, start_node, goal_node)
    if found is None:
        return None
    path, total = found

    legs = _legs_from_layered_path(graph, path, dest_x, dest_y, dest_slot_id)
    flat: list[tuple[float, float]] = []
    for leg in legs:
        if leg.floor_transition:
            continue
        for wp in leg.waypoints:
            if not flat or math.hypot(wp[0] - flat[-1][0], wp[1] - flat[-1][1]) >= 0.05:
                flat.append(wp)

    return Route(
        waypoints=flat,
        total_distance=total,
        legs=legs,
        start_floor=start_floor,
        dest_floor=dest_floor,
    )


# ---------------------------------------------------------------- join the walkway
# Walkways are corridor CENTRE lines and corridors can be ~8 m wide, so the user is often
# metres to the side of where the route starts. Instead of starting the route on the line
# beside them, start it at their position and merge onto the line a little way ahead —
# the 2D map, camera chevrons and AR then all show one path from where the user stands.
JOIN_MIN_OFF_M = 0.75  # closer than this to the line: no join leg
JOIN_MIN_M = 3.0  # merge point at least this far along the route…
JOIN_MAX_M = 8.0  # …and at most this far (2× the sideways offset in between)


def join_walkway(waypoints: list, x: float, y: float) -> tuple[list, float]:
    """[(x, y)] + merge into `waypoints` (whose first point is the line point beside the
    user). Never merges past the first corner. Returns (new waypoints, added length)."""
    wps = [(float(p[0]), float(p[1])) for p in (waypoints or [])]
    if len(wps) < 2:
        return wps, 0.0
    (ax, ay), (bx, by) = wps[0], wps[1]
    off = math.hypot(x - ax, y - ay)
    if off < JOIN_MIN_OFF_M:
        return wps, 0.0
    want = max(JOIN_MIN_M, min(JOIN_MAX_M, 2.0 * off))
    seg = math.hypot(bx - ax, by - ay)
    if seg <= want:
        merge, rest = (bx, by), wps[2:]  # short first stretch: join at the first corner
        skipped = seg
    else:
        k = want / seg
        merge, rest = (ax + (bx - ax) * k, ay + (by - ay) * k), wps[1:]
        skipped = want
    out = [(x, y), merge, *rest]
    added = math.hypot(merge[0] - x, merge[1] - y) - skipped
    return out, added


LANE_STRAIGHT_DEG = 20.0  # turn smaller than this at a vertex = straight-through junction
LANE_ON_LINE_M = 0.3  # lane end this close to segment 2's line → connect directly (no L step)


def _unit(ax: float, ay: float, bx: float, by: float) -> tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    return (dx / n, dy / n, n) if n > 1e-9 else (0.0, 0.0, 0.0)


def lane_walkway(waypoints: list, x: float, y: float, ends_at_bay: bool = False) -> tuple[list, float, str]:
    """Route from the user's own lane, no diagonals.

    waypoints[0] is the centre-line point beside the user. Segment 1 runs from the user
    PARALLEL to the corridor, at their sideways offset, through any straight-through
    junctions, up to the first corner. There it joins segment 2:
      * "direct" — the lane end already lies on segment 2's line (always at a 90° corner):
        just turn and carry on along segment 2;
      * "L" — it doesn't (angled corner): one perpendicular step onto segment 2, then on.
    If the leg ends while still in the lane (e.g. a lift door straight ahead), a final
    sideways step reaches it. The spur into a bay counts as a corner like any other.
    Returns (new waypoints, added length, mode) with mode "none" | "direct" | "L" | "end".
    """
    w = [(float(p[0]), float(p[1])) for p in (waypoints or [])]
    if len(w) < 2:
        return w, 0.0, "none"
    ox, oy = x - w[0][0], y - w[0][1]  # sideways offset from the centre line
    if math.hypot(ox, oy) < JOIN_MIN_OFF_M:
        return w, 0.0, "none"
    old_len = _path_length(w)
    out = [(x, y)]
    mode = "end"
    k = 1
    while k < len(w):
        cx, cy = w[k]
        ex, ey = cx + ox, cy + oy  # lane point level with vertex k
        if k == len(w) - 1:
            # leg ends while still in the lane: step sideways onto its end point
            out.append((ex, ey))
            out.append((cx, cy))
            break
        u1 = _unit(*w[k - 1], cx, cy)
        u2 = _unit(cx, cy, *w[k + 1])
        cos_t = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        if math.degrees(math.acos(cos_t)) < LANE_STRAIGHT_DEG:
            k += 1  # straight-through junction: stay in the lane
            continue
        # corner: lane ends level with it, then join segment 2
        out.append((ex, ey))
        sx, sy, slen = u2
        t = (ex - cx) * sx + (ey - cy) * sy  # how far along segment 2 the lane end sits
        h = abs((ex - cx) * sy - (ey - cy) * sx)  # how far off segment 2's line
        if h < LANE_ON_LINE_M:
            mode = "direct"
            if t >= slen - 0.05:
                out.extend(w[k + 2:])  # lane end already past segment 2's end
            else:
                out.extend(w[k + 1:])
        else:
            mode = "L"
            tc = max(0.0, min(slen, t))
            out.append((cx + sx * tc, cy + sy * tc))  # perpendicular step onto segment 2
            out.extend(w[k + 1:])
        break
    # drop near-duplicate consecutive points
    clean = [out[0]]
    for q in out[1:]:
        if math.hypot(q[0] - clean[-1][0], q[1] - clean[-1][1]) >= 0.05:
            clean.append(q)
    return clean, _path_length(clean) - old_len, mode


def join_route_to_position(route: "Route", start_floor, x: float, y: float) -> tuple[float, str]:
    """Start the route's first walk leg (on start_floor) at the user's position, following
    their own lane (lane_walkway). Returns (sideways offset, mode)."""
    from fmc.floors import normalize_floor_id

    if not route or not route.legs:
        return 0.0, "none"
    leg = route.legs[0]
    if leg.floor_transition or not leg.waypoints:
        return 0.0, "none"
    if leg.floor is not None and normalize_floor_id(leg.floor) != normalize_floor_id(start_floor):
        return 0.0, "none"
    old = [(float(p[0]), float(p[1])) for p in leg.waypoints]
    ends_at_bay = len(route.legs) == 1  # otherwise leg 0 ends at a lift/stairs door
    new_wps, added, mode = lane_walkway(old, x, y, ends_at_bay)
    if mode == "none":
        return 0.0, "none"
    leg.waypoints = new_wps
    leg.distance = float(leg.distance) + added
    route.total_distance = float(route.total_distance) + added
    flat: list[tuple[float, float]] = []
    for lg in route.legs:
        if lg.floor_transition:
            continue
        for wp in lg.waypoints:
            if not flat or math.hypot(wp[0] - flat[-1][0], wp[1] - flat[-1][1]) >= 0.05:
                flat.append((float(wp[0]), float(wp[1])))
    route.waypoints = flat
    return math.hypot(x - old[0][0], y - old[0][1]), mode
