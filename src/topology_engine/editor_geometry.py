"""Compile visual shared-wall gestures into coordinated topology changes."""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, Iterable, List

from .models import TopologyChange


def expand_shared_wall_edits(
    graph: Dict[str, Any], changes: Iterable[TopologyChange]
) -> List[TopologyChange]:
    """Add affected room-boundary updates for wall endpoint/segment edits."""
    expanded = list(changes)
    nodes = {str(node.get("@id")): node for node in graph.get("@graph", [])}
    boundary_updates: Dict[str, List[List[float]]] = {}
    for change in expanded:
        if change.op != "update" or not change.semantic_id or not change.geometry:
            continue
        wall = nodes.get(change.semantic_id)
        if wall is None or wall.get("@type") != "top:Wall":
            continue
        old_geometry = wall.get("cad:geometry", {})
        old_start = old_geometry.get("start")
        old_end = old_geometry.get("end")
        if not old_start or not old_end:
            continue
        new_start = change.geometry.get("start")
        new_end = change.geometry.get("end")
        if not new_start or not new_end:
            continue
        room_ids = wall.get("cad:boundingRooms", [])
        if not isinstance(room_ids, list):
            room_ids = []
        thickness = float(old_geometry.get("thickness", 200.0))
        for room_id in room_ids:
            room = nodes.get(str(room_id))
            if room is None or room.get("@type") not in {"top:Room", "top:Space"}:
                continue
            boundary = boundary_updates.get(str(room_id))
            if boundary is None:
                boundary = copy.deepcopy(
                    room.get("cad:geometry", {}).get("boundary", [])
                )
            if len(boundary) < 3:
                continue
            moved = _move_boundary_edge(
                boundary,
                old_start,
                old_end,
                new_start,
                new_end,
                thickness / 2.0 + 1.0,
            )
            if moved is not None:
                boundary_updates[str(room_id)] = moved
    for semantic_id, boundary in sorted(boundary_updates.items()):
        expanded.append(
            TopologyChange.model_validate(
                {
                    "op": "update",
                    "@id": semantic_id,
                    "geometry": {"boundary": boundary},
                }
            )
        )
    return expanded


def _move_boundary_edge(
    boundary: List[List[float]],
    old_start: List[float],
    old_end: List[float],
    new_start: List[float],
    new_end: List[float],
    maximum_distance: float,
) -> List[List[float]] | None:
    dx = float(old_end[0]) - float(old_start[0])
    dy = float(old_end[1]) - float(old_start[1])
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return None
    updated = copy.deepcopy(boundary)
    changed = 0
    for index, point in enumerate(boundary):
        px = float(point[0]) - float(old_start[0])
        py = float(point[1]) - float(old_start[1])
        station = max(0.0, min(1.0, (px * dx + py * dy) / length_squared))
        projection = (
            float(old_start[0]) + station * dx,
            float(old_start[1]) + station * dy,
        )
        if math.dist((float(point[0]), float(point[1])), projection) > maximum_distance:
            continue
        delta_x = (
            float(new_start[0])
            + station * (float(new_end[0]) - float(new_start[0]))
            - projection[0]
        )
        delta_y = (
            float(new_start[1])
            + station * (float(new_end[1]) - float(new_start[1]))
            - projection[1]
        )
        updated[index] = [
            round(float(point[0]) + delta_x, 9),
            round(float(point[1]) + delta_y, 9),
        ]
        changed += 1
    return updated if changed >= 2 else None
