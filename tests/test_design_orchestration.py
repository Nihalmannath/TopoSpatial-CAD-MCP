"""Regression and efficiency tests for high-level design orchestration."""

from __future__ import annotations

import json
from typing import Any, Dict, Sequence

import pytest
from pydantic import TypeAdapter, ValidationError

from design_engine.models import DesignRequest
from design_engine.orchestrator import DesignOrchestrator
from design_engine.retry import RetryPolicy
from topology_engine import DrawingSnapshot, EntitySnapshot

REQUEST_ADAPTER = TypeAdapter(DesignRequest)


class FakeAdapter:
    """Record adapter refreshes requested by retry recovery."""

    def __init__(self) -> None:
        """Initialize the refresh counter."""
        self.refresh_count = 0

    def refresh_view(self) -> None:
        self.refresh_count += 1


class FakeBridge:
    """Provide deterministic snapshot and mutation behavior for orchestration tests."""

    def __init__(
        self, base: DrawingSnapshot, post: DrawingSnapshot | None = None
    ) -> None:
        """Initialize deterministic pre- and post-mutation snapshots."""
        self.base = base
        self.current = base
        self.post = post or base
        self.snapshot_calls = 0
        self.applied: list[Dict[str, Any]] = []
        self.rollback_calls = 0

    def snapshot(self, _adapter: Any, scope: str = "all") -> DrawingSnapshot:
        assert scope in {"all", "selected"}
        self.snapshot_calls += 1
        return self.current

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
        self.current = self.post
        return {
            "success": True,
            "created": [{"handles": ["A1"]}],
            "modified_handles": [],
        }

    def rollback_last_transaction(
        self, _adapter: Any, *, expected_revision: str, **_kwargs: Any
    ) -> None:
        assert expected_revision == self.base.revision
        self.rollback_calls += 1
        self.current = self.base

    @staticmethod
    def write_sidecars(
        snapshot: DrawingSnapshot, _graph: Dict[str, Any], _turtle: str
    ) -> Dict[str, str]:
        stem = snapshot.drawing_name.removesuffix(".dwg")
        return {
            "jsonld": f"C:/{stem}.topology.jsonld",
            "ttl": f"C:/{stem}.topology.ttl",
        }


def empty_snapshot() -> DrawingSnapshot:
    return DrawingSnapshot("plan.dwg", "C:/plan.dwg", "mm", [])


def semantic_snapshot() -> DrawingSnapshot:
    wall_geometry = {
        "kind": "line",
        "start": [0, 0, 0],
        "end": [5000, 0, 0],
        "thickness": 200,
    }
    return DrawingSnapshot(
        "plan.dwg",
        "C:/plan.dwg",
        "mm",
        [
            EntitySnapshot(
                "A1",
                "AcDbPolyline",
                "AI-WALLS",
                wall_geometry,
                {
                    "semantic_id": "urn:wall:north",
                    "ontology_class": "top:Wall",
                    "label": "North Wall",
                    "managed": True,
                    "geometry": wall_geometry,
                },
            )
        ],
    )


def room_snapshot() -> DrawingSnapshot:
    boundary = [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
    return DrawingSnapshot(
        "room.dwg",
        "C:/room.dwg",
        "mm",
        [
            EntitySnapshot(
                "20",
                "AcDbPolyline",
                "AI-ROOMS",
                {
                    "kind": "polyline",
                    "vertices": [[x, y, 0] for x, y in boundary],
                    "closed": True,
                },
                {
                    "semantic_id": "urn:room:kitchen",
                    "ontology_class": "top:Room",
                    "label": "Kitchen",
                    "managed": True,
                    "geometry": {
                        "origin": [0, 0],
                        "clear_width": 4000,
                        "clear_depth": 3000,
                        "wall_thickness": 200,
                    },
                },
            )
        ],
    )


def door_snapshot() -> DrawingSnapshot:
    wall = {
        "start": [0, 0],
        "end": [5000, 0],
        "thickness": 200,
        "height": 3000,
        "style": "Standard",
    }
    door = {
        "host_wall_id": "urn:wall:host",
        "offset": 1000,
        "width": 900,
        "height": 2100,
        "style": "Standard",
        "hinge": "left",
        "swing": "in",
        "swing_angle_deg": 90,
    }
    return DrawingSnapshot(
        "door.dwg",
        "C:/door.dwg",
        "mm",
        [
            EntitySnapshot(
                "30",
                "AcDbPolyline",
                "AI-WALLS",
                {"kind": "line", **wall},
                {
                    "semantic_id": "urn:wall:host",
                    "ontology_class": "top:Wall",
                    "label": "Kitchen Wall",
                    "managed": True,
                    "geometry": wall,
                },
            ),
            EntitySnapshot(
                "31",
                "AcDbLine",
                "AI-DOORS",
                {"kind": "point", "position": [1000, 0, 0]},
                {
                    "semantic_id": "urn:door:kitchen",
                    "ontology_class": "top:Door",
                    "label": "Kitchen Door",
                    "managed": True,
                    "host_wall_id": "urn:wall:host",
                    "geometry": door,
                },
            ),
        ],
    )


def create_wall_request(task_id: str = "task-create") -> Any:
    snapshot = empty_snapshot()
    return REQUEST_ADAPTER.validate_python(
        {
            "action": "create",
            "task_id": task_id,
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "create",
                    "@id": "urn:wall:north",
                    "@type": "top:Wall",
                    "label": "North Wall",
                    "geometry": {
                        "start": [0, 0],
                        "end": [5000, 0],
                        "thickness": 200,
                    },
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        {"action": "inspect", "budget": {"include_geometry": {}}},
        {"action": "apply", "transaction_id": []},
        {"action": "metrics"},
    ],
)
def test_design_schema_rejects_null_empty_and_wrong_types(payload: Any) -> None:
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(payload)


def test_create_plan_is_compact_cached_and_locally_inspectable() -> None:
    bridge = FakeBridge(empty_snapshot())
    orchestrator = DesignOrchestrator(bridge=bridge)
    adapter = FakeAdapter()
    request = create_wall_request()

    preview = orchestrator.execute(request, adapter)
    inspect = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {"action": "inspect", "task_id": "task-create"}
        ),
        adapter,
    )

    assert preview["success"] is True
    assert preview["changed"] == {"wall": 1}
    assert preview["operation_count"] == 1
    assert preview["mutated"] is False
    assert "diff" not in preview
    assert inspect["cache_hit"] is True
    assert bridge.snapshot_calls == 2
    assert orchestrator.metrics.snapshot("task-create")["topology_operations"] == 1


def test_create_reorders_opening_after_its_host_wall_locally() -> None:
    snapshot = empty_snapshot()
    bridge = FakeBridge(snapshot)
    orchestrator = DesignOrchestrator(bridge=bridge)
    request = REQUEST_ADAPTER.validate_python(
        {
            "action": "create",
            "task_id": "task-dependency",
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "create",
                    "@id": "urn:door:entry",
                    "@type": "top:Door",
                    "geometry": {
                        "host_wall_id": "urn:wall:north",
                        "offset": 1000,
                        "width": 900,
                        "hinge": "left",
                        "swing": "in",
                    },
                },
                {
                    "op": "create",
                    "@id": "urn:wall:north",
                    "@type": "top:Wall",
                    "geometry": {
                        "start": [0, 0],
                        "end": [5000, 0],
                        "thickness": 200,
                    },
                },
            ],
        }
    )

    preview = orchestrator.execute(request, FakeAdapter())
    transaction = orchestrator.transactions.get(preview["transaction_id"])

    assert preview["success"] is True
    assert preview["problems"]["auto_fixable"] == 1
    assert [item["ontology_class"] for item in transaction.operations] == [
        "top:Wall",
        "top:Door",
    ]


def test_duplicate_walls_and_overlapping_rooms_need_an_llm_decision() -> None:
    snapshot = empty_snapshot()
    orchestrator = DesignOrchestrator(bridge=FakeBridge(snapshot))
    request = REQUEST_ADAPTER.validate_python(
        {
            "action": "create",
            "base_revision": snapshot.revision,
            "changes": [
                *[
                    {
                        "op": "create",
                        "@id": f"urn:wall:{index}",
                        "@type": "top:Wall",
                        "geometry": {
                            "start": [0, 0],
                            "end": [5000, 0],
                            "thickness": 200,
                        },
                    }
                    for index in (1, 2)
                ],
                *[
                    {
                        "op": "create",
                        "@id": f"urn:room:{index}",
                        "@type": "top:Room",
                        "geometry": {
                            "origin": [offset, 0],
                            "clear_width": 4000,
                            "clear_depth": 3000,
                            "wall_thickness": 200,
                        },
                    }
                    for index, offset in ((1, 0), (2, 2000))
                ],
            ],
        }
    )

    result = orchestrator.execute(request, FakeAdapter())

    assert result["success"] is False
    assert result["error"]["code"] == "DESIGN_VALIDATION_FAILED"
    assert result["needs_llm_decision"] is True
    assert {problem["code"] for problem in result["problems"]} == {
        "DUPLICATE_WALL",
        "ROOMS_OVERLAP",
    }


def test_context_query_returns_only_bounded_semantic_neighborhood() -> None:
    room_left = EntitySnapshot(
        "10",
        "AcDbPolyline",
        "AI-ROOMS",
        {"kind": "polyline", "vertices": [], "closed": True},
        {
            "semantic_id": "urn:room:kitchen",
            "ontology_class": "top:Room",
            "label": "Kitchen",
            "managed": True,
            "geometry": {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]},
        },
    )
    room_right = EntitySnapshot(
        "11",
        "AcDbPolyline",
        "AI-ROOMS",
        {"kind": "polyline", "vertices": [], "closed": True},
        {
            "semantic_id": "urn:room:living",
            "ontology_class": "top:Room",
            "label": "Living",
            "managed": True,
            "geometry": {
                "boundary": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]]
            },
        },
    )
    snapshot = DrawingSnapshot(
        "rooms.dwg", "C:/rooms.dwg", "mm", [room_left, room_right]
    )
    orchestrator = DesignOrchestrator(bridge=FakeBridge(snapshot))
    request = REQUEST_ADAPTER.validate_python(
        {
            "action": "get_context",
            "entity": "Kitchen",
            "budget": {
                "graph_depth": 1,
                "max_entities": 2,
                "include_geometry": False,
            },
        }
    )

    result = orchestrator.execute(request, FakeAdapter())

    assert result["success"] is True
    assert result["root"] == "urn:room:kitchen"
    assert {node["id"] for node in result["nodes"]} == {
        "urn:room:kitchen",
        "urn:room:living",
    }
    assert all("geometry" not in node for node in result["nodes"])


def test_truncated_context_can_be_paged_without_reconnecting_to_cad() -> None:
    class GraphEngine:
        def ensure_available(self) -> None:
            return None

        def analyze(self, snapshot: DrawingSnapshot) -> Dict[str, Any]:
            nodes = []
            for index in range(4):
                node: Dict[str, Any] = {
                    "@id": f"urn:room:{index}",
                    "@type": "top:Room",
                    "rdfs:label": f"Room {index}",
                }
                neighbors = []
                if index > 0:
                    neighbors.append({"@id": f"urn:room:{index - 1}"})
                if index < 3:
                    neighbors.append({"@id": f"urn:room:{index + 1}"})
                node["top:adjacentTo"] = neighbors
                nodes.append(node)
            return {
                "success": True,
                "drawing": snapshot.drawing_name,
                "revision": snapshot.revision,
                "node_count": 4,
                "relation_count": 6,
                "candidate_count": 0,
                "graph": {"@graph": nodes},
                "candidates": [],
                "issues": [],
            }

    bridge = FakeBridge(empty_snapshot())
    orchestrator = DesignOrchestrator(engine=GraphEngine(), bridge=bridge)
    first = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "get_context",
                "entity": "Room 0",
                "budget": {"graph_depth": 3, "max_entities": 2},
            }
        ),
        FakeAdapter(),
    )
    page = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "get_result",
                "result_id": first["result_id"],
                "offset": 2,
                "max_entities": 2,
            }
        ),
        None,
    )

    assert first["truncated"] is True
    assert [node["id"] for node in page["nodes"]] == ["urn:room:2", "urn:room:3"]
    assert bridge.snapshot_calls == 1


def test_preview_apply_cancel_and_rollback_are_transactional() -> None:
    base = empty_snapshot()
    post = semantic_snapshot()
    bridge = FakeBridge(base, post)
    orchestrator = DesignOrchestrator(bridge=bridge)
    adapter = FakeAdapter()

    preview = orchestrator.execute(create_wall_request("task-transaction"), adapter)
    transaction_id = preview["transaction_id"]
    applied = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "task_id": "task-transaction",
                "transaction_id": transaction_id,
            }
        ),
        adapter,
    )
    applied_again = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {"action": "apply", "transaction_id": transaction_id}
        ),
        adapter,
    )
    rolled_back = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {"action": "rollback", "transaction_id": transaction_id}
        ),
        adapter,
    )

    second_preview = orchestrator.execute(create_wall_request("task-cancel"), adapter)
    cancelled = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "cancel",
                "transaction_id": second_preview["transaction_id"],
            }
        ),
        adapter,
    )

    assert applied["status"] == "applied"
    assert applied_again["idempotent"] is True
    assert rolled_back["status"] == "rolled_back"
    assert bridge.rollback_calls == 1
    assert cancelled["status"] == "cancelled"


def test_identical_failure_stops_after_two_attempts() -> None:
    bridge = FakeBridge(empty_snapshot())
    orchestrator = DesignOrchestrator(bridge=bridge)
    request = REQUEST_ADAPTER.validate_python(
        {"action": "get_context", "entity": "Missing"}
    )

    first = orchestrator.execute(request, FakeAdapter())
    second = orchestrator.execute(request, FakeAdapter())
    third = orchestrator.execute(request, FakeAdapter())

    assert first["error"]["repeat_count"] == 1
    assert second["error"]["repeat_count"] == 2
    assert third["error"]["code"] == "REPEATED_FAILURE_STOPPED"
    assert bridge.snapshot_calls == 2


def test_retry_policy_retries_only_one_known_transient() -> None:
    attempts = 0
    reconnects = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("RPC_E_CALL_REJECTED")
        return "ok"

    def reconnect(_exc: Exception) -> None:
        nonlocal reconnects
        reconnects += 1

    value, retries = RetryPolicy().run(operation, on_retry=reconnect)

    assert value == "ok"
    assert retries == 1
    assert reconnects == 1


def test_summary_response_is_smaller_than_debug_response() -> None:
    summary_orchestrator = DesignOrchestrator(bridge=FakeBridge(empty_snapshot()))
    debug_orchestrator = DesignOrchestrator(bridge=FakeBridge(empty_snapshot()))
    summary = summary_orchestrator.execute(
        create_wall_request("summary"), FakeAdapter()
    )
    debug_request = create_wall_request("debug")
    debug_request.detail_level = "debug"
    debug = debug_orchestrator.execute(debug_request, FakeAdapter())

    summary_bytes = len(json.dumps(summary, separators=(",", ":")).encode())
    debug_bytes = len(json.dumps(debug, separators=(",", ":")).encode())

    assert "operations" not in summary
    assert "operations" in debug
    assert summary_bytes < debug_bytes


def test_high_level_workflow_needs_three_major_calls_for_semantic_create() -> None:
    """Inspect, preview, apply replaces one call per wall/opening plus validation."""
    base = empty_snapshot()
    bridge = FakeBridge(base, semantic_snapshot())
    orchestrator = DesignOrchestrator(bridge=bridge)
    adapter = FakeAdapter()
    task_id = "benchmark-create"

    inspect = orchestrator.execute(
        REQUEST_ADAPTER.validate_python({"action": "inspect", "task_id": task_id}),
        adapter,
    )
    request = create_wall_request(task_id)
    request.base_revision = inspect["revision"]
    preview = orchestrator.execute(request, adapter)
    orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "task_id": task_id,
                "transaction_id": preview["transaction_id"],
            }
        ),
        adapter,
    )

    metrics = orchestrator.metrics.snapshot(task_id)
    assert metrics["mcp_calls"] == 3
    assert metrics["cad_operations"] == 1
    assert metrics["retries"] == 0


def test_room_modification_workflow_needs_three_major_calls() -> None:
    snapshot = room_snapshot()
    orchestrator = DesignOrchestrator(bridge=FakeBridge(snapshot, snapshot))
    adapter = FakeAdapter()
    task_id = "benchmark-room"

    context = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {"action": "get_context", "entity": "Kitchen", "task_id": task_id}
        ),
        adapter,
    )
    preview = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "modify",
                "task_id": task_id,
                "base_revision": context["revision"],
                "changes": [
                    {
                        "op": "update",
                        "@id": "urn:room:kitchen",
                        "geometry": {
                            "origin": [1000, 1000],
                            "clear_width": 4000,
                            "clear_depth": 3000,
                            "wall_thickness": 200,
                        },
                    }
                ],
            }
        ),
        adapter,
    )
    orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "task_id": task_id,
                "transaction_id": preview["transaction_id"],
            }
        ),
        adapter,
    )

    assert orchestrator.metrics.snapshot(task_id)["mcp_calls"] == 3


def test_opening_modification_workflow_needs_three_major_calls() -> None:
    snapshot = door_snapshot()
    orchestrator = DesignOrchestrator(bridge=FakeBridge(snapshot, snapshot))
    adapter = FakeAdapter()
    task_id = "benchmark-opening"

    context = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "get_context",
                "entity": "Kitchen Door",
                "task_id": task_id,
            }
        ),
        adapter,
    )
    preview = orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "modify",
                "task_id": task_id,
                "base_revision": context["revision"],
                "changes": [
                    {
                        "op": "update",
                        "@id": "urn:door:kitchen",
                        "geometry": {
                            "host_wall_id": "urn:wall:host",
                            "offset": 1500,
                            "width": 900,
                            "hinge": "right",
                            "swing": "in",
                        },
                    }
                ],
            }
        ),
        adapter,
    )
    orchestrator.execute(
        REQUEST_ADAPTER.validate_python(
            {
                "action": "apply",
                "task_id": task_id,
                "transaction_id": preview["transaction_id"],
            }
        ),
        adapter,
    )

    assert orchestrator.metrics.snapshot(task_id)["mcp_calls"] == 3
