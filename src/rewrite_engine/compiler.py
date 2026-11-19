"""Compile rewrite matches into existing preview-compatible proposals."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Dict, List

from design_engine.workspace import EditorAction, EditorCommand
from topology_engine.models import TopologyChange

from .models import GraphRewriteMatch, GraphRewriteRule, RewritePatch


class RewriteCompiler:
    """Convert graph rewrite patches into editor commands and topology changes."""

    def compile_editor_action(
        self,
        rule: GraphRewriteRule,
        match: GraphRewriteMatch,
        graph: Dict[str, Any],
    ) -> EditorAction:
        """Return an atomic editor action suitable for workspace preview."""
        commands = [
            self._compile_editor_command(patch, match)
            for patch in rule.rewrites
            if patch.op in {"upsert_node", "patch_node", "delete_node"}
        ]
        return EditorAction(
            action_id=self._action_id(rule.rule_id, match.bindings),
            description=rule.description,
            commands=commands,
            topology_changes=self.compile_topology_changes(rule, match, graph),
        )

    def compile_topology_changes(
        self,
        rule: GraphRewriteRule,
        match: GraphRewriteMatch,
        graph: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return best-effort topology preview changes for create/update/delete."""
        changes: List[Dict[str, Any]] = []
        existing_ids = {str(node.get("@id")) for node in graph.get("@graph", [])}
        for patch in rule.rewrites:
            change = self._compile_topology_change(patch, match, existing_ids)
            if change is not None:
                changes.append(change.model_dump(by_alias=True, exclude_none=True))
        return changes

    @staticmethod
    def _compile_editor_command(
        patch: RewritePatch, match: GraphRewriteMatch
    ) -> EditorCommand:
        semantic_id = _resolve_semantic_id(patch, match)
        payload = {
            "op": patch.op,
            "semantic_id": semantic_id,
            "patch": copy.deepcopy(patch.patch),
        }
        if patch.node is not None:
            node = copy.deepcopy(patch.node)
            node["@id"] = semantic_id
            payload["node"] = node
        return EditorCommand.model_validate(payload)

    @staticmethod
    def _compile_topology_change(
        patch: RewritePatch, match: GraphRewriteMatch, existing_ids: set[str]
    ) -> TopologyChange | None:
        semantic_id = _resolve_semantic_id(patch, match)
        if patch.op == "delete_node":
            return TopologyChange(op="delete", **{"@id": semantic_id})
        if patch.op == "patch_node":
            geometry = patch.patch.get("cad:geometry")
            label = patch.patch.get("rdfs:label") or patch.label
            if geometry is None and label is None:
                return None
            return TopologyChange(
                op="update",
                **{"@id": semantic_id},
                geometry=copy.deepcopy(geometry),
                label=label,
            )
        if patch.op == "upsert_node" and patch.node:
            ontology_class = patch.node.get("@type")
            geometry = patch.node.get("cad:geometry")
            label = patch.node.get("rdfs:label") or patch.label
            if semantic_id in existing_ids:
                if geometry is None and label is None:
                    return None
                return TopologyChange(
                    op="update",
                    **{"@id": semantic_id},
                    label=label,
                    geometry=copy.deepcopy(geometry),
                )
            if ontology_class and geometry is not None:
                return TopologyChange(
                    op="create",
                    **{"@id": semantic_id, "@type": ontology_class},
                    label=label,
                    geometry=copy.deepcopy(geometry),
                )
        return None

    @staticmethod
    def _action_id(rule_id: str, bindings: Dict[str, str]) -> str:
        payload = "|".join(f"{key}:{value}" for key, value in sorted(bindings.items()))
        digest = hashlib.sha256(f"{rule_id}|{payload}".encode("utf-8")).hexdigest()
        return f"rewrite_{digest[:20]}"


def _resolve_semantic_id(patch: RewritePatch, match: GraphRewriteMatch) -> str:
    if patch.semantic_id:
        return patch.semantic_id
    if patch.target_alias and patch.target_alias in match.bindings:
        return match.bindings[patch.target_alias]
    if patch.node and patch.node.get("@id"):
        return str(patch.node["@id"])
    raise ValueError("Rewrite patch target was not bound by the matcher")
