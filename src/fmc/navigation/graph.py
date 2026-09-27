"""Build a routing graph from the site's walkable path segments.

Nodes are unique segment endpoints; edges connect the two endpoints of
each segment, weighted by Euclidean distance. Endpoints within
NODE_SNAP_TOLERANCE_M of an existing node are merged into it (see
_NodeRegistry) rather than becoming a new, separate node -- manually
clicking segment endpoints on a floor-plan image is never pixel-perfect,
and even a few pixels of imprecision can translate to a real-world gap of
several centimeters depending on the site's scale, which would otherwise
silently disconnect two segments meant to share a corner. Arbitrary
start/destination points (which won't sit exactly on a graph node -- e.g.
a live position fix or a vehicle slot) are attached via attach_point(),
which snaps the point onto its nearest segment and splices it in as a new
node connected to that segment's endpoints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

NodeId = tuple[float, float]

# Endpoints closer together than this are treated as the same junction.
# Chosen to comfortably absorb manual floor-plan-clicking imprecision
# (typically a few cm to ~10cm) without merging genuinely distinct nearby
# points (e.g. adjacent parking-row corridors) -- revisit per-site if a
# facility has walkable features closer together than this.
NODE_SNAP_TOLERANCE_M = 0.3


def _round_node(x: float, y: float, precision: int = 3) -> NodeId:
    return (round(x, precision), round(y, precision))


class _NodeRegistry:
    """Resolves an (x, y) coordinate to a stable node id, snapping to an
    already-registered node within NODE_SNAP_TOLERANCE_M instead of always
    creating a new one. Shared by a Graph's build_graph() call and any
    later attach_point() calls against it, so segment endpoints and
    attachment points agree on node identity."""

    def __init__(self, tolerance: float = NODE_SNAP_TOLERANCE_M):
        self.tolerance = tolerance
        self._nodes: list[NodeId] = []

    def resolve(self, x: float, y: float) -> NodeId:
        for node in self._nodes:
            if math.hypot(node[0] - x, node[1] - y) <= self.tolerance:
                return node
        new_node = _round_node(x, y)
        self._nodes.append(new_node)
        return new_node


@dataclass
class Graph:
    edges: dict[NodeId, dict[NodeId, float]] = field(default_factory=dict)
    _registry: _NodeRegistry = field(default_factory=_NodeRegistry)

    def add_edge(self, a: NodeId, b: NodeId, weight: float) -> None:
        self.edges.setdefault(a, {})[b] = weight
        self.edges.setdefault(b, {})[a] = weight

    def resolve_node(self, x: float, y: float) -> NodeId:
        """Resolve (x, y) to a node id, snapping to a nearby existing node
        within NODE_SNAP_TOLERANCE_M if one exists."""
        return self._registry.resolve(x, y)


def build_graph(segments: list[dict]) -> Graph:
    graph = Graph()
    for seg in segments:
        a = graph.resolve_node(seg["x1"], seg["y1"])
        b = graph.resolve_node(seg["x2"], seg["y2"])
        weight = math.hypot(a[0] - b[0], a[1] - b[1])
        graph.add_edge(a, b, weight)
    return graph


def attach_point(graph: Graph, x: float, y: float, segments: list[dict]) -> tuple[NodeId, int]:
    """Snap (x, y) onto the nearest walkable segment and splice it into
    `graph` (mutated in place) as a new node connected to that segment's two
    endpoints. Returns (new_node_id, index_of_the_matched_segment_in
    `segments`) -- the segment index lets a caller (see
    routing.calculate_route) detect when two separately-attached points
    (e.g. a start position and a destination) landed on the SAME segment,
    since this function only wires a new point to its segment's two far
    endpoints and has no way to know about other points attached to that
    same segment in earlier calls. Without that check, two points a few
    meters apart on one long corridor would have no direct edge between
    them at all, forcing a route out to a distant junction and back even
    though the direct straight-line hop is right there.
    """
    from fmc.fusion.map_matching import Segment, _nearest_point_on_segment

    if not segments:
        raise ValueError("No walkable segments to attach to")

    best = None
    for i, seg_dict in enumerate(segments):
        seg = Segment(**seg_dict)
        nx, ny, dist = _nearest_point_on_segment(x, y, seg)
        if best is None or dist < best[1]:
            best = ((nx, ny), dist, seg, i)

    (snap_x, snap_y), _, seg, seg_index = best
    node = _round_node(snap_x, snap_y)
    a = graph.resolve_node(seg.x1, seg.y1)
    b = graph.resolve_node(seg.x2, seg.y2)

    graph.add_edge(node, a, math.hypot(node[0] - a[0], node[1] - a[1]))
    graph.add_edge(node, b, math.hypot(node[0] - b[0], node[1] - b[1]))
    return node, seg_index
