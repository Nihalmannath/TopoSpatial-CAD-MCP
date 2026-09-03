"""Persistent topology-editor drafts, merge state, and semantic transactions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import get_config
from topology_engine.navigation import (
    graph_revision,
    infer_connection_candidates,
)
from topology_engine.spatial_program import diagnose_graph, validate_semantic_node

WORKSPACE_SCHEMA_VERSION = 3
EDITOR_REQUEST_SCHEMA_VERSION = 1
ACTIVE_EDITOR_REQUEST_STATUSES = {"pending", "claimed", "preview_ready"}


class EditorCommand(BaseModel):
    """One semantic edit staged by the visual topology editor."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[
        "upsert_node",
        "patch_node",
        "delete_node",
        "confirm_connection",
        "reject_connection",
    ]
    semantic_id: str = Field(min_length=1)
    node: Optional[Dict[str, Any]] = None
    patch: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_command(self) -> "EditorCommand":
        """Require the payload appropriate for the selected editor command."""
        if self.op == "upsert_node" and self.node is None:
            raise ValueError("upsert_node requires node")
        if self.op == "patch_node" and not self.patch:
            raise ValueError("patch_node requires patch")
        return self


class EditorAction(BaseModel):
    """One atomic architect gesture in the topology playground."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=256)
    commands: List[EditorCommand] = Field(default_factory=list, max_length=1000)
    topology_changes: List[Dict[str, Any]] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def non_empty_action(self) -> "EditorAction":
        if not self.commands and not self.topology_changes:
            raise ValueError("EditorAction requires commands or topology_changes")
        self.description = self.description.strip()
        return self


class DraftRequest(BaseModel):
    """A revision-bound semantic draft from the topology editor."""

    model_config = ConfigDict(extra="forbid")

    drawing_name: str = Field(min_length=1)
    base_drawing_revision: str = Field(min_length=8)
    base_graph_revision: str = Field(min_length=8)
    commands: List[EditorCommand] = Field(default_factory=list, max_length=1000)


class WorkingDraftRequest(DraftRequest):
    """A complete deterministic playground command history."""

    actions: List[EditorAction] = Field(default_factory=list, max_length=100)
    required_width_mm: float = Field(default=1200.0, ge=0.0)
    undo_position: int = Field(default=0, ge=0, le=100)
    topology_changes: List[Dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_actions(self) -> "WorkingDraftRequest":
        """Accept version-2 callers while making actions authoritative."""
        if not self.actions and (self.commands or self.topology_changes):
            self.actions = [
                EditorAction(
                    action_id="legacy_request",
                    description="Imported version-2 request",
                    commands=self.commands,
                    topology_changes=self.topology_changes,
                )
            ]
            if self.undo_position > 0 or self.commands or self.topology_changes:
                self.undo_position = 1
        if self.undo_position > len(self.actions):
            raise ValueError("undo_position cannot exceed the number of actions")
        if sum(len(item.commands) for item in self.actions[: self.undo_position]) > 1000:
            raise ValueError("Active draft exceeds 1000 commands")
        return self


@dataclass
class WorkspaceTransaction:
    """One non-mutating editor preview waiting for explicit apply."""

    transaction_id: str
    drawing_name: str
    base_drawing_revision: str
    base_graph_revision: str
    graph: Dict[str, Any]
    commands: List[Dict[str, Any]]
    diagnostics: List[Dict[str, Any]]
    created_at: float
    expires_at: float
    editor_request_id: str | None = None


class TopologyWorkspaceService:
    """Merge CAD topology with editor-owned semantic graph state."""

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        """Initialize thread-safe per-drawing workspace and preview storage."""
        topology = get_config().topology
        self.ttl_seconds = int(ttl_seconds or topology.transaction_ttl_seconds)
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._transactions: Dict[str, WorkspaceTransaction] = {}

    def merge_graph(
        self,
        drawing_name: str,
        drawing_revision: str,
        base_graph: Dict[str, Any],
        *,
        include_candidates: bool = False,
    ) -> Dict[str, Any]:
        """Overlay committed editor semantics on a CAD-derived graph."""
        with self._lock:
            state = self._load_state(drawing_name)
            graph = copy.deepcopy(base_graph)
            nodes = {
                str(node.get("@id")): node
                for node in graph.get("@graph", [])
                if node.get("@type") not in {"top:CandidateSpace", "top:CandidatePortal"}
            }
            conflicts = self._detect_conflicts(
                state, nodes, drawing_revision=drawing_revision
            )
            for semantic_id in state.get("deleted_ids", []):
                nodes.pop(str(semantic_id), None)
            for semantic_id, override in state.get("overrides", {}).items():
                existing = nodes.get(semantic_id, {"@id": semantic_id})
                nodes[semantic_id] = self._deep_merge(existing, override)
            graph["@graph"] = sorted(nodes.values(), key=lambda item: item["@id"])
            graph["cad:revision"] = drawing_revision
            graph["cad:graphRevision"] = graph_revision(graph)
            graph["cad:workspaceConflicts"] = len(conflicts)
            if include_candidates:
                candidates = infer_connection_candidates(graph)
                existing_ids = {node["@id"] for node in graph["@graph"]}
                graph["@graph"].extend(
                    item for item in candidates if item["@id"] not in existing_ids
                )
                graph["@graph"].sort(key=lambda item: item["@id"])
            state["conflicts"] = conflicts
            state["last_seen_drawing_revision"] = drawing_revision
            working = state.get("working_draft")
            if working and (
                working.get("base_drawing_revision") != drawing_revision
                or working.get("base_graph_revision") != graph["cad:graphRevision"]
            ):
                working["frozen"] = True
                working["freeze_reason"] = "BASE_REVISION_CHANGED"
                state["working_draft"] = working
            self._states[self._key(drawing_name)] = state
            self._write_state(drawing_name, state)
            return graph

    def commit_design_semantics(
        self,
        drawing_name: str,
        drawing_revision: str,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        """Persist handleless design nodes that cannot be reconstructed from XData.

        Physical rooms, walls and portals are recovered from their CAD handles.
        Pure graph objects such as ``top:Connection`` require a durable workspace
        override or they disappear during the verification snapshot.
        The returned state is a rollback token for the caller's CAD transaction.
        """
        semantic_ops = [
            item
            for item in operations
            if item.get("ontology_class") in {"top:Connection", "top:SpatialIntent"}
            and not item.get("handles")
            and not item.get("old_handles")
        ]
        if not semantic_ops:
            return None
        with self._lock:
            previous = copy.deepcopy(self._load_state(drawing_name))
            state = copy.deepcopy(previous)
            overrides = dict(state.get("overrides", {}))
            deleted = set(map(str, state.get("deleted_ids", [])))
            for operation in semantic_ops:
                semantic_id = str(operation.get("semantic_id", ""))
                if not semantic_id:
                    continue
                kind = str(operation.get("kind", ""))
                if kind.startswith("delete"):
                    overrides.pop(semantic_id, None)
                    deleted.add(semantic_id)
                    continue
                existing = copy.deepcopy(overrides.get(semantic_id, {"@id": semantic_id}))
                existing.update(
                    {
                        "@id": semantic_id,
                        "@type": operation.get("ontology_class"),
                        "cad:managed": True,
                    }
                )
                if operation.get("label") is not None:
                    existing["rdfs:label"] = operation.get("label", "")
                if isinstance(operation.get("geometry"), dict):
                    existing["cad:geometry"] = copy.deepcopy(operation["geometry"])
                if operation.get("properties"):
                    existing["cad:properties"] = self._deep_merge(
                        existing.get("cad:properties", {}),
                        copy.deepcopy(operation["properties"]),
                    )
                overrides[semantic_id] = self._semantic_projection(existing)
                deleted.discard(semantic_id)
            state.update(
                {
                    "schema_version": WORKSPACE_SCHEMA_VERSION,
                    "drawing_name": drawing_name,
                    "last_synced_drawing_revision": drawing_revision,
                    "overrides": overrides,
                    "deleted_ids": sorted(deleted),
                    "updated_at": time.time(),
                }
            )
            self._states[self._key(drawing_name)] = state
            self._write_state(drawing_name, state)
            return previous

    def restore_state(self, drawing_name: str, state: Dict[str, Any]) -> None:
        """Restore an exact workspace state after a failed CAD transaction."""
        with self._lock:
            restored = copy.deepcopy(state)
            self._states[self._key(drawing_name)] = restored
            self._write_state(drawing_name, restored)

    def evaluate_draft(
        self, request: WorkingDraftRequest, current_graph: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Evaluate and persist a playground draft using graph data only."""
        with self._lock:
            current_drawing_revision = str(current_graph.get("cad:revision", ""))
            current_graph_revision = str(
                current_graph.get("cad:graphRevision") or graph_revision(current_graph)
            )
            if request.base_drawing_revision != current_drawing_revision:
                raise ValueError("STALE_DRAWING_REVISION")
            if request.base_graph_revision != current_graph_revision:
                raise ValueError("STALE_GRAPH_REVISION")
            active_actions = list(request.actions[: request.undo_position])
            commands = [command for action in active_actions for command in action.commands]
            topology_changes = [
                change for action in active_actions for change in action.topology_changes
            ]
            draft_graph = copy.deepcopy(current_graph)
            affected: set[str] = set()
            for command in commands:
                self._apply_command(draft_graph, command)
                affected.add(command.semantic_id)
            for node in draft_graph.get("@graph", []):
                validate_semantic_node(node)
            draft_graph["@graph"] = sorted(
                draft_graph.get("@graph", []), key=lambda item: item["@id"]
            )
            draft_graph["cad:graphRevision"] = graph_revision(draft_graph)
            diagnostics = diagnose_graph(draft_graph, request.required_width_mm)
            state = self._load_state(request.drawing_name)
            working = {
                "schema_version": WORKSPACE_SCHEMA_VERSION,
                "drawing_name": request.drawing_name,
                "base_drawing_revision": request.base_drawing_revision,
                "base_graph_revision": request.base_graph_revision,
                "base_graph": copy.deepcopy(current_graph),
                "graph": draft_graph,
                "graph_revision": draft_graph["cad:graphRevision"],
                "actions": [item.model_dump(mode="json") for item in request.actions],
                "commands": [item.model_dump(mode="json") for item in commands],
                "topology_changes": copy.deepcopy(topology_changes),
                "required_width_mm": request.required_width_mm,
                "affected_ids": sorted(affected),
                "diagnostics": diagnostics,
                "undo_position": request.undo_position,
                "dirty": bool(commands or topology_changes),
                "frozen": False,
                "conflicts": [],
                "updated_at": time.time(),
            }
            self._supersede_editor_requests(state, working)
            state["working_draft"] = working
            self._states[self._key(request.drawing_name)] = state
            self._write_state(request.drawing_name, state)
            return self._draft_response(working)

    def reset_draft(self, drawing_name: str) -> Dict[str, Any]:
        """Discard only the playground draft; never touch CAD or committed data."""
        with self._lock:
            state = self._load_state(drawing_name)
            state["working_draft"] = None
            self._write_state(drawing_name, state)
            return {"success": True, "status": "reset", "mutated": False}

    def working_draft(self, drawing_name: str) -> Dict[str, Any] | None:
        """Return the complete persisted playground draft."""
        with self._lock:
            return copy.deepcopy(self._load_state(drawing_name).get("working_draft"))

    def working_draft_payload(self, drawing_name: str) -> Dict[str, Any] | None:
        """Return the complete draft with its public evaluated response fields."""
        with self._lock:
            working = self._load_state(drawing_name).get("working_draft")
            if not working:
                return None
            payload = self._draft_response(working)
            payload["actions"] = copy.deepcopy(working.get("actions", []))
            payload["commands"] = copy.deepcopy(working.get("commands", []))
            payload["topology_changes"] = copy.deepcopy(
                working.get("topology_changes", [])
            )
            return payload

    def draft_graph(self, drawing_name: str, revision: str) -> Dict[str, Any]:
        """Resolve an exact persisted draft graph without CAD access."""
        working = self.working_draft(drawing_name)
        if not working or working.get("graph_revision") != revision:
            raise ValueError("UNKNOWN_DRAFT_REVISION")
        if working.get("frozen"):
            raise ValueError("DRAFT_FROZEN")
        return copy.deepcopy(working["graph"])

    def rebase_draft(
        self, drawing_name: str, current_graph: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Replay unchanged draft targets on a new base or report conflicts."""
        with self._lock:
            state = self._load_state(drawing_name)
            working = state.get("working_draft")
            if not working:
                raise ValueError("NO_WORKING_DRAFT")
            old_nodes = {
                str(n.get("@id")): n
                for n in working.get("base_graph", {}).get("@graph", [])
            }
            new_nodes = {str(n.get("@id")): n for n in current_graph.get("@graph", [])}
            conflicts: List[Dict[str, Any]] = []
            actions = self._actions_from_working(working)
            active_actions = actions[: int(working.get("undo_position", len(actions)))]
            commands = [command for action in active_actions for command in action.commands]
            for command in commands:
                if command.op == "upsert_node" and command.semantic_id not in old_nodes:
                    if command.semantic_id in new_nodes:
                        conflicts.append(
                            self._conflict(
                                command.semantic_id,
                                "@id",
                                None,
                                new_nodes[command.semantic_id],
                                command.node,
                            )
                        )
                    continue
                before = old_nodes.get(command.semantic_id)
                after = new_nodes.get(command.semantic_id)
                if before != after:
                    conflicts.append(
                        self._conflict(
                            command.semantic_id,
                            "@id",
                            before,
                            after,
                            command.model_dump(mode="json"),
                        )
                    )
            if conflicts:
                working["frozen"] = True
                working["freeze_reason"] = "REBASE_CONFLICTS"
                working["conflicts"] = conflicts
                working["rebase_graph"] = copy.deepcopy(current_graph)
                state["working_draft"] = working
                self._write_state(drawing_name, state)
                return self._draft_response(working)
            request = WorkingDraftRequest(
                drawing_name=drawing_name,
                base_drawing_revision=str(current_graph.get("cad:revision")),
                base_graph_revision=str(current_graph.get("cad:graphRevision")),
                actions=actions,
                required_width_mm=float(working.get("required_width_mm", 1200.0)),
                undo_position=min(int(working.get("undo_position", len(actions))), len(actions)),
            )
            return self.evaluate_draft(request, current_graph)

    def resolve_draft_conflicts(
        self,
        drawing_name: str,
        resolutions: Dict[str, Literal["cad", "editor"]],
    ) -> Dict[str, Any]:
        """Resolve frozen rebase conflicts and replay the chosen command set."""
        with self._lock:
            state = self._load_state(drawing_name)
            working = state.get("working_draft")
            if not working or not working.get("rebase_graph"):
                raise ValueError("NO_DRAFT_REBASE_CONFLICTS")
            conflicts = list(working.get("conflicts", []))
            unresolved = [
                item for item in conflicts if item["conflict_id"] not in resolutions
            ]
            if unresolved:
                working["conflicts"] = unresolved
                state["working_draft"] = working
                self._write_state(drawing_name, state)
                return self._draft_response(working)
            cad_targets = {
                item["semantic_id"]
                for item in conflicts
                if resolutions.get(item["conflict_id"]) == "cad"
            }
            actions = self._actions_from_working(working)
            filtered_actions: List[EditorAction] = []
            for action in actions:
                retained = [
                    command
                    for command in action.commands
                    if command.semantic_id not in cad_targets
                ]
                if retained or action.topology_changes:
                    filtered_actions.append(
                        EditorAction(
                            action_id=action.action_id,
                            description=action.description,
                            commands=retained,
                            topology_changes=action.topology_changes,
                        )
                    )
            new_graph = copy.deepcopy(working["rebase_graph"])
            request = WorkingDraftRequest(
                drawing_name=drawing_name,
                base_drawing_revision=str(new_graph.get("cad:revision")),
                base_graph_revision=str(new_graph.get("cad:graphRevision")),
                actions=filtered_actions,
                required_width_mm=float(working.get("required_width_mm", 1200.0)),
                undo_position=min(int(working.get("undo_position", 0)), len(filtered_actions)),
            )
            return self.evaluate_draft(request, new_graph)

    def preview(
        self,
        request: DraftRequest,
        current_graph: Dict[str, Any],
        *,
        required_width_mm: float = 1200.0,
        editor_request_id: str | None = None,
    ) -> Dict[str, Any]:
        """Validate and store an editor draft without modifying CAD or sidecars."""
        with self._lock:
            self._purge_transactions()
            current_drawing_revision = str(current_graph.get("cad:revision", ""))
            current_graph_revision = str(
                current_graph.get("cad:graphRevision") or graph_revision(current_graph)
            )
            if request.base_drawing_revision != current_drawing_revision:
                raise ValueError("STALE_DRAWING_REVISION")
            if request.base_graph_revision != current_graph_revision:
                raise ValueError("STALE_GRAPH_REVISION")
            state = self._load_state(request.drawing_name)
            if state.get("conflicts"):
                raise ValueError("WORKSPACE_CONFLICTS_REQUIRE_REVIEW")
            draft_graph = copy.deepcopy(current_graph)
            for command in request.commands:
                self._apply_command(draft_graph, command)
            for node in draft_graph.get("@graph", []):
                validate_semantic_node(node)
            draft_graph["@graph"] = sorted(
                draft_graph.get("@graph", []), key=lambda item: item["@id"]
            )
            draft_graph["cad:graphRevision"] = graph_revision(draft_graph)
            diagnostics = diagnose_graph(draft_graph, required_width_mm)
            now = time.time()
            transaction = WorkspaceTransaction(
                transaction_id=f"workspace_{uuid.uuid4().hex}",
                drawing_name=request.drawing_name,
                base_drawing_revision=request.base_drawing_revision,
                base_graph_revision=request.base_graph_revision,
                graph=draft_graph,
                commands=[
                    command.model_dump(mode="json") for command in request.commands
                ],
                diagnostics=diagnostics,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                editor_request_id=editor_request_id,
            )
            self._transactions[transaction.transaction_id] = transaction
            state["preview"] = {
                "transaction_id": transaction.transaction_id,
                "base_drawing_revision": transaction.base_drawing_revision,
                "base_graph_revision": transaction.base_graph_revision,
                "graph_revision": draft_graph["cad:graphRevision"],
                "commands": transaction.commands,
                "expires_at": transaction.expires_at,
                "editor_request_id": editor_request_id,
            }
            if editor_request_id:
                self._transition_editor_request(
                    state,
                    editor_request_id,
                    "preview_ready",
                    preview_transaction_id=transaction.transaction_id,
                )
            self._states[self._key(request.drawing_name)] = state
            self._write_state(request.drawing_name, state)
            return {
                "success": True,
                "transaction_id": transaction.transaction_id,
                "expires_at": transaction.expires_at,
                "base_drawing_revision": transaction.base_drawing_revision,
                "base_graph_revision": transaction.base_graph_revision,
                "graph_revision": draft_graph["cad:graphRevision"],
                "diagnostics": diagnostics,
                "mutated": False,
                "command_count": len(transaction.commands),
                "editor_request_id": editor_request_id,
            }

    def apply(
        self,
        transaction_id: str,
        current_graph: Dict[str, Any],
        *,
        resulting_drawing_revision: str | None = None,
    ) -> Dict[str, Any]:
        """Commit one non-expired semantic preview to the workspace sidecar."""
        with self._lock:
            self._purge_transactions()
            transaction = self._transactions.get(transaction_id)
            if transaction is None:
                raise ValueError("Unknown or expired workspace transaction")
            current_drawing_revision = str(current_graph.get("cad:revision", ""))
            current_graph_revision = str(
                current_graph.get("cad:graphRevision") or graph_revision(current_graph)
            )
            if current_drawing_revision != transaction.base_drawing_revision:
                raise ValueError("STALE_DRAWING_REVISION")
            if current_graph_revision != transaction.base_graph_revision:
                raise ValueError("STALE_GRAPH_REVISION")
            state = self._load_state(transaction.drawing_name)
            base_nodes = {
                str(node.get("@id")): node for node in current_graph.get("@graph", [])
            }
            target_nodes = {
                str(node.get("@id")): node
                for node in transaction.graph.get("@graph", [])
            }
            overrides: Dict[str, Any] = {}
            for semantic_id, target in target_nodes.items():
                base = base_nodes.get(semantic_id)
                if base != target:
                    overrides[semantic_id] = self._semantic_projection(target)
            deleted_ids = sorted(set(base_nodes) - set(target_nodes))
            state.update(
                {
                    "schema_version": WORKSPACE_SCHEMA_VERSION,
                    "drawing_name": transaction.drawing_name,
                    "last_synced_drawing_revision": (
                        resulting_drawing_revision or current_drawing_revision
                    ),
                    "last_common_graph": {
                        semantic_id: self._semantic_projection(node)
                        for semantic_id, node in base_nodes.items()
                    },
                    "overrides": overrides,
                    "deleted_ids": deleted_ids,
                    "preview": None,
                    "working_draft": None,
                    "conflicts": [],
                    "graph_revision": transaction.graph["cad:graphRevision"],
                    "updated_at": time.time(),
                }
            )
            if transaction.editor_request_id:
                self._transition_editor_request(
                    state,
                    transaction.editor_request_id,
                    "completed",
                    preview_transaction_id=transaction.transaction_id,
                )
            self._states[self._key(transaction.drawing_name)] = state
            path = self._write_state(transaction.drawing_name, state)
            self._transactions.pop(transaction_id, None)
            return {
                "success": True,
                "status": "applied",
                "drawing_revision": current_drawing_revision,
                "graph_revision": state["graph_revision"],
                "workspace_path": str(path),
                "diagnostics": transaction.diagnostics,
            }

    def cancel(self, drawing_name: str, transaction_id: str) -> Dict[str, Any]:
        """Discard a pending editor transaction without mutation."""
        with self._lock:
            transaction = self._transactions.pop(transaction_id, None)
            if transaction is None:
                raise ValueError("Unknown or expired workspace transaction")
            state = self._load_state(drawing_name)
            if state.get("preview", {}).get("transaction_id") == transaction_id:
                state["preview"] = None
            if transaction.editor_request_id:
                self._transition_editor_request(
                    state,
                    transaction.editor_request_id,
                    "pending",
                    preview_transaction_id=None,
                )
            self._write_state(drawing_name, state)
            return {"success": True, "status": "cancelled", "mutated": False}

    def status(self, drawing_name: str) -> Dict[str, Any]:
        """Return compact workspace status for dashboard and MCP clients."""
        with self._lock:
            state = self._load_state(drawing_name)
            draft = state.get("working_draft")
            preview = state.get("preview")
            requests = list(state.get("editor_requests", {}).values())
            return {
                "graph_revision": state.get("graph_revision"),
                "draft_dirty": bool(draft and draft.get("dirty")),
                "preview_pending": bool(preview),
                "draft_revision": (
                    draft.get("graph_revision")
                    if draft
                    else preview.get("graph_revision")
                    if preview
                    else None
                ),
                "affected_ids": draft.get("affected_ids", []) if draft else [],
                "draft_frozen": bool(draft and draft.get("frozen")),
                "conflict_count": len(state.get("conflicts", [])),
                "conflicts": copy.deepcopy(state.get("conflicts", [])),
                "pending_editor_request_count": sum(
                    1 for item in requests if item.get("status") in ACTIVE_EDITOR_REQUEST_STATUSES
                ),
            }

    def draft_context(
        self, drawing_name: str, *, detail_level: str = "summary"
    ) -> Dict[str, Any] | None:
        """Return compact draft context unless full detail is explicitly requested."""
        with self._lock:
            state = self._load_state(drawing_name)
            working = state.get("working_draft")
            if working is None:
                return None
            if detail_level == "debug":
                return copy.deepcopy(working)
            nodes = {
                str(node.get("@id")): node
                for node in working.get("graph", {}).get("@graph", [])
            }
            affected = []
            for semantic_id in working.get("affected_ids", []):
                node = nodes.get(semantic_id, {})
                affected.append(
                    {
                        "semantic_id": semantic_id,
                        "label": node.get("rdfs:label"),
                        "ontology_class": node.get("@type"),
                    }
                )
            result = {
                "schema_version": working.get("schema_version"),
                "drawing_name": working.get("drawing_name"),
                "base_drawing_revision": working.get("base_drawing_revision"),
                "base_graph_revision": working.get("base_graph_revision"),
                "draft_revision": working.get("graph_revision"),
                "dirty": bool(working.get("dirty")),
                "frozen": bool(working.get("frozen")),
                "conflict_count": len(working.get("conflicts", [])),
                "action_count": int(working.get("undo_position", 0)),
                "affected_entities": affected,
                "diagnostic_summary": self._diagnostic_summary(
                    working.get("diagnostics", [])
                ),
            }
            if detail_level in {"normal", "detailed"}:
                result["actions"] = copy.deepcopy(
                    working.get("actions", [])[: int(working.get("undo_position", 0))]
                )
                result["diagnostics"] = copy.deepcopy(working.get("diagnostics", []))
            return result

    def create_editor_request(
        self,
        drawing_name: str,
        draft_revision: str,
        *,
        requested_action: str = "review_and_preview",
        user_note: str = "",
    ) -> Dict[str, Any]:
        """Create an immutable, idempotent MCP inbox item from a persisted draft."""
        with self._lock:
            state = self._load_state(drawing_name)
            working = state.get("working_draft")
            if not working:
                raise ValueError("NO_WORKING_DRAFT")
            if working.get("graph_revision") != draft_revision:
                raise ValueError("UNKNOWN_DRAFT_REVISION")
            if working.get("frozen"):
                raise ValueError("DRAFT_FROZEN")
            if not working.get("dirty"):
                raise ValueError("DRAFT_NOT_DIRTY")
            note = str(user_note).strip()
            if len(note) > 4000:
                raise ValueError("Editor request note exceeds 4000 characters")
            identity = "|".join(
                (
                    self._key(drawing_name),
                    str(working.get("base_drawing_revision")),
                    draft_revision,
                    requested_action,
                )
            )
            request_id = "editor_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            requests = state.setdefault("editor_requests", {})
            existing = requests.get(request_id)
            if existing:
                return copy.deepcopy(existing)

            nodes = {
                str(node.get("@id")): node
                for node in working.get("graph", {}).get("@graph", [])
            }
            affected_entities = []
            changed_fields = self._changed_fields(working.get("commands", []))
            for semantic_id in working.get("affected_ids", []):
                node = nodes.get(semantic_id, {})
                affected_entities.append(
                    {
                        "semantic_id": semantic_id,
                        "label": node.get("rdfs:label"),
                        "ontology_class": node.get("@type"),
                        "changed_fields": changed_fields.get(semantic_id, []),
                    }
                )
            now = time.time()
            action_descriptions = [
                str(item.get("description", "Change"))
                for item in working.get("actions", [])[: int(working.get("undo_position", 0))]
            ]
            request = {
                "request_id": request_id,
                "schema_version": EDITOR_REQUEST_SCHEMA_VERSION,
                "status": "pending",
                "drawing_name": drawing_name,
                "base_drawing_revision": working.get("base_drawing_revision"),
                "base_graph_revision": working.get("base_graph_revision"),
                "draft_revision": draft_revision,
                "created_at": now,
                "updated_at": now,
                "summary": "; ".join(action_descriptions) or "Review editor draft",
                "user_note": note,
                "requested_action": requested_action,
                "affected_entities": affected_entities,
                "actions": copy.deepcopy(
                    working.get("actions", [])[: int(working.get("undo_position", 0))]
                ),
                "topology_changes": copy.deepcopy(working.get("topology_changes", [])),
                "diagnostic_summary": self._diagnostic_summary(
                    working.get("diagnostics", [])
                ),
                "preview_transaction_id": None,
                "design_transaction_id": None,
                "superseded_by": None,
            }
            for item in requests.values():
                if item.get("status") in ACTIVE_EDITOR_REQUEST_STATUSES or (
                    item.get("status") == "superseded"
                    and not item.get("superseded_by")
                ):
                    item["status"] = "superseded"
                    item["superseded_by"] = request_id
                    item["updated_at"] = now
            requests[request_id] = request
            state["editor_requests"] = requests
            self._write_state(drawing_name, state)
            return copy.deepcopy(request)

    def list_editor_requests(
        self,
        *,
        drawing_name: str | None = None,
        status: str | None = "pending",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return newest compact MCP inbox items across loaded or one drawing."""
        with self._lock:
            states = (
                [self._load_state(drawing_name)]
                if drawing_name
                else self._all_editor_request_states()
            )
            items = []
            for state in states:
                for request in state.get("editor_requests", {}).values():
                    if status and request.get("status") != status:
                        continue
                    items.append(self._compact_editor_request(request))
            items.sort(key=lambda item: float(item.get("created_at", 0)), reverse=True)
            return copy.deepcopy(items[: max(1, min(int(limit), 20))])

    def get_editor_request(
        self, request_id: str, *, detail_level: str = "summary"
    ) -> Dict[str, Any]:
        """Resolve one inbox request without CAD access."""
        with self._lock:
            state, request = self._find_editor_request(request_id)
            if detail_level == "summary":
                return self._compact_editor_request(request)
            result = copy.deepcopy(request)
            if detail_level != "debug":
                for action in result.get("actions", []):
                    for command in action.get("commands", []):
                        node = command.get("node")
                        if isinstance(node, dict):
                            node.pop("cad:geometry", None)
            result["workspace_status"] = self.status(str(state.get("drawing_name")))
            return result

    def set_editor_request_status(
        self, request_id: str, status: str
    ) -> Dict[str, Any]:
        """Apply one legal user/agent inbox status transition."""
        with self._lock:
            state, request = self._find_editor_request(request_id)
            allowed = {
                "pending": {"claimed", "rejected", "withdrawn", "stale"},
                "claimed": {"pending", "rejected", "withdrawn", "stale", "preview_ready"},
                "preview_ready": {"pending", "withdrawn", "stale", "completed"},
            }
            current = str(request.get("status"))
            if status not in allowed.get(current, set()):
                raise ValueError(f"Illegal editor request transition {current} -> {status}")
            self._transition_editor_request(state, request_id, status)
            self._write_state(str(state.get("drawing_name")), state)
            return self._compact_editor_request(request)

    def mark_design_transaction(
        self, request_id: str, design_transaction_id: str | None
    ) -> None:
        """Attach the optional physical CAD preview to an editor request."""
        with self._lock:
            state, _request = self._find_editor_request(request_id)
            self._transition_editor_request(
                state,
                request_id,
                "preview_ready",
                design_transaction_id=design_transaction_id,
            )
            self._write_state(str(state.get("drawing_name")), state)

    def resolve_conflicts(
        self, drawing_name: str, resolutions: Dict[str, Literal["cad", "editor"]]
    ) -> Dict[str, Any]:
        """Resolve recorded field conflicts with explicit per-field choices."""
        with self._lock:
            state = self._load_state(drawing_name)
            remaining = []
            for conflict in state.get("conflicts", []):
                conflict_id = conflict["conflict_id"]
                choice = resolutions.get(conflict_id)
                if choice is None:
                    remaining.append(conflict)
                    continue
                semantic_id = conflict["semantic_id"]
                field = conflict["field"]
                if choice == "cad":
                    if field == "@id":
                        state.get("overrides", {}).pop(semantic_id, None)
                        continue
                    override = state.get("overrides", {}).get(semantic_id, {})
                    override.pop(field, None)
                common = state.setdefault("last_common_graph", {}).setdefault(
                    semantic_id, {}
                )
                if field == "@id" and conflict.get("cad") is None:
                    state["last_common_graph"].pop(semantic_id, None)
                else:
                    common[field] = copy.deepcopy(conflict.get("cad"))
            state["conflicts"] = remaining
            if not remaining:
                state["last_synced_drawing_revision"] = state.get(
                    "last_seen_drawing_revision"
                )
            self._write_state(drawing_name, state)
            return {
                "success": True,
                "remaining_conflicts": len(remaining),
            }

    def _apply_command(self, graph: Dict[str, Any], command: EditorCommand) -> None:
        nodes = {str(node.get("@id")): node for node in graph.setdefault("@graph", [])}
        if command.op == "delete_node":
            nodes.pop(command.semantic_id, None)
        elif command.op == "upsert_node":
            assert command.node is not None
            node = copy.deepcopy(command.node)
            node["@id"] = command.semantic_id
            nodes[command.semantic_id] = node
        elif command.op == "patch_node":
            if command.semantic_id not in nodes:
                raise ValueError(f"Unknown semantic node '{command.semantic_id}'")
            nodes[command.semantic_id] = self._deep_merge(
                nodes[command.semantic_id], command.patch
            )
        else:
            node = nodes.get(command.semantic_id)
            if node is None or node.get("@type") != "top:Connection":
                raise ValueError("Connection command requires a top:Connection node")
            geometry = dict(node.get("cad:geometry", {}))
            geometry["status"] = (
                "confirmed" if command.op == "confirm_connection" else "rejected"
            )
            geometry["source"] = "explicit"
            node["cad:geometry"] = geometry
            node["cad:managed"] = True
        graph["@graph"] = list(nodes.values())

    def _detect_conflicts(
        self,
        state: Dict[str, Any],
        cad_nodes: Dict[str, Dict[str, Any]],
        *,
        drawing_revision: str,
    ) -> List[Dict[str, Any]]:
        if state.get("last_synced_drawing_revision") in {None, drawing_revision}:
            return list(state.get("conflicts", []))
        base = state.get("last_common_graph", {})
        conflicts = []
        for semantic_id, override in state.get("overrides", {}).items():
            cad = cad_nodes.get(semantic_id)
            previous = base.get(semantic_id)
            if cad is None and previous is not None:
                conflicts.append(
                    self._conflict(semantic_id, "@id", previous, None, override)
                )
                continue
            for field, editor_value in override.items():
                if field not in {"cad:geometry", "cad:handles", "cad:representation"}:
                    continue
                base_value = previous.get(field) if previous else None
                cad_value = cad.get(field) if cad else None
                if cad_value != base_value and editor_value != base_value:
                    conflicts.append(
                        self._conflict(
                            semantic_id, field, base_value, cad_value, editor_value
                        )
                    )
        return conflicts

    @staticmethod
    def _conflict(
        semantic_id: str,
        field: str,
        base_value: Any,
        cad_value: Any,
        editor_value: Any,
    ) -> Dict[str, Any]:
        digest = hashlib.sha256(f"{semantic_id}|{field}".encode()).hexdigest()[:16]
        return {
            "conflict_id": f"conflict_{digest}",
            "semantic_id": semantic_id,
            "field": field,
            "base": base_value,
            "cad": cad_value,
            "editor": editor_value,
        }

    @staticmethod
    def _semantic_projection(node: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in node.items()
            if key
            not in {
                "top:adjacentTo",
                "top:boundedBy",
                "top:bounds",
                "top:connectsTo",
                "top:containsElement",
                "top:isPartOf",
            }
        }

    @classmethod
    def _deep_merge(cls, base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _load_state(self, drawing_name: str) -> Dict[str, Any]:
        key = self._key(drawing_name)
        if key in self._states:
            return self._states[key]
        path = self._workspace_path(drawing_name)
        state: Dict[str, Any]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            state = value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}
        version = int(state.get("schema_version", 1))
        if version < 2:
            state["draft"] = None
            state["preview"] = None
            state["working_draft"] = None
        elif version == 2:
            working = state.get("working_draft")
            if isinstance(working, dict):
                commands = list(working.get("commands", []))
                topology_changes = list(working.get("topology_changes", []))
                if commands or topology_changes:
                    action = {
                        "action_id": "migrated_v2",
                        "description": "Imported version-2 draft",
                        "commands": commands,
                        "topology_changes": topology_changes,
                    }
                    working["actions"] = [action]
                    working["undo_position"] = 1
                    working["dirty"] = True
                else:
                    working["actions"] = []
                    working["undo_position"] = 0
                    working["dirty"] = False
                working["schema_version"] = WORKSPACE_SCHEMA_VERSION
                state["working_draft"] = working
            state["preview"] = None
        state["schema_version"] = WORKSPACE_SCHEMA_VERSION
        state.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
        state.setdefault("drawing_name", drawing_name)
        state.setdefault("overrides", {})
        state.setdefault("deleted_ids", [])
        state.setdefault("conflicts", [])
        state.setdefault("draft", None)
        state.setdefault("preview", None)
        state.setdefault("working_draft", None)
        state.setdefault("editor_requests", {})
        self._states[key] = state
        if version < WORKSPACE_SCHEMA_VERSION:
            self._write_state(drawing_name, state)
        return state

    @staticmethod
    def _draft_response(working: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "drawing": working.get("drawing_name"),
            "base_drawing_revision": working.get("base_drawing_revision"),
            "base_graph_revision": working.get("base_graph_revision"),
            "draft_revision": working.get("graph_revision"),
            "graph": copy.deepcopy(working.get("graph", {})),
            "affected_ids": list(working.get("affected_ids", [])),
            "diagnostics": copy.deepcopy(working.get("diagnostics", [])),
            "undo_position": int(working.get("undo_position", 0)),
            "action_count": len(working.get("actions", [])),
            "dirty": bool(working.get("dirty")),
            "frozen": bool(working.get("frozen")),
            "conflicts": copy.deepcopy(working.get("conflicts", [])),
            "mutated": False,
        }

    @staticmethod
    def _actions_from_working(working: Dict[str, Any]) -> List[EditorAction]:
        actions = working.get("actions")
        if actions is None:
            commands = working.get("commands", [])
            topology_changes = working.get("topology_changes", [])
            if not commands and not topology_changes:
                return []
            actions = [
                {
                    "action_id": "legacy_working",
                    "description": "Imported legacy draft",
                    "commands": commands,
                    "topology_changes": topology_changes,
                }
            ]
        return [EditorAction.model_validate(item) for item in actions]

    @staticmethod
    def _diagnostic_summary(diagnostics: List[Dict[str, Any]]) -> Dict[str, int]:
        result = {"error": 0, "warning": 0, "info": 0}
        for item in diagnostics:
            severity = str(item.get("severity", "info"))
            result[severity] = result.get(severity, 0) + 1
        return result

    @staticmethod
    def _changed_fields(commands: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        fields: Dict[str, set[str]] = {}
        for command in commands:
            semantic_id = str(command.get("semantic_id", ""))
            if not semantic_id:
                continue
            target = fields.setdefault(semantic_id, set())
            if command.get("op") == "delete_node":
                target.add("@deleted")
            for payload_name in ("node", "patch"):
                payload = command.get(payload_name)
                if isinstance(payload, dict):
                    target.update(str(key) for key in payload if key != "@id")
        return {key: sorted(value) for key, value in fields.items()}

    @staticmethod
    def _compact_editor_request(request: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "request_id": request.get("request_id"),
            "status": request.get("status"),
            "drawing_name": request.get("drawing_name"),
            "base_drawing_revision": request.get("base_drawing_revision"),
            "draft_revision": request.get("draft_revision"),
            "created_at": request.get("created_at"),
            "summary": request.get("summary"),
            "requested_action": request.get("requested_action"),
            "affected_entities": copy.deepcopy(request.get("affected_entities", [])),
            "diagnostic_summary": copy.deepcopy(request.get("diagnostic_summary", {})),
            "next_action": (
                "preview_editor_request"
                if request.get("status") in {"pending", "claimed"}
                else "apply_after_approval"
                if request.get("status") == "preview_ready"
                else None
            ),
        }

    def _find_editor_request(
        self, request_id: str
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        for state in self._states.values():
            request = state.get("editor_requests", {}).get(request_id)
            if request is not None:
                return state, request
        for state in self._all_editor_request_states():
            request = state.get("editor_requests", {}).get(request_id)
            if request is not None:
                return state, request
        raise ValueError("UNKNOWN_EDITOR_REQUEST")

    def _all_editor_request_states(self) -> List[Dict[str, Any]]:
        """Load persisted inboxes using the drawing identity stored in each sidecar."""
        states: Dict[str, Dict[str, Any]] = dict(self._states)
        output = Path(get_config().output.directory).expanduser().resolve()
        for path in output.glob("*.topology.workspace.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            drawing_name = str(raw.get("drawing_name") or "")
            if not drawing_name:
                continue
            key = self._key(drawing_name)
            if key not in states:
                states[key] = self._load_state(drawing_name)
        return list(states.values())

    @staticmethod
    def _transition_editor_request(
        state: Dict[str, Any], request_id: str, status: str, **updates: Any
    ) -> None:
        request = state.get("editor_requests", {}).get(request_id)
        if request is None:
            raise ValueError("UNKNOWN_EDITOR_REQUEST")
        request["status"] = status
        request["updated_at"] = time.time()
        request.update(updates)

    @staticmethod
    def _supersede_editor_requests(
        state: Dict[str, Any], working: Dict[str, Any]
    ) -> None:
        requests = state.get("editor_requests", {})
        for request in requests.values():
            if (
                request.get("status") in ACTIVE_EDITOR_REQUEST_STATUSES
                and request.get("draft_revision") != working.get("graph_revision")
            ):
                request["status"] = "superseded"
                request["updated_at"] = time.time()

    def _write_state(self, drawing_name: str, state: Dict[str, Any]) -> Path:
        path = self._workspace_path(drawing_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path

    @staticmethod
    def _key(drawing_name: str) -> str:
        return drawing_name.casefold()

    @staticmethod
    def _workspace_path(drawing_name: str) -> Path:
        output = Path(get_config().output.directory).expanduser().resolve()
        stem = Path(drawing_name).stem or "Drawing"
        return output / f"{stem}.topology.workspace.json"

    def _purge_transactions(self) -> None:
        now = time.time()
        expired = [
            transaction_id
            for transaction_id, transaction in self._transactions.items()
            if transaction.expires_at <= now
        ]
        for transaction_id in expired:
            self._transactions.pop(transaction_id, None)
