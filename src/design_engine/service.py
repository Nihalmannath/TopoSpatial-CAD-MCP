"""Shared application services for MCP tools and the visual editor."""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any, Dict, List

from topology_engine.navigation import find_route
from topology_engine.spatial_program import diagnose_graph
from web.runtime import runtime_metadata, write_runtime_state

from .orchestrator import DesignOrchestrator
from .workspace import TopologyWorkspaceService


class EditorEventBroker:
    """Small thread-safe event journal consumed by browser WebSockets."""

    def __init__(self, max_events: int = 1000) -> None:
        """Initialize a bounded event sequence."""
        self._lock = threading.Lock()
        self._events: deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._sequence = 0

    def publish(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append and return one timestamped event."""
        with self._lock:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "type": event_type,
                "event_type": event_type,
                "timestamp": time.time(),
                "payload": copy.deepcopy(payload),
            }
            self._events.append(event)
            return copy.deepcopy(event)

    def since(self, sequence: int) -> List[Dict[str, Any]]:
        """Return all retained events after a sequence number."""
        with self._lock:
            return [
                copy.deepcopy(event)
                for event in self._events
                if int(event["sequence"]) > sequence
            ]


class TopologyApplicationService:
    """Coordinate cached CAD analysis, workspace semantics, and editor events."""

    def __init__(self) -> None:
        """Create the shared workspace, orchestrator, and event broker."""
        self.workspace = TopologyWorkspaceService()
        self.orchestrator = DesignOrchestrator()
        self.events = EditorEventBroker()
        self._workspace_lock = threading.RLock()
        self._workspace_cache: Dict[str, Dict[str, Any]] = {}
        self._latest_summary: Dict[str, Any] = {}

    @staticmethod
    def _plan_entities(snapshot: Any) -> List[Dict[str, Any]]:
        """Return a bounded, read-only CAD underlay for the Studio canvas."""
        supported = {"line", "polyline", "arc", "circle", "dimension", "point"}
        result: List[Dict[str, Any]] = []
        for entity in snapshot.entities[:5000]:
            geometry = copy.deepcopy(entity.geometry)
            if geometry.get("kind") not in supported:
                continue
            result.append(
                {
                    "handle": entity.handle,
                    "object_type": entity.object_type,
                    "layer": entity.layer,
                    "geometry": geometry,
                }
            )
        return result

    @staticmethod
    def _drawing_extents(plan_entities: List[Dict[str, Any]]) -> Dict[str, float] | None:
        points: List[tuple[float, float]] = []
        for entity in plan_entities:
            geometry = entity.get("geometry", {})
            kind = geometry.get("kind")
            if kind == "line":
                values = [geometry.get("start"), geometry.get("end")]
            elif kind == "dimension":
                values = [geometry.get("start"), geometry.get("end"), geometry.get("text_position")]
            elif kind == "polyline":
                values = geometry.get("vertices", [])
            elif kind == "point":
                values = [geometry.get("position")]
            elif kind in {"arc", "circle"}:
                center = geometry.get("center")
                radius = float(geometry.get("radius", 0.0) or 0.0)
                values = (
                    [
                        [float(center[0]) - radius, float(center[1]) - radius],
                        [float(center[0]) + radius, float(center[1]) + radius],
                    ]
                    if isinstance(center, (list, tuple)) and len(center) >= 2
                    else []
                )
            else:
                values = []
            for value in values:
                if isinstance(value, (list, tuple)) and len(value) >= 2:
                    try:
                        points.append((float(value[0]), float(value[1])))
                    except (TypeError, ValueError):
                        continue
        if not points:
            return None
        min_x = min(point[0] for point in points)
        max_x = max(point[0] for point in points)
        min_y = min(point[1] for point in points)
        max_y = max(point[1] for point in points)
        return {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "width": max_x - min_x,
            "height": max_y - min_y,
        }

    @staticmethod
    def _unit_diagnostics(
        units: str,
        entity_count: int,
        extents: Dict[str, float] | None,
    ) -> List[Dict[str, Any]]:
        if not extents:
            return []
        span = max(extents["width"], extents["height"])
        if units == "mm" and entity_count >= 20 and 10.0 <= span <= 500.0:
            return [
                {
                    "code": "SUSPECTED_FEET_COORDINATES_IN_MM_DRAWING",
                    "severity": "warning",
                    "message": (
                        "The drawing declares millimetres but its coordinate extents "
                        "look like feet. Room detection may fail until a repaired copy "
                        "is scaled by 304.8."
                    ),
                    "affected": [],
                    "suggested_scale_factor": 304.8,
                }
            ]
        return []

    def current_workspace(
        self,
        adapter: Any,
        *,
        include_candidates: bool = True,
        expected_drawing: str | None = None,
    ) -> Dict[str, Any]:
        """Return the current merged graph and connectivity diagnostics."""
        snapshot, analysis, cache_hit = self.orchestrator._snapshot_and_analysis(
            adapter, "all", "editor-workspace", expected_drawing=expected_drawing
        )
        graph = self.workspace.merge_graph(
            snapshot.drawing_name,
            snapshot.revision,
            analysis["graph"],
            include_candidates=include_candidates,
        )
        plan_entities = self._plan_entities(snapshot)
        drawing_extents = self._drawing_extents(plan_entities)
        unit_diagnostics = self._unit_diagnostics(
            snapshot.units, len(snapshot.entities), drawing_extents
        )
        readiness = copy.deepcopy(analysis.get("readiness", {}))
        portal_discoveries = [
            item
            for item in analysis.get("discoveries", [])
            if item.get("kind") in {"door", "opening"}
        ]
        unresolved_portals = [
            item
            for item in portal_discoveries
            if len(item.get("endpoint_candidates", [])) != 2
        ]
        backend = runtime_metadata()
        result = {
            "success": True,
            "drawing": snapshot.drawing_name,
            "drawing_revision": snapshot.revision,
            "graph_revision": graph["cad:graphRevision"],
            "units": snapshot.units,
            "cad_entity_count": len(snapshot.entities),
            "cad_plan_entities": plan_entities,
            "drawing_extents": drawing_extents,
            "unit_diagnostics": unit_diagnostics,
            "topology_status": {
                "graph_ready": bool(analysis.get("graph_ready")),
                "pending_candidate_count": len(analysis.get("candidates", [])),
                "portal_discovery_count": len(portal_discoveries),
                "unresolved_portal_count": len(unresolved_portals),
                "readiness": readiness,
            },
            "backend": backend,
            "graph": graph,
            "candidates": copy.deepcopy(analysis.get("candidates", [])),
            "discoveries": copy.deepcopy(analysis.get("discoveries", [])),
            "diagnostics": [*diagnose_graph(graph), *unit_diagnostics],
            "analysis_diagnostics": copy.deepcopy(analysis.get("issues", [])),
            "workspace": self.workspace.status(snapshot.drawing_name),
            "cache_hit": cache_hit,
        }
        with self._workspace_lock:
            self._workspace_cache[snapshot.drawing_name.casefold()] = copy.deepcopy(
                result
            )
            self._latest_summary = {
                "drawing": snapshot.drawing_name,
                "drawing_revision": snapshot.revision,
                "graph_revision": graph["cad:graphRevision"],
            }
        write_runtime_state(
            "dashboard",
            drawing=snapshot.drawing_name,
            drawing_revision=snapshot.revision,
            graph_revision=graph["cad:graphRevision"],
        )
        return result

    def latest_summary(self) -> Dict[str, Any]:
        """Return the last editor workspace revisions without touching CAD."""
        with self._workspace_lock:
            return copy.deepcopy(self._latest_summary)

    def cached_workspace(
        self,
        drawing_name: str,
        drawing_revision: str,
        graph_revision_value: str,
    ) -> Dict[str, Any]:
        """Return an exact cached base workspace without invoking CAD or analysis."""
        with self._workspace_lock:
            value = self._workspace_cache.get(drawing_name.casefold())
            if value is None:
                raise ValueError("WORKSPACE_NOT_CACHED")
            if value.get("drawing_revision") != drawing_revision:
                raise ValueError("STALE_DRAWING_REVISION")
            if value.get("graph_revision") != graph_revision_value:
                raise ValueError("STALE_GRAPH_REVISION")
            return copy.deepcopy(value)

    def route(
        self,
        adapter: Any,
        start_id: str,
        end_id: str,
        minimum_width_mm: float = 0.0,
    ) -> Dict[str, Any]:
        """Query a confirmed circulation route on the merged graph."""
        workspace = self.current_workspace(adapter, include_candidates=False)
        result = find_route(workspace["graph"], start_id, end_id, minimum_width_mm)
        result.update(
            {
                "drawing": workspace["drawing"],
                "drawing_revision": workspace["drawing_revision"],
                "graph_revision": workspace["graph_revision"],
            }
        )
        return result

    def route_draft(
        self,
        drawing_name: str,
        draft_revision: str,
        start_id: str,
        end_id: str,
        minimum_width_mm: float = 0.0,
    ) -> Dict[str, Any]:
        """Route against one persisted draft without touching CAD."""
        graph = self.workspace.draft_graph(drawing_name, draft_revision)
        result = find_route(graph, start_id, end_id, minimum_width_mm)
        result.update(
            {
                "drawing": drawing_name,
                "drawing_revision": graph.get("cad:revision"),
                "graph_revision": draft_revision,
                "draft": True,
            }
        )
        return result


application_service = TopologyApplicationService()
