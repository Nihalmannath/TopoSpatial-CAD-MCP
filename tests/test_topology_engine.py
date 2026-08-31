"""Tests for pure 2D topology analysis and change planning."""

from __future__ import annotations

import json

import pytest

from topology_engine import (
    ChangeDocument,
    DrawingSnapshot,
    EntitySnapshot,
    TopologyEngine,
)


def _line(handle: str, start, end, semantic=None) -> EntitySnapshot:
    return EntitySnapshot(
        handle=handle,
        object_type="AcDbLine",
        layer="AI-WALLS",
        geometry={"kind": "line", "start": [*start, 0], "end": [*end, 0]},
        semantic=semantic or {},
    )


def current_room_snapshot() -> DrawingSnapshot:
    segments = [
        ((0, 0), (2050, 0)),
        ((2950, 0), (5000, 0)),
        ((200, 200), (2050, 200)),
        ((2950, 200), (4800, 200)),
        ((2050, 0), (2050, 200)),
        ((2950, 0), (2950, 200)),
        ((0, 4000), (5000, 4000)),
        ((200, 3800), (4800, 3800)),
        ((0, 0), (0, 1400)),
        ((0, 2600), (0, 4000)),
        ((200, 200), (200, 1400)),
        ((200, 2600), (200, 3800)),
        ((0, 1400), (200, 1400)),
        ((0, 2600), (200, 2600)),
        ((5000, 0), (5000, 1400)),
        ((5000, 2600), (5000, 4000)),
        ((4800, 200), (4800, 1400)),
        ((4800, 2600), (4800, 3800)),
        ((4800, 1400), (5000, 1400)),
        ((4800, 2600), (5000, 2600)),
    ]
    entities = [
        _line(f"{index:X}", start, end)
        for index, (start, end) in enumerate(segments, start=1)
    ]
    return DrawingSnapshot("Drawing1.dwg", "", "mm", entities)


def test_untagged_current_plan_returns_one_clear_room_candidate() -> None:
    result = TopologyEngine().analyze(current_room_snapshot())

    assert result["node_count"] == 0
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["suggested_type"] is None
    assert result["candidates"][0]["requires_explicit_tag"] is True
    assert result["candidates"][0]["clear_area_mm2"] == pytest.approx(16_560_000)
    assert result["candidates"][0]["clear_area_m2"] == pytest.approx(16.56)


def test_explicit_room_xdata_creates_semantic_node_and_deterministic_turtle() -> None:
    boundary = [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]
    room = EntitySnapshot(
        handle="AB",
        object_type="AcDbPolyline",
        layer="AI-ROOMS",
        geometry={
            "kind": "polyline",
            "vertices": [[x, y, 0] for x, y in boundary],
            "closed": True,
        },
        semantic={
            "semantic_id": "urn:room:1",
            "ontology_class": "top:Room",
            "label": "Room 1",
            "managed": True,
            "geometry": {"boundary": boundary},
        },
    )
    snapshot = DrawingSnapshot("room.dwg", "C:/room.dwg", "mm", [room])
    engine = TopologyEngine()

    result = engine.analyze(snapshot)
    node = result["graph"]["@graph"][0]
    turtle_a = engine.to_turtle(result["graph"])
    turtle_b = engine.to_turtle(result["graph"])

    assert node["@type"] == "top:Room"
    assert node["top:hasArea"] == pytest.approx(20_000_000)
    assert node["cad:areaSquareMetres"] == pytest.approx(20.0)
    assert turtle_a == turtle_b
    assert "top:Room" in turtle_a
    assert "Room 1" in turtle_a


def test_preview_candidate_annotation_is_non_mutating_and_explicit() -> None:
    snapshot = current_room_snapshot()
    engine = TopologyEngine()
    analysis = engine.analyze(snapshot)
    candidate_id = analysis["candidates"][0]["candidate_id"]
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "annotate",
                    "@type": "top:Room",
                    "label": "Living Room",
                    "targets": {"candidate_id": candidate_id},
                }
            ],
        }
    )

    operations, diff, warnings = engine.plan_changes(document, snapshot, analysis)

    assert operations[0]["kind"] == "create_room_boundary"
    assert operations[0]["ontology_class"] == "top:Room"
    assert operations[0]["boundary"] == analysis["candidates"][0]["boundary"]
    assert diff[0]["action"] == "create"
    assert warnings == []
    assert all(not entity.semantic for entity in snapshot.entities)


def test_create_room_uses_clear_interior_contract() -> None:
    snapshot = DrawingSnapshot("empty.dwg", "", "mm", [])
    engine = TopologyEngine()
    analysis = engine.analyze(snapshot)
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "create",
                    "@id": "urn:room:new",
                    "@type": "top:Room",
                    "geometry": {
                        "origin": [0, 0],
                        "clear_width": 5000,
                        "clear_depth": 4000,
                        "wall_thickness": 200,
                    },
                }
            ],
        }
    )

    operations, _, _ = engine.plan_changes(document, snapshot, analysis)

    assert operations[0]["geometry"] == {
        "origin": [0.0, 0.0],
        "clear_width": 5000.0,
        "clear_depth": 4000.0,
        "wall_thickness": 200.0,
        "rotation_deg": 0.0,
    }


def test_change_document_rejects_unsupported_or_ambiguous_operations() -> None:
    with pytest.raises(ValueError, match="Unsupported ontology class"):
        ChangeDocument.model_validate(
            {
                "@context": {"top": "http://w3id.org/topologicpy#"},
                "base_revision": "sha256:x",
                "changes": [
                    {
                        "op": "create",
                        "@type": "top:Roof",
                        "geometry": {"origin": [0, 0]},
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="exactly one"):
        ChangeDocument.model_validate(
            {
                "@context": {"top": "http://w3id.org/topologicpy#"},
                "base_revision": "sha256:x",
                "changes": [
                    {
                        "op": "annotate",
                        "@type": "top:Wall",
                        "targets": {"handles": ["A"], "layer": "WALLS"},
                    }
                ],
            }
        )


def test_snapshot_revision_changes_with_geometry_or_semantics() -> None:
    base = DrawingSnapshot("a.dwg", "", "mm", [_line("1", (0, 0), (1, 0))])
    moved = DrawingSnapshot("a.dwg", "", "mm", [_line("1", (0, 0), (2, 0))])
    tagged = DrawingSnapshot(
        "a.dwg",
        "",
        "mm",
        [
            _line(
                "1",
                (0, 0),
                (1, 0),
                {"semantic_id": "urn:wall:1", "ontology_class": "top:Wall"},
            )
        ],
    )

    assert base.revision != moved.revision
    assert base.revision != tagged.revision
    json.dumps(base.entities[0].canonical())


def test_room_delete_requires_cascade_for_hosted_openings() -> None:
    snapshot = DrawingSnapshot("managed.dwg", "", "mm", [])
    nodes = {
        "urn:room:1": {
            "@id": "urn:room:1",
            "@type": "top:Room",
            "cad:managed": True,
            "cad:handles": ["10"],
        },
        "urn:wall:1": {
            "@id": "urn:wall:1",
            "@type": "top:Wall",
            "cad:managed": True,
            "cad:handles": ["11"],
            "cad:parent": "urn:room:1",
        },
        "urn:door:1": {
            "@id": "urn:door:1",
            "@type": "top:Door",
            "cad:managed": True,
            "cad:handles": ["12", "13"],
            "cad:hostWall": "urn:wall:1",
        },
    }
    analysis = {"graph": {"@graph": list(nodes.values())}, "candidates": []}
    base_payload = {
        "@context": {"top": "http://w3id.org/topologicpy#"},
        "base_revision": snapshot.revision,
        "changes": [{"op": "delete", "@id": "urn:room:1"}],
    }

    with pytest.raises(ValueError, match="cascade=true"):
        TopologyEngine().plan_changes(
            ChangeDocument.model_validate(base_payload), snapshot, analysis
        )

    base_payload["changes"][0]["cascade"] = True
    operations, _, _ = TopologyEngine().plan_changes(
        ChangeDocument.model_validate(base_payload), snapshot, analysis
    )

    assert operations[0]["handles"] == ["10", "11", "12", "13"]


def test_preview_rejects_a_stale_drawing_revision() -> None:
    snapshot = DrawingSnapshot("empty.dwg", "", "mm", [])
    analysis = TopologyEngine().analyze(snapshot)
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": "sha256:stale",
            "changes": [
                {
                    "op": "create",
                    "@type": "top:Wall",
                    "geometry": {
                        "start": [0, 0],
                        "end": [5000, 0],
                        "thickness": 200,
                    },
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="base_revision"):
        TopologyEngine().plan_changes(document, snapshot, analysis)


def test_relationships_include_host_containment_adjacency_and_connectivity() -> None:
    nodes = [
        {
            "@id": "urn:room:left",
            "@type": "top:Room",
            "cad:geometry": {"boundary": [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]},
        },
        {
            "@id": "urn:room:right",
            "@type": "top:Room",
            "cad:geometry": {
                "boundary": [[5000, 0], [9000, 0], [9000, 4000], [5000, 4000]]
            },
        },
        {
            "@id": "urn:wall:shared",
            "@type": "top:Wall",
            "cad:parent": "urn:room:left",
            "cad:geometry": {
                "start": [5000, 0, 0],
                "end": [5000, 4000, 0],
                "thickness": 200,
            },
        },
        {
            "@id": "urn:door:shared",
            "@type": "top:Door",
            "cad:hostWall": "urn:wall:shared",
            "cad:geometry": {
                "host_wall_id": "urn:wall:shared",
                "offset": 1500,
                "width": 900,
                "hinge": "left",
                "swing": "in",
            },
        },
    ]

    relations = TopologyEngine()._build_relations(nodes)
    triples = {
        (item["subject"], item["predicate"], item["object"]) for item in relations
    }

    assert (
        "urn:room:left",
        "top:adjacentTo",
        "urn:room:right",
    ) in triples
    assert (
        "urn:wall:shared",
        "top:containsElement",
        "urn:door:shared",
    ) in triples
    assert (
        "urn:room:left",
        "top:connectsTo",
        "urn:room:right",
    ) in triples


def test_analysis_reports_unsupported_geometry_and_ontology_classes() -> None:
    snapshot = DrawingSnapshot(
        "review.dwg",
        "",
        "mm",
        [
            EntitySnapshot(
                handle="A",
                object_type="AecDbWall",
                layer="A-Wall",
                geometry={"kind": "unsupported"},
            ),
            _line(
                "B",
                (0, 0),
                (1000, 0),
                {"semantic_id": "urn:roof:1", "ontology_class": "top:Roof"},
            ),
        ],
    )

    result = TopologyEngine().analyze(snapshot)

    assert result["node_count"] == 0
    assert {issue["code"] for issue in result["issues"]} == {
        "unsupported_geometry",
        "unsupported_ontology_class",
    }


def test_explicit_closed_polyline_annotation_becomes_room_boundary() -> None:
    entity = EntitySnapshot(
        handle="A",
        object_type="AcDbPolyline",
        layer="ROOMS",
        geometry={
            "kind": "polyline",
            "vertices": [[0, 0, 0], [5000, 0, 0], [5000, 4000, 0], [0, 4000, 0]],
            "closed": True,
        },
        semantic={
            "semantic_id": "urn:room:polyline",
            "ontology_class": "top:Room",
            "managed": False,
        },
    )

    result = TopologyEngine().analyze(DrawingSnapshot("room.dwg", "", "mm", [entity]))

    node = result["graph"]["@graph"][0]
    assert node["cad:geometry"]["boundary"] == [
        [0.0, 0.0],
        [5000.0, 0.0],
        [5000.0, 4000.0],
        [0.0, 4000.0],
    ]
    assert node["cad:areaSquareMetres"] == pytest.approx(20.0)


def test_preview_rejects_missing_host_wall_before_mutation() -> None:
    snapshot = DrawingSnapshot("empty.dwg", "", "mm", [])
    analysis = TopologyEngine().analyze(snapshot)
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "create",
                    "@type": "top:Door",
                    "geometry": {
                        "host_wall_id": "urn:wall:missing",
                        "offset": 1000,
                        "width": 900,
                        "hinge": "left",
                        "swing": "in",
                    },
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="Missing host wall"):
        TopologyEngine().plan_changes(document, snapshot, analysis)


def test_annotated_legacy_wall_persists_host_geometry_and_warning() -> None:
    wall = _line("A", (0, 0), (5000, 0))
    snapshot = DrawingSnapshot("wall.dwg", "", "mm", [wall])
    engine = TopologyEngine()
    analysis = engine.analyze(snapshot)
    document = ChangeDocument.model_validate(
        {
            "@context": {"top": "http://w3id.org/topologicpy#"},
            "base_revision": snapshot.revision,
            "changes": [
                {
                    "op": "annotate",
                    "@id": "urn:wall:legacy",
                    "@type": "top:Wall",
                    "targets": {"handles": ["A"]},
                },
                {
                    "op": "create",
                    "@id": "urn:window:new",
                    "@type": "top:Window",
                    "geometry": {
                        "host_wall_id": "urn:wall:legacy",
                        "offset": 1000,
                        "width": 1200,
                    },
                },
            ],
        }
    )

    operations, _, warnings = engine.plan_changes(document, snapshot, analysis)

    assert operations[0]["geometry"] == {
        "kind": "line",
        "start": [0.0, 0.0, 0.0],
        "end": [5000.0, 0.0, 0.0],
        "thickness": 200.0,
    }
    assert "defaults to 200 mm" in warnings[0]
