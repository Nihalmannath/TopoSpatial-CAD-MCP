"""Bridge between Raster2Seq vector polygon predictions and CAD TraceResult.

Transforms autoregressive polygon sequences (CubiCasa category space)
into structured architectural entities:
- WallsGraph (centerline network with internal/external classification)
- TraceOpening (doors and windows hosted on wall edges)
- TraceZone (room labels with seed points)
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point as ShapelyPoint, Polygon

from .models import Point, TraceEdge, TraceNode, TraceOpening, TraceResult, TraceZone, WallsGraph

logger = logging.getLogger(__name__)

CC5K_LABEL: Dict[int, str] = {
    0: "Outdoor",
    1: "Kitchen",
    2: "Living Room",
    3: "Bed Room",
    4: "Bath",
    5: "Entry",
    6: "Storage",
    7: "Garage",
    8: "Undefined",
    9: "Window",
    10: "Door",
}


def unproject_point(
    x: float,
    y: float,
    orig_w: int,
    orig_h: int,
    target_size: int = 256,
) -> Tuple[float, float]:
    """Invert Raster2Seq's ResizeAndPad transformation back to original image space."""
    scale = min(target_size / orig_h, target_size / orig_w)
    new_h = int(orig_h * scale)
    new_w = int(orig_w * scale)
    top = (target_size - new_h) // 2
    left = (target_size - new_w) // 2

    orig_x = (x - left) / scale
    orig_y = (y - top) / scale
    return max(0.0, min(float(orig_w), orig_x)), max(0.0, min(float(orig_h), orig_y))


def clean_polygon(pts: List[List[float]]) -> Optional[Polygon]:
    """Clean and repair raw predicted polygon points."""
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if isinstance(poly, MultiPolygon):
        if not poly.geoms:
            return None
        poly = max(poly.geoms, key=lambda g: g.area)
    if not isinstance(poly, Polygon) or poly.is_empty or poly.area <= 0:
        return None
    return poly


def snap_point(pt: Tuple[float, float], grid_nodes: List[Tuple[float, float]], tol: float = 15.0) -> Tuple[float, float]:
    """Snap point to nearest existing node within tolerance."""
    for node in grid_nodes:
        if math.dist(pt, node) <= tol:
            return node
    return pt


def raster2seq_to_trace(
    predictions: List[Dict[str, Any]],
    orig_size: Tuple[int, int],
    mm_per_px: float = 10.0,
    wall_thickness_mm: float = 150.0,
    snap_tol_px: float = 12.0,
    min_wall_len_mm: float = 200.0,
    origin_px: Tuple[float, float] = (0.0, 0.0),
) -> TraceResult:
    """Convert Raster2Seq prediction list into a fully valid TraceResult.

    Args:
        predictions: List of prediction dicts with 'category_id' and 'segmentation'
        orig_size: Tuple of (orig_width, orig_height)
        mm_per_px: Spatial resolution scale in millimetres per pixel
        wall_thickness_mm: Nominal wall thickness in mm
        snap_tol_px: Snapping tolerance for node merging in pixels
        min_wall_len_mm: Minimum wall length in mm to retain
        origin_px: Offset of this inference image inside the original image.
            This restores predictions made on a cropped plan to original-image
            coordinates without changing the physical scale.
    """
    orig_w, orig_h = orig_size

    room_items = [it for it in predictions if it.get("category_id") not in (9, 10)]
    opening_items = [it for it in predictions if it.get("category_id") in (9, 10)]

    # 1. Unproject and repair room polygons
    repaired_rooms: List[Tuple[int, Polygon]] = []
    for item in room_items:
        raw_pts = item.get("segmentation", [])
        if len(raw_pts) < 3:
            continue
        unproj_pts = []
        for p in raw_pts:
            local_x, local_y = unproject_point(p[0], p[1], orig_w, orig_h)
            unproj_pts.append((local_x + origin_px[0], local_y + origin_px[1]))
        poly = clean_polygon(unproj_pts)
        if poly is not None and poly.area > 50.0:
            repaired_rooms.append((item.get("category_id", 8), poly))

    # 2. Extract boundary segments & snap shared nodes
    raw_segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for _, poly in repaired_rooms:
        coords = list(poly.exterior.coords)
        for i in range(len(coords) - 1):
            p1, p2 = coords[i], coords[i + 1]
            if math.dist(p1, p2) > 2.0:
                raw_segments.append((p1, p2))

    # Cluster node coordinates
    unique_nodes: List[Tuple[float, float]] = []
    for p1, p2 in raw_segments:
        s1 = snap_point(p1, unique_nodes, tol=snap_tol_px)
        if s1 not in unique_nodes:
            unique_nodes.append(s1)
        s2 = snap_point(p2, unique_nodes, tol=snap_tol_px)
        if s2 not in unique_nodes:
            unique_nodes.append(s2)

    node_to_id: Dict[Tuple[float, float], str] = {
        node: f"node_{i+1}" for i, node in enumerate(unique_nodes)
    }

    # 3. Build unique undirected edges
    edge_counts: Dict[Tuple[str, str], int] = {}
    for p1, p2 in raw_segments:
        s1 = snap_point(p1, unique_nodes, tol=snap_tol_px)
        s2 = snap_point(p2, unique_nodes, tol=snap_tol_px)
        if s1 == s2:
            continue
        id1, id2 = node_to_id[s1], node_to_id[s2]
        edge_key = tuple(sorted([id1, id2]))
        edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1

    # Convert nodes to mm coordinates
    trace_nodes: List[TraceNode] = []
    for node, nid in node_to_id.items():
        x_mm = round(node[0] * mm_per_px, 1)
        y_mm = round(node[1] * mm_per_px, 1)
        trace_nodes.append(TraceNode(id=nid, x=x_mm, y=y_mm))

    id_to_coords = {n.id: (n.x, n.y) for n in trace_nodes}

    # Build TraceEdges
    trace_edges: List[TraceEdge] = []
    for (src, tgt), count in edge_counts.items():
        p_src = id_to_coords[src]
        p_tgt = id_to_coords[tgt]
        length_mm = math.dist(p_src, p_tgt)
        if length_mm < min_wall_len_mm:
            continue
        # Shared boundary between rooms is internal; unshared is external
        wall_type = "internal" if count > 1 else "external"
        trace_edges.append(
            TraceEdge(
                source=src,
                target=tgt,
                type=wall_type,
                thickness=wall_thickness_mm,
            )
        )

    # Filter out orphan nodes
    used_node_ids = {e.source for e in trace_edges} | {e.target for e in trace_edges}
    trace_nodes = [n for n in trace_nodes if n.id in used_node_ids]

    walls_graph = WallsGraph(nodes=trace_nodes, edges=trace_edges)

    # 4. Extract Zones (Rooms)
    trace_zones: List[TraceZone] = []
    for cat_id, poly in repaired_rooms:
        centroid = poly.centroid
        c_x_mm = round(centroid.x * mm_per_px, 1)
        c_y_mm = round(centroid.y * mm_per_px, 1)
        room_name = CC5K_LABEL.get(cat_id, "Undefined")
        trace_zones.append(TraceZone(name=room_name, seedPoint=(c_x_mm, c_y_mm)))

    # 5. Extract Openings (Doors & Windows)
    doors: List[TraceOpening] = []
    windows: List[TraceOpening] = []

    for item in opening_items:
        cat_id = item.get("category_id")
        raw_pts = item.get("segmentation", [])
        if len(raw_pts) < 2:
            continue
        p1_local = unproject_point(raw_pts[0][0], raw_pts[0][1], orig_w, orig_h)
        p2_local = unproject_point(raw_pts[-1][0], raw_pts[-1][1], orig_w, orig_h)
        p1 = (p1_local[0] + origin_px[0], p1_local[1] + origin_px[1])
        p2 = (p2_local[0] + origin_px[0], p2_local[1] + origin_px[1])
        op_line_mm = LineString([
            (p1[0] * mm_per_px, p1[1] * mm_per_px),
            (p2[0] * mm_per_px, p2[1] * mm_per_px),
        ])
        op_len = op_line_mm.length
        if op_len < 300.0:  # Minimum realistic opening width (300 mm)
            continue
        op_center = (op_line_mm.centroid.x, op_line_mm.centroid.y)

        # Host on closest wall edge
        best_edge: Optional[TraceEdge] = None
        best_dist = float("inf")
        best_offset = 0.0

        for edge in trace_edges:
            w_start = id_to_coords[edge.source]
            w_end = id_to_coords[edge.target]
            w_line = LineString([w_start, w_end])
            dist = w_line.distance(ShapelyPoint(op_center))
            if dist < best_dist:
                best_dist = dist
                best_edge = edge
                best_offset = w_line.project(ShapelyPoint(op_center))

        if best_edge is not None and best_dist < (wall_thickness_mm * 2.5):
            opening = TraceOpening(
                edge_nodes=(best_edge.source, best_edge.target),
                length=round(op_len, 1),
                distance_from_lower_node=round(best_offset, 1),
                center=(round(op_center[0], 1), round(op_center[1], 1)),
            )
            if cat_id == 10:
                doors.append(opening)
            elif cat_id == 9:
                windows.append(opening)

    return TraceResult(
        walls_graph=walls_graph,
        doors_list=doors,
        windows_list=windows,
        zones_list=trace_zones,
        units="mm",
        mm_per_unit=1.0,
        # Raster2Seq coordinates have their origin at the image's top-left.
        # Coordinates are already metric here, but the compiler must still
        # invert the y axis before emitting CAD's y-up geometry.
        y_down=True,
        source="raster2seq",
    )


@dataclass(frozen=True)
class Raster2SeqQuality:
    """Topology-only confidence used to reject visibly collapsed checkpoints."""

    score: float
    rooms: int
    openings: int
    edges: int
    nodes: int
    dead_ends: int
    components: int
    collapsed: bool

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def assess_trace_quality(trace: TraceResult) -> Raster2SeqQuality:
    """Score a prediction without ground truth.

    This deliberately rewards coherent topology rather than raw polygon count.
    A model that emits many disconnected fragments therefore loses to a model
    with slightly fewer rooms but a substantially cleaner wall network.
    """
    nodes = trace.walls_graph.nodes
    edges = trace.walls_graph.edges
    degree = {node.id: 0 for node in nodes}
    adjacency = {node.id: set() for node in nodes}
    for edge in edges:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)

    seen = set()
    components = 0
    for node_id in adjacency:
        if node_id in seen:
            continue
        components += 1
        stack = [node_id]
        seen.add(node_id)
        while stack:
            current = stack.pop()
            for neighbour in adjacency[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)

    rooms = len(trace.zones_list)
    openings = len(trace.doors_list) + len(trace.windows_list)
    edge_count = len(edges)
    dead_ends = sum(value == 1 for value in degree.values())
    room_score = min(rooms, 16) / 16.0
    opening_score = min(openings, 16) / 16.0
    dead_ratio = dead_ends / max(1, len(nodes))
    component_ratio = components / max(1, rooms)
    collapsed = (
        rooms < 3
        or edge_count < 4
        or (openings == 0 and dead_ratio > 0.35)
    )
    # Normal floor-plan room polygons produce roughly 4-8 distinct boundary
    # edges per room. Both near-zero and explosive ratios indicate collapse.
    edges_per_room = edge_count / max(1, rooms)
    density_score = max(0.0, 1.0 - abs(edges_per_room - 5.0) / 8.0)
    coherence = max(0.0, 1.0 - dead_ratio) * max(0.0, 1.0 - component_ratio / 2.0)
    score = 0.35 * room_score + 0.15 * opening_score + 0.25 * density_score + 0.25 * coherence
    if collapsed:
        score *= 0.05
    return Raster2SeqQuality(
        score=round(score, 6),
        rooms=rooms,
        openings=openings,
        edges=edge_count,
        nodes=len(nodes),
        dead_ends=dead_ends,
        components=components,
        collapsed=collapsed,
    )


def trace_with_raster2seq(
    json_path: str | Path,
    image_size: Tuple[int, int],
    mm_per_px: float = 10.0,
    origin_px: Tuple[float, float] = (0.0, 0.0),
) -> TraceResult:
    """Load Raster2Seq prediction JSON and construct a CAD TraceResult."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return raster2seq_to_trace(
        data,
        orig_size=image_size,
        mm_per_px=mm_per_px,
        origin_px=origin_px,
    )
