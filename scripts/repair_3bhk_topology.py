"""Promote the scaled 3BHK room candidates into durable semantic topology."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adapters.adapter_manager import get_adapter, shutdown_all  # noqa: E402
from core.config import get_config  # noqa: E402
from topology_engine import ChangeDocument, TopologyEngine  # noqa: E402
from topology_engine.navigation import OUTSIDE_SPACE_ID  # noqa: E402
from topology_engine.cad_bridge import CADTopologyBridge  # noqa: E402

EXPECTED_DRAWING = "3BHK_House_Plan_Topology_Repaired.dwg"

DOORS = [
    {
        "id": "door:ground:main_entrance",
        "label": "Main Entrance",
        "handles": ["DE", "DF"],
        "endpoints": [OUTSIDE_SPACE_ID, "space:ground:foyer_lobby"],
        "storey": "ground",
    },
    {
        "id": "door:ground:bedroom_1",
        "label": "Bedroom 1 Door",
        "handles": ["E1", "E2"],
        "endpoints": ["space:ground:bedroom_1_guest_elder", "space:ground:staircase"],
        "storey": "ground",
    },
    {
        "id": "door:ground:ensuite_1",
        "label": "Ensuite 1 Door",
        "handles": ["E4", "E5"],
        "endpoints": ["space:ground:ensuite_1", "space:ground:passage_1"],
        "storey": "ground",
    },
    {
        "id": "door:ground:powder_room",
        "label": "Powder Room Door",
        "handles": ["E6", "E7"],
        "endpoints": ["space:ground:powder_rm", "space:ground:passage_2"],
        "storey": "ground",
    },
    {
        "id": "door:ground:utility_exit",
        "label": "Utility Service Exit",
        "handles": ["E8", "E9"],
        "endpoints": ["space:ground:utility_yard", OUTSIDE_SPACE_ID],
        "storey": "ground",
    },
    {
        "id": "door:first:master_bedroom",
        "label": "Master Bedroom Door",
        "handles": ["1BD", "1BE"],
        "endpoints": ["space:first:family_lounge", "space:first:master_bedroom_suite"],
        "storey": "first",
    },
    {
        "id": "door:first:walk_in_dress",
        "label": "Walk-In Dress Door",
        "handles": ["1C0", "1C1"],
        "endpoints": ["space:first:master_bedroom_suite", "space:first:walk_in_dress"],
        "storey": "first",
    },
    {
        "id": "door:first:bedroom_3",
        "label": "Bedroom 3 Door",
        "handles": ["1C3", "1C4"],
        "endpoints": ["space:first:bedroom_3", "space:first:family_lounge"],
        "storey": "first",
    },
    {
        "id": "door:first:ensuite_3",
        "label": "Ensuite 3 Door",
        "handles": ["1C6", "1C7"],
        "endpoints": ["space:first:bedroom_3", "space:first:ensuite_3"],
        "storey": "first",
    },
]

LABEL_OVERRIDES = {
    "CENTRAL DINING": "Central Dining / Modular Kitchen",
    "SPACE 1": "Passage 1",
    "SPACE 2": "Passage 2",
    "UP (18 RISERS)": "Staircase",
}
TYPE_OVERRIDES = {
    "WALK-IN DRESS": "storage",
    "SPACE 1": "corridor",
    "SPACE 2": "corridor",
    "UP (18 RISERS)": "staircase",
}
ZONE_BY_TYPE = {
    "bedroom": "sleeping",
    "bathroom": "service",
    "utility": "service",
    "storage": "service",
    "corridor": "circulation",
    "staircase": "circulation",
    "living_room": "living",
    "dining_room": "living",
    "kitchen": "service",
}


def _slug(value: str) -> str:
    return re.sub(r"(^_+|_+$)", "", re.sub(r"[^a-z0-9]+", "_", value.casefold()))


def _changes(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    for index, candidate in enumerate(analysis.get("candidates", []), start=1):
        original_label = str(candidate.get("suggested_label") or f"Space {index}")
        label = LABEL_OVERRIDES.get(original_label.upper(), original_label.title())
        space_type = TYPE_OVERRIDES.get(
            original_label.upper(), str(candidate.get("suggested_type") or "other")
        )
        boundary = candidate["boundary"]
        first_floor = min(float(point[0]) for point in boundary) >= 15000.0
        storey_key = "first" if first_floor else "ground"
        storey_name = "First Floor" if first_floor else "Ground Floor"
        changes.append(
            {
                "op": "annotate",
                "@id": f"space:{storey_key}:{_slug(label)}",
                "@type": "top:Room",
                "label": label,
                "targets": {"candidate_id": candidate["candidate_id"]},
                "properties": {
                    "space_type": space_type,
                    "zone": ZONE_BY_TYPE.get(space_type, "unassigned"),
                    "storey_id": f"storey:{storey_key}",
                    "storey_name": storey_name,
                    "source_candidate_id": candidate["candidate_id"],
                    "ventilation": (
                        "natural_required"
                        if space_type in {"bedroom", "living_room"}
                        else "natural_preferred"
                    ),
                },
                "representation": "standard",
            }
        )
    return changes


def _door_changes(snapshot: Any, analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    available_handles = {entity.handle for entity in snapshot.entities}
    node_ids = {node["@id"] for node in analysis["graph"].get("@graph", [])}
    changes: List[Dict[str, Any]] = []
    for door in DOORS:
        missing_handles = sorted(set(door["handles"]) - available_handles)
        if missing_handles:
            raise RuntimeError(
                f"Door {door['id']} is missing CAD handles: {', '.join(missing_handles)}"
            )
        missing_spaces = sorted(
            endpoint
            for endpoint in door["endpoints"]
            if endpoint != OUTSIDE_SPACE_ID and endpoint not in node_ids
        )
        if missing_spaces:
            raise RuntimeError(
                f"Door {door['id']} has unknown endpoints: {', '.join(missing_spaces)}"
            )
        changes.append(
            {
                "op": "annotate",
                "@id": door["id"],
                "@type": "top:Door",
                "label": door["label"],
                "targets": {
                    "handles": door["handles"],
                    "grouping": "single_object",
                },
                "properties": {
                    **_door_width_properties(snapshot, door),
                    "endpoint_space_ids": door["endpoints"],
                    "storey_id": f"storey:{door['storey']}",
                    "storey_name": (
                        "Ground Floor" if door["storey"] == "ground" else "First Floor"
                    ),
                    "source_handles": door["handles"],
                    "status": "confirmed",
                },
                "representation": "standard",
            }
        )
    return changes


def _door_width_properties(snapshot: Any, door: Dict[str, Any]) -> Dict[str, Any]:
    """Measure the designated source door leaf, not its swing arc bounding box."""
    entity = next(item for item in snapshot.entities if item.handle == door["handles"][0])
    geometry = entity.geometry
    if geometry.get("kind") != "line":
        raise ValueError(f"Expected a door-leaf line at handle {entity.handle}")
    width = math.dist(geometry["start"][:2], geometry["end"][:2])
    if not 500 <= width <= 2000:
        raise ValueError(f"Implausible door-leaf width {width} mm at {entity.handle}")
    return {
        "nominal_width_mm": round(width, 3),
        "width_source": f"CAD door-leaf line {entity.handle}; clear opening not surveyed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-walls", action="store_true")
    parser.add_argument("--repair-doors", action="store_true")
    parser.add_argument("--repair-door-widths", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--expected-drawing", default=EXPECTED_DRAWING)
    args = parser.parse_args()

    try:
        import pythoncom

        pythoncom.CoInitialize()
    except ImportError:
        pythoncom = None

    bridge = CADTopologyBridge()
    topology_config = get_config().topology
    engine = TopologyEngine(
        snap_tolerance_mm=topology_config.snap_tolerance_mm,
        max_opening_gap_mm=topology_config.max_opening_gap_mm,
        min_room_dimension_mm=topology_config.min_room_dimension_mm,
    )
    try:
        adapter = get_adapter(only_if_running=True)
        snapshot = bridge.snapshot(
            adapter, scope="all", expected_drawing=args.expected_drawing
        )
        analysis = engine.analyze(snapshot)
        committed = [
            node
            for node in analysis["graph"].get("@graph", [])
            if node.get("@type") in {"top:Room", "top:Space"}
        ]
        if args.repair_door_widths:
            changes = [{
                "op": "update", "@id": door["id"],
                "properties": _door_width_properties(snapshot, door),
            } for door in DOORS]
            document = ChangeDocument.model_validate({
                "@context": {"top": "http://w3id.org/topologicpy#"},
                "base_revision": snapshot.revision, "changes": changes,
            })
            operations, _, warnings = engine.plan_changes(document, snapshot, analysis)
            preview = {"drawing": snapshot.drawing_name, "base_revision": snapshot.revision,
                       "operations": operations, "warnings": warnings, "mutated": False}
            if not args.apply:
                print(json.dumps(preview, indent=2))
                return 0
            # Revision-bound, metadata-only batch; no visible geometry changes.
            current = bridge.snapshot(adapter, expected_drawing=args.expected_drawing)
            if current.revision != snapshot.revision:
                raise RuntimeError("Drawing changed since door-width preview")
            bridge.apply_operations(adapter, operations, refresh=False,
                                    rollback_revision=snapshot.revision)
            try:
                updated = bridge.snapshot(adapter, expected_drawing=args.expected_drawing)
                verified = engine.analyze(updated)
                nodes = {node["@id"]: node for node in verified["graph"]["@graph"]}
                for change in changes:
                    assert nodes[change["@id"]]["cad:properties"]["nominal_width_mm"] == change["properties"]["nominal_width_mm"]
                sidecars = bridge.write_sidecars(updated, verified["graph"], engine.to_turtle(verified["graph"]))
                adapter._get_document("repair_3bhk_door_widths").Save()
            except Exception:
                bridge.rollback_last_transaction(adapter, expected_revision=snapshot.revision)
                raise
            print(json.dumps({"success": True, "mutated": True, "door_count": len(changes),
                              "revision": updated.revision, "graph_revision": verified["graph"]["cad:graphRevision"],
                              "sidecars": sidecars}, indent=2))
            return 0
        if committed:
            if args.report:
                print(
                    json.dumps(
                        {
                            "drawing": snapshot.drawing_name,
                            "revision": snapshot.revision,
                            "graph_revision": analysis["graph"].get("cad:graphRevision"),
                            "node_count": analysis["node_count"],
                            "relation_count": analysis["relation_count"],
                            "storeys": analysis.get("storeys", []),
                            "discoveries": [
                                item
                                for item in analysis.get("discoveries", [])
                                if item.get("kind") in {"door", "opening"}
                            ],
                        },
                        indent=2,
                    )
                )
                return 0
            if args.repair_doors:
                existing_door_ids = {
                    node["@id"]
                    for node in analysis["graph"].get("@graph", [])
                    if node.get("@type") == "top:Door"
                }
                requested_ids = {door["id"] for door in DOORS}
                already_committed = sorted(existing_door_ids & requested_ids)
                if already_committed:
                    raise RuntimeError(
                        "Refusing duplicate door repair; already committed: "
                        + ", ".join(already_committed)
                    )
                changes = _door_changes(snapshot, analysis)
                document = ChangeDocument.model_validate(
                    {
                        "@context": {"top": "http://w3id.org/topologicpy#"},
                        "base_revision": snapshot.revision,
                        "changes": changes,
                    }
                )
                operations, _, warnings = engine.plan_changes(
                    document, snapshot, analysis
                )
                door_preview = {
                    "success": True,
                    "drawing": snapshot.drawing_name,
                    "base_revision": snapshot.revision,
                    "door_count": len(changes),
                    "operation_count": len(operations),
                    "warnings": warnings,
                    "mutated": False,
                }
                if not args.apply:
                    print(json.dumps(door_preview, indent=2))
                    return 0
                cad_result = bridge.apply_operations(
                    adapter,
                    operations,
                    refresh=False,
                    rollback_revision=snapshot.revision,
                )
                try:
                    updated = bridge.snapshot(
                        adapter, scope="all", expected_drawing=args.expected_drawing
                    )
                    verified = engine.analyze(updated)
                    nodes = verified["graph"].get("@graph", [])
                    door_nodes = [
                        node
                        for node in nodes
                        if node.get("@type") == "top:Door"
                        and node.get("@id") in requested_ids
                    ]
                    connections = [
                        node for node in nodes if node.get("@type") == "top:Connection"
                    ]
                    portal_issues = [
                        issue
                        for issue in verified.get("issues", [])
                        if str(issue.get("code", "")).startswith(("PORTAL_", "DOOR_"))
                    ]
                    if len(door_nodes) != len(DOORS):
                        raise RuntimeError(
                            f"Post-apply verification found {len(door_nodes)} of "
                            f"{len(DOORS)} doors"
                        )
                    if len(connections) < len(DOORS) or portal_issues:
                        raise RuntimeError(
                            f"Portal verification failed: {len(connections)} connections, "
                            f"issues={portal_issues}"
                        )
                    turtle = engine.to_turtle(verified["graph"])
                    sidecars = bridge.write_sidecars(updated, verified["graph"], turtle)
                    adapter._get_document("repair_3bhk_doors").Save()
                    adapter.refresh_view()
                except Exception:
                    bridge.rollback_last_transaction(
                        adapter, expected_revision=snapshot.revision
                    )
                    raise
                print(
                    json.dumps(
                        {
                            **door_preview,
                            "mutated": True,
                            "revision": updated.revision,
                            "graph_revision": verified["graph"].get("cad:graphRevision"),
                            "verified_door_count": len(door_nodes),
                            "connection_count": len(connections),
                            "portal_issue_count": len(portal_issues),
                            "node_count": verified["node_count"],
                            "relation_count": verified["relation_count"],
                            "cad": cad_result,
                            "sidecars": sidecars,
                        },
                        indent=2,
                    )
                )
                return 0
            if not args.repair_walls:
                raise RuntimeError(
                    f"Refusing duplicate repair: {len(committed)} spaces are already committed"
                )
            nodes_by_id = {
                node["@id"]: node for node in analysis["graph"].get("@graph", [])
            }
            operations: List[Dict[str, Any]] = []
            for room in committed:
                boundary = room.get("cad:geometry", {}).get("boundary")
                if boundary:
                    operations.extend(
                        engine._derive_adjacent_wall_updates(
                            room["@id"], boundary, snapshot, nodes_by_id
                        )
                    )
            operations = engine._merge_wall_update_operations(operations)
            wall_preview = {
                "success": True,
                "drawing": snapshot.drawing_name,
                "base_revision": snapshot.revision,
                "committed_rooms": len(committed),
                "wall_relationship_operations": len(operations),
                "mutated": False,
            }
            if not args.apply:
                print(json.dumps(wall_preview, indent=2))
                return 0
            cad_result = bridge.apply_operations(
                adapter,
                operations,
                refresh=False,
                rollback_revision=snapshot.revision,
            )
            try:
                updated = bridge.snapshot(
                    adapter, scope="all", expected_drawing=args.expected_drawing
                )
                verified = engine.analyze(updated)
                wall_nodes = [
                    node
                    for node in verified["graph"].get("@graph", [])
                    if node.get("@type") == "top:Wall"
                ]
                bounded_walls = [
                    node for node in wall_nodes if node.get("cad:boundingRooms")
                ]
                if not bounded_walls:
                    raise RuntimeError("Post-apply verification found no bounded walls")
                turtle = engine.to_turtle(verified["graph"])
                sidecars = bridge.write_sidecars(updated, verified["graph"], turtle)
                adapter._get_document("repair_3bhk_walls").Save()
                adapter.refresh_view()
            except Exception:
                bridge.rollback_last_transaction(
                    adapter, expected_revision=snapshot.revision
                )
                raise
            print(
                json.dumps(
                    {
                        **wall_preview,
                        "mutated": True,
                        "revision": updated.revision,
                        "graph_revision": verified["graph"].get("cad:graphRevision"),
                        "wall_node_count": len(wall_nodes),
                        "bounded_wall_count": len(bounded_walls),
                        "relation_count": verified["relation_count"],
                        "cad": cad_result,
                        "sidecars": sidecars,
                    },
                    indent=2,
                )
            )
            return 0
        changes = _changes(analysis)
        if len(changes) != 16:
            raise RuntimeError(
                f"Expected 16 verified room candidates, found {len(changes)}"
            )
        document = ChangeDocument.model_validate(
            {
                "@context": {"top": "http://w3id.org/topologicpy#"},
                "base_revision": snapshot.revision,
                "changes": changes,
            }
        )
        operations, diff, warnings = engine.plan_changes(
            document, snapshot, analysis
        )
        preview = {
            "success": True,
            "drawing": snapshot.drawing_name,
            "base_revision": snapshot.revision,
            "candidate_count": len(changes),
            "operation_count": len(operations),
            "room_boundary_operations": sum(
                item.get("kind") == "create_room_boundary" for item in operations
            ),
            "wall_relationship_operations": sum(
                item.get("kind") == "update_wall_rooms" for item in operations
            ),
            "warnings": warnings,
            "mutated": False,
        }
        if not args.apply:
            print(json.dumps(preview, indent=2))
            return 0

        cad_result = bridge.apply_operations(
            adapter,
            operations,
            refresh=False,
            rollback_revision=snapshot.revision,
        )
        try:
            updated = bridge.snapshot(
                adapter, scope="all", expected_drawing=args.expected_drawing
            )
            verified = engine.analyze(updated)
            nodes = verified["graph"].get("@graph", [])
            rooms = [node for node in nodes if node.get("@type") == "top:Room"]
            bedrooms = [
                node
                for node in rooms
                if node.get("cad:properties", {}).get("space_type") == "bedroom"
            ]
            storeys = {
                node.get("cad:properties", {}).get("storey_id") for node in rooms
            }
            if len(rooms) != 16 or len(bedrooms) != 3:
                raise RuntimeError(
                    f"Post-apply verification failed: {len(rooms)} rooms, "
                    f"{len(bedrooms)} bedrooms"
                )
            if storeys != {"storey:ground", "storey:first"}:
                raise RuntimeError(f"Unexpected storey assignments: {sorted(storeys)}")
            turtle = engine.to_turtle(verified["graph"])
            sidecars = bridge.write_sidecars(updated, verified["graph"], turtle)
            adapter._get_document("repair_3bhk_topology").Save()
            adapter.refresh_view()
        except Exception:
            bridge.rollback_last_transaction(
                adapter, expected_revision=snapshot.revision
            )
            raise

        print(
            json.dumps(
                {
                    **preview,
                    "mutated": True,
                    "revision": updated.revision,
                    "graph_revision": verified["graph"].get("cad:graphRevision"),
                    "room_count": len(rooms),
                    "bedroom_count": len(bedrooms),
                    "storeys": sorted(storeys),
                    "node_count": verified["node_count"],
                    "relation_count": verified["relation_count"],
                    "cad": cad_result,
                    "sidecars": sidecars,
                },
                indent=2,
            )
        )
        return 0
    finally:
        shutdown_all()
        if pythoncom is not None:
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
