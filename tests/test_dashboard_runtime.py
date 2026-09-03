from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from design_engine.service import TopologyApplicationService
from topology_engine import DrawingSnapshot, EntitySnapshot
from web.runtime import (
    BUILD_ID,
    DashboardOwnership,
    read_runtime_state,
    runtime_metadata,
    write_runtime_state,
)


def test_dashboard_port_has_exactly_one_owner(tmp_path) -> None:
    first = DashboardOwnership(8888, tmp_path)
    second = DashboardOwnership(8888, tmp_path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_runtime_identity_and_revision_heartbeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    metadata = runtime_metadata()
    assert metadata["build_id"] == BUILD_ID
    assert metadata["studio_version"]
    assert metadata["workspace_schema_version"] == 3

    written = write_runtime_state(
        "mcp",
        drawing="plan.dwg",
        drawing_revision="sha256:drawing",
        graph_revision="sha256:graph",
    )
    assert read_runtime_state("mcp") == written


def test_concurrent_runtime_heartbeats_are_atomic(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def publish(index):
        return write_runtime_state(
            "mcp", drawing=f"plan-{index}.dwg", drawing_revision=f"drawing:{index}",
            graph_revision=f"graph:{index}",
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(30)))
    assert read_runtime_state("mcp") in results
    assert not list(tmp_path.rglob("*.tmp"))


def test_engine_and_workspace_graph_revisions_match_without_overrides(tmp_path, monkeypatch) -> None:
    from topology_engine import TopologyEngine
    from topology_engine.navigation import graph_revision
    from design_engine.workspace import TopologyWorkspaceService

    snapshot = DrawingSnapshot("empty.dwg", "", "mm", [])
    graph = TopologyEngine().analyze(snapshot)["graph"]
    assert graph["cad:graphRevision"] == graph_revision(graph)
    workspace = TopologyWorkspaceService()
    monkeypatch.setattr(workspace, "_load_state", lambda name: {})
    merged = workspace.merge_graph(snapshot.drawing_name, snapshot.revision, graph)
    assert graph["cad:graphRevision"] == merged["cad:graphRevision"]


def test_workspace_underlay_and_unit_mismatch_diagnostic() -> None:
    snapshot = DrawingSnapshot(
        drawing_name="feet-like.dwg",
        full_name="",
        units="mm",
        entities=[
            EntitySnapshot(
                handle=str(index),
                object_type="AcDbLine",
                layer="A-WALL",
                geometry={"kind": "line", "start": [0, 0, 0], "end": [52, 64, 0]},
            )
            for index in range(20)
        ],
    )
    plan_entities = TopologyApplicationService._plan_entities(snapshot)
    extents = TopologyApplicationService._drawing_extents(plan_entities)
    diagnostics = TopologyApplicationService._unit_diagnostics(
        snapshot.units, len(snapshot.entities), extents
    )

    assert len(plan_entities) == 20
    assert extents == {
        "min_x": 0.0,
        "min_y": 0.0,
        "max_x": 52.0,
        "max_y": 64.0,
        "width": 52.0,
        "height": 64.0,
    }
    assert diagnostics[0]["code"] == "SUSPECTED_FEET_COORDINATES_IN_MM_DRAWING"
    assert diagnostics[0]["suggested_scale_factor"] == 304.8
