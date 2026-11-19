"""Unit tests for Reconstructed Plan Tracing Engine.

Verifies:
1. Collinear edge merging with opening remapping (no dropped doors/windows).
2. Planar noded room polygon extraction (solves un-noded T-junctions).
3. Normal-offset wall exposure solver (exterior 300mm vs interior 150mm).
4. Stable 64-bit FNV-1a room content hashing.
5. Corner mitering and T-junction fill solver.
6. OBB furniture spatial room containment.
7. End-to-end PlanGeometryTracer pipeline on the 3-bedroom reference layout.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import sys
import pytest
from shapely.geometry import Polygon

# Ensure repository root and Plan directory are on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trace_engine.geometry_kernel import (
    CoordinateProjector,
    DEFAULT_WALL_THICKNESS,
    PlanGeometryTracer,
    clean_walls_graph,
    compute_face_content_hash,
    edge_key,
    host_openings_on_walls,
    inscribe_obb_furniture,
    solve_rooms_from_walls,
    solve_wall_exposure,
    solve_wall_fills,
)
from tests.test_trace_engine import reference_trace


def test_collinear_opening_remapping():
    """Verify that an opening hosted on a sub-edge of a collinear chain is preserved and correctly offset."""
    # Continuous wall from x=0 to x=6000, split at x=3000 into two segments: n1->n2, n2->n3
    node_map = {
        "n1": {"id": "n1", "x": 0.0, "y": 0.0},
        "n2": {"id": "n2", "x": 3000.0, "y": 0.0},
        "n3": {"id": "n3", "x": 6000.0, "y": 0.0},
    }
    raw_edges = [
        {"source": "n1", "target": "n2", "type": "internal"},
        {"source": "n2", "target": "n3", "type": "internal"},
    ]
    raw_graph = {"nodes": list(node_map.values()), "edges": raw_edges}

    # Clean and merge collinear segments
    cleaned = clean_walls_graph(raw_graph)
    edges = cleaned["graph"]["edges"]
    assert len(edges) == 1, "Expected collinear edges to merge into 1 edge"
    merged_edge = edges[0]
    assert merged_edge["source"] in ("n1", "n3") and merged_edge["target"] in ("n1", "n3")

    # A door located on the second segment (n2 -> n3) at x=4500 (distance 1500 from n2)
    door_opening = {
        "kind": "door",
        "edge_nodes": ["n2", "n3"],
        "center": [4500.0, 0.0],
        "length": 900.0,
    }

    projector = CoordinateProjector(
        {"x": 0.0, "y": 0.0, "width": 6000.0, "height": 6000.0},
        {"x": 0.0, "y": 0.0, "width": 6000.0, "height": 6000.0},
    )

    walls = [
        {
            "id": "wall_1",
            "startNodeId": merged_edge["source"],
            "endNodeId": merged_edge["target"],
            "type": "internal",
            "thickness": 150.0,
        }
    ]

    hosted = host_openings_on_walls(
        [door_opening],
        walls,
        node_map,
        projector,
        sub_edge_to_merged=cleaned["subEdgeToMerged"],
    )

    assert len(hosted) == 1, "Door on merged sub-edge must not be dropped!"
    h_door = hosted[0]
    assert h_door["parentId"] == "wall_1"
    # Canonical start is n1 (x=0). Center is at 4500. Width is 900. Clamped left offset = 4500 - 450 = 4050.
    assert abs(h_door["scalarOffset"] - 4050.0) < 5.0
    assert abs(h_door["distanceFromLowerNode"] - 4500.0) < 5.0


def test_un_noded_t_junction_room_recovery():
    """Verify that planar noding (unary_union) correctly recovers rooms even when a partition hits a wall without an existing node."""
    # 2 adjacent rooms: Room A (0..3000, 0..4000) and Room B (3000..6000, 0..4000)
    # The top wall is a single continuous edge from (0, 4000) to (6000, 4000) with NO node at (3000, 4000)
    nodes = {
        "n1": {"id": "n1", "x": 0.0, "y": 0.0},
        "n2": {"id": "n2", "x": 3000.0, "y": 0.0},
        "n3": {"id": "n3", "x": 6000.0, "y": 0.0},
        "n4": {"id": "n4", "x": 6000.0, "y": 4000.0},
        "n5": {"id": "n5", "x": 0.0, "y": 4000.0},
        # Note: no node at (3000, 4000)!
    }

    walls = [
        {"id": "w_bottom_1", "startNodeId": "n1", "endNodeId": "n2"},
        {"id": "w_bottom_2", "startNodeId": "n2", "endNodeId": "n3"},
        {"id": "w_right", "startNodeId": "n3", "endNodeId": "n4"},
        {"id": "w_top_continuous", "startNodeId": "n4", "endNodeId": "n5"},  # Continuous without mid-node!
        {"id": "w_left", "startNodeId": "n5", "endNodeId": "n1"},
        {"id": "w_partition", "startNodeId": "n2", "endNodeId": "n_top_fake"},  # Hits the top wall
    ]
    nodes["n_top_fake"] = {"id": "n_top_fake", "x": 3000.0, "y": 4000.0}

    projector = CoordinateProjector(
        {"x": 0.0, "y": 0.0, "width": 6000.0, "height": 4000.0},
        {"x": 0.0, "y": 0.0, "width": 6000.0, "height": 4000.0},
    )

    zones_list = [
        {"name": "Room A", "center": [1500.0, 2000.0]},
        {"name": "Room B", "center": [4500.0, 2000.0]},
    ]

    rooms = solve_rooms_from_walls(walls, nodes, zones_list, projector)

    # In naive polygonize without unary_union, this produces only 1 room or fails at the dead-end branch.
    # With planar noding, both rooms are extracted.
    assert len(rooms) == 2, f"Expected 2 rooms, got {len(rooms)}"
    names = {r["name"] for r in rooms}
    assert "Room A" in names and "Room B" in names
    for r in rooms:
        assert abs(r["areaM2"] - 12.0) < 0.2  # 3m x 4m = 12 m^2


def test_solve_wall_exposure():
    """Verify normal-offset spatial probing correctly classifies exterior vs interior walls."""
    # Room 1: (0..3000, 0..4000), Room 2: (3000..6000, 0..4000)
    poly1 = Polygon([(0, 0), (3000, 0), (3000, 4000), (0, 4000)])
    poly2 = Polygon([(3000, 0), (6000, 0), (6000, 4000), (3000, 4000)])
    room_polys = [poly1, poly2]

    node_map = {
        "n1": {"id": "n1", "x": 0.0, "y": 0.0},
        "n2": {"id": "n2", "x": 3000.0, "y": 0.0},
        "n3": {"id": "n3", "x": 6000.0, "y": 0.0},
        "n4": {"id": "n4", "x": 6000.0, "y": 4000.0},
        "n5": {"id": "n5", "x": 3000.0, "y": 4000.0},
        "n6": {"id": "n6", "x": 0.0, "y": 4000.0},
    }

    walls = [
        {"id": "w1", "startNodeId": "n1", "endNodeId": "n2"},  # Perimeter south 1
        {"id": "w2", "startNodeId": "n2", "endNodeId": "n3"},  # Perimeter south 2
        {"id": "w3", "startNodeId": "n3", "endNodeId": "n4"},  # Perimeter east
        {"id": "w4", "startNodeId": "n4", "endNodeId": "n5"},  # Perimeter north 2
        {"id": "w5", "startNodeId": "n5", "endNodeId": "n6"},  # Perimeter north 1
        {"id": "w6", "startNodeId": "n6", "endNodeId": "n1"},  # Perimeter west
        {"id": "w_part", "startNodeId": "n2", "endNodeId": "n5"},  # Interior partition (x=3000)
    ]

    classified = solve_wall_exposure(walls, node_map, room_polys, probe_offset_mm=50.0)

    for w in classified:
        if w["id"] == "w_part":
            assert w["type"] == "internal", f"Partition wall should be internal, got {w['type']}"
            assert w["thickness"] == DEFAULT_WALL_THICKNESS["internalMm"]
            assert w["leftRoom"] is True and w["rightRoom"] is True
        else:
            assert w["type"] == "external", f"Perimeter wall {w['id']} should be external, got {w['type']}"
            assert w["thickness"] == DEFAULT_WALL_THICKNESS["externalMm"]


def test_face_content_hashing():
    """Verify that 64-bit FNV-1a face hashing is deterministic, format-compliant, and stable to sub-millimeter noise."""
    coords1 = [(0.0, 0.0), (3000.0, 0.0), (3000.0, 4000.0), (0.0, 4000.0), (0.0, 0.0)]
    h1 = compute_face_content_hash(coords1)

    assert re.match(r"^face:[0-9a-f]{16}$", h1), f"Hash '{h1}' does not match face:16hex format"

    # Perturb coordinates within tolerance (< 0.04 mm)
    coords2 = [(0.02, -0.01), (3000.01, 0.04), (2999.98, 4000.02), (-0.03, 3999.99), (0.02, -0.01)]
    h2 = compute_face_content_hash(coords2)
    assert h1 == h2, "Quantized FNV-1a hash should be identical for sub-0.05mm jitter"

    # Different polygon should yield different hash
    coords3 = [(0.0, 0.0), (5000.0, 0.0), (5000.0, 4000.0), (0.0, 4000.0), (0.0, 0.0)]
    h3 = compute_face_content_hash(coords3)
    assert h1 != h3


def test_solve_wall_fills():
    """Verify corner miter and T-junction classification for clean graphics."""
    node_map = {
        "c1": {"id": "c1", "x": 0.0, "y": 0.0},
        "c2": {"id": "c2", "x": 3000.0, "y": 0.0},
        "c3": {"id": "c3", "x": 3000.0, "y": 3000.0},
        "t_mid": {"id": "t_mid", "x": 3000.0, "y": 1500.0},
        "t_branch": {"id": "t_branch", "x": 1000.0, "y": 1500.0},
    }
    walls = [
        {"id": "w1", "startNodeId": "c1", "endNodeId": "c2", "thickness": 300.0},
        {"id": "w2", "startNodeId": "c2", "endNodeId": "t_mid", "thickness": 300.0},
        {"id": "w3", "startNodeId": "t_mid", "endNodeId": "c3", "thickness": 300.0},
        {"id": "w4", "startNodeId": "t_mid", "endNodeId": "t_branch", "thickness": 150.0},
    ]

    fills = solve_wall_fills(walls, node_map)
    kinds = {f["junctionId"]: f["kind"] for f in fills}
    assert kinds.get("c2") == "miter_corner", "c2 is a 2-wall corner miter"
    assert kinds.get("t_mid") == "t_junction", "t_mid is a 3-wall T-junction"


def test_obb_furniture_inscription():
    """Verify OBB furniture is placed and mapped into the correct containing room."""
    poly = Polygon([(0, 0), (4000, 0), (4000, 4000), (0, 4000)])
    rooms = [{"id": "face:test_room_1", "polygon": poly, "vertices": list(poly.exterior.coords)}]

    projector = CoordinateProjector(
        {"x": 0.0, "y": 0.0, "width": 4000.0, "height": 4000.0},
        {"x": 0.0, "y": 0.0, "width": 4000.0, "height": 4000.0},
    )

    obb_in = [
        {
            "class": "bed_double",
            "center": [2000.0, 2000.0],
            "size": [1800.0, 2000.0],
            "rotation": math.radians(90.0),
        }
    ]

    furniture = inscribe_obb_furniture(obb_in, rooms, projector)
    assert len(furniture) == 1
    item = furniture[0]
    assert item["roomId"] == "face:test_room_1"
    assert item["class"] == "bed_double"
    assert item["transform"]["rotationDeg"] == 90.0


def test_end_to_end_plan_tracer_reference_layout():
    """Verify complete PlanGeometryTracer pipeline on the full 3-bedroom reference floorplan."""
    raw_payload = reference_trace(noise_mm=0.0)
    tracer = PlanGeometryTracer()

    result = tracer.trace(raw_payload, image_size=(15240, 12192), mm_per_unit=1.0)

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["wallCount"] >= 15
    assert metrics["roomCount"] == 12, f"Expected 12 rooms, got {metrics['roomCount']}"
    assert metrics["doorCount"] == 13, f"Expected 13 doors, got {metrics['doorCount']}"
    assert metrics["windowCount"] == 13, f"Expected 13 windows, got {metrics['windowCount']}"
    assert len(result["miterFills"]) > 0

    # Ensure all rooms have valid face content hash IDs
    for rm in result["rooms"]:
        assert rm["id"].startswith("face:")
        assert rm["areaM2"] > 1.0


def test_plan_trace_to_topospatial_compilation():
    """Verify that PlanGeometryTracer output converts cleanly to TraceResult and compiles into TopoSpatial CAD changes."""
    from trace_engine.compiler import compile_trace

    raw_payload = reference_trace(noise_mm=0.0)
    tracer = PlanGeometryTracer()
    cad_output = tracer.trace(raw_payload, image_size=(15240, 12192), mm_per_unit=1.0)

    trace_result = tracer.to_trace_result(cad_output)

    # Compile the trace into TopoSpatial CAD changes
    compiled = compile_trace(trace_result)
    summary = compiled.summary()

    counts = summary["counts"]
    assert counts.get("wall", 0) > 0, "Expected compiled walls"
    assert counts.get("room", 0) == 12, f"Expected 12 compiled rooms, got {counts.get('room')}"
    assert counts.get("door", 0) == 13, f"Expected 13 compiled doors, got {counts.get('door')}"
    assert counts.get("window", 0) == 13, f"Expected 13 compiled windows, got {counts.get('window')}"
    assert len(summary["warnings"]) == 0, f"Expected 0 compiler warnings, got: {summary['warnings']}"

