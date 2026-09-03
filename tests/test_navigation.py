from __future__ import annotations

from topology_engine.navigation import (
    connection_semantic_id,
    diagnose_connectivity,
    find_route,
    graph_revision,
    infer_connection_candidates,
    portal_width_mm,
)


def test_connection_id_is_order_independent_and_portal_specific() -> None:
    first = connection_semantic_id("space:a", "space:b", "door:1")
    assert first == connection_semantic_id("space:b", "space:a", "door:1")
    assert first != connection_semantic_id("space:a", "space:b", "door:2")


def test_portal_width_uses_metadata_and_clear_width_before_fallback() -> None:
    assert portal_width_mm({"cad:geometry": {"clear_width_mm": 1219.2}}) == 1219.2
    assert portal_width_mm({"cad:properties": {"nominal_width_mm": 838.2}}) == 838.2
    assert portal_width_mm({
        "cad:geometry": {"width": 1000},
        "cad:properties": {"clear_width_mm": 950},
    }) == 950
    assert portal_width_mm({"cad:geometry": {"width": float("nan")}}) == 900


def test_confirmed_unhosted_portal_does_not_duplicate_logical_candidate() -> None:
    left = _space("space:a")
    left["top:connectsTo"] = [{"@id": "space:b"}]
    graph = {"@graph": [left, _space("space:b"), {
        "@id": "connection:ab", "@type": "top:Connection",
        "cad:geometry": {"from_space_id": "space:b", "to_space_id": "space:a",
                         "via_id": "door:ab", "status": "confirmed"},
    }]}
    assert infer_connection_candidates(graph) == []


def _space(semantic_id: str, *, entry: bool = False) -> dict:
    return {
        "@id": semantic_id,
        "@type": "top:Space",
        "rdfs:label": semantic_id,
        "cad:geometry": {"boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]},
        "cad:properties": {"is_entry": entry},
    }


def test_confirmed_connection_produces_route_and_reachability() -> None:
    graph = {
        "@graph": [
            _space("space:a", entry=True),
            _space("space:b"),
            {
                "@id": "connection:ab",
                "@type": "top:Connection",
                "cad:geometry": {
                    "from_space_id": "space:a",
                    "to_space_id": "space:b",
                    "clear_width_mm": 1250,
                    "status": "confirmed",
                    "direction": "bidirectional",
                },
            },
        ]
    }

    route = find_route(graph, "space:a", "space:b", 1200)

    assert route["success"] is True
    assert route["connections"] == ["connection:ab"]
    assert route["limiting_width_mm"] == 1250
    assert not any(
        issue["code"] == "UNREACHABLE_SPACE"
        for issue in diagnose_connectivity(graph, 1200)
    )


def test_derived_relation_is_only_a_stable_candidate() -> None:
    left = _space("space:a", entry=True)
    left["top:connectsTo"] = [{"@id": "space:b"}]
    graph = {
        "@graph": [
            left,
            _space("space:b"),
            {
                "@id": "wall:ab",
                "@type": "top:Wall",
                "cad:boundingRooms": ["space:a", "space:b"],
            },
            {
                "@id": "door:ab",
                "@type": "top:Door",
                "cad:hostWall": "wall:ab",
                "cad:geometry": {"host_wall_id": "wall:ab", "width": 1000},
            },
        ]
    }

    first = infer_connection_candidates(graph)
    second = infer_connection_candidates(graph)

    assert first == second
    assert first[0]["cad:geometry"]["status"] == "candidate"
    assert first[0]["cad:geometry"]["via_id"] == "door:ab"
    assert first[0]["cad:geometry"]["clear_width_mm"] == 1000
    assert (
        find_route({"@graph": graph["@graph"] + first}, "space:a", "space:b")["success"]
        is False
    )


def test_graph_revision_is_independent_of_derived_revision_fields() -> None:
    graph = {"@graph": [_space("space:a")]}
    revision = graph_revision(graph)
    graph["cad:graphRevision"] = "old"
    graph["cad:workspaceConflicts"] = 99
    assert graph_revision(graph) == revision
