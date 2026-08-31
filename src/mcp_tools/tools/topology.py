"""Unified MCP tool for 2D architectural topology and ontology workflows."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from pydantic import ValidationError

from core.config import get_config
from mcp_tools.decorators import cad_tool, get_current_adapter
from topology_engine import ChangeDocument, TopologyEngine, TransactionStore
from topology_engine.cad_bridge import CADTopologyBridge

logger = logging.getLogger(__name__)

_topology_config = get_config().topology
_engine = TopologyEngine(
    snap_tolerance_mm=_topology_config.snap_tolerance_mm,
    max_opening_gap_mm=_topology_config.max_opening_gap_mm,
    min_room_dimension_mm=_topology_config.min_room_dimension_mm,
)
_bridge = CADTopologyBridge()
_transactions = TransactionStore(ttl_seconds=_topology_config.transaction_ttl_seconds)
_analysis_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="topospatial-topology"
)


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _analyze(snapshot: Any) -> Dict[str, Any]:
    """Run all geometry work away from COM-initialized MCP workers."""

    def analyze_snapshot() -> Dict[str, Any]:
        if any(
            entity.semantic.get("ontology_class") == "top:Room"
            for entity in snapshot.entities
        ):
            _engine.ensure_available()
        return _engine.analyze(snapshot)

    return _analysis_executor.submit(analyze_snapshot).result(timeout=120)


def _ensure_topology_available() -> None:
    """Verify the pinned TopologicPy backend on the geometry worker."""
    _analysis_executor.submit(_engine.ensure_available).result(timeout=120)


def register_topology_tools(mcp: Any) -> None:
    """Register the optional ``manage_topology`` tool."""

    @cad_tool(mcp, "manage_topology")
    def manage_topology(
        action: str,
        scope: str = "all",
        payload: Optional[Dict[str, Any]] = None,
        format: str = "jsonld",
    ) -> str:
        """Analyze and safely round-trip a 2D architectural semantic graph.

        Args:
            action: ``analyze``, ``query``, ``preview``, ``apply``, or ``export``.
            scope: ``all`` or ``selected`` for analyze/query/export.
            payload: Action-specific structured data. ``preview`` accepts a strict
                JSON-LD-shaped change document. ``apply`` requires
                ``{"transaction_id": "..."}``.
                Create/update changes may set ``representation`` to ``auto``
                (default), ``native_aec``, or ``standard``. Auto uses native
                AecDbWall/AecDbDoor/AecDbWindow objects when AutoCAD Architecture
                is active and safely falls back to standard entities otherwise.
            format: ``jsonld`` or ``ttl`` for export.

        Returns:
            JSON result. Mutations occur only during ``apply`` and only for a
            previously previewed, unexpired transaction.
        """
        adapter = get_current_adapter()
        action_lower = action.strip().lower()
        format_lower = format.strip().lower()
        payload = payload or {}

        if not _topology_config.enabled:
            return _json(
                {
                    "success": False,
                    "error": "Topology support is disabled in config.json",
                }
            )

        if action_lower not in {"analyze", "query", "preview", "apply", "export"}:
            return _json(
                {
                    "success": False,
                    "error": (
                        "Unknown action. Use: analyze, query, preview, apply, export"
                    ),
                }
            )
        if scope not in {"all", "selected"}:
            return _json({"success": False, "error": "scope must be all or selected"})
        if format_lower not in {"jsonld", "ttl"}:
            return _json({"success": False, "error": "format must be jsonld or ttl"})

        try:
            if action_lower in {"preview", "apply"} and scope != "all":
                raise ValueError("preview and apply require scope='all'")

            if action_lower == "apply":
                transaction_id = str(payload.get("transaction_id", "")).strip()
                if not transaction_id:
                    raise ValueError("apply requires payload.transaction_id")
                transaction = _transactions.get(transaction_id)
                if transaction.status == "applied":
                    return _json(
                        {
                            "success": True,
                            "idempotent": True,
                            "transaction_id": transaction_id,
                            "result": transaction.result,
                        }
                    )

                current = _bridge.snapshot(adapter, scope="all")
                logger.info(
                    "Topology apply captured %d entities from %s",
                    len(current.entities),
                    current.drawing_name,
                )
                if current.drawing_name != transaction.drawing_name:
                    raise ValueError("The active drawing changed after preview")
                if current.revision != transaction.base_revision:
                    raise ValueError("The drawing changed after preview; preview again")

                cad_result = _bridge.apply_operations(adapter, transaction.operations)
                try:
                    updated = _bridge.snapshot(adapter, scope="all")
                    analysis = _analyze(updated)
                    turtle = _engine.to_turtle(analysis["graph"])
                    sidecars = _bridge.write_sidecars(
                        updated, analysis["graph"], turtle
                    )
                except Exception:
                    logger.exception(
                        "Topology post-apply validation failed; rolling CAD back"
                    )
                    adapter.undo(1)
                    adapter.refresh_view()
                    raise
                result = {
                    "drawing": updated.drawing_name,
                    "revision": updated.revision,
                    "cad": cad_result,
                    "sidecars": sidecars,
                    "node_count": analysis["node_count"],
                    "relation_count": analysis["relation_count"],
                }
                _transactions.mark_applied(transaction_id, result)
                return _json(
                    {
                        "success": True,
                        "transaction_id": transaction_id,
                        "result": result,
                    }
                )

            snapshot = _bridge.snapshot(adapter, scope=scope)
            logger.info(
                "Topology %s captured %d entities from %s",
                action_lower,
                len(snapshot.entities),
                snapshot.drawing_name,
            )
            analysis = _analyze(snapshot)
            logger.info(
                "Topology %s built %d nodes and %d candidates",
                action_lower,
                analysis["node_count"],
                analysis["candidate_count"],
            )

            if action_lower == "analyze":
                return _json(analysis)

            if action_lower == "query":
                nodes = analysis["graph"].get("@graph", [])
                semantic_id = payload.get("id")
                ontology_class = payload.get("class")
                predicate = payload.get("relation")
                matches = []
                for node in nodes:
                    if semantic_id and node.get("@id") != semantic_id:
                        continue
                    if ontology_class and node.get("@type") != ontology_class:
                        continue
                    if predicate and predicate not in node:
                        continue
                    matches.append(node)
                return _json(
                    {
                        "success": True,
                        "revision": snapshot.revision,
                        "count": len(matches),
                        "nodes": matches,
                    }
                )

            if action_lower == "preview":
                if snapshot.units != "mm":
                    raise ValueError(
                        "Topology changes require an AutoCAD drawing with INSUNITS=mm"
                    )
                document = ChangeDocument.model_validate(payload)
                _ensure_topology_available()
                operations, diff, warnings = _engine.plan_changes(
                    document, snapshot, analysis
                )
                _bridge.resolve_operation_representations(
                    adapter, operations, analysis
                )
                for item, operation in zip(diff, operations):
                    item["representation"] = operation.get("representation")
                transaction = _transactions.create(
                    drawing_name=snapshot.drawing_name,
                    base_revision=snapshot.revision,
                    operations=operations,
                    diff=diff,
                    warnings=warnings,
                )
                return _json(
                    {
                        "success": True,
                        "transaction_id": transaction.transaction_id,
                        "expires_at": transaction.expires_at,
                        "base_revision": transaction.base_revision,
                        "change_count": len(diff),
                        "diff": diff,
                        "warnings": warnings,
                        "mutated": False,
                    }
                )

            turtle = _engine.to_turtle(analysis["graph"])
            sidecars = _bridge.write_sidecars(snapshot, analysis["graph"], turtle)
            return _json(
                {
                    "success": True,
                    "format": format_lower,
                    "path": sidecars[format_lower],
                    "paths": sidecars,
                    "count": analysis["node_count"],
                    "data": analysis["graph"] if format_lower == "jsonld" else turtle,
                }
            )
        except ValidationError as exc:
            return _json(
                {
                    "success": False,
                    "error": "Invalid topology change document",
                    "details": exc.errors(include_url=False),
                }
            )
        except Exception as exc:
            logger.exception("Topology action '%s' failed", action_lower)
            return _json({"success": False, "error": str(exc)})
