"""
Deterministic physical-wall expansion and normalization.

Room boundaries are semantic geometry. Physical walls are compiled separately so
every wall is visible in preview before any CAD mutation occurs.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Tuple

Point2D = Tuple[float, float]
CenterlineKey = Tuple[Point2D, Point2D]

# This is deliberately independent from topology snap/healing tolerance. Ten
# microns absorbs serialization noise without merging visibly separate CAD walls.
WALL_NORMALIZATION_TOLERANCE_MM = 0.01


def canonical_point(
    point: Sequence[float],
    tolerance: float = WALL_NORMALIZATION_TOLERANCE_MM,
) -> Point2D:
    """Quantize a 2D point to the deterministic wall-comparison grid."""
    return tuple(
        round(round(float(value) / tolerance) * tolerance, 9) for value in point[:2]
    )  # type: ignore[return-value]


def centerline_key(
    geometry: Dict[str, Any],
    tolerance: float = WALL_NORMALIZATION_TOLERANCE_MM,
) -> CenterlineKey:
    """Return a direction-independent, tolerance-normalized centerline key."""
    start = canonical_point(geometry.get("start", ()), tolerance)
    end = canonical_point(geometry.get("end", ()), tolerance)
    if len(start) != 2 or len(end) != 2 or start == end:
        raise ValueError("Wall geometry requires two distinct 2D endpoints")
    return tuple(sorted((start, end)))  # type: ignore[return-value]


def wall_spec_key(
    geometry: Dict[str, Any],
    tolerance: float = WALL_NORMALIZATION_TOLERANCE_MM,
) -> Tuple[Any, ...]:
    """Return physical specifications that must agree before walls can merge."""

    def quantized(name: str, default: float) -> float:
        value = float(geometry.get(name, default))
        return round(round(value / tolerance) * tolerance, 9)

    return (
        quantized("thickness", 200.0),
        quantized("height", 3000.0),
        str(geometry.get("style", "Standard")).strip().casefold(),
    )


def room_wall_segments(geometry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand one clear-interior rectangle into four physical wall centerlines."""
    origin = tuple(float(value) for value in geometry["origin"][:2])
    width = float(geometry["clear_width"])
    depth = float(geometry["clear_depth"])
    thickness = float(geometry["wall_thickness"])
    rotation = float(geometry.get("rotation_deg", 0.0))
    half = thickness / 2.0
    local_segments = [
        ((-thickness, -half), (width + thickness, -half)),
        ((width + half, -thickness), (width + half, depth + thickness)),
        ((width + thickness, depth + half), (-thickness, depth + half)),
        ((-half, depth + thickness), (-half, -thickness)),
    ]
    return [
        {
            "start": list(_transform(start, origin, rotation)),
            "end": list(_transform(end, origin, rotation)),
            "thickness": thickness,
            "height": float(geometry.get("wall_height", 3000.0)),
            "style": str(geometry.get("wall_style", "Standard")),
        }
        for start, end in local_segments
    ]


def normalize_wall_network(
    operations: Sequence[Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    tolerance: float = WALL_NORMALIZATION_TOLERANCE_MM,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Expand room walls and merge compatible implicit/shared requirements.

    Explicit duplicate walls remain separate so the validation layer can return
    ``DUPLICATE_WALL``. Incompatible specifications remain separate so it can
    return ``WALL_SPEC_CONFLICT``. Implicit room requirements share either one
    new deterministic wall or an existing compatible semantic wall.
    """
    passthrough: List[Dict[str, Any]] = []
    requirements: Dict[CenterlineKey, List[Dict[str, Any]]] = defaultdict(list)
    warnings: List[str] = []
    replaced_room_walls: Dict[str, set[str]] = {}

    for original in operations:
        operation = dict(original)
        ontology_class = operation.get("ontology_class")
        kind = operation.get("kind")
        if ontology_class == "top:Room" and kind in {
            "create_managed",
            "replace_managed",
        }:
            passthrough.append(operation)
            if "boundary" in operation.get("geometry", {}):
                continue
            room_id = str(operation["semantic_id"])
            if kind == "replace_managed":
                replaced_room_walls[room_id] = set(
                    operation.get("previous_wall_ids", [])
                )
            room_label = str(operation.get("label", "")).strip()
            for index, geometry in enumerate(
                room_wall_segments(operation["geometry"]), start=1
            ):
                legacy_id = f"{room_id}:wall:{index}"
                requirement = {
                    "explicit": False,
                    "semantic_id": legacy_id,
                    "legacy_id": legacy_id,
                    "room_ids": [room_id],
                    "label": f"{room_label} Wall {index}".strip(),
                    "geometry": geometry,
                    "representation": operation.get("representation", "auto"),
                }
                requirements[centerline_key(geometry, tolerance)].append(requirement)
            continue
        if ontology_class == "top:Wall" and kind in {
            "annotate_handles",
            "create_managed",
            "replace_managed",
        }:
            requirement = {
                "explicit": True,
                "semantic_id": str(operation["semantic_id"]),
                "room_ids": list(operation.get("bounding_room_ids", [])),
                "aliases": list(operation.get("aliases", [])),
                "operation": operation,
                "label": operation.get("label", ""),
                "geometry": operation["geometry"],
                "representation": operation.get("representation", "auto"),
            }
            requirements[centerline_key(operation["geometry"], tolerance)].append(
                requirement
            )
            continue
        passthrough.append(operation)

    existing_by_line: Dict[CenterlineKey, List[Dict[str, Any]]] = defaultdict(list)
    for node in nodes.values():
        if node.get("@type") != "top:Wall":
            continue
        geometry = node.get("cad:geometry", {})
        try:
            existing_by_line[centerline_key(geometry, tolerance)].append(node)
        except (TypeError, ValueError):
            continue

    alias_targets: Dict[str, Tuple[str, Dict[str, Any], Dict[str, Any]]] = {}
    reused_by_room: Dict[str, set[str]] = defaultdict(set)
    compiled_walls: List[Dict[str, Any]] = []
    relationship_updates: List[Dict[str, Any]] = []

    for line_key in sorted(requirements):
        by_spec: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
        for requirement in requirements[line_key]:
            by_spec[wall_spec_key(requirement["geometry"], tolerance)].append(
                requirement
            )

        compatible_groups: List[Tuple[Tuple[Any, ...], List[Dict[str, Any]]]] = []
        for spec_key, candidates in by_spec.items():
            concrete_representations = sorted(
                {
                    str(item.get("representation"))
                    for item in candidates
                    if item.get("representation") not in {None, "auto"}
                }
            )
            if len(concrete_representations) <= 1:
                compatible_groups.append((spec_key, candidates))
                continue
            for representation in concrete_representations:
                compatible_groups.append(
                    (
                        spec_key,
                        [
                            item
                            for item in candidates
                            if item.get("representation") == representation
                        ],
                    )
                )
            automatic = [
                item
                for item in candidates
                if item.get("representation") in {None, "auto"}
            ]
            if automatic:
                compatible_groups.append((spec_key, automatic))

        for spec_key, group in sorted(
            compatible_groups,
            key=lambda item: (str(item[0]), _merged_representation(item[1])),
        ):
            requested_representation = _merged_representation(group)
            explicit = [item for item in group if item["explicit"]]
            implicit = [item for item in group if not item["explicit"]]
            room_ids = sorted(
                {room_id for item in group for room_id in item.get("room_ids", [])}
            )
            aliases = sorted(
                {
                    alias
                    for item in group
                    for alias in (
                        [item.get("legacy_id")] if item.get("legacy_id") else []
                    )
                    + list(item.get("aliases", []))
                    if alias
                }
            )
            compatible_existing = [
                node
                for node in existing_by_line.get(line_key, [])
                if wall_spec_key(node.get("cad:geometry", {}), tolerance) == spec_key
                and requested_representation
                in {"auto", str(node.get("cad:representation", "standard"))}
            ]

            if compatible_existing and not explicit:
                existing = sorted(compatible_existing, key=lambda item: item["@id"])[0]
                existing_rooms = _node_room_ids(existing)
                merged_rooms = sorted(existing_rooms | set(room_ids))
                existing_aliases = set(existing.get("cad:aliases", []))
                merged_aliases = sorted(existing_aliases | set(aliases))
                if merged_rooms != sorted(existing_rooms) or merged_aliases != sorted(
                    existing_aliases
                ):
                    relationship_updates.append(
                        {
                            "kind": "update_wall_rooms",
                            "semantic_id": existing["@id"],
                            "ontology_class": "top:Wall",
                            "handles": list(existing.get("cad:handles", [])),
                            "bounding_room_ids": merged_rooms,
                            "aliases": merged_aliases,
                        }
                    )
                for item in implicit:
                    alias_targets[item["legacy_id"]] = (
                        existing["@id"],
                        item["geometry"],
                        existing.get("cad:geometry", {}),
                    )
                for room_id in room_ids:
                    reused_by_room[room_id].add(str(existing["@id"]))
                continue

            if explicit:
                target = explicit[0]
                target_operation = dict(target["operation"])
                target_operation["bounding_room_ids"] = room_ids
                target_operation["aliases"] = aliases
                compiled_walls.append(target_operation)
                for item in implicit:
                    alias_targets[item["legacy_id"]] = (
                        target["semantic_id"],
                        item["geometry"],
                        target["geometry"],
                    )
                # Retain additional explicit walls for deterministic validation.
                compiled_walls.extend(dict(item["operation"]) for item in explicit[1:])
                continue

            canonical_geometry = {
                "start": list(line_key[0]),
                "end": list(line_key[1]),
                "thickness": spec_key[0],
                "height": spec_key[1],
                "style": str(group[0]["geometry"].get("style", "Standard")),
            }
            wall_id = _stable_wall_id(line_key, spec_key, requested_representation)
            label_parts = sorted(
                {
                    str(item.get("label", "")).strip()
                    for item in group
                    if item.get("label")
                }
            )
            compiled_walls.append(
                {
                    "kind": "create_managed",
                    "semantic_id": wall_id,
                    "ontology_class": "top:Wall",
                    "label": " / ".join(label_parts),
                    "geometry": canonical_geometry,
                    "representation": requested_representation,
                    "bounding_room_ids": room_ids,
                    "aliases": aliases,
                }
            )
            for item in implicit:
                alias_targets[item["legacy_id"]] = (
                    wall_id,
                    item["geometry"],
                    canonical_geometry,
                )

    for room_id, previous_wall_ids in sorted(replaced_room_walls.items()):
        obsolete_ids = previous_wall_ids - reused_by_room.get(room_id, set())
        for wall_id in sorted(obsolete_ids):
            existing_node = nodes.get(wall_id)
            if existing_node is None:
                continue
            remaining_rooms = sorted(_node_room_ids(existing_node) - {room_id})
            remaining_aliases = sorted(
                alias
                for alias in existing_node.get("cad:aliases", [])
                if not str(alias).startswith(f"{room_id}:wall:")
            )
            if remaining_rooms:
                relationship_updates.append(
                    {
                        "kind": "update_wall_rooms",
                        "semantic_id": wall_id,
                        "ontology_class": "top:Wall",
                        "handles": list(existing_node.get("cad:handles", [])),
                        "bounding_room_ids": remaining_rooms,
                        "aliases": remaining_aliases,
                    }
                )
            else:
                passthrough.append(
                    {
                        "kind": "delete_managed",
                        "semantic_id": wall_id,
                        "ontology_class": "top:Wall",
                        "handles": list(existing_node.get("cad:handles", [])),
                        "deleted_semantic_ids": [wall_id],
                        "cascade": False,
                    }
                )

    result = passthrough + relationship_updates + compiled_walls
    for operation in result:
        if operation.get("ontology_class") not in {"top:Door", "top:Window"}:
            continue
        geometry = operation.get("geometry", {})
        host_id = geometry.get("host_wall_id")
        alias_target = alias_targets.get(str(host_id))
        if alias_target is None:
            continue
        canonical_id, source_geometry, target_geometry = alias_target
        geometry["host_wall_id"] = canonical_id
        if _directions_reversed(source_geometry, target_geometry, tolerance):
            width = float(geometry.get("width", 0.0))
            source_length = math.dist(
                source_geometry["start"][:2], source_geometry["end"][:2]
            )
            geometry["offset"] = round(
                source_length - float(geometry.get("offset", 0.0)) - width, 9
            )
            if operation.get("ontology_class") == "top:Door":
                geometry["hinge"] = (
                    "right" if geometry.get("hinge") == "left" else "left"
                )
        warnings.append(
            f"Opening host '{host_id}' normalized to shared wall '{canonical_id}'."
        )

    return _order_operations(result), sorted(set(warnings))


def _stable_wall_id(
    line_key: CenterlineKey,
    spec_key: Tuple[Any, ...],
    representation: str,
) -> str:
    payload = json.dumps(
        {
            "centerline": line_key,
            "spec": spec_key,
            "representation": representation,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"urn:topospatial:wall:{digest}"


def _node_room_ids(node: Dict[str, Any]) -> set[str]:
    room_ids = node.get("cad:boundingRooms", [])
    if isinstance(room_ids, list):
        result = {str(item) for item in room_ids if item}
    else:
        result = set()
    parent = node.get("cad:parent")
    if parent:
        result.add(str(parent))
    return result


def _merged_representation(group: Iterable[Dict[str, Any]]) -> str:
    requested = {
        str(item.get("representation", "auto"))
        for item in group
        if item.get("representation") != "auto"
    }
    return sorted(requested)[0] if len(requested) == 1 else "auto"


def _directions_reversed(
    source: Dict[str, Any],
    target: Dict[str, Any],
    tolerance: float,
) -> bool:
    source_start = canonical_point(source["start"], tolerance)
    target_start = canonical_point(target["start"], tolerance)
    target_end = canonical_point(target["end"], tolerance)
    return source_start == target_end and source_start != target_start


def _order_operations(
    operations: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    priority = {
        "top:Room": 0,
        "top:Wall": 1,
        "top:Door": 2,
        "top:Window": 2,
    }
    return sorted(
        operations,
        key=lambda item: (
            priority.get(str(item.get("ontology_class")), 3),
            str(item.get("semantic_id", "")),
            str(item.get("kind", "")),
        ),
    )


def _transform(
    point: Sequence[float], origin: Sequence[float], rotation_deg: float
) -> Point2D:
    angle = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (
        float(origin[0]) + point[0] * cos_a - point[1] * sin_a,
        float(origin[1]) + point[0] * sin_a + point[1] * cos_a,
    )
