from __future__ import annotations

import json

from core.config import get_config
from design_engine.workspace import (
    EditorAction,
    EditorCommand,
    TopologyWorkspaceService,
    WorkingDraftRequest,
)
from topology_engine.navigation import graph_revision


def _graph() -> dict:
    graph = {
        "@context": {"top": "http://w3id.org/topologicpy#"},
        "cad:revision": "sha256:drawing-a",
        "@graph": [
            {
                "@id": "space:a",
                "@type": "top:Space",
                "rdfs:label": "Bedroom",
                "cad:properties": {"legacy": "preserved"},
            }
        ],
    }
    graph["cad:graphRevision"] = graph_revision(graph)
    return graph


def _action(label: str = "Bedroom 1") -> EditorAction:
    return EditorAction(
        action_id="action-1",
        description="Update Bedroom program",
        commands=[
            EditorCommand(
                op="patch_node",
                semantic_id="space:a",
                patch={"rdfs:label": label},
            )
        ],
    )


def test_actions_are_atomic_and_empty_draft_is_clean(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService()
    current = _graph()
    empty = WorkingDraftRequest(
        drawing_name="plan.dwg",
        base_drawing_revision=current["cad:revision"],
        base_graph_revision=current["cad:graphRevision"],
        actions=[],
        undo_position=0,
    )
    response = service.evaluate_draft(empty, current)
    assert response["dirty"] is False
    assert service.status("plan.dwg")["draft_dirty"] is False

    request = empty.model_copy(update={"actions": [_action()], "undo_position": 1})
    response = service.evaluate_draft(request, current)
    assert response["action_count"] == 1
    assert response["graph"]["@graph"][0]["rdfs:label"] == "Bedroom 1"
    undone = service.evaluate_draft(request.model_copy(update={"undo_position": 0}), current)
    assert undone["graph"]["@graph"][0]["rdfs:label"] == "Bedroom"


def test_version_two_draft_migrates_without_losing_committed_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    path = tmp_path / "plan.topology.workspace.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "drawing_name": "plan.dwg",
                "overrides": {"space:a": {"rdfs:label": "Committed"}},
                "deleted_ids": ["space:old"],
                "conflicts": [{"conflict_id": "keep"}],
                "working_draft": {
                    "drawing_name": "plan.dwg",
                    "commands": [_action().commands[0].model_dump(mode="json")],
                    "topology_changes": [],
                    "graph": _graph(),
                    "graph_revision": _graph()["cad:graphRevision"],
                    "dirty": True,
                },
            }
        ),
        encoding="utf-8",
    )
    service = TopologyWorkspaceService()
    state = service._load_state("plan.dwg")
    assert state["schema_version"] == 3
    assert state["overrides"]["space:a"]["rdfs:label"] == "Committed"
    assert state["conflicts"] == [{"conflict_id": "keep"}]
    assert state["working_draft"]["actions"][0]["description"] == "Imported version-2 draft"


def test_editor_request_is_idempotent_and_superseded_by_new_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService()
    current = _graph()
    first_draft = service.evaluate_draft(
        WorkingDraftRequest(
            drawing_name="plan.dwg",
            base_drawing_revision=current["cad:revision"],
            base_graph_revision=current["cad:graphRevision"],
            actions=[_action()],
            undo_position=1,
        ),
        current,
    )
    first = service.create_editor_request("plan.dwg", first_draft["draft_revision"])
    again = service.create_editor_request("plan.dwg", first_draft["draft_revision"])
    assert first["request_id"] == again["request_id"]

    second_draft = service.evaluate_draft(
        WorkingDraftRequest(
            drawing_name="plan.dwg",
            base_drawing_revision=current["cad:revision"],
            base_graph_revision=current["cad:graphRevision"],
            actions=[_action("Bedroom Suite")],
            undo_position=1,
        ),
        current,
    )
    second = service.create_editor_request("plan.dwg", second_draft["draft_revision"])
    assert second["request_id"] != first["request_id"]
    assert service.get_editor_request(first["request_id"])["status"] == "superseded"
    assert service.get_editor_request(first["request_id"], detail_level="normal")["superseded_by"] == second["request_id"]
    assert service.list_editor_requests(drawing_name="plan.dwg", status="pending")[0]["request_id"] == second["request_id"]

