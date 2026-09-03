"""Circulation graph inference, routing, and connectivity diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from typing import Any, Dict, Iterable, List, Tuple

try:
    from shapely.geometry import Point, Polygon
except ImportError:  # pragma: no cover - optional topology dependency
    Point = Polygon = None  # type: ignore[assignment]

SPACE_CLASSES = {"top:Room", "top:Space"}
PORTAL_CLASSES = {"top:Door", "top:Opening"}
OUTSIDE_SPACE_ID = "urn:topospatial:space:outside"


def portal_width_mm(node: Dict[str, Any], default: float = 900.0) -> float:
    """Prefer an explicit clear width, then nominal geometry, over a fallback."""
    geometry = node.get("cad:geometry", {})
    properties = node.get("cad:properties", {})
    for value in (
        properties.get("clear_width_mm"),
        geometry.get("clear_width_mm"),
        geometry.get("width"),
        properties.get("nominal_width_mm"),
    ):
        try:
            width = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(width) and width > 0:
            return width
    return default


def connection_semantic_id(left_id: str, right_id: str, via_id: str = "") -> str:
    """Return an order-independent connection ID, distinct per physical portal."""
    left, right = _pair(str(left_id), str(right_id))
    portal_identity = str(via_id).strip() or "logical"
    digest = hashlib.sha256(
        f"{left}|{right}|{portal_identity}".encode("utf-8")
    ).hexdigest()[:20]
    return f"urn:topospatial:connection:{digest}"


def graph_revision(graph: Dict[str, Any]) -> str:
    """Return a stable content revision for a semantic graph."""
    canonical = json.loads(json.dumps(graph))
    canonical.pop("cad:graphRevision", None)
    canonical.pop("cad:workspaceConflicts", None)
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def infer_connection_candidates(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create stable candidate connection nodes from derived graph relations."""
    nodes = list(graph.get("@graph", []))
    node_ids = {str(node.get("@id")) for node in nodes}
    existing_connections = {
        (
            *_pair(
                str(node.get("cad:geometry", {}).get("from_space_id", "")),
                str(node.get("cad:geometry", {}).get("to_space_id", "")),
            ),
            str(node.get("cad:geometry", {}).get("via_id", "")),
        )
        for node in nodes
        if node.get("@type") == "top:Connection"
    }
    nodes_by_id = {str(node.get("@id")): node for node in nodes}
    pair_portals: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for portal in nodes:
        if portal.get("@type") not in PORTAL_CLASSES:
            continue
        geometry = portal.get("cad:geometry", {})
        host_id = str(portal.get("cad:hostWall") or geometry.get("host_wall_id", ""))
        room_ids = nodes_by_id.get(host_id, {}).get("cad:boundingRooms", [])
        if isinstance(room_ids, list) and len(set(room_ids)) == 2:
            pair_portals.setdefault(_pair(*sorted(set(map(str, room_ids)))), []).append(
                portal
            )
    pairs: set[Tuple[str, str]] = set()
    for node in nodes:
        if node.get("@type") not in SPACE_CLASSES:
            continue
        source = str(node.get("@id"))
        for target in node.get("top:connectsTo", []):
            if isinstance(target, dict) and target.get("@id"):
                pairs.add(_pair(source, str(target["@id"])))

    candidates = []
    for left_id, right_id in sorted(pairs):
        if (
            not left_id
            or not right_id
            or left_id not in node_ids
            or right_id not in node_ids
        ):
            continue
        portals = sorted(
            pair_portals.get((left_id, right_id), []),
            key=lambda item: str(item.get("@id")),
        )
        # Explicit endpoint annotations need not have a host wall. Their
        # confirmed portal connection already represents this relation.
        if not portals and any(
            (left, right) == (left_id, right_id)
            for left, right, _ in existing_connections
        ):
            continue
        choices = portals or [{}]
        for portal in choices:
            portal_id = str(portal.get("@id", ""))
            if (left_id, right_id, portal_id) in existing_connections:
                continue
            candidates.append(
                {
                    "@id": connection_semantic_id(left_id, right_id, portal_id),
                    "@type": "top:Connection",
                    "rdfs:label": "Suggested connection",
                    "cad:managed": False,
                    "cad:geometry": {
                        "from_space_id": left_id,
                        "to_space_id": right_id,
                        "via_id": portal_id,
                        "clear_width_mm": portal_width_mm(portal),
                        "direction": "bidirectional",
                        "accessible": True,
                        "status": "candidate",
                        "source": "derived",
                    },
                }
            )
    return candidates


def diagnose_connectivity(
    graph: Dict[str, Any], required_width_mm: float = 1200.0
) -> List[Dict[str, Any]]:
    """Return deterministic, non-code-compliance circulation diagnostics."""
    nodes = {str(node.get("@id")): node for node in graph.get("@graph", [])}
    spaces = {
        node_id: node
        for node_id, node in nodes.items()
        if node.get("@type") in SPACE_CLASSES
    }
    issues: List[Dict[str, Any]] = []
    edges, connection_issues = _connection_edges(nodes, required_width_mm)
    issues.extend(connection_issues)

    entry_ids = {
        node_id
        for node_id, node in spaces.items()
        if bool(node.get("cad:properties", {}).get("is_entry"))
        or node.get("cad:properties", {}).get("space_type") == "outdoor"
        or node_id == OUTSIDE_SPACE_ID
    }
    if spaces and not entry_ids:
        issues.append(
            {
                "code": "NO_ENTRY_SPACE",
                "severity": "warning",
                "message": "No entrance or outdoor space is marked for reachability.",
                "affected": sorted(spaces),
            }
        )
    reachable = _reachable(entry_ids, edges)
    for node_id in sorted(set(spaces) - reachable):
        issues.append(
            {
                "code": "UNREACHABLE_SPACE",
                "severity": "error",
                "message": "Space has no confirmed path to an entrance.",
                "affected": [node_id],
            }
        )

    for node_id, node in sorted(spaces.items()):
        properties = node.get("cad:properties", {})
        if properties.get("space_type") not in {"corridor", "lobby", "passage"}:
            continue
        declared = properties.get("clear_width_mm")
        if declared is not None and float(declared) < required_width_mm:
            issues.append(
                {
                    "code": "PASSAGE_WIDTH_BELOW_CONFIGURED_MINIMUM",
                    "severity": "error",
                    "message": (
                        f"Declared clear width {float(declared):g} mm is below the "
                        f"configured {required_width_mm:g} mm."
                    ),
                    "affected": [node_id],
                }
            )
        issues.extend(_buffer_clearance_issues(node_id, node, required_width_mm))
    return sorted(issues, key=lambda item: (item["code"], item["affected"]))


def find_route(
    graph: Dict[str, Any],
    start_id: str,
    end_id: str,
    minimum_width_mm: float = 0.0,
) -> Dict[str, Any]:
    """Return a shortest confirmed topological route between two spaces."""
    nodes = {str(node.get("@id")): node for node in graph.get("@graph", [])}
    if start_id not in nodes or end_id not in nodes:
        raise ValueError("Unknown route endpoint")
    edges, _ = _connection_edges(nodes, minimum_width_mm)
    queue = deque([(start_id, [start_id], [])])
    visited = {start_id}
    adjacency: Dict[str, List[Tuple[str, str, float]]] = {}
    for left, right, connection_id, width in edges:
        adjacency.setdefault(left, []).append((right, connection_id, width))
    while queue:
        current, spaces, connections = queue.popleft()
        if current == end_id:
            widths = [item[1] for item in connections if item[1] > 0]
            return {
                "success": True,
                "spaces": spaces,
                "connections": [item[0] for item in connections],
                "limiting_width_mm": min(widths) if widths else None,
                "hop_count": max(0, len(spaces) - 1),
            }
        for neighbor, connection_id, width in sorted(adjacency.get(current, [])):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append(
                (neighbor, spaces + [neighbor], connections + [(connection_id, width)])
            )
    return {
        "success": False,
        "spaces": [],
        "connections": [],
        "limiting_width_mm": None,
        "hop_count": None,
        "error": "NO_ROUTE",
    }


def _connection_edges(
    nodes: Dict[str, Dict[str, Any]], minimum_width_mm: float
) -> Tuple[List[Tuple[str, str, str, float]], List[Dict[str, Any]]]:
    edges: List[Tuple[str, str, str, float]] = []
    issues: List[Dict[str, Any]] = []
    for connection_id, node in sorted(nodes.items()):
        if node.get("@type") != "top:Connection":
            continue
        geometry = node.get("cad:geometry", {})
        if geometry.get("status", "confirmed") != "confirmed":
            continue
        left = str(geometry.get("from_space_id", ""))
        right = str(geometry.get("to_space_id", ""))
        if left not in nodes or right not in nodes:
            issues.append(
                {
                    "code": "CONNECTION_ENDPOINT_MISSING",
                    "severity": "error",
                    "message": "Connection references an unknown space.",
                    "affected": [connection_id, left, right],
                }
            )
            continue
        via_id = str(geometry.get("via_id", ""))
        if via_id and via_id not in nodes:
            issues.append(
                {
                    "code": "CONNECTION_PORTAL_MISSING",
                    "severity": "error",
                    "message": "Connection references an unknown door or opening.",
                    "affected": [connection_id, via_id],
                }
            )
        width = float(geometry.get("clear_width_mm", 0.0) or 0.0)
        if minimum_width_mm and width and width < minimum_width_mm:
            issues.append(
                {
                    "code": "CONNECTION_WIDTH_BELOW_CONFIGURED_MINIMUM",
                    "severity": "error",
                    "message": (
                        f"Connection clear width {width:g} mm is below the configured "
                        f"{minimum_width_mm:g} mm."
                    ),
                    "affected": [connection_id],
                }
            )
            continue
        direction = geometry.get("direction", "bidirectional")
        if direction in {"bidirectional", "forward"}:
            edges.append((left, right, connection_id, width))
        if direction in {"bidirectional", "reverse"}:
            edges.append((right, left, connection_id, width))
    return edges, issues


def _buffer_clearance_issues(
    node_id: str, node: Dict[str, Any], required_width_mm: float
) -> List[Dict[str, Any]]:
    if Polygon is None or Point is None:
        return []
    boundary = node.get("cad:geometry", {}).get("boundary")
    portals = node.get("cad:properties", {}).get("portal_points", [])
    if not boundary or len(portals) < 2:
        return []
    polygon = Polygon(boundary)
    navigable = polygon.buffer(-required_width_mm / 2.0)
    if navigable.is_empty:
        return [
            {
                "code": "PASSAGE_CLEARANCE_BLOCKED",
                "severity": "error",
                "message": "Configured clearance cannot pass through this space.",
                "affected": [node_id],
            }
        ]
    components: Iterable[Any] = (
        navigable.geoms
        if getattr(navigable, "geom_type", "") == "MultiPolygon"
        else [navigable]
    )
    component_ids = []
    for portal in portals:
        point = Point(float(portal[0]), float(portal[1]))
        component_ids.append(
            next(
                (
                    index
                    for index, component in enumerate(components)
                    if component.distance(point) <= required_width_mm / 2.0
                ),
                None,
            )
        )
    if None in component_ids or len(set(component_ids)) > 1:
        return [
            {
                "code": "PASSAGE_CLEARANCE_DISCONNECTED",
                "severity": "error",
                "message": (
                    "Portals are not mutually reachable at configured clearance."
                ),
                "affected": [node_id],
            }
        ]
    return []


def _reachable(
    entry_ids: Iterable[str], edges: Iterable[Tuple[str, str, str, float]]
) -> set[str]:
    adjacency: Dict[str, set[str]] = {}
    for left, right, _connection_id, _width in edges:
        adjacency.setdefault(left, set()).add(right)
    visited = set(entry_ids)
    queue = deque(sorted(visited))
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency.get(current, set())):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited


def _pair(left: str, right: str) -> Tuple[str, str]:
    return tuple(sorted((left, right)))  # type: ignore[return-value]
