"""Build a routing graph from the site's walkable path segments.

Nodes are unique segment endpoints; edges connect the two endpoints of
each segment, weighted by Euclidean distance. Endpoints within
NODE_SNAP_TOLERANCE_M of an existing node are merged into it.

Multi-floor: LayeredNodeId = (floor, x, y). Intra-floor edges come from
each floor's walkable_segments; inter-floor edges come from
vertical_connectors (elevators / stairs / ramps).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Union

# legacy 2D node (single-floor graphs)
NodeId = tuple[float, float]
# layered node used for multi-floor Dijkstra
LayeredNodeId = tuple[str, float, float]
AnyNodeId = Union[NodeId, LayeredNodeId]

# Endpoints closer together than this are treated as the same junction.
NODE_SNAP_TOLERANCE_M = 0.3


def _round_xy(x: float, y: float, precision: int = 3) -> tuple[float, float]:
    return (round(x, precision), round(y, precision))


def _round_node(x: float, y: float, precision: int = 3) -> NodeId:
    return _round_xy(x, y, precision)


def _round_layered(floor, x: float, y: float, precision: int = 3) -> LayeredNodeId:
    from fmc.floors import normalize_floor_id
    rx, ry = _round_xy(x, y, precision)
    return (normalize_floor_id(floor), rx, ry)


class _NodeRegistry:
    """Resolves an (x, y) coordinate to a stable node id, snapping to an
    already-registered node within NODE_SNAP_TOLERANCE_M."""

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


class _LayeredNodeRegistry:
    """Per-floor snap registry for (floor, x, y) nodes."""

    def __init__(self, tolerance: float = NODE_SNAP_TOLERANCE_M):
        self.tolerance = tolerance
        self._by_floor: dict[str, list[LayeredNodeId]] = {}

    def resolve(self, floor, x: float, y: float) -> LayeredNodeId:
        from fmc.floors import normalize_floor_id
        floor = normalize_floor_id(floor)
        bucket = self._by_floor.setdefault(floor, [])
        for node in bucket:
            if math.hypot(node[1] - x, node[2] - y) <= self.tolerance:
                return node
        new_node = _round_layered(floor, x, y)
        bucket.append(new_node)
        return new_node


@dataclass
class Graph:
    edges: dict[AnyNodeId, dict[AnyNodeId, float]] = field(default_factory=dict)
    _registry: _NodeRegistry = field(default_factory=_NodeRegistry)

    def add_edge(self, a: AnyNodeId, b: AnyNodeId, weight: float) -> None:
        self.edges.setdefault(a, {})[b] = weight
        self.edges.setdefault(b, {})[a] = weight

    def resolve_node(self, x: float, y: float) -> NodeId:
        return self._registry.resolve(x, y)


@dataclass
class LayeredGraph:
    edges: dict[LayeredNodeId, dict[LayeredNodeId, float]] = field(default_factory=dict)
    edge_meta: dict[tuple[LayeredNodeId, LayeredNodeId], dict] = field(default_factory=dict)
    _registry: _LayeredNodeRegistry = field(default_factory=_LayeredNodeRegistry)

    def add_edge(
        self,
        a: LayeredNodeId,
        b: LayeredNodeId,
        weight: float,
        meta: dict | None = None,
    ) -> None:
        self.edges.setdefault(a, {})[b] = weight
        self.edges.setdefault(b, {})[a] = weight
        if meta:
            self.edge_meta[(a, b)] = meta
            self.edge_meta[(b, a)] = meta

    def resolve_node(self, floor, x: float, y: float) -> LayeredNodeId:
        return self._registry.resolve(floor, x, y)


def split_at_junctions(segments: list[dict], tolerance: float = NODE_SNAP_TOLERANCE_M) -> list[dict]:
    """Split segments wherever another segment meets them mid-span.

    The graph only joins segments at shared endpoints, so a corridor drawn to end
    against the middle of another (T-junction) or crossing it (X-junction) would be
    an island. Cut the through-segment there so both share a node.
    """
    segs = [
        (float(s["x1"]), float(s["y1"]), float(s["x2"]), float(s["y2"]))
        for s in (segments or [])
    ]
    cuts: list[list[float]] = [[] for _ in segs]

    def _interior_t(seg, px, py):
        x1, y1, x2, y2 = seg
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        if length2 < 1e-12:
            return None
        t = ((px - x1) * dx + (py - y1) * dy) / length2
        length = math.sqrt(length2)
        # strictly inside (not within tolerance of either end) and close to the line
        if t * length <= tolerance or (1 - t) * length <= tolerance:
            return None
        if math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)) > tolerance:
            return None
        return t

    for i, si in enumerate(segs):
        for j, sj in enumerate(segs):
            if i == j:
                continue
            # T-junction: an endpoint of j lies on the interior of i
            for px, py in ((sj[0], sj[1]), (sj[2], sj[3])):
                t = _interior_t(si, px, py)
                if t is not None:
                    cuts[i].append(t)
            # X-junction: proper crossing of the two interiors
            if j > i:
                x1, y1, x2, y2 = si
                x3, y3, x4, y4 = sj
                den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
                if abs(den) < 1e-12:
                    continue
                ti = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
                tj = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
                if 0.0 < ti < 1.0 and 0.0 < tj < 1.0:
                    px, py = x1 + ti * (x2 - x1), y1 + ti * (y2 - y1)
                    for k, seg in ((i, si), (j, sj)):
                        t = _interior_t(seg, px, py)
                        if t is not None:
                            cuts[k].append(t)

    out: list[dict] = []
    for src, (x1, y1, x2, y2), ts in zip(segments or [], segs, cuts):
        # carry per-segment attributes (e.g. corridor width) onto the pieces
        extra = {k: v for k, v in src.items() if k not in ("x1", "y1", "x2", "y2")}
        pts = [0.0] + sorted(set(round(t, 6) for t in ts)) + [1.0]
        for a, b in zip(pts[:-1], pts[1:]):
            out.append({
                **extra,
                "x1": x1 + a * (x2 - x1), "y1": y1 + a * (y2 - y1),
                "x2": x1 + b * (x2 - x1), "y2": y1 + b * (y2 - y1),
            })
    return out


def build_graph(segments: list[dict]) -> Graph:
    graph = Graph()
    for seg in split_at_junctions(segments):
        a = graph.resolve_node(seg["x1"], seg["y1"])
        b = graph.resolve_node(seg["x2"], seg["y2"])
        weight = math.hypot(a[0] - b[0], a[1] - b[1])
        graph.add_edge(a, b, weight)
    return graph


def _splice_landing(
    graph: LayeredGraph,
    floor: str,
    x: float,
    y: float,
    segments: list[dict],
) -> LayeredNodeId:
    """Snap a connector landing onto that floor's walkable network."""
    from fmc.fusion.map_matching import Segment, _nearest_point_on_segment

    if not segments:
        return graph.resolve_node(floor, x, y)

    best = None
    for seg_dict in split_at_junctions(segments):
        seg = Segment(**seg_dict)
        nx, ny, dist = _nearest_point_on_segment(x, y, seg)
        if best is None or dist < best[1]:
            best = ((nx, ny), dist, seg)

    (snap_x, snap_y), _, seg = best
    node = graph.resolve_node(floor, snap_x, snap_y)
    a = graph.resolve_node(floor, seg.x1, seg.y1)
    b = graph.resolve_node(floor, seg.x2, seg.y2)
    graph.add_edge(node, a, math.hypot(node[1] - a[1], node[2] - a[2]))
    graph.add_edge(node, b, math.hypot(node[1] - b[1], node[2] - b[2]))
    return node


def build_layered_graph(
    floors_segments: dict,
    vertical_connectors: list[dict] | None = None,
) -> LayeredGraph:
    """Build a multi-floor walkable graph with vertical conduit edges."""
    from fmc.floors import normalize_floor_id

    graph = LayeredGraph()
    segs_by_floor = {
        normalize_floor_id(floor): list(segments or [])
        for floor, segments in (floors_segments or {}).items()
    }

    for fid, segments in segs_by_floor.items():
        for seg in split_at_junctions(segments):
            a = graph.resolve_node(fid, seg["x1"], seg["y1"])
            b = graph.resolve_node(fid, seg["x2"], seg["y2"])
            weight = math.hypot(a[1] - b[1], a[2] - b[2])
            graph.add_edge(a, b, weight)

    for conn in vertical_connectors or []:
        nodes = conn.get("nodes") or {}
        # preserve connector declaration order (user-defined stack)
        floors = [normalize_floor_id(f) for f in nodes.keys()]
        penalty = float(conn.get("penalty_cost", 10.0))
        ctype = str(conn.get("type", "stairs"))
        cid = str(conn.get("connector_id", "connector"))

        # resolve/splice each landing once
        landings: dict[str, LayeredNodeId] = {}
        for fid in floors:
            n = None
            for k, v in nodes.items():
                if normalize_floor_id(k) == fid:
                    n = v
                    break
            if not n:
                continue
            landings[fid] = _splice_landing(
                graph, fid, float(n["x"]), float(n["y"]), segs_by_floor.get(fid) or []
            )

        for i in range(len(floors) - 1):
            f0, f1 = floors[i], floors[i + 1]
            a = landings.get(f0)
            b = landings.get(f1)
            if not a or not b:
                continue
            planar = math.hypot(a[1] - b[1], a[2] - b[2])
            weight = penalty + planar
            meta = {
                "kind": "vertical",
                "type": ctype,
                "connector_id": cid,
                "from_floor": f0,
                "to_floor": f1,
            }
            graph.add_edge(a, b, weight, meta=meta)

    return graph


def attach_point(graph: Graph, x: float, y: float, segments: list[dict]) -> tuple[NodeId, int]:
    """Snap (x, y) onto the nearest walkable segment and splice it into graph."""
    from fmc.fusion.map_matching import Segment, _nearest_point_on_segment

    if not segments:
        raise ValueError("No walkable segments to attach to")

    best = None
    # split first so a point near a T-junction attaches to the junction, not the far ends
    for i, seg_dict in enumerate(split_at_junctions(segments)):
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


def attach_layered_point(
    graph: LayeredGraph,
    floor,
    x: float,
    y: float,
    segments: list[dict],
) -> tuple[LayeredNodeId, int]:
    """Snap a point onto a floor's walkable segments and splice into layered graph."""
    from fmc.floors import normalize_floor_id
    from fmc.fusion.map_matching import Segment, _nearest_point_on_segment

    floor = normalize_floor_id(floor)
    if not segments:
        raise ValueError(f"No walkable segments on floor {floor}")

    best = None
    # split first so a point near a T-junction attaches to the junction, not the far ends
    for i, seg_dict in enumerate(split_at_junctions(segments)):
        seg = Segment(**seg_dict)
        nx, ny, dist = _nearest_point_on_segment(x, y, seg)
        if best is None or dist < best[1]:
            best = ((nx, ny), dist, seg, i)

    (snap_x, snap_y), _, seg, seg_index = best
    node = graph.resolve_node(floor, snap_x, snap_y)
    a = graph.resolve_node(floor, seg.x1, seg.y1)
    b = graph.resolve_node(floor, seg.x2, seg.y2)

    graph.add_edge(node, a, math.hypot(node[1] - a[1], node[2] - a[2]))
    graph.add_edge(node, b, math.hypot(node[1] - b[1], node[2] - b[2]))
    return node, seg_index
