"""
Plan Geometry and Architectural CAD Regularization Engine
=========================================================
Production geometric kernel for architectural floor plan processing:
- Sub-edge to merged-edge opening remapping (eliminates opening loss on merged walls)
- Planar graph noding with unary_union (resolves un-noded T-junctions and crossing walls)
- Exact exterior vs interior exposure solver (solve_wall_exposure)
- FNV-1a 64-bit content hashing for stable room cycle identities
- Unified PlanGeometryTracer producing both CAD schema and TopoSpatial TraceResult

Maintenance notes (2 Nov 2026, local fixes to this repository's implementation;
the provenance comments below are kept as written):
- One frame: ``PlanGeometryTracer.trace`` now projects nodes, openings, zone
  seeds and furniture into millimetres once, *before* cleanup, so cleaning,
  room extraction, exposure and hosting all see the same coordinates
  (previously rooms were projected but exposure got the raw node map).
- ``snap_tol`` is applied in millimetres (it was applied in source units).
- ``CoordinateProjector`` supports uniform scale, translation and Y inversion;
  anisotropic scaling must be requested explicitly (``allow_anisotropic``).
- Inputs are never modified in place (doors/windows lists were mutated).
- Door ``door_type`` / ``hinge`` / ``swing`` survive hosting and
  ``to_trace_result`` (they were reset to defaults).
- ``solve_wall_exposure`` probes several stations per wall, keeps measured
  thickness, reports ``unknown`` / ``mixed`` instead of guessing, and accepts
  plain vertex lists as well as shapely polygons.
- shapely is imported lazily, so the pure functions (hashing, fills, exposure
  on vertex lists) work without it.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # shapely is needed for planar noding / polygonize, not for the pure helpers
    import shapely.geometry as sg
    import shapely.ops as so
    from shapely.geometry import LineString, Point as ShapelyPoint, Polygon
except ImportError:  # pragma: no cover - exercised only without shapely
    sg = so = None
    LineString = ShapelyPoint = Polygon = None


def _need_shapely(what: str) -> None:
    if sg is None:
        raise ImportError(f"{what} needs shapely (pip install shapely)")


# ==============================================================================
# 1. CONSTANTS & ARCHITECTURAL STANDARDS (Standard Architectural Metrics)
# ==============================================================================

EPSILON_WGT = 1e-6       # Minimum wall length threshold
COLLINEAR_DUR = 1e-4     # Maximum perpendicular distance deviation for collinearity

DEFAULT_WALL_THICKNESS = {
    "internalMm": 150.0,  # Standard interior partition thickness (150 mm)
    "externalMm": 300.0,  # Standard exterior wall thickness (300 mm)
    "dividerMm": 6.0,     # Room divider line thickness (6 mm)
    "railingMm": 50.0,    # Balcony railing thickness (50 mm)
    "fallbackInternal": 150.0,
    "fallbackExternal": 300.0,
}

OPENING_STANDARDS = {
    "door": {
        "height": 2100.0,     # Standard door height (2100 mm)
        "level": 0.0,         # Standard door sill level (0 mm)
        "frameThickness": 60.0,
    },
    "window": {
        "height": 1500.0,     # Standard window height (1500 mm)
        "level": 900.0,       # Standard window sill height (900 mm)
        "frameThickness": 60.0,
        "glassThickness": 10.0,
    },
}


# ==============================================================================
# 2. COORDINATE MATHEMATICS & AFFINE PROJECTION (Reconstructed from WUr & sae)
# ==============================================================================

class CoordinateProjector:
    """
    Reconstructs CAD's WUr() and sae() coordinate projection functions.
    Maps normalized AI inference coordinates into metric canvas world coordinates.

    ``flip_y`` maps a y-down source (image rows) into a y-up target: the source
    top edge lands on the target top edge. Different x / y scale factors change
    angles and opening widths, so they raise unless ``allow_anisotropic=True``.
    """
    def __init__(
        self,
        source_bounds: Dict[str, float],
        target_bounds: Dict[str, float],
        *,
        flip_y: bool = False,
        allow_anisotropic: bool = False,
    ):
        self.src_x = source_bounds.get("x", 0.0)
        self.src_y = source_bounds.get("y", 0.0)
        self.src_w = max(source_bounds.get("width", 1.0), 1e-6)
        self.src_h = max(source_bounds.get("height", 1.0), 1e-6)

        self.tgt_x = target_bounds["x"]
        self.tgt_y = target_bounds["y"]
        self.tgt_w = target_bounds["width"]
        self.tgt_h = target_bounds["height"]

        # Anisotropic scaling factors
        self.scale_x = self.tgt_w / self.src_w
        self.scale_y = self.tgt_h / self.src_h
        self.flip_y = flip_y
        if not self.is_uniform and not allow_anisotropic:
            raise ValueError(
                f"anisotropic projection (sx={self.scale_x:.6g}, sy={self.scale_y:.6g}) changes angles; "
                "pass allow_anisotropic=True if that is intended"
            )

    @classmethod
    def from_scale(
        cls, mm_per_unit: float, *, source_height: float = 0.0, y_down: bool = False
    ) -> "CoordinateProjector":
        """Uniform projection: multiply by ``mm_per_unit``; with ``y_down`` the
        source y axis is inverted about ``source_height`` (image height)."""
        h = max(float(source_height), 1e-6) if y_down else 1.0
        return cls(
            {"x": 0.0, "y": 0.0, "width": 1.0, "height": h},
            {"x": 0.0, "y": 0.0, "width": float(mm_per_unit), "height": h * float(mm_per_unit)},
            flip_y=y_down,
        )

    @property
    def is_uniform(self) -> bool:
        return math.isclose(self.scale_x, self.scale_y, rel_tol=1e-9, abs_tol=1e-12)

    def map_point(self, pt: Dict[str, float]) -> Dict[str, float]:
        """Maps a point (x, y) from source AI image space to target canvas space."""
        if self.flip_y:
            y = self.tgt_y + (self.src_y + self.src_h - pt["y"]) * self.scale_y
        else:
            y = self.tgt_y + (pt["y"] - self.src_y) * self.scale_y
        return {"x": self.tgt_x + (pt["x"] - self.src_x) * self.scale_x, "y": y}

    def map_vector(self, dx: float, dy: float) -> Tuple[float, float]:
        """Maps a directional vector without translation."""
        return (dx * self.scale_x, (-dy if self.flip_y else dy) * self.scale_y)

    def map_length(self, value: float) -> float:
        """Maps a length (thickness, width). Only defined for uniform scale."""
        if not self.is_uniform:
            raise ValueError("lengths are direction-dependent under anisotropic scaling")
        return float(value) * self.scale_x


# ==============================================================================
# 3. TOPOLOGICAL GRAPH CLEANER & COLLINEAR MERGER (Reconstructed from pUr & hUr)
# ==============================================================================

def check_collinear_edges(
    e1: Dict[str, Any],
    e2: Dict[str, Any],
    junction_node_id: str,
    node_map: Dict[str, Dict[str, float]],
) -> bool:
    """
    Reconstructs CAD's hUr() function.
    Determines if two edges meeting at junction_node_id form a single continuous straight wall.

    Conditions:
    1. Dot product < 0 (vectors point in opposite directions, angle ~ 180 degrees)
    2. Perpendicular distance deviation <= COLLINEAR_DUR (1e-4)
    """
    s = node_map.get(junction_node_id)
    # Find opposite endpoints for both edges
    o = e1["target"] if e1["source"] == junction_node_id else e1["source"]
    i = e2["target"] if e2["source"] == junction_node_id else e2["source"]
    if not s or o is None or i is None:
        return False

    a = node_map.get(o)
    c = node_map.get(i)
    if not a or not c:
        return False

    l = a["x"] - s["x"]
    u = a["y"] - s["y"]
    d = c["x"] - s["x"]
    f = c["y"] - s["y"]

    h = math.hypot(l, u)
    p = math.hypot(d, f)
    if h < EPSILON_WGT or p < EPSILON_WGT:
        return False

    # 2D cross product magnitude: |l*f - u*d|
    cross_prod = abs(l * f - u * d)
    m = cross_prod / h
    y = cross_prod / p

    # Must point in opposite directions (dot < 0) and max deviation <= 1e-4
    dot_prod = l * d + u * f
    return (dot_prod < 0) and (max(m, y) <= COLLINEAR_DUR)


def edge_key(id1: str, id2: str) -> str:
    """Canonical undirected edge key matching CAD's eMe(t, e)"""
    return f"{id1}:{id2}" if str(id1) < str(id2) else f"{id2}:{id1}"


def clean_walls_graph(walls_graph: Dict[str, Any], snap_tol: float = 15.0) -> Dict[str, Any]:
    """
    Reconstructs CAD's pUr() function with sub-edge opening tracking.
    ``snap_tol`` is in the graph's own units; ``PlanGeometryTracer`` projects
    to millimetres first, so there it is millimetres.
    Cleans raw AI neural detection graph:
    1. Spatially snaps and deduplicates nodes within snap_tol and filters zero-length edges
    2. Identifies chains of collinear wall segments meeting at degree-2 nodes
    3. Merges multi-segment collinear chains into single continuous wall edges
    4. Builds subEdgeToMerged map so openings hosted on sub-edges map cleanly to the merged edge.
    """
    raw_nodes = walls_graph.get("nodes", [])
    raw_edges = walls_graph.get("edges", [])

    # 1. Spatially cluster and deduplicate nodes within snap_tol
    node_remap: Dict[str, str] = {}
    node_map: Dict[str, Dict[str, float]] = {}
    canonical_coords: List[Tuple[float, float, str]] = []

    for n in raw_nodes:
        nid = str(n["id"])
        x = float(n["x"])
        y = float(n["y"])
        matched_id = None
        for cx, cy, cid in canonical_coords:
            if math.hypot(x - cx, y - cy) <= snap_tol:
                matched_id = cid
                break
        if matched_id is not None:
            node_remap[nid] = matched_id
        else:
            canonical_coords.append((x, y, nid))
            node_map[nid] = {"id": nid, "x": x, "y": y}
            node_remap[nid] = nid

    # Remap edges to canonical nodes and remove zero-length and duplicate edges
    remapped_edges = []
    seen_edge_keys = set()
    for e in raw_edges:
        src = node_remap.get(str(e["source"]), str(e["source"]))
        tgt = node_remap.get(str(e["target"]), str(e["target"]))
        if src == tgt:
            continue
        k = edge_key(src, tgt)
        if k in seen_edge_keys:
            continue
        seen_edge_keys.add(k)
        remapped_edges.append({**e, "source": src, "target": tgt})

    # 2. Build incidence map: node_id -> list of incident edge indices
    incidence = {nid: set() for nid in node_map}
    for idx, e in enumerate(remapped_edges):
        if e["source"] in incidence:
            incidence[e["source"]].add(idx)
        if e["target"] in incidence:
            incidence[e["target"]].add(idx)

    # 3. Filter valid candidate edges (length >= EPSILON_WGT)
    valid_edge_indices = set()
    for idx, e in enumerate(remapped_edges):
        src = node_map.get(e["source"])
        tgt = node_map.get(e["target"])
        if not src or not tgt or e["source"] == e["target"]:
            continue
        dist = math.hypot(tgt["x"] - src["x"], tgt["y"] - src["y"])
        if dist >= EPSILON_WGT:
            valid_edge_indices.add(idx)

    # 4. Find collinear pairs meeting at degree-2 nodes
    collinear_pairs = {idx: set() for idx in valid_edge_indices}
    for nid, edge_indices in incidence.items():
        if len(edge_indices) != 2:
            continue
        idx1, idx2 = list(edge_indices)
        if idx1 not in valid_edge_indices or idx2 not in valid_edge_indices:
            continue
        e1 = remapped_edges[idx1]
        e2 = remapped_edges[idx2]
        # Must share identical type (internal vs external)
        if e1.get("type") != e2.get("type"):
            continue
        if check_collinear_edges(e1, e2, nid, node_map):
            collinear_pairs[idx1].add(idx2)
            collinear_pairs[idx2].add(idx1)

    # 5. Connected components traversal (collinear edge chains)
    visited_edges = set()
    merged_edge_map = {}
    source_edges_by_out = {}
    edge_remap = {}
    sub_edge_to_merged = {}

    for idx in sorted(valid_edge_indices):
        if idx in visited_edges:
            continue
        stack = [idx]
        chain = []
        while stack:
            curr = stack.pop()
            if curr not in visited_edges:
                visited_edges.add(curr)
                chain.append(curr)
                for neighbor in collinear_pairs.get(curr, []):
                    if neighbor not in visited_edges:
                        stack.append(neighbor)

        if len(chain) < 2:
            continue

        # Count node appearances in chain to find the two extreme endpoints
        chain_node_counts = {}
        for c_idx in chain:
            edge = remapped_edges[c_idx]
            chain_node_counts[edge["source"]] = chain_node_counts.get(edge["source"], 0) + 1
            chain_node_counts[edge["target"]] = chain_node_counts.get(edge["target"], 0) + 1

        # Endpoints appear exactly once in a simple line chain
        endpoints = [nid for nid, cnt in chain_node_counts.items() if cnt == 1]
        if len(endpoints) != 2:
            continue

        # Orient merged edge to match original first edge
        first_edge = remapped_edges[chain[0]]
        k = node_map[first_edge["source"]]
        p = node_map[first_edge["target"]]
        a = node_map[endpoints[0]]
        m = node_map[endpoints[1]]

        dx_orig = p["x"] - k["x"]
        dy_orig = p["y"] - k["y"]
        dx_new = m["x"] - a["x"]
        dy_new = m["y"] - a["y"]

        is_same_dir = (dx_orig * dx_new + dy_orig * dy_new) >= 0
        src_id = endpoints[0] if is_same_dir else endpoints[1]
        tgt_id = endpoints[1] if is_same_dir else endpoints[0]

        merged_edge = {
            **first_edge,
            "source": src_id,
            "target": tgt_id,
            "merged": True,
        }
        lead_idx = chain[0]
        merged_edge_map[lead_idx] = merged_edge
        source_edges_by_out[lead_idx] = [remapped_edges[c] for c in chain]
        for c in chain:
            edge_remap[c] = lead_idx
            sub_e = remapped_edges[c]
            sub_k = edge_key(sub_e["source"], sub_e["target"])
            sub_edge_to_merged[sub_k] = merged_edge

    # Also map original un-snapped edge keys into sub_edge_to_merged
    for e in raw_edges:
        raw_k = edge_key(str(e["source"]), str(e["target"]))
        snapped_k = edge_key(node_remap.get(str(e["source"]), str(e["source"])), node_remap.get(str(e["target"]), str(e["target"])))
        if snapped_k in sub_edge_to_merged and raw_k not in sub_edge_to_merged:
            sub_edge_to_merged[raw_k] = sub_edge_to_merged[snapped_k]

    # 6. Assemble cleaned output graph
    output_edges = []
    output_source_edge_map = []
    for idx, e in enumerate(remapped_edges):
        if idx in edge_remap:
            lead = edge_remap[idx]
            if lead == idx:
                output_edges.append(merged_edge_map[lead])
                output_source_edge_map.append(source_edges_by_out[lead])
        else:
            output_edges.append(e)
            output_source_edge_map.append([e])

    # Filter out nodes not referenced by any output edge
    active_node_ids = set()
    for e in output_edges:
        active_node_ids.add(e["source"])
        active_node_ids.add(e["target"])
    active_nodes = [node_map[nid] for nid in active_node_ids if nid in node_map]

    return {
        "graph": {
            "nodes": active_nodes,
            "edges": output_edges,
        },
        "sourceEdgesByOutputIndex": output_source_edge_map,
        "subEdgeToMerged": sub_edge_to_merged,
        "nodeRemap": node_remap,
    }


# ==============================================================================
# 4. CORNER MITERING & JOINT SOLVER (Reconstructed from solve_fills in Rust Wasm)
# ==============================================================================

def solve_wall_fills(walls: List[Dict[str, Any]], node_map: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
    """
    Reconstructs CAD's solve_fills() corner miter joint solver.
    For each wall junction, computes 2D miter polygon vertices to eliminate overlapping corners.
    """
    junction_walls = {nid: [] for nid in node_map}
    for w in walls:
        junction_walls[w["startNodeId"]].append((w, "start"))
        junction_walls[w["endNodeId"]].append((w, "end"))

    miter_diffs = []
    for nid, incident in junction_walls.items():
        if len(incident) < 2:
            continue
        junction_pt = np.array([node_map[nid]["x"], node_map[nid]["y"]])

        rays = []
        for w, end_tag in incident:
            other_id = w["endNodeId"] if end_tag == "start" else w["startNodeId"]
            other_pt = np.array([node_map[other_id]["x"], node_map[other_id]["y"]])
            vec = other_pt - junction_pt
            norm = np.linalg.norm(vec)
            if norm > 1e-6:
                rays.append((vec / norm, w, end_tag))

        if len(rays) == 2:
            # 2-wall miter joint
            v1, w1, _ = rays[0]
            v2, w2, _ = rays[1]
            t1 = w1.get("thickness", DEFAULT_WALL_THICKNESS["internalMm"])
            t2 = w2.get("thickness", DEFAULT_WALL_THICKNESS["internalMm"])

            dot = float(np.dot(v1, v2))
            cross = float(v1[0] * v2[1] - v1[1] * v2[0])
            if abs(cross) > 1e-4:
                miter_diffs.append({
                    "junctionId": nid,
                    "kind": "miter_corner",
                    "angleRad": math.atan2(cross, dot),
                    "wallIds": [w1["id"], w2["id"]],
                    "thicknesses": [t1, t2],
                })
        elif len(rays) > 2:
            # T-junction or cross junction
            miter_diffs.append({
                "junctionId": nid,
                "kind": "t_junction",
                "wallCount": len(rays),
                "wallIds": [w["id"] for _, w, _ in rays],
            })

    return miter_diffs


# ==============================================================================
# 5. WALL EXPOSURE SOLVER (Reconstructed from solve_exposed in Rust Wasm)
# ==============================================================================

def _contains(poly: Any, x: float, y: float) -> bool:
    """Containment for a shapely polygon or a plain vertex list (no holes)."""
    if hasattr(poly, "contains") and ShapelyPoint is not None:
        return bool(poly.contains(ShapelyPoint(x, y)))
    pts = list(poly)
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def solve_wall_exposure(
    walls: List[Dict[str, Any]],
    node_map: Dict[str, Dict[str, float]],
    room_polygons: List[Any],
    probe_offset_mm: float = 40.0,
    probe_count: int = 5,
) -> List[Dict[str, Any]]:
    """
    Reconstructs CAD's solve_exposed() kernel algorithm.
    Determines whether each wall is external (perimeter envelope) or internal (partition)
    by probing both sides of its centerline against closed room polygon spaces.

    Classification:
    - Internal: Both sides are contained inside room polygons.
    - External: Exactly one side is inside a room (or bounds the exterior envelope).
    - Freestanding: Neither side is inside a room.

    Maintenance (2 Nov 2026): each wall is probed at ``probe_count`` stations
    (not only the midpoint). The result adds ``exposure``:
    ``internal`` | ``external`` | ``unknown`` (no side touches a room) |
    ``mixed`` (stations disagree; ``exposureTransitions`` gives the fractions
    along start->end where the classification changes, so a caller can split
    the wall there). ``type`` and ``thickness`` change only when the evidence
    is consistent; a thickness is replaced by the default only when the wall
    has none or its ``thicknessSource`` is ``"default"``.
    ``room_polygons`` may be shapely polygons or vertex lists.
    """
    classified_walls = []
    stations = max(1, int(probe_count))

    def classify(left: bool, right: bool) -> str:
        if left and right:
            return "internal"
        if left or right:
            return "external"
        return "unknown"

    for w in walls:
        p1 = node_map[w["startNodeId"]]
        p2 = node_map[w["endNodeId"]]
        dx = p2["x"] - p1["x"]
        dy = p2["y"] - p1["y"]
        length = math.hypot(dx, dy)
        if length < EPSILON_WGT:
            classified_walls.append({**w, "exposure": "unknown"})
            continue

        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux  # 90 deg normal

        profile = []
        for k in range(stations):
            f = (k + 0.5) / stations
            px, py = p1["x"] + dx * f, p1["y"] + dy * f
            left = any(_contains(poly, px + nx * probe_offset_mm, py + ny * probe_offset_mm) for poly in room_polygons)
            right = any(_contains(poly, px - nx * probe_offset_mm, py - ny * probe_offset_mm) for poly in room_polygons)
            profile.append((f, left, right, classify(left, right)))

        kinds = {c for _, _, _, c in profile}
        mid = profile[len(profile) // 2]
        left_in_room, right_in_room = mid[1], mid[2]
        explicit = "thickness" in w and w.get("thicknessSource", "measured") != "default"
        out = {**w, "leftRoom": left_in_room, "rightRoom": right_in_room}
        if len(kinds) == 1:
            exposure_type = kinds.pop()
            out["exposure"] = exposure_type
            if exposure_type in ("internal", "external"):
                if w.get("type") != "separator":
                    out["type"] = exposure_type
                if not explicit:
                    out["thickness"] = DEFAULT_WALL_THICKNESS[f"{exposure_type}Mm"]
            else:
                # No evidence either way: keep what the recogniser said.
                fallback = w.get("type", "external")
                out["type"] = fallback
                if not explicit:
                    out["thickness"] = DEFAULT_WALL_THICKNESS.get(f"{fallback}Mm", DEFAULT_WALL_THICKNESS["externalMm"])
        else:
            out["exposure"] = "mixed"
            out["exposureTransitions"] = [
                round((a[0] + b[0]) / 2, 4) for a, b in zip(profile, profile[1:]) if a[3] != b[3]
            ]
            out.setdefault("type", w.get("type", "internal"))
            if not explicit:
                out["thickness"] = w.get("thickness", DEFAULT_WALL_THICKNESS["internalMm"])
        classified_walls.append(out)

    return classified_walls


# ==============================================================================
# 6. PARAMETRIC OPENING HOSTER (Reconstructed from LUr, zUr & sMe)
# ==============================================================================

def _point_segment_distance(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def host_openings_on_walls(
    openings: List[Dict[str, Any]],
    walls: List[Dict[str, Any]],
    node_map: Dict[str, Dict[str, float]],
    projector: CoordinateProjector,
    sub_edge_to_merged: Optional[Dict[str, Dict[str, Any]]] = None,
    node_remap: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Reconstructs CAD's LUr() and zUr() opening hosting algorithms.
    Maps door swings and window lengths onto parent wall centerlines as canonical 1D scalars.
    Uses sub_edge_to_merged to ensure openings on merged collinear segments are preserved.
    """
    sub_edge_to_merged = sub_edge_to_merged or {}
    node_remap = node_remap or {}

    wall_by_edge = {}
    for w in walls:
        k = edge_key(w["startNodeId"], w["endNodeId"])
        wall_by_edge[k] = w

    hosted_openings = []

    for op in openings:
        kind = op.get("kind", "door")
        edge_nodes = op.get("edge_nodes")
        if not edge_nodes or len(edge_nodes) < 2:
            continue

        n1, n2 = edge_nodes[0], edge_nodes[1]
        k = edge_key(n1, n2)
        s1 = node_remap.get(str(n1), str(n1))
        s2 = node_remap.get(str(n2), str(n2))
        k_snapped = edge_key(s1, s2)

        parent_wall = wall_by_edge.get(k) or wall_by_edge.get(k_snapped)
        if not parent_wall and k in sub_edge_to_merged:
            # Remap to merged parent wall
            merged_info = sub_edge_to_merged[k]
            parent_edge_k = edge_key(merged_info["source"], merged_info["target"])
            parent_wall = wall_by_edge.get(parent_edge_k)
        elif not parent_wall and k_snapped in sub_edge_to_merged:
            merged_info = sub_edge_to_merged[k_snapped]
            parent_edge_k = edge_key(merged_info["source"], merged_info["target"])
            parent_wall = wall_by_edge.get(parent_edge_k)

        # Fallback: find nearest wall line geometrically if edge key was lost during cleanup
        if not parent_wall:
            best_dist = float("inf")
            best_wall = None
            raw_c = op.get("center")
            if raw_c:
                c_pt = projector.map_point({"x": raw_c[0], "y": raw_c[1]})
                for w in walls:
                    p1 = projector.map_point(node_map[w["startNodeId"]])
                    p2 = projector.map_point(node_map[w["endNodeId"]])
                    d = _point_segment_distance((c_pt["x"], c_pt["y"]), (p1["x"], p1["y"]), (p2["x"], p2["y"]))
                    if d < best_dist:
                        best_dist = d
                        best_wall = w
            if best_wall is not None and best_dist < 400.0:
                parent_wall = best_wall

        if not parent_wall:
            continue

        # Get lower and upper node coordinates (CAD canonical ordering)
        w_start_id = parent_wall["startNodeId"]
        w_end_id = parent_wall["endNodeId"]
        l_id = min(w_start_id, w_end_id, key=lambda x: str(x))
        u_id = max(w_start_id, w_end_id, key=lambda x: str(x))
        d_pt = node_map[l_id]
        f_pt = node_map[u_id]

        mapped_start_node = projector.map_point(d_pt)
        mapped_end_node = projector.map_point(f_pt)

        # Canvas metric wall length
        canvas_wall_len = math.hypot(
            mapped_end_node["x"] - mapped_start_node["x"],
            mapped_end_node["y"] - mapped_start_node["y"],
        )
        if canvas_wall_len < 1e-6:
            continue

        wall_dir_canvas = (
            (mapped_end_node["x"] - mapped_start_node["x"]) / canvas_wall_len,
            (mapped_end_node["y"] - mapped_start_node["y"]) / canvas_wall_len,
        )

        # Determine center in world coordinates
        dist_from_lower = op.get("distance_from_lower_node")
        if dist_from_lower is not None and math.isfinite(dist_from_lower):
            pt_a = node_map.get(node_remap.get(str(n1), str(n1)))
            pt_b = node_map.get(node_remap.get(str(n2), str(n2)))
            if pt_a and pt_b:
                edx = pt_b["x"] - pt_a["x"]
                edy = pt_b["y"] - pt_a["y"]
                e_len = math.hypot(edx, edy)
                if e_len > 1e-6:
                    raw_cx = pt_a["x"] + (edx / e_len) * dist_from_lower
                    raw_cy = pt_a["y"] + (edy / e_len) * dist_from_lower
                    mapped_center = projector.map_point({"x": raw_cx, "y": raw_cy})
                else:
                    mapped_center = projector.map_point(pt_a)
            else:
                raw_cx = d_pt["x"] + wall_dir_canvas[0] * dist_from_lower
                raw_cy = d_pt["y"] + wall_dir_canvas[1] * dist_from_lower
                mapped_center = projector.map_point({"x": raw_cx, "y": raw_cy})
        elif op.get("center") is not None:
            raw_cx, raw_cy = op["center"][0], op["center"][1]
            mapped_center = projector.map_point({"x": raw_cx, "y": raw_cy})
        else:
            mapped_center = {
                "x": (mapped_start_node["x"] + mapped_end_node["x"]) / 2.0,
                "y": (mapped_start_node["y"] + mapped_end_node["y"]) / 2.0,
            }

        # Map opening length
        raw_length = float(op.get("length", 900.0))
        mapped_width = raw_length * projector.scale_x

        # Calculate 1D scalar parameter along wall [0, canvas_wall_len]
        vec_to_center = (
            mapped_center["x"] - mapped_start_node["x"],
            mapped_center["y"] - mapped_start_node["y"],
        )
        t_scalar = vec_to_center[0] * wall_dir_canvas[0] + vec_to_center[1] * wall_dir_canvas[1]

        # Clamp opening to stay within wall boundaries
        half_w = mapped_width / 2.0
        clamped_left = max(0.0, min(t_scalar - half_w, max(0.0, canvas_wall_len - mapped_width)))

        # Architectural specs
        std = OPENING_STANDARDS.get(kind, OPENING_STANDARDS["door"])
        wall_thickness = parent_wall.get("thickness", DEFAULT_WALL_THICKNESS["internalMm"])

        canonical_edge_k = edge_key(parent_wall["startNodeId"], parent_wall["endNodeId"])
        # hinge / swing are given relative to edge_nodes[0] -> edge_nodes[1];
        # re-express them relative to lower -> upper node (the hosted frame).
        hinge = op.get("hinge", "start")
        swing = op.get("swing", "left")
        reported_dir = None
        pa_raw = node_map.get(node_remap.get(str(n1), str(n1)))
        pb_raw = node_map.get(node_remap.get(str(n2), str(n2)))
        if pa_raw and pb_raw:
            reported_dir = (pb_raw["x"] - pa_raw["x"], pb_raw["y"] - pa_raw["y"])
        if reported_dir is not None:
            rdx, rdy = projector.map_vector(*reported_dir)
            if rdx * wall_dir_canvas[0] + rdy * wall_dir_canvas[1] < 0:
                hinge = "end" if hinge == "start" else "start"
                swing = "right" if swing == "left" else "left"
        hosted_openings.append({
            "id": f"opening_{len(hosted_openings) + 1}",
            "type": "opening",
            "name": "Window" if kind == "window" else "Door",
            "parentId": parent_wall["id"],
            "wallEdgeKey": canonical_edge_k,
            "scalarOffset": round(clamped_left, 1),
            "distanceFromLowerNode": round(clamped_left + half_w, 1),
            "center": [round(mapped_center["x"], 1), round(mapped_center["y"], 1)],
            "props": {
                "kind": kind,
                "width": round(mapped_width, 1),
                "height": std["height"],
                "level": std["level"],
                "frame": {"thickness": std["frameThickness"]},
                "depth": wall_thickness,
                "doorType": op.get("door_type", "swing"),
                "direction": op.get("direction", "inward_right"),
                "hinge": hinge,
                "swing": swing,
            },
        })

    return hosted_openings


# ==============================================================================
# 7. INCREMENTAL ROOM CYCLE SOLVER (Reconstructed from solve_rooms in Rust Wasm)
# ==============================================================================

def compute_face_content_hash(vertices: List[Tuple[float, float]]) -> str:
    """
    Reconstructs CAD's 64-bit face content hashing: `face:${hash.toString(16)}`.
    Quantizes coordinates to 0.1 mm (``round(v, 1)``) and computes a stable 64-bit
    FNV-1a integer hash. The hash depends on vertex order and start vertex;
    ``regularize.canonical_face_hash`` canonicalises winding / start first.
    """
    quantized = tuple((round(x, 1), round(y, 1)) for x, y in vertices)
    FNV_OFFSET = 14695981039346656037
    FNV_PRIME = 1099511628211
    h = FNV_OFFSET
    for px, py in quantized:
        for val in (int(px * 10), int(py * 10)):
            h ^= (val & 0xFFFFFFFFFFFFFFFF)
            h = (h * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return f"face:{h:016x}"


def solve_rooms_from_walls(
    walls: List[Dict[str, Any]],
    node_map: Dict[str, Dict[str, float]],
    zones_list: List[Dict[str, Any]],
    projector: CoordinateProjector,
) -> List[Dict[str, Any]]:
    """
    Reconstructs CAD's solve_rooms() and ZUr() room assignment.
    Uses unary_union planar noding to guarantee all T-junctions and crossings form closed faces.
    1. Extracts closed planar cycles from the noded wall network
    2. Tags each with a 64-bit FNV-1a content hash
    3. Matches OCR zone labels to room polygons via point-in-polygon containment
    """
    _need_shapely("solve_rooms_from_walls")
    lines = []
    for w in walls:
        p1 = projector.map_point(node_map[w["startNodeId"]])
        p2 = projector.map_point(node_map[w["endNodeId"]])
        line = sg.LineString([(p1["x"], p1["y"]), (p2["x"], p2["y"])])
        if line.length >= EPSILON_WGT:
            lines.append(line)

    if not lines:
        return []

    # Planar noding via unary_union: splits lines at all intersections & T-junctions
    noded_network = so.unary_union(lines)
    polys = list(so.polygonize(noded_network))

    rooms = []
    for poly in polys:
        # Ignore degenerate polygons
        if not poly.is_valid or poly.area < 5000.0:  # < 0.005 m^2
            continue

        coords = list(poly.exterior.coords)
        face_id = compute_face_content_hash(coords)
        centroid = poly.centroid
        # Label point inside concave rooms (the centroid of an L can be outside).
        label_pt = poly.representative_point()

        # Match with OCR zones_list
        room_name = "Room"
        for z in zones_list:
            z_c = z.get("center", z.get("seedPoint"))
            if z_c:
                z_center = projector.map_point({"x": z_c[0], "y": z_c[1]})
                pt = sg.Point(z_center["x"], z_center["y"])
                if poly.contains(pt):
                    room_name = z.get("text", z.get("name", "Room"))
                    break

        rooms.append({
            "id": face_id,
            "type": "room",
            "name": room_name,
            "areaM2": round(poly.area / 1e6, 2),  # mm^2 to m^2
            "perimeterMm": round(poly.length, 1),
            "vertices": coords,
            "centroid": {"x": centroid.x, "y": centroid.y},
            "labelPoint": {"x": label_pt.x, "y": label_pt.y},
            "polygon": poly,
        })

    return rooms


# ==============================================================================
# 8. ORIENTED BOUNDING BOX (OBB) FURNITURE INSCRIBER (Reconstructed from uUr/aae)
# ==============================================================================

def inscribe_obb_furniture(
    obb_detections: List[Dict[str, Any]],
    rooms: List[Dict[str, Any]],
    projector: CoordinateProjector,
) -> List[Dict[str, Any]]:
    """
    Reconstructs CAD's uUr() and aae() OBB furniture instantiation.
    Takes rotated rectangles [center, size, rotation] from the neural vision model
    and instantiates scaled, rotated 3D CAD furniture assets in their containing room.
    """
    _need_shapely("inscribe_obb_furniture")
    furniture_items = []
    for item in obb_detections:
        cls_name = item.get("class", "furniture")
        raw_center = item.get("center", [0, 0])
        raw_size = item.get("size", [800, 800])
        rotation_rad = float(item.get("rotation", item.get("front_angle", 0.0)))

        mapped_center = projector.map_point({"x": raw_center[0], "y": raw_center[1]})
        mapped_w = raw_size[0] * projector.scale_x
        mapped_h = raw_size[1] * projector.scale_y

        pt = ShapelyPoint(mapped_center["x"], mapped_center["y"])
        containing_room_id = None
        for rm in rooms:
            poly = rm.get("polygon")
            if poly is None:
                poly = Polygon(rm["vertices"])
            if poly.contains(pt):
                containing_room_id = rm["id"]
                break

        furniture_items.append({
            "id": f"asset_{len(furniture_items) + 1}",
            "type": "furniture",
            "class": cls_name,
            "roomId": containing_room_id,
            "transform": {
                "x": round(mapped_center["x"], 1),
                "y": round(mapped_center["y"], 1),
                "rotationDeg": round(math.degrees(rotation_rad), 1),
            },
            "dimensions": {
                "width": round(mapped_w, 1),
                "length": round(mapped_h, 1),
                "height": 750.0,
            },
        })

    return furniture_items


# ==============================================================================
# 9. UNIFIED PRODUCTION TRACER PIPELINE (PlanGeometryTracer)
# ==============================================================================

class PlanGeometryTracer:
    """
    Production-grade architectural tracing engine reverse-engineered from CAD.
    Integrates all kernel steps into a seamless end-to-end pipeline:
    1. Scale & coordinate projection
    2. Topological cleaning & collinear edge merging (with opening tracking)
    3. Planar noding for closed room extraction
    4. Room cycle extraction with 64-bit content hashing
    5. Exterior vs interior wall exposure solving
    6. Corner mitering and joint classification
    7. Parametric opening hosting and clamping
    8. OBB furniture inscription
    """

    def __init__(self, target_bounds: Optional[Dict[str, float]] = None):
        self.target_bounds = target_bounds

    def trace(
        self,
        raw_floorplan: Dict[str, Any],
        image_size: Optional[Tuple[int, int]] = None,
        mm_per_unit: float = 1.0,
        *,
        y_down: bool = False,
        snap_tol_mm: float = 15.0,
    ) -> Dict[str, Any]:
        """
        Executes complete CAD tracing on raw floor plan detection payload.

        Maintenance (2 Nov 2026): everything is projected into millimetres once,
        up front (nodes, edge thickness, opening centres / offsets / widths,
        zone seeds, furniture), and every later step works in that frame with
        an identity projector. ``y_down=True`` inverts image rows so the output
        is y-up. ``snap_tol_mm`` is in millimetres. The input dict is not
        modified.
        """
        img_w = image_size[0] if image_size else 2048
        img_h = image_size[1] if image_size else 1536

        src_bounds = {"x": 0.0, "y": 0.0, "width": float(img_w), "height": float(img_h)}
        if not self.target_bounds:
            target_bounds = {
                "x": 0.0,
                "y": 0.0,
                "width": float(img_w) * mm_per_unit,
                "height": float(img_h) * mm_per_unit,
            }
        else:
            target_bounds = self.target_bounds

        projector = CoordinateProjector(src_bounds, target_bounds, flip_y=y_down)
        scale = projector.scale_x  # uniform (checked by the projector)

        def mm(pt: Sequence[float]) -> List[float]:
            q = projector.map_point({"x": float(pt[0]), "y": float(pt[1])})
            return [q["x"], q["y"]]

        # 0. Project the whole payload into millimetres (no in-place edits).
        raw_graph = raw_floorplan.get("walls_graph", {})
        mm_nodes = []
        for n in raw_graph.get("nodes", []):
            x, y = mm((n["x"], n["y"]))
            mm_nodes.append({**n, "id": str(n["id"]), "x": x, "y": y})
        mm_edges = []
        for e in raw_graph.get("edges", []):
            edge = {**e}
            if "thickness" in e and e["thickness"] is not None:
                edge["thickness"] = float(e["thickness"]) * scale
            mm_edges.append(edge)

        def mm_opening(op: Dict[str, Any], kind: str) -> Dict[str, Any]:
            out = {**op, "kind": kind}
            if op.get("center") is not None:
                out["center"] = mm(op["center"])
            for key in ("length", "distance_from_lower_node", "height", "sill_height"):
                if op.get(key) is not None:
                    out[key] = float(op[key]) * scale
            return out

        doors_in = [mm_opening(d, "door") for d in raw_floorplan.get("doors_list", [])]
        windows_in = [mm_opening(w, "window") for w in raw_floorplan.get("windows_list", [])]
        zones_list = []
        for z in raw_floorplan.get("zones_list", []):
            zc = dict(z)
            for key in ("center", "seedPoint", "seed_point"):
                if zc.get(key) is not None:
                    zc[key] = mm(zc[key])
            zones_list.append(zc)
        furniture_in = []
        for item in raw_floorplan.get("furniture_list", []):
            f = dict(item)
            if f.get("center") is not None:
                f["center"] = mm(f["center"])
            if f.get("size") is not None:
                f["size"] = [float(v) * scale for v in f["size"]]
            if y_down and f.get("rotation") is not None:
                f["rotation"] = -float(f["rotation"])  # angles flip with the y axis
            furniture_in.append(f)

        identity = CoordinateProjector.from_scale(1.0)

        # 1. Clean wall graph and merge collinear runs (tolerance in mm)
        cleaned_bundle = clean_walls_graph({"nodes": mm_nodes, "edges": mm_edges}, snap_tol=snap_tol_mm)
        cleaned_graph = cleaned_bundle["graph"]
        sub_edge_map = cleaned_bundle.get("subEdgeToMerged", {})
        node_remap = cleaned_bundle.get("nodeRemap", {})

        node_map = {n["id"]: n for n in cleaned_graph["nodes"]}

        # Prepare initial wall entities
        walls = []
        for i, edge in enumerate(cleaned_graph["edges"]):
            has_thickness = edge.get("thickness") is not None
            walls.append({
                "id": f"wall_{i+1}",
                "startNodeId": edge["source"],
                "endNodeId": edge["target"],
                "type": edge.get("type", "internal"),
                "thickness": float(edge["thickness"]) if has_thickness else DEFAULT_WALL_THICKNESS["internalMm"],
                "thicknessSource": edge.get("thickness_source", "measured") if has_thickness else "default",
            })

        # 2. Extract closed room cycles using planar noding
        rooms = solve_rooms_from_walls(walls, node_map, zones_list, identity)

        # 3. Solve exterior vs interior exposure (same frame as the rooms)
        room_polys = [r["polygon"] for r in rooms]
        walls = solve_wall_exposure(walls, node_map, room_polys)

        # 4. Solve corner miter fills
        miter_fills = solve_wall_fills(walls, node_map)

        # 5. Host parametric openings
        all_openings_in = doors_in + windows_in
        hosted_openings = host_openings_on_walls(
            all_openings_in,
            walls,
            node_map,
            identity,
            sub_edge_to_merged=sub_edge_map,
            node_remap=node_remap,
        )

        # 6. Inscribe furniture
        furniture = inscribe_obb_furniture(furniture_in, rooms, identity)

        # Clean room polygons for serialization
        serializable_rooms = []
        for r in rooms:
            r_copy = {k: v for k, v in r.items() if k != "polygon"}
            serializable_rooms.append(r_copy)

        return {
            "success": True,
            "frame": {"units": "mm", "y_up": True, "mm_per_source_unit": scale, "source_y_down": y_down},
            "nodes": cleaned_graph["nodes"],
            "walls": walls,
            "rooms": serializable_rooms,
            "openings": hosted_openings,
            "furniture": furniture,
            "miterFills": miter_fills,
            "metrics": {
                "wallCount": len(walls),
                "roomCount": len(rooms),
                "doorCount": sum(1 for o in hosted_openings if o["props"]["kind"] == "door"),
                "windowCount": sum(1 for o in hosted_openings if o["props"]["kind"] == "window"),
                "furnitureCount": len(furniture),
            },
        }

    def to_trace_result(
        self, trace_output: Dict[str, Any], units: str = "mm", *, include_furniture: bool = False
    ) -> Any:
        """
        Converts CAD CAD output dict to a multiCAD-mcp TraceResult object.

        Maintenance (2 Nov 2026): keeps door type / hinge / swing, measured
        thickness provenance, label points inside concave rooms and (with
        ``include_furniture``) the inscribed furniture.
        """
        from trace_engine.models import TraceEdge, TraceNode, TraceOpening, TraceResult, TraceZone, WallsGraph

        nodes = [TraceNode(id=n["id"], x=float(n["x"]), y=float(n["y"])) for n in trace_output.get("nodes", [])]
        edges = [
            TraceEdge(
                id=w["id"],
                source=w["startNodeId"],
                target=w["endNodeId"],
                type=w.get("type", "internal") if w.get("type") in ("internal", "external", "separator") else "internal",
                thickness=float(w.get("thickness", DEFAULT_WALL_THICKNESS["internalMm"])),
                thickness_source=w.get("thicknessSource", "measured"),
                provenance="geometry_kernel",
            )
            for w in trace_output.get("walls", [])
        ]
        walls_graph = WallsGraph(nodes=nodes, edges=edges)

        wall_lookup = {w["id"]: w for w in trace_output.get("walls", [])}

        door_types = {"swing", "sliding", "double", "opening"}
        doors = []
        windows = []
        for op in trace_output.get("openings", []):
            parent = wall_lookup.get(op["parentId"])
            if not parent:
                continue
            w_start_id = parent["startNodeId"]
            w_end_id = parent["endNodeId"]
            l_id = min(w_start_id, w_end_id, key=lambda x: str(x))
            u_id = max(w_start_id, w_end_id, key=lambda x: str(x))
            edge_nodes = (l_id, u_id)

            props = op.get("props", {})
            width = float(props.get("width", 900.0))
            dist_lower = float(op.get("distanceFromLowerNode", 0.0))
            center = tuple(op.get("center", [0.0, 0.0]))
            door_type = props.get("doorType", "swing")

            trace_op = TraceOpening(
                id=op.get("id"),
                edge_nodes=edge_nodes,
                length=width,
                distance_from_lower_node=dist_lower,
                center=center,
                door_type=door_type if door_type in door_types else "swing",
                hinge=props.get("hinge", "start") if props.get("hinge") in ("start", "end") else "start",
                swing=props.get("swing", "left") if props.get("swing") in ("left", "right") else "left",
                provenance="geometry_kernel",
            )
            if props.get("kind") == "window":
                windows.append(trace_op)
            else:
                doors.append(trace_op)

        zones = []
        for rm in trace_output.get("rooms", []):
            pt = rm.get("labelPoint") or rm["centroid"]
            zones.append(TraceZone(name=rm["name"], seedPoint=(pt["x"], pt["y"])))

        result = TraceResult(
            walls_graph=walls_graph,
            doors_list=doors,
            windows_list=windows,
            zones_list=zones,
            units=units,
            mm_per_unit=1.0,
            y_down=False,
            source="cad_reconstructed",
            schema_version=2,
        )
        if include_furniture:
            from trace_engine.models import FurnitureItem

            result = result.model_copy(update={"furniture_list": [
                FurnitureItem(
                    cls=f["class"],
                    center=(f["transform"]["x"], f["transform"]["y"]),
                    size=(f["dimensions"]["width"], f["dimensions"]["length"]),
                    rotation_deg=f["transform"]["rotationDeg"],
                    room_id=f.get("roomId"),
                )
                for f in trace_output.get("furniture", [])
            ]})
        return result


# Backward compatibility aliases
PlanTracer = PlanGeometryTracer

CadTracer = PlanGeometryTracer
