"""Regression coverage for deterministic shared physical-wall planning."""

from __future__ import annotations

from typing import Any, Dict, Sequence
from unittest.mock import MagicMock

import pytest
from pydantic import TypeAdapter

from design_engine.models import DesignRequest
from design_engine.orchestrator import DesignOrchestrator
from topology_engine import (
    ChangeDocument,
    DrawingSnapshot,
    EntitySnapshot,
    TopologyEngine,
)
from topology_engine.cad_bridge import CADTopologyBridge
from topology_engine.wall_network import WALL_NORMALIZATION_TOLERANCE_MM

REQUEST_ADAPTER = TypeAdapter(DesignRequest)


def _empty() -> DrawingSnapshot:
    return DrawingSnapshot("shared.dwg", "C:/shared.dwg", "mm", [])


def _room(room_id: str, origin_x: float, thickness: float = 200.0) -> Dict[str, Any]:
    return {
        "op": "create",
        "@id": room_id,
        "@type": "top:Room",
        "label": room_id.rsplit(":", 1)[-1].title(),
        "geometry": {
            "origin": [origin_x, 0],
            "clear_width": 4000,
            "clear_depth": 4000,
            "wall_thickness": thickness,
        },
    }


def _document(
    changes: Sequence[Dict[str, Any]],
) -> tuple[ChangeDocument, DrawingSnapshot]:
    snapshot = _empty()
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": list(changes),
        }
    )
    return document, snapshot


def _planned(changes: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    document, snapshot = _document(changes)
    engine = TopologyEngine()
    operations, _, _ = engine.plan_changes(document, snapshot, engine.analyze(snapshot))
    return operations


def _walls(operations: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [item for item in operations if item.get("ontology_class") == "top:Wall"]


def test_two_adjacent_rooms_compile_seven_physical_walls() -> None:
    operations = _planned([_room("urn:room:a", 0), _room("urn:room:b", 4200)])
    walls = _walls(operations)
    shared = [wall for wall in walls if len(wall["bounding_room_ids"]) == 2]

    assert len(walls) == 7
    assert len(shared) == 1
    assert shared[0]["bounding_room_ids"] == ["urn:room:a", "urn:room:b"]


def test_three_adjacent_rooms_compile_each_partition_once() -> None:
    operations = _planned(
        [
            _room("urn:room:a", 0),
            _room("urn:room:b", 4200),
            _room("urn:room:c", 8400),
        ]
    )
    walls = _walls(operations)

    assert len(walls) == 10
    assert sum(len(wall["bounding_room_ids"]) == 2 for wall in walls) == 2


def test_reversed_explicit_walls_are_duplicate_at_validation() -> None:
    snapshot = _empty()
    orchestrator = DesignOrchestrator(bridge=_PlanningBridge(snapshot))
    result = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "create",
                "base_revision": snapshot.revision,
                "changes": [
                    _explicit_wall("urn:wall:a", [0, 0], [4000, 0]),
                    _explicit_wall("urn:wall:b", [4000, 0], [0, 0]),
                ],
            }
        ),
        _Adapter(),
    )

    assert result["success"] is False
    assert {problem["code"] for problem in result["problems"]} == {"DUPLICATE_WALL"}


def test_sub_tolerance_coordinate_noise_merges_explicit_and_implicit_wall() -> None:
    noise = WALL_NORMALIZATION_TOLERANCE_MM / 10.0
    explicit = _explicit_wall(
        "urn:wall:right", [4100 + noise, -200], [4100 - noise, 4200]
    )
    walls = _walls(_planned([_room("urn:room:a", 0), explicit]))

    assert len(walls) == 4
    assert sum(wall["semantic_id"] == "urn:wall:right" for wall in walls) == 1


def test_same_centerline_with_different_thickness_is_a_spec_conflict() -> None:
    snapshot = _empty()
    orchestrator = DesignOrchestrator(bridge=_PlanningBridge(snapshot))
    result = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "create",
                "base_revision": snapshot.revision,
                "changes": [
                    _explicit_wall("urn:wall:150", [0, 0], [4000, 0], 150),
                    _explicit_wall("urn:wall:230", [0, 0], [4000, 0], 230),
                ],
            }
        ),
        _Adapter(),
    )

    problem = next(
        item for item in result["problems"] if item["code"] == "WALL_SPEC_CONFLICT"
    )
    assert problem["classification"] == "NEEDS_LLM_DECISION"


def test_explicit_and_room_implicit_wall_share_the_explicit_identity() -> None:
    explicit = _explicit_wall("urn:wall:partition", [4100, -200], [4100, 4200])
    walls = _walls(_planned([_room("urn:room:a", 0), explicit]))

    assert len(walls) == 4
    partition = next(
        wall for wall in walls if wall["semantic_id"] == "urn:wall:partition"
    )
    assert partition["bounding_room_ids"] == ["urn:room:a"]
    assert "urn:room:a:wall:2" in partition["aliases"]


def test_door_legacy_host_resolves_to_single_shared_wall() -> None:
    door = {
        "op": "create",
        "@id": "urn:door:shared",
        "@type": "top:Door",
        "geometry": {
            "host_wall_id": "urn:room:a:wall:2",
            "offset": 1200,
            "width": 900,
            "hinge": "left",
            "swing": "in",
        },
    }
    operations = _planned([_room("urn:room:a", 0), _room("urn:room:b", 4200), door])
    shared = next(
        wall
        for wall in _walls(operations)
        if wall["bounding_room_ids"] == ["urn:room:a", "urn:room:b"]
    )
    planned_door = next(
        item for item in operations if item.get("ontology_class") == "top:Door"
    )

    assert planned_door["geometry"]["host_wall_id"] == shared["semantic_id"]


def test_reversed_room_wall_alias_preserves_opening_position_and_hinge() -> None:
    door = {
        "op": "create",
        "@id": "urn:door:shared",
        "@type": "top:Door",
        "geometry": {
            "host_wall_id": "urn:room:b:wall:4",
            "offset": 1200,
            "width": 900,
            "hinge": "left",
            "swing": "in",
        },
    }
    operations = _planned([_room("urn:room:a", 0), _room("urn:room:b", 4200), door])
    planned_door = next(
        item for item in operations if item.get("ontology_class") == "top:Door"
    )

    assert planned_door["geometry"]["offset"] == pytest.approx(2300)
    assert planned_door["geometry"]["hinge"] == "right"


def test_isolated_room_keeps_boundary_and_four_explicit_planned_walls() -> None:
    operations = _planned([_room("urn:room:solo", 0)])

    assert len(_walls(operations)) == 4
    assert sum(item.get("ontology_class") == "top:Room" for item in operations) == 1


@pytest.mark.parametrize(
    ("requested", "native_available", "expected"),
    [("standard", False, "standard"), ("auto", True, "native_aec")],
)
def test_normalized_walls_preserve_standard_and_native_representation_flow(
    requested: str, native_available: bool, expected: str
) -> None:
    change = _room("urn:room:a", 0)
    change["representation"] = requested
    operations = _planned([change])
    adapter = MagicMock()
    adapter.get_architecture_capabilities.side_effect = lambda include_styles: {
        "native_aec": native_available,
        **({"styles": {"wall": ["Standard"]}} if include_styles else {}),
    }

    CADTopologyBridge().resolve_operation_representations(
        adapter, operations, {"graph": {"@graph": []}}
    )

    assert {wall["representation"] for wall in _walls(operations)} == {expected}


def test_preview_and_apply_use_the_same_normalized_wall_count() -> None:
    snapshot = _empty()
    bridge = _PlanningBridge(snapshot)
    orchestrator = DesignOrchestrator(bridge=bridge)
    preview = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "create",
                "task_id": "shared-preview",
                "detail_level": "debug",
                "base_revision": snapshot.revision,
                "changes": [_room("urn:room:a", 0), _room("urn:room:b", 4200)],
            }
        ),
        _Adapter(),
    )
    apply_result = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "transaction_id": preview["transaction_id"],
            }
        ),
        _Adapter(),
    )

    assert preview["changed"] == {"room": 2, "wall": 7}
    assert len(_walls(preview["operations"])) == 7
    assert len(_walls(bridge.applied)) == 7
    assert apply_result["status"] == "applied"


def test_apply_snapshot_reanalysis_preserves_one_shared_semantic_wall() -> None:
    snapshot = _empty()
    bridge = _MaterializingBridge(snapshot)
    orchestrator = DesignOrchestrator(bridge=bridge)
    changes = [
        _room("urn:room:a", 0),
        _room("urn:room:b", 4200),
        {
            "op": "create",
            "@id": "urn:door:shared",
            "@type": "top:Door",
            "geometry": {
                "host_wall_id": "urn:room:a:wall:2",
                "offset": 1200,
                "width": 900,
                "hinge": "left",
                "swing": "in",
            },
        },
    ]
    preview = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "create",
                "base_revision": snapshot.revision,
                "changes": changes,
            }
        ),
        _Adapter(),
    )
    applied = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "transaction_id": preview["transaction_id"],
            }
        ),
        _Adapter(),
    )
    first = orchestrator.engine.analyze(bridge.snapshot_value)
    second = orchestrator.engine.analyze(bridge.snapshot_value)
    walls = [
        node for node in first["graph"]["@graph"] if node.get("@type") == "top:Wall"
    ]
    shared = [node for node in walls if len(node.get("cad:boundingRooms", [])) == 2]

    assert applied["status"] == "applied"
    assert len(walls) == 7
    assert len(shared) == 1
    assert first["graph"] == second["graph"]
    assert orchestrator.engine.to_turtle(first["graph"]) == (
        orchestrator.engine.to_turtle(second["graph"])
    )


def test_shared_wall_semantics_reconstruct_adjacency_connectivity_and_exports() -> None:
    room_a = _room_entity("urn:room:a", "10", 0)
    room_b = _room_entity("urn:room:b", "11", 4200)
    wall_geometry = {
        "start": [4100, -200],
        "end": [4100, 4200],
        "thickness": 200,
        "height": 3000,
        "style": "Standard",
    }
    wall = EntitySnapshot(
        "12",
        "AcDbPolyline",
        "AI-WALLS",
        {"kind": "line", **wall_geometry},
        {
            "semantic_id": "urn:wall:shared",
            "ontology_class": "top:Wall",
            "managed": True,
            "room_ids": ["urn:room:a", "urn:room:b"],
            "geometry": wall_geometry,
        },
    )
    door_geometry = {
        "host_wall_id": "urn:wall:shared",
        "offset": 1200,
        "width": 900,
        "height": 2100,
        "hinge": "left",
        "swing": "in",
    }
    door = EntitySnapshot(
        "13",
        "AcDbLine",
        "AI-DOORS",
        {"kind": "point", "position": [4100, 1200, 0]},
        {
            "semantic_id": "urn:door:shared",
            "ontology_class": "top:Door",
            "managed": True,
            "host_wall_id": "urn:wall:shared",
            "geometry": door_geometry,
        },
    )
    snapshot = DrawingSnapshot(
        "shared.dwg", "C:/shared.dwg", "mm", [room_a, room_b, wall, door]
    )
    engine = TopologyEngine()

    first = engine.analyze(snapshot)
    second = engine.analyze(snapshot)
    graph = first["graph"]
    shared_node = next(
        node for node in graph["@graph"] if node["@id"] == "urn:wall:shared"
    )
    room_a_node = next(node for node in graph["@graph"] if node["@id"] == "urn:room:a")

    assert first == second
    assert shared_node["cad:boundingRooms"] == ["urn:room:a", "urn:room:b"]
    assert {item["@id"] for item in shared_node["top:bounds"]} == {
        "urn:room:a",
        "urn:room:b",
    }
    assert {item["@id"] for item in room_a_node["top:boundedBy"]} == {"urn:wall:shared"}
    assert {item["@id"] for item in room_a_node["top:adjacentTo"]} == {"urn:room:b"}
    assert {item["@id"] for item in room_a_node["top:connectsTo"]} == {"urn:room:b"}
    assert engine.to_turtle(graph) == engine.to_turtle(second["graph"])
    assert "top:boundedBy" in engine.to_turtle(graph)


def test_room_order_does_not_change_wall_ids_or_network() -> None:
    forward = _walls(_planned([_room("urn:room:a", 0), _room("urn:room:b", 4200)]))
    reverse = _walls(_planned([_room("urn:room:b", 4200), _room("urn:room:a", 0)]))

    assert {wall["semantic_id"] for wall in forward} == {
        wall["semantic_id"] for wall in reverse
    }
    assert {
        (tuple(wall["geometry"]["start"]), tuple(wall["geometry"]["end"]))
        for wall in forward
    } == {
        (tuple(wall["geometry"]["start"]), tuple(wall["geometry"]["end"]))
        for wall in reverse
    }


def test_new_room_reuses_compatible_existing_physical_wall() -> None:
    geometry = {
        "start": [4100, -200],
        "end": [4100, 4200],
        "thickness": 200,
        "height": 3000,
        "style": "Standard",
    }
    existing = EntitySnapshot(
        "E1",
        "AcDbPolyline",
        "AI-WALLS",
        {"kind": "line", **geometry},
        {
            "semantic_id": "urn:wall:existing",
            "ontology_class": "top:Wall",
            "managed": True,
            "geometry": geometry,
            "representation": "standard",
        },
    )
    snapshot = DrawingSnapshot("existing.dwg", "C:/existing.dwg", "mm", [existing])
    engine = TopologyEngine()
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [_room("urn:room:a", 0)],
        }
    )

    operations, _, _ = engine.plan_changes(document, snapshot, engine.analyze(snapshot))

    assert (
        sum(
            item.get("kind") == "create_managed"
            and item.get("ontology_class") == "top:Wall"
            for item in operations
        )
        == 3
    )
    relation_update = next(
        item for item in operations if item.get("kind") == "update_wall_rooms"
    )
    assert relation_update["semantic_id"] == "urn:wall:existing"
    assert relation_update["bounding_room_ids"] == ["urn:room:a"]


def test_deleting_one_room_preserves_shared_wall_for_remaining_room() -> None:
    snapshot = _empty()
    nodes = [
        {
            "@id": "urn:room:a",
            "@type": "top:Room",
            "cad:managed": True,
            "cad:handles": ["R1"],
        },
        {
            "@id": "urn:room:b",
            "@type": "top:Room",
            "cad:managed": True,
            "cad:handles": ["R2"],
        },
        {
            "@id": "urn:wall:shared",
            "@type": "top:Wall",
            "cad:managed": True,
            "cad:handles": ["W1"],
            "cad:boundingRooms": ["urn:room:a", "urn:room:b"],
            "cad:aliases": ["urn:room:a:wall:2", "urn:room:b:wall:4"],
        },
    ]
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [{"op": "delete", "@id": "urn:room:a"}],
        }
    )

    operations, _, _ = TopologyEngine().plan_changes(
        document,
        snapshot,
        {"graph": {"@graph": nodes}, "candidates": []},
    )
    deletion = next(item for item in operations if item["kind"] == "delete_managed")
    update = next(item for item in operations if item["kind"] == "update_wall_rooms")

    assert deletion["handles"] == ["R1"]
    assert update["handles"] == ["W1"]
    assert update["bounding_room_ids"] == ["urn:room:b"]


def _explicit_wall(
    wall_id: str,
    start: Sequence[float],
    end: Sequence[float],
    thickness: float = 200,
) -> Dict[str, Any]:
    return {
        "op": "create",
        "@id": wall_id,
        "@type": "top:Wall",
        "geometry": {
            "start": list(start),
            "end": list(end),
            "thickness": thickness,
        },
    }


def _room_entity(room_id: str, handle: str, origin_x: float) -> EntitySnapshot:
    boundary = [
        [origin_x, 0],
        [origin_x + 4000, 0],
        [origin_x + 4000, 4000],
        [origin_x, 4000],
    ]
    return EntitySnapshot(
        handle,
        "AcDbPolyline",
        "AI-ROOMS",
        {
            "kind": "polyline",
            "vertices": [[*point, 0] for point in boundary],
            "closed": True,
        },
        {
            "semantic_id": room_id,
            "ontology_class": "top:Room",
            "managed": True,
            "geometry": {"boundary": boundary},
        },
    )


class _Adapter:
    def refresh_view(self) -> None:
        """Mirror the adapter refresh contract without controlling CAD."""


class _PlanningBridge:
    def __init__(self, snapshot: DrawingSnapshot) -> None:
        self.snapshot_value = snapshot
        self.applied: list[Dict[str, Any]] = []

    def snapshot(self, _adapter: Any, scope: str = "all") -> DrawingSnapshot:
        assert scope in {"all", "selected"}
        return self.snapshot_value

    def resolve_operation_representations(
        self,
        _adapter: Any,
        operations: Sequence[Dict[str, Any]],
        _analysis: Dict[str, Any],
    ) -> None:
        for operation in operations:
            if operation.get("representation") == "auto":
                operation["representation"] = "standard"

    def apply_operations(
        self,
        _adapter: Any,
        operations: Sequence[Dict[str, Any]],
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        self.applied = list(operations)
        return {"success": True, "created": [], "modified_handles": []}

    def rollback_last_transaction(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Rollback was not expected")

    @staticmethod
    def write_sidecars(
        _snapshot: DrawingSnapshot, _graph: Dict[str, Any], _turtle: str
    ) -> Dict[str, str]:
        return {"jsonld": "C:/shared.jsonld", "ttl": "C:/shared.ttl"}


class _MaterializingBridge(_PlanningBridge):
    """Materialize planned operations as a fresh thread-neutral CAD snapshot."""

    def apply_operations(
        self,
        _adapter: Any,
        operations: Sequence[Dict[str, Any]],
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        self.applied = list(operations)
        entities: list[EntitySnapshot] = []
        for index, operation in enumerate(operations, start=1):
            ontology_class = operation.get("ontology_class")
            if operation.get("kind") != "create_managed":
                continue
            geometry = dict(operation.get("geometry", {}))
            semantic = {
                "semantic_id": operation["semantic_id"],
                "ontology_class": ontology_class,
                "managed": True,
                "geometry": geometry,
                "representation": operation.get("representation", "standard"),
            }
            layer = "AI-WALLS"
            entity_geometry: Dict[str, Any] = {"kind": "line", **geometry}
            if ontology_class == "top:Room":
                origin = geometry["origin"]
                width = geometry["clear_width"]
                depth = geometry["clear_depth"]
                boundary = [
                    [origin[0], origin[1]],
                    [origin[0] + width, origin[1]],
                    [origin[0] + width, origin[1] + depth],
                    [origin[0], origin[1] + depth],
                ]
                semantic["geometry"] = {**geometry, "boundary": boundary}
                entity_geometry = {
                    "kind": "polyline",
                    "vertices": [[*point, 0] for point in boundary],
                    "closed": True,
                }
                layer = "AI-ROOMS"
            elif ontology_class == "top:Wall":
                semantic["room_ids"] = operation.get("bounding_room_ids", [])
                semantic["aliases"] = operation.get("aliases", [])
            elif ontology_class == "top:Door":
                semantic["host_wall_id"] = geometry["host_wall_id"]
                entity_geometry = {"kind": "point", "position": [4100, 1200, 0]}
                layer = "AI-DOORS"
            entities.append(
                EntitySnapshot(
                    f"{index:X}",
                    "AcDbPolyline",
                    layer,
                    entity_geometry,
                    semantic,
                )
            )
        self.snapshot_value = DrawingSnapshot(
            "shared.dwg", "C:/shared.dwg", "mm", entities
        )
        return {
            "success": True,
            "created": [{"handles": [entity.handle]} for entity in entities],
            "modified_handles": [],
        }
