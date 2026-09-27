"""Shortest-path route calculation from current position to a destination
(typically the user's vehicle slot).

Implements project brief Section 10: current position + destination slot +
indoor walkable map -> ordered list of waypoints. Dijkstra over the walkable
graph (navigation/graph.py) -- a single parking floor's graph is small enough
that A*'s heuristic speedup isn't needed; Dijkstra is the simpler correct
choice here and can be swapped later if graphs grow much larger.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from fmc.navigation.graph import Graph, NodeId, attach_point, build_graph


@dataclass
class Route:
    waypoints: list[tuple[float, float]]
    total_distance: float


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


def calculate_route(
    walkable_segments: list[dict],
    start_x: float,
    start_y: float,
    dest_x: float,
    dest_y: float,
) -> Route | None:
    """Route from an arbitrary current position to an arbitrary destination
    (e.g. a vehicle slot), both snapped onto the walkable graph. Returns None
    if the destination is unreachable (disconnected walkable graph)."""
    graph = build_graph(walkable_segments)
    start_node, start_seg_idx = attach_point(graph, start_x, start_y, walkable_segments)
    goal_node, goal_seg_idx = attach_point(graph, dest_x, dest_y, walkable_segments)

    if start_seg_idx == goal_seg_idx and start_node != goal_node:
        # Both points landed on the same original segment. attach_point()
        # only wires each new point to its segment's two far endpoints, with
        # no idea another point was already attached to that same segment --
        # without this direct edge, Dijkstra would be forced to route out to
        # a distant junction and back even though the straight-line hop
        # between them is right there (this was a real bug: a ~2.6m walk
        # along one corridor came out as a 24m route via a junction and back).
        direct_dist = math.hypot(start_node[0] - goal_node[0], start_node[1] - goal_node[1])
        graph.add_edge(start_node, goal_node, direct_dist)

    route = _dijkstra(graph, start_node, goal_node)
    if route is None:
        return None

    # Vehicle slots typically sit a short distance off the corridor itself
    # (e.g. inside the bay) -- append that final hop so the last waypoint is
    # the actual slot, not just where it snapped onto the walkable graph.
    last = route.waypoints[-1]
    if (dest_x, dest_y) != last:
        route.waypoints.append((dest_x, dest_y))
        route.total_distance += math.hypot(dest_x - last[0], dest_y - last[1])

    return route
