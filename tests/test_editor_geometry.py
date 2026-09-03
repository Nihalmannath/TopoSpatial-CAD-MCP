from __future__ import annotations

from topology_engine.editor_geometry import expand_shared_wall_edits
from topology_engine.models import TopologyChange


def test_shared_wall_endpoint_move_updates_both_space_boundaries() -> None:
    graph = {
        "@graph": [
            {
                "@id": "room:a",
                "@type": "top:Room",
                "cad:geometry": {
                    "boundary": [[0, 0], [4000, 0], [4000, 4000], [0, 4000]]
                },
            },
            {
                "@id": "room:b",
                "@type": "top:Room",
                "cad:geometry": {
                    "boundary": [
                        [4000, 0],
                        [8000, 0],
                        [8000, 4000],
                        [4000, 4000],
                    ]
                },
            },
            {
                "@id": "wall:shared",
                "@type": "top:Wall",
                "cad:boundingRooms": ["room:a", "room:b"],
                "cad:geometry": {
                    "start": [4000, 0],
                    "end": [4000, 4000],
                    "thickness": 200,
                },
            },
        ]
    }
    change = TopologyChange.model_validate(
        {
            "op": "update",
            "@id": "wall:shared",
            "geometry": {
                "start": [4200, 0],
                "end": [4200, 4000],
                "thickness": 200,
            },
        }
    )

    expanded = expand_shared_wall_edits(graph, [change])
    updates = {item.semantic_id: item.geometry for item in expanded}

    assert len(expanded) == 3
    assert updates["room:a"]["boundary"][1] == [4200.0, 0.0]
    assert updates["room:a"]["boundary"][2] == [4200.0, 4000.0]
    assert updates["room:b"]["boundary"][0] == [4200.0, 0.0]
    assert updates["room:b"]["boundary"][3] == [4200.0, 4000.0]
