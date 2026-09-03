from __future__ import annotations

import pytest

from topology_engine import (
    ChangeDocument,
    DrawingSnapshot,
    EntitySnapshot,
    TopologyEngine,
)


def _semantic_entity(handle: str, semantic: dict) -> EntitySnapshot:
    return EntitySnapshot(
        handle=handle,
        object_type="AcDbPolyline",
        layer="AI-SPACES",
        geometry={"kind": "point", "point": [0, 0, 0]},
        semantic=semantic,
    )


def test_space_connection_and_properties_survive_analysis_and_export() -> None:
    nodes = [
        {
            "semantic_id": "space:a",
            "ontology_class": "top:Space",
            "label": "Entry lobby",
            "managed": True,
            "geometry": {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]},
            "properties": {"is_entry": True, "space_type": "lobby"},
        },
        {
            "semantic_id": "space:b",
            "ontology_class": "top:Space",
            "label": "Passage",
            "managed": True,
            "geometry": {
                "boundary": [
                    [4000, 0],
                    [7000, 0],
                    [7000, 3000],
                    [4000, 3000],
                ]
            },
            "properties": {"clear_width_mm": 1200, "space_type": "passage"},
        },
        {
            "semantic_id": "connection:ab",
            "ontology_class": "top:Connection",
            "label": "Lobby to passage",
            "managed": True,
            "geometry": {
                "from_space_id": "space:a",
                "to_space_id": "space:b",
                "via_id": "",
                "clear_width_mm": 1200,
                "direction": "bidirectional",
                "accessible": True,
                "status": "confirmed",
                "source": "explicit",
            },
        },
    ]
    snapshot = DrawingSnapshot(
        "spaces.dwg",
        "C:/spaces.dwg",
        "mm",
        [_semantic_entity(f"{index:X}", node) for index, node in enumerate(nodes, 1)],
    )
    engine = TopologyEngine()

    result = engine.analyze(snapshot)
    graph_nodes = {node["@id"]: node for node in result["graph"]["@graph"]}
    turtle = engine.to_turtle(result["graph"])

    assert graph_nodes["space:a"]["top:hasArea"] == pytest.approx(12_000_000)
    assert graph_nodes["space:a"]["cad:properties"]["is_entry"] is True
    assert {item["@id"] for item in graph_nodes["space:a"]["top:connectsTo"]} == {
        "space:b"
    }
    assert "top:Connection" in turtle
    assert "Lobby to passage" in turtle


def test_arbitrary_room_boundary_does_not_manufacture_implicit_walls() -> None:
    snapshot = DrawingSnapshot("empty.dwg", "C:/empty.dwg", "mm", [])
    engine = TopologyEngine()
    analysis = engine.analyze(snapshot)
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "create",
                    "@id": "room:polygon",
                    "@type": "top:Room",
                    "label": "Irregular room",
                    "geometry": {
                        "boundary": [
                            [0, 0],
                            [5000, 0],
                            [4200, 3000],
                            [0, 4000],
                        ]
                    },
                }
            ],
        }
    )

    operations, _diff, _warnings = engine.plan_changes(document, snapshot, analysis)

    assert len(operations) == 1
    assert operations[0]["ontology_class"] == "top:Room"
    assert operations[0]["geometry"]["boundary"][2] == [4200.0, 3000.0]
