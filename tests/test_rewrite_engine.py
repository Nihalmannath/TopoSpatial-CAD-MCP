from __future__ import annotations

import pytest

from design_engine.workspace import DraftRequest, TopologyWorkspaceService
from rewrite_engine import GraphRewriteRule, RewriteEngine
from topology_engine.navigation import graph_revision


def _graph() -> dict:
    graph = {
        "cad:revision": "sha256:drawing-a",
        "@graph": [
            {
                "@id": "space:a",
                "@type": "top:Space",
                "rdfs:label": "Entry",
                "cad:properties": {"is_entry": True},
                "cad:geometry": {
                    "boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
                },
            }
        ],
    }
    graph["cad:graphRevision"] = graph_revision(graph)
    return graph


def test_rewrite_proposal_does_not_mutate_source_graph() -> None:
    source = _graph()
    original_revision = source["cad:graphRevision"]
    rule = GraphRewriteRule.model_validate(
        {
            "rule_id": "mark-entry-public",
            "description": "Mark entry spaces as public rooms",
            "patterns": [
                {
                    "alias": "space",
                    "@type": "top:Space",
                    "properties": {"cad:properties.is_entry": True},
                }
            ],
            "rewrites": [
                {
                    "op": "patch_node",
                    "target_alias": "space",
                    "patch": {"cad:properties": {"zone": "public"}},
                }
            ],
        }
    )

    proposals = RewriteEngine().propose(source, [rule])

    assert source["cad:graphRevision"] == original_revision
    assert source["@graph"][0]["cad:properties"] == {"is_entry": True}
    assert proposals[0].mutated is False
    assert proposals[0].editor_action["commands"][0]["op"] == "patch_node"
    assert proposals[0].graph["@graph"][0]["cad:properties"]["zone"] == "public"


def test_rewrite_editor_action_can_be_previewed(tmp_path, monkeypatch) -> None:
    from core.config import get_config

    monkeypatch.setattr(get_config().output, "directory", str(tmp_path))
    current = _graph()
    rule = GraphRewriteRule.model_validate(
        {
            "rule_id": "rename-entry",
            "description": "Rename entry space",
            "patterns": [{"alias": "space", "@type": "top:Space"}],
            "rewrites": [
                {
                    "op": "patch_node",
                    "target_alias": "space",
                    "patch": {"rdfs:label": "Arrival"},
                }
            ],
        }
    )
    proposal = RewriteEngine().propose(current, [rule])[0]
    command = proposal.editor_action["commands"][0]
    request = DraftRequest.model_validate(
        {
            "drawing_name": "plan.dwg",
            "base_drawing_revision": current["cad:revision"],
            "base_graph_revision": current["cad:graphRevision"],
            "commands": [command],
        }
    )

    preview = TopologyWorkspaceService(ttl_seconds=60).preview(request, current)

    assert preview["mutated"] is False
    assert preview["command_count"] == 1
    assert proposal.topology_changes[0]["op"] == "update"
    assert proposal.topology_changes[0]["@id"] == "space:a"
    assert proposal.topology_changes[0]["label"] == "Arrival"


def test_upsert_requires_a_resolvable_semantic_id() -> None:
    with pytest.raises(ValueError, match="requires a semantic ID"):
        GraphRewriteRule.model_validate(
            {
                "rule_id": "missing-id",
                "description": "Create an unnamed space",
                "patterns": [{"alias": "space", "@type": "top:Space"}],
                "rewrites": [
                    {
                        "op": "upsert_node",
                        "node": {"@type": "top:Space"},
                    }
                ],
            }
        )


def test_rule_matches_only_connected_spaces() -> None:
    graph = _graph()
    graph["@graph"].extend(
        [
            {
                "@id": "space:b",
                "@type": "top:Space",
                "cad:geometry": {
                    "boundary": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]]
                },
            },
            {
                "@id": "space:c",
                "@type": "top:Space",
                "cad:geometry": {
                    "boundary": [[8000, 0], [12000, 0], [12000, 3000], [8000, 3000]]
                },
            },
        ]
    )
    graph["@graph"][0]["top:connectsTo"] = [{"@id": "space:b"}]
    graph["cad:graphRevision"] = graph_revision(graph)
    rule = GraphRewriteRule.model_validate(
        {
            "rule_id": "mark-connected",
            "description": "Mark a space connected to an entry",
            "patterns": [
                {"alias": "entry", "properties": {"cad:properties.is_entry": True}},
                {"alias": "neighbor", "@type": "top:Space"},
            ],
            "relations": [
                {
                    "source_alias": "entry",
                    "predicate": "top:connectsTo",
                    "target_alias": "neighbor",
                }
            ],
            "rewrites": [
                {
                    "op": "patch_node",
                    "target_alias": "neighbor",
                    "patch": {"cad:properties": {"entry_connected": True}},
                }
            ],
            "max_applications": 3,
        }
    )

    proposals = RewriteEngine().propose(graph, [rule])

    assert [item.match.bindings["neighbor"] for item in proposals] == ["space:b"]
    assert "cad:properties" not in graph["@graph"][1]


@pytest.mark.parametrize(
    ("semantic_id", "expected_op"),
    [("space:a", "update"), ("space:new", "create")],
)
def test_upsert_compiles_the_right_topology_change(
    semantic_id: str, expected_op: str
) -> None:
    rule = GraphRewriteRule.model_validate(
        {
            "rule_id": "upsert-space",
            "description": "Propose a space change",
            "patterns": [{"alias": "space", "@type": "top:Space"}],
            "rewrites": [
                {
                    "op": "upsert_node",
                    "node": {
                        "@id": semantic_id,
                        "@type": "top:Space",
                        "rdfs:label": "Arrival",
                        "cad:geometry": {
                            "boundary": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
                        },
                    },
                }
            ],
        }
    )

    proposal = RewriteEngine().propose(_graph(), [rule])[0]

    assert proposal.topology_changes[0]["op"] == expected_op
    assert proposal.topology_changes[0]["@id"] == semantic_id
