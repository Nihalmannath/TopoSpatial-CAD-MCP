from __future__ import annotations

import json

import pytest

from core.config import get_config
from design_engine.workspace import (
    DraftRequest,
    TopologyWorkspaceService,
    WorkingDraftRequest,
)


def _base_graph() -> dict:
    return {
        "cad:revision": "sha256:drawing-a",
        "@graph": [
            {
                "@id": "space:a",
                "@type": "top:Space",
                "rdfs:label": "Entry",
                "cad:geometry": {
                    "boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
                },
                "cad:properties": {"is_entry": True},
                "top:connectsTo": [{"@id": "space:b"}],
            },
            {
                "@id": "space:b",
                "@type": "top:Space",
                "rdfs:label": "Bedroom",
                "cad:geometry": {
                    "boundary": [
                        [4000, 0],
                        [8000, 0],
                        [8000, 3000],
                        [4000, 3000],
                    ]
                },
                "cad:properties": {},
            },
        ],
    }


def test_handleless_design_connection_is_durable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    service.commit_design_semantics(
        "plan.dwg",
        "sha256:drawing-b",
        [
            {
                "kind": "create_managed",
                "semantic_id": "connection:door-ab",
                "ontology_class": "top:Connection",
                "label": "Entry to Bedroom",
                "geometry": {
                    "from_space_id": "space:a",
                    "to_space_id": "space:b",
                    "via_id": "door:ab",
                    "status": "confirmed",
                },
            }
        ],
    )
    reloaded = TopologyWorkspaceService(ttl_seconds=60)
    merged = reloaded.merge_graph("plan.dwg", "sha256:drawing-b", _base_graph())
    connection = next(node for node in merged["@graph"] if node["@id"] == "connection:door-ab")
    assert connection["cad:geometry"]["via_id"] == "door:ab"


def test_semantic_draft_is_non_mutating_until_apply(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph(
        "plan.dwg", "sha256:drawing-a", _base_graph(), include_candidates=True
    )
    candidate = next(
        node for node in current["@graph"] if node["@type"] == "top:Connection"
    )
    request = DraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": [
                {
                    "op": "confirm_connection",
                    "semantic_id": candidate["@id"],
                }
            ],
        }
    )

    preview = service.preview(request, current)

    assert preview["mutated"] is False
    assert service.status("plan.dwg")["draft_dirty"] is False
    assert service.status("plan.dwg")["preview_pending"] is True
    preview_state = json.loads(
        (tmp_path / "plan.topology.workspace.json").read_text(encoding="utf-8")
    )
    assert preview_state["overrides"] == {}

    applied = service.apply(preview["transaction_id"], current)
    committed = service.merge_graph(
        "plan.dwg", "sha256:drawing-a", _base_graph(), include_candidates=False
    )
    connection = next(
        node for node in committed["@graph"] if node["@type"] == "top:Connection"
    )

    assert applied["status"] == "applied"
    assert connection["cad:geometry"]["status"] == "confirmed"
    assert connection["cad:managed"] is True
    assert service.status("plan.dwg")["draft_dirty"] is False


def test_stale_graph_revision_rejects_preview(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    request = DraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": "sha256:stale",
            "commands": [],
        }
    )

    with pytest.raises(ValueError, match="STALE_GRAPH_REVISION"):
        service.preview(request, current)


def test_split_authority_geometry_conflict_requires_review(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    request = DraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": [
                {
                    "op": "patch_node",
                    "semantic_id": "space:a",
                    "patch": {"cad:geometry": {"boundary": [[0, 0], [1, 0], [1, 1]]}},
                }
            ],
        }
    )
    transaction = service.preview(request, current)
    service.apply(transaction["transaction_id"], current)
    cad_changed = _base_graph()
    cad_changed["@graph"][0]["cad:geometry"]["boundary"][1] = [4100, 0]

    merged = service.merge_graph(
        "plan.dwg", "sha256:drawing-b", cad_changed, include_candidates=False
    )

    assert merged["cad:workspaceConflicts"] == 1
    conflict = service.status("plan.dwg")["conflicts"][0]
    assert conflict["field"] == "cad:geometry"

    service.resolve_conflicts("plan.dwg", {conflict["conflict_id"]: "editor"})
    merged_again = service.merge_graph(
        "plan.dwg", "sha256:drawing-b", cad_changed, include_candidates=False
    )
    assert merged_again["cad:workspaceConflicts"] == 0
    assert (
        merged_again["@graph"][0]["cad:geometry"]
        != cad_changed["@graph"][0]["cad:geometry"]
    )


def test_working_draft_persists_and_reset_never_mutates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    request = WorkingDraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": [
                {
                    "op": "patch_node",
                    "semantic_id": "space:a",
                    "patch": {"cad:properties": {"is_entry": False}},
                }
            ],
            "undo_position": 1,
        }
    )

    result = service.evaluate_draft(request, current)

    assert result["mutated"] is False
    assert result["dirty"] is True
    assert result["affected_ids"] == ["space:a"]
    assert service.draft_graph("plan.dwg", result["draft_revision"])["@graph"]
    assert service.reset_draft("plan.dwg")["mutated"] is False
    assert service.working_draft("plan.dwg") is None


def test_cancel_preview_preserves_playground_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    working_request = WorkingDraftRequest(
        drawing_name="plan.dwg",
        base_drawing_revision=current["cad:revision"],
        base_graph_revision=current["cad:graphRevision"],
        commands=[],
    )
    service.evaluate_draft(working_request, current)
    preview = service.preview(
        DraftRequest.model_validate(
            working_request.model_dump(
                exclude={"required_width_mm", "undo_position", "topology_changes", "actions"}
            )
        ),
        current,
    )

    service.cancel("plan.dwg", preview["transaction_id"])

    assert service.working_draft("plan.dwg") is not None


def test_revision_change_freezes_working_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    service.evaluate_draft(
        WorkingDraftRequest(
            drawing_name="plan.dwg",
            base_drawing_revision=current["cad:revision"],
            base_graph_revision=current["cad:graphRevision"],
            commands=[],
        ),
        current,
    )

    service.merge_graph("plan.dwg", "sha256:drawing-b", _base_graph())

    assert service.status("plan.dwg")["draft_frozen"] is True


def test_rebase_requires_explicit_choice_for_changed_target(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph())
    service.evaluate_draft(
        WorkingDraftRequest(
            drawing_name="plan.dwg",
            base_drawing_revision=current["cad:revision"],
            base_graph_revision=current["cad:graphRevision"],
            commands=[
                {
                    "op": "patch_node",
                    "semantic_id": "space:a",
                    "patch": {"rdfs:label": "Draft entry"},
                }
            ],
        ),
        current,
    )
    changed = _base_graph()
    changed["@graph"][0]["rdfs:label"] = "CAD entry"
    new_graph = service.merge_graph(
        "plan.dwg", "sha256:drawing-b", changed, include_candidates=False
    )

    rebased = service.rebase_draft("plan.dwg", new_graph)
    resolved = service.resolve_draft_conflicts(
        "plan.dwg",
        {rebased["conflicts"][0]["conflict_id"]: "editor"},
    )

    assert rebased["frozen"] is True
    assert resolved["frozen"] is False
    node = next(
        item for item in resolved["graph"]["@graph"] if item["@id"] == "space:a"
    )
    assert node["rdfs:label"] == "Draft entry"


def test_schema_one_migration_discards_only_obsolete_transaction(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    path = tmp_path / "plan.topology.workspace.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "drawing_name": "plan.dwg",
                "overrides": {"space:a": {"rdfs:label": "Kept"}},
                "deleted_ids": ["space:old"],
                "conflicts": [{"conflict_id": "kept"}],
                "draft": {"transaction_id": "obsolete"},
            }
        ),
        encoding="utf-8",
    )

    service = TopologyWorkspaceService()
    context = service.draft_context("plan.dwg")

    assert context is None
    state = service._load_state("plan.dwg")
    assert state["schema_version"] == 3
    assert state["overrides"]["space:a"]["rdfs:label"] == "Kept"
    assert state["deleted_ids"] == ["space:old"]
    assert state["conflicts"] == [{"conflict_id": "kept"}]


def test_unclassified_candidates_do_not_pollute_graph(tmp_path, monkeypatch) -> None:
    """Candidates must not appear as unclassified top:Space nodes in @graph."""
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    raw_graph = {
        "cad:revision": "sha256:drawing-candidates",
        "@graph": [
            {
                "@id": "space:a",
                "@type": "top:Space",
                "rdfs:label": "Living",
                "cad:geometry": {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]},
                "cad:properties": {},
            },
            {
                "@id": "urn:topospatial:candidate:poly-1",
                "@type": "top:CandidateSpace",
                "rdfs:label": "Unclassified Polygon",
                "cad:geometry": {"boundary": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]]},
                "cad:properties": {"confidence": 0.65, "source_handle": "H101", "source_layer": "A-WALL"},
            },
        ],
    }

    result = service.merge_graph("candidates.dwg", "sha256:drawing-candidates", raw_graph, include_candidates=False)
    graph_ids = {n["@id"] for n in result["@graph"]}
    assert "space:a" in graph_ids
    # Candidate should not be promoted to a bare top:Space
    assert "urn:topospatial:candidate:poly-1" not in graph_ids


def test_discovery_confirmation_creates_managed_room(tmp_path, monkeypatch) -> None:
    """Confirming a discovery creates a managed top:Room with spatial program."""
    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    service = TopologyWorkspaceService(ttl_seconds=60)
    current = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph(), include_candidates=False)

    room_node = {
        "@id": "urn:topospatial:space:discovery-1",
        "@type": "top:Room",
        "rdfs:label": "Bedroom 2",
        "cad:managed": True,
        "cad:geometry": {"boundary": [[0, 3000], [4000, 3000], [4000, 6000], [0, 6000]]},
        "cad:properties": {
            "spatial_program": {
                "schema_version": 1,
                "space_type": "bedroom",
                "zone": "sleeping",
                "area_targets_m2": {"minimum": 10.0, "target": 12.0, "maximum": 16.0},
                "occupancy": {"design_occupancy": 2, "accessible_required": True},
                "privacy": "private",
            },
            "source_discovery_id": "disc-001",
        },
    }

    request = WorkingDraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": [
                {
                    "op": "upsert_node",
                    "semantic_id": room_node["@id"],
                    "node": room_node,
                }
            ],
        }
    )

    draft_result = service.evaluate_draft(request, current)
    assert draft_result["dirty"] is True
    assert room_node["@id"] in draft_result["affected_ids"]

    # Preview and apply
    preview_req = DraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": request.commands,
        }
    )
    preview = service.preview(preview_req, current)
    service.apply(preview["transaction_id"], current)

    # Merged graph now contains the managed room
    final = service.merge_graph("plan.dwg", "sha256:drawing-a", _base_graph(), include_candidates=False)
    final_ids = {n["@id"] for n in final["@graph"]}
    assert room_node["@id"] in final_ids
    persisted_room = next(n for n in final["@graph"] if n["@id"] == room_node["@id"])
    assert persisted_room["rdfs:label"] == "Bedroom 2"
    assert persisted_room["cad:properties"]["spatial_program"]["space_type"] == "bedroom"
