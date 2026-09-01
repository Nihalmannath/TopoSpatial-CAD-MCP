"""Pure topology analysis, semantic graph construction, and change planning."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .models import (
    ONTOLOGY_CLASSES,
    ChangeDocument,
    DrawingSnapshot,
    EntitySnapshot,
    TopologyChange,
)
from .wall_network import normalize_wall_network

Point2D = Tuple[float, float]

# pythonocc-core is not distributed by PyPI on this Windows setup.  Select the
# verified wheel-backed kernel before TopologicPy performs its first lazy import.
os.environ["TOPOLOGICPY_CORE_BACKEND"] = "topologic_core"

# NumPy/GEOS extension loading can stall when its first import happens inside a
# COM-initialized AnyIO worker on Windows. Import Shapely while the MCP server is
# still on its main startup thread, but preserve the optional-dependency contract.
try:
    from shapely.geometry import LineString as _LineString
    from shapely.geometry import Point as _Point
    from shapely.geometry import Polygon as _Polygon
    from shapely.ops import polygonize as _polygonize
    from shapely.ops import unary_union as _unary_union
except ImportError:  # pragma: no cover - exercised when topology extra is absent
    _LineString = _Point = _Polygon = None
    _polygonize = _unary_union = None

# TopologicPy's façade modules load additional native extensions. They need the
# same main-thread preload on Windows; the resulting topology objects are still
# created and consumed only by the dedicated analysis worker.
try:
    from topologicpy.Edge import Edge as _TopologicEdge
    from topologicpy.Face import Face as _TopologicFace
    from topologicpy.Ontology import Ontology as _TopologicOntology
    from topologicpy.Vertex import Vertex as _TopologicVertex
    from topologicpy.Wire import Wire as _TopologicWire
except ImportError:  # pragma: no cover - exercised when topology extra is absent
    _TopologicEdge = _TopologicFace = _TopologicOntology = None
    _TopologicVertex = _TopologicWire = None


class TopologyUnavailableError(RuntimeError):
    """Raised when the optional topology dependency group is not installed."""


class TopologyEngine:
    """Build and manipulate a 2D architectural semantic graph."""

    CONTEXT: Dict[str, str] = {
        "top": "http://w3id.org/topologicpy#",
        "bot": "https://w3id.org/bot#",
        "cad": "urn:topospatial:cad#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }

    def __init__(
        self,
        snap_tolerance_mm: float = 1.0,
        max_opening_gap_mm: float = 1500.0,
        min_room_dimension_mm: float = 1000.0,
    ) -> None:
        self.snap_tolerance_mm = float(snap_tolerance_mm)
        self.max_opening_gap_mm = float(max_opening_gap_mm)
        self.min_room_dimension_mm = float(min_room_dimension_mm)

    @staticmethod
    def ensure_available() -> None:
        """Verify both the Python facade and its geometry kernel are importable."""
        try:
            import topologic_core  # noqa: F401
            import topologicpy  # noqa: F401
        except ImportError as exc:
            raise TopologyUnavailableError(
                "Topology support is not installed. Run `uv sync --extra topology` "
                "and restart the MCP server."
            ) from exc
        if any(
            item is None
            for item in (
                _TopologicEdge,
                _TopologicFace,
                _TopologicOntology,
                _TopologicVertex,
                _TopologicWire,
            )
        ):
            raise TopologyUnavailableError(
                "TopologicPy facade modules failed to load with topologic_core"
            )

    def analyze(self, snapshot: DrawingSnapshot) -> Dict[str, Any]:
        """Build a semantic graph and unclassified room candidates."""
        issues: List[Dict[str, Any]] = [
            {
                "code": "unsupported_geometry",
                "handle": entity.handle,
                "object_type": entity.object_type,
                "layer": entity.layer,
            }
            for entity in snapshot.entities
            if entity.geometry.get("kind") == "unsupported"
        ]
        nodes = self._semantic_nodes(snapshot.entities, issues)
        candidates = self._room_candidates(snapshot.entities, issues)

        explicit_room_polygons = self._explicit_room_polygons(nodes)
        if explicit_room_polygons:
            candidates = [
                candidate
                for candidate in candidates
                if not self._duplicates_explicit_room(
                    candidate["boundary"], explicit_room_polygons
                )
            ]

        for node in nodes:
            if node.get("@type") != "top:Room":
                continue
            boundary = node.get("cad:geometry", {}).get("boundary")
            if not boundary:
                continue
            try:
                node["top:hasArea"] = self._topologic_face_area(
                    boundary,
                    ontology_class="top:Room",
                    label=node.get("rdfs:label"),
                )
                node["top:hasUnit"] = "mm"
                node["cad:areaSquareMetres"] = round(
                    node["top:hasArea"] / 1_000_000.0, 6
                )
            except Exception as exc:
                issues.append(
                    {
                        "code": "topologic_face_failed",
                        "semantic_id": node.get("@id"),
                        "message": str(exc),
                    }
                )

        relations = self._build_relations(nodes)
        graph = self.to_jsonld(snapshot, nodes, relations)
        return {
            "success": True,
            "drawing": snapshot.drawing_name,
            "units": snapshot.units,
            "revision": snapshot.revision,
            "node_count": len(nodes),
            "relation_count": len(relations),
            "candidate_count": len(candidates),
            "graph": graph,
            "candidates": candidates,
            "issues": issues,
        }

    def _semantic_nodes(
        self,
        entities: Iterable[EntitySnapshot],
        issues: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[EntitySnapshot]] = defaultdict(list)
        for entity in entities:
            semantic_id = str(entity.semantic.get("semantic_id", "")).strip()
            ontology_class = str(entity.semantic.get("ontology_class", "")).strip()
            if semantic_id and ontology_class:
                if ontology_class not in ONTOLOGY_CLASSES:
                    issues.append(
                        {
                            "code": "unsupported_ontology_class",
                            "handle": entity.handle,
                            "semantic_id": semantic_id,
                            "ontology_class": ontology_class,
                        }
                    )
                    continue
                grouped[semantic_id].append(entity)

        nodes: List[Dict[str, Any]] = []
        for semantic_id, members in sorted(grouped.items()):
            ontology_classes = {
                str(member.semantic.get("ontology_class")) for member in members
            }
            if len(ontology_classes) != 1:
                issues.append(
                    {
                        "code": "ambiguous_semantic_group",
                        "semantic_id": semantic_id,
                        "ontology_classes": sorted(ontology_classes),
                        "handles": sorted(member.handle for member in members),
                    }
                )
                continue
            first = members[0]
            semantic = first.semantic
            geometry = semantic.get("geometry")
            if not isinstance(geometry, dict):
                geometry = self._combined_geometry(members)
            if (
                semantic.get("ontology_class") == "top:Room"
                and geometry.get("kind") == "polyline"
                and geometry.get("closed")
                and len(geometry.get("vertices", [])) >= 3
            ):
                geometry = {
                    "boundary": [
                        [float(point[0]), float(point[1])]
                        for point in geometry["vertices"]
                    ]
                }
            node: Dict[str, Any] = {
                "@id": semantic_id,
                "@type": semantic.get("ontology_class"),
                "cad:handles": sorted(item.handle for item in members),
                "cad:layers": sorted({item.layer for item in members}),
                "cad:managed": bool(semantic.get("managed", False)),
                "cad:geometry": geometry,
            }
            for source_key, target_key in (
                ("label", "rdfs:label"),
                ("group_id", "cad:groupId"),
                ("host_wall_id", "cad:hostWall"),
                ("parent_id", "cad:parent"),
                ("room_ids", "cad:boundingRooms"),
                ("aliases", "cad:aliases"),
                ("representation", "cad:representation"),
            ):
                value = semantic.get(source_key)
                if value not in (None, ""):
                    node[target_key] = value
            nodes.append(node)
        return nodes

    @staticmethod
    def _combined_geometry(members: Sequence[EntitySnapshot]) -> Dict[str, Any]:
        if len(members) == 1:
            return dict(members[0].geometry)
        return {
            "kind": "compound",
            "parts": [dict(member.geometry) for member in members],
        }

    def _room_candidates(
        self, entities: Iterable[EntitySnapshot], issues: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if _LineString is None or _polygonize is None or _unary_union is None:
            raise TopologyUnavailableError(
                "Shapely is missing from the topology dependency group."
            )

        segments: List[Tuple[Point2D, Point2D]] = []
        for entity in entities:
            ontology_class = entity.semantic.get("ontology_class")
            is_wall_hint = (
                ontology_class == "top:Wall" or "WALL" in entity.layer.upper()
            )
            if not is_wall_hint:
                continue
            segments.extend(self._segments_from_entity(entity))

        if not segments:
            return []

        healed = self._heal_axis_aligned_gaps(segments)
        linework = [_LineString([start, end]) for start, end in healed]
        try:
            polygons = list(_polygonize(_unary_union(linework)))
        except Exception as exc:
            issues.append({"code": "polygonize_failed", "message": str(exc)})
            return []

        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for polygon in polygons:
            if polygon.is_empty or not polygon.is_valid:
                continue
            min_dimension = self._minimum_rectangle_dimension(polygon)
            if min_dimension < self.min_room_dimension_mm:
                continue
            if polygon.area < self.min_room_dimension_mm**2:
                continue
            envelope_area = polygon.minimum_rotated_rectangle.area
            # Wall bands and jamb fragments can have a large bounding box but a
            # very small occupied area.  Keep plausible room regions, while
            # leaving strongly concave fragments as quality issues rather than
            # semantic candidates.
            if envelope_area <= 0 or polygon.area / envelope_area < 0.5:
                continue
            boundary = [
                [round(float(x), 6), round(float(y), 6)]
                for x, y in list(polygon.exterior.coords)[:-1]
            ]
            digest = hashlib.sha256(
                json.dumps(boundary, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16]
            candidate_id = f"candidate:room:{digest}"
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            # Candidates have no semantic class by design.  Keep their metric
            # calculation in Shapely and defer TopologicPy object construction
            # until a user explicitly annotates the candidate as a room.
            area = float(polygon.area)
            centroid = polygon.centroid
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "suggested_type": None,
                    "boundary": boundary,
                    "centroid": [round(centroid.x, 6), round(centroid.y, 6)],
                    "clear_area_mm2": round(float(area), 3),
                    "clear_area_m2": round(float(area) / 1_000_000.0, 6),
                    "requires_explicit_tag": True,
                }
            )
        return sorted(candidates, key=lambda item: item["candidate_id"])

    @staticmethod
    def _segments_from_entity(entity: EntitySnapshot) -> List[Tuple[Point2D, Point2D]]:
        geometry = entity.geometry
        kind = geometry.get("kind")
        if kind == "line":
            return [(tuple(geometry["start"][:2]), tuple(geometry["end"][:2]))]
        if kind == "polyline":
            vertices = [tuple(point[:2]) for point in geometry.get("vertices", [])]
            result = list(zip(vertices, vertices[1:]))
            if geometry.get("closed") and len(vertices) > 2:
                result.append((vertices[-1], vertices[0]))
            return result
        return []

    def _heal_axis_aligned_gaps(
        self, segments: Sequence[Tuple[Point2D, Point2D]]
    ) -> List[Tuple[Point2D, Point2D]]:
        result = list(segments)
        horizontal: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
        vertical: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
        tolerance = max(self.snap_tolerance_mm, 0.001)

        for start, end in segments:
            if abs(start[1] - end[1]) <= tolerance:
                key = round(((start[1] + end[1]) / 2.0) / tolerance)
                horizontal[key].append((min(start[0], end[0]), max(start[0], end[0])))
            elif abs(start[0] - end[0]) <= tolerance:
                key = round(((start[0] + end[0]) / 2.0) / tolerance)
                vertical[key].append((min(start[1], end[1]), max(start[1], end[1])))

        for key, intervals in horizontal.items():
            y = key * tolerance
            for left, right in self._gaps_to_bridge(intervals):
                result.append(((left, y), (right, y)))
        for key, intervals in vertical.items():
            x = key * tolerance
            for bottom, top in self._gaps_to_bridge(intervals):
                result.append(((x, bottom), (x, top)))
        return result

    def _gaps_to_bridge(
        self, intervals: Sequence[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        if len(intervals) < 2:
            return []
        ordered = sorted(intervals)
        bridges: List[Tuple[float, float]] = []
        current_end = ordered[0][1]
        for start, end in ordered[1:]:
            gap = start - current_end
            if self.snap_tolerance_mm < gap <= self.max_opening_gap_mm:
                bridges.append((current_end, start))
            current_end = max(current_end, end)
        return bridges

    @staticmethod
    def _minimum_rectangle_dimension(polygon: Any) -> float:
        coords = list(polygon.minimum_rotated_rectangle.exterior.coords)
        lengths = [
            math.dist(coords[index], coords[index + 1])
            for index in range(min(4, len(coords) - 1))
        ]
        return min(lengths) if lengths else 0.0

    @staticmethod
    def _topologic_face_area(
        boundary: Sequence[Sequence[float]],
        ontology_class: str | None = None,
        label: str | None = None,
    ) -> float:
        TopologyEngine.ensure_available()
        assert _TopologicVertex is not None
        assert _TopologicEdge is not None
        assert _TopologicWire is not None
        assert _TopologicFace is not None
        assert _TopologicOntology is not None
        vertices = [
            _TopologicVertex.ByCoordinates(point[0], point[1], 0.0)
            for point in boundary
        ]
        edges = [
            _TopologicEdge.ByVertices(
                [vertices[index], vertices[(index + 1) % len(vertices)]]
            )
            for index in range(len(vertices))
        ]
        wire = _TopologicWire.ByEdges(edges)
        face = _TopologicFace.ByWire(wire)
        if face is None:
            raise ValueError("TopologicPy could not construct a face from the boundary")
        if ontology_class:
            _TopologicOntology.SetClass(face, ontology_class, silent=True)
        if label:
            _TopologicOntology.SetLabel(face, label, silent=True)
        return float(_TopologicFace.Area(face))

    @staticmethod
    def _explicit_room_polygons(nodes: Sequence[Dict[str, Any]]) -> List[Any]:
        if _Polygon is None:
            raise TopologyUnavailableError(
                "Shapely is missing from the topology dependency group."
            )

        polygons = []
        for node in nodes:
            if node.get("@type") != "top:Room":
                continue
            boundary = node.get("cad:geometry", {}).get("boundary")
            if boundary:
                polygons.append(_Polygon(boundary))
        return polygons

    @staticmethod
    def _duplicates_explicit_room(
        boundary: Sequence[Sequence[float]], polygons: List[Any]
    ) -> bool:
        if _Polygon is None:
            raise TopologyUnavailableError(
                "Shapely is missing from the topology dependency group."
            )

        candidate = _Polygon(boundary)
        for explicit in polygons:
            smaller_area = min(candidate.area, explicit.area)
            larger_area = max(candidate.area, explicit.area)
            overlap = candidate.intersection(explicit).area
            # Native wall centerlines enclose the clear room plus one wall
            # thickness. Treat that near-sized enclosing polygon as the same
            # explicitly managed room, while retaining genuinely larger spaces.
            if (
                smaller_area
                and overlap / smaller_area >= 0.95
                and larger_area / smaller_area <= 1.25
            ):
                return True
        return False

    def _build_relations(self, nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        if _LineString is None or _Point is None or _Polygon is None:
            raise TopologyUnavailableError(
                "Shapely is missing from the topology dependency group."
            )

        relations: set[Tuple[str, str, str]] = set()
        room_shapes: Dict[str, Any] = {}
        element_shapes: Dict[str, Any] = {}
        nodes_by_id = {node["@id"]: node for node in nodes}
        wall_rooms: Dict[str, List[str]] = {}

        for node in nodes:
            semantic_id = node["@id"]
            geometry = node.get("cad:geometry", {})
            if node.get("@type") == "top:Room" and geometry.get("boundary"):
                room_shapes[semantic_id] = _Polygon(geometry["boundary"])
            if node.get("@type") in {"top:Door", "top:Window"}:
                shape = self._opening_shape(node, nodes_by_id, _LineString)
            else:
                shape = self._shape_from_geometry(
                    geometry, _LineString, _Point, _Polygon
                )
            if shape is not None:
                element_shapes[semantic_id] = shape

            parent = node.get("cad:parent")
            if parent:
                relations.add((parent, "top:containsElement", semantic_id))
                relations.add((semantic_id, "top:isPartOf", parent))
            host = node.get("cad:hostWall")
            if host:
                relations.add((host, "top:containsElement", semantic_id))
                # ``cad:hostWall`` remains the stable scalar host identifier
                # used by transaction planning.  Express the graph edge with
                # the ontology predicate instead of overwriting that scalar
                # with a JSON-LD relationship list during serialization.
                relations.add((semantic_id, "top:isPartOf", host))

            if node.get("@type") == "top:Wall":
                bounding_rooms = node.get("cad:boundingRooms", [])
                if not isinstance(bounding_rooms, list):
                    bounding_rooms = []
                room_ids = sorted(
                    {
                        str(room_id)
                        for room_id in bounding_rooms
                        if str(room_id) in room_shapes or str(room_id) in nodes_by_id
                    }
                )
                if parent and str(parent) not in room_ids:
                    room_ids.append(str(parent))
                    room_ids.sort()
                wall_rooms[semantic_id] = room_ids
                for room_id in room_ids:
                    relations.add((room_id, "top:boundedBy", semantic_id))
                    relations.add((semantic_id, "top:bounds", room_id))

        for room_id, room in room_shapes.items():
            for element_id, element in element_shapes.items():
                if element_id == room_id:
                    continue
                if room.boundary.distance(element) <= self.snap_tolerance_mm:
                    relations.add((room_id, "top:containsElement", element_id))
                    relations.add((element_id, "top:isPartOf", room_id))

        room_items = list(room_shapes.items())
        for index, (left_id, left_shape) in enumerate(room_items):
            for right_id, right_shape in room_items[index + 1 :]:
                if (
                    left_shape.boundary.intersection(right_shape.boundary).length
                    > self.snap_tolerance_mm
                ):
                    relations.add((left_id, "top:adjacentTo", right_id))
                    relations.add((right_id, "top:adjacentTo", left_id))

        for room_ids in wall_rooms.values():
            for index, left_id in enumerate(room_ids):
                for right_id in room_ids[index + 1 :]:
                    relations.add((left_id, "top:adjacentTo", right_id))
                    relations.add((right_id, "top:adjacentTo", left_id))

        for node in nodes:
            if node.get("@type") != "top:Door":
                continue
            door_shape = element_shapes.get(node["@id"])
            if door_shape is None:
                continue
            touching_rooms = [
                room_id
                for room_id, room_shape in room_shapes.items()
                if room_shape.boundary.distance(door_shape)
                <= self.max_opening_gap_mm / 2.0
            ]
            host_id = node.get("cad:hostWall") or node.get("cad:geometry", {}).get(
                "host_wall_id"
            )
            touching_rooms.extend(wall_rooms.get(str(host_id), []))
            touching_rooms = sorted(set(touching_rooms))
            for index, left_id in enumerate(touching_rooms):
                for right_id in touching_rooms[index + 1 :]:
                    relations.add((left_id, "top:connectsTo", right_id))
                    relations.add((right_id, "top:connectsTo", left_id))

        return [
            {"subject": subject, "predicate": predicate, "object": obj}
            for subject, predicate, obj in sorted(relations)
        ]

    @staticmethod
    def _opening_shape(
        node: Dict[str, Any], nodes_by_id: Dict[str, Dict[str, Any]], line_cls: Any
    ) -> Any:
        geometry = node.get("cad:geometry", {})
        direct = TopologyEngine._shape_from_geometry(
            geometry, line_cls, _Point, _Polygon
        )
        if direct is not None:
            return direct
        host_id = node.get("cad:hostWall") or geometry.get("host_wall_id")
        host = nodes_by_id.get(host_id, {})
        host_geometry = host.get("cad:geometry", {})
        start = host_geometry.get("start")
        end = host_geometry.get("end")
        if not start or not end:
            return None
        length = math.dist(start[:2], end[:2])
        if length <= 0:
            return None
        direction = (
            (float(end[0]) - float(start[0])) / length,
            (float(end[1]) - float(start[1])) / length,
        )
        offset = float(geometry.get("offset", 0.0))
        width = float(geometry.get("width", 0.0))
        opening_start = (
            float(start[0]) + direction[0] * offset,
            float(start[1]) + direction[1] * offset,
        )
        opening_end = (
            opening_start[0] + direction[0] * width,
            opening_start[1] + direction[1] * width,
        )
        return line_cls([opening_start, opening_end])

    @staticmethod
    def _shape_from_geometry(
        geometry: Dict[str, Any], line_cls: Any, point_cls: Any, polygon_cls: Any
    ) -> Any:
        kind = geometry.get("kind")
        if kind == "line" or (geometry.get("start") and geometry.get("end")):
            return line_cls([geometry["start"][:2], geometry["end"][:2]])
        if kind == "polyline":
            vertices = geometry.get("vertices", [])
            if geometry.get("closed") and len(vertices) >= 3:
                return polygon_cls([point[:2] for point in vertices])
            if len(vertices) >= 2:
                return line_cls([point[:2] for point in vertices])
        if kind == "point":
            return point_cls(geometry["position"][:2])
        if geometry.get("boundary"):
            return polygon_cls(geometry["boundary"])
        if kind == "compound":
            shapes = [
                TopologyEngine._shape_from_geometry(
                    part, line_cls, point_cls, polygon_cls
                )
                for part in geometry.get("parts", [])
            ]
            shapes = [shape for shape in shapes if shape is not None]
            if shapes:
                if _unary_union is None:
                    raise TopologyUnavailableError(
                        "Shapely is missing from the topology dependency group."
                    )
                return _unary_union(shapes)
        return None

    def plan_changes(
        self,
        document: ChangeDocument,
        snapshot: DrawingSnapshot,
        analysis: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Validate a change document and produce a non-mutating CAD operation plan."""
        if document.base_revision != snapshot.revision:
            raise ValueError(
                "base_revision does not match the active drawing; run analyze again"
            )
        nodes = {node["@id"]: node for node in analysis["graph"].get("@graph", [])}
        candidates = {
            candidate["candidate_id"]: candidate
            for candidate in analysis.get("candidates", [])
        }
        operations: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for change in document.changes:
            if change.op == "annotate":
                planned = self._plan_annotation(change, snapshot, candidates)
            elif change.op == "create":
                planned = [self._plan_create(change)]
            elif change.op == "update":
                planned = [self._plan_update(change, nodes)]
            else:
                planned = self._plan_delete(change, nodes)
            operations.extend(planned)
            for operation in planned:
                warnings.extend(operation.get("warnings", []))

        operations, network_warnings = normalize_wall_network(operations, nodes)
        warnings.extend(network_warnings)
        self._validate_operation_hosts(operations, nodes)
        diff = [self._operation_diff(item) for item in operations]
        return operations, diff, warnings

    def _plan_annotation(
        self,
        change: TopologyChange,
        snapshot: DrawingSnapshot,
        candidates: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        assert change.targets is not None
        assert change.ontology_class is not None
        target = change.targets
        if target.candidate_id:
            if change.ontology_class != "top:Room":
                raise ValueError("room candidates can only be annotated as top:Room")
            candidate = candidates.get(target.candidate_id)
            if candidate is None:
                raise ValueError(f"Unknown candidate_id '{target.candidate_id}'")
            semantic_id = change.semantic_id or self._new_semantic_id()
            return [
                {
                    "kind": "create_room_boundary",
                    "semantic_id": semantic_id,
                    "ontology_class": "top:Room",
                    "label": change.label or "",
                    "boundary": candidate["boundary"],
                }
            ]

        if target.handles:
            requested = {handle.upper() for handle in target.handles}
            matched = [
                entity
                for entity in snapshot.entities
                if entity.handle.upper() in requested
            ]
            missing = requested - {entity.handle.upper() for entity in matched}
            if missing:
                raise ValueError(
                    f"Unknown entity handles: {', '.join(sorted(missing))}"
                )
        else:
            matched = [
                entity for entity in snapshot.entities if entity.layer == target.layer
            ]
            if not matched:
                raise ValueError(f"No entities found on layer '{target.layer}'")

        groups = self._annotation_groups(matched, target.grouping)
        planned = []
        for group in groups:
            unsupported = [
                entity.handle
                for entity in group
                if entity.geometry.get("kind") == "unsupported"
            ]
            if unsupported:
                raise ValueError(
                    "Unsupported CAD geometry cannot be annotated: "
                    + ", ".join(sorted(unsupported))
                )
            if change.ontology_class == "top:Room":
                is_closed_boundary = (
                    len(group) == 1
                    and group[0].geometry.get("kind") == "polyline"
                    and bool(group[0].geometry.get("closed"))
                )
                if not is_closed_boundary:
                    raise ValueError(
                        "Existing room annotations require one closed polyline; "
                        "use a candidate_id for enclosed linework"
                    )
            annotation_geometry = self._annotation_geometry(
                change.ontology_class, group
            )
            source_representations = {
                str(entity.geometry.get("representation", "standard"))
                for entity in group
            }
            representation = (
                "native_aec"
                if source_representations == {"native_aec"}
                else "standard"
            )
            group_semantic_id: str | None = (
                change.semantic_id if len(groups) == 1 else None
            )
            operation = {
                "kind": "annotate_handles",
                "handles": sorted(entity.handle for entity in group),
                "semantic_id": group_semantic_id or self._new_semantic_id(),
                "ontology_class": change.ontology_class,
                "label": change.label or "",
                "managed": False,
                "geometry": annotation_geometry,
                "representation": representation,
            }
            if (
                change.ontology_class == "top:Wall"
                and "thickness" not in group[0].geometry
            ):
                operation["warnings"] = [
                    "Annotated wall thickness defaults to 200 mm; update the "
                    "semantic wall if a different thickness is required"
                ]
            planned.append(operation)
        return planned

    def _annotation_geometry(
        self, ontology_class: str, group: Sequence[EntitySnapshot]
    ) -> Dict[str, Any]:
        if ontology_class == "top:Room":
            vertices = group[0].geometry["vertices"]
            return {
                "boundary": [[float(point[0]), float(point[1])] for point in vertices]
            }
        if ontology_class == "top:Wall":
            if len(group) != 1:
                raise ValueError(
                    "A straight wall annotation requires exactly one line or "
                    "two-vertex polyline"
                )
            source = group[0].geometry
            if source.get("kind") == "line":
                start, end = source["start"], source["end"]
            elif (
                source.get("kind") == "polyline"
                and len(source.get("vertices", [])) == 2
            ):
                start, end = source["vertices"]
            else:
                raise ValueError(
                    "A straight wall annotation requires exactly one line or "
                    "two-vertex polyline"
                )
            geometry = {
                "kind": "line",
                "start": [float(start[0]), float(start[1]), 0.0],
                "end": [float(end[0]), float(end[1]), 0.0],
                "thickness": float(source.get("thickness", 200.0)),
            }
            if "height" in source:
                geometry["height"] = float(source["height"])
            if "style" in source:
                geometry["style"] = str(source["style"])
            return geometry
        return self._combined_geometry(group)

    def _annotation_groups(
        self, entities: Sequence[EntitySnapshot], grouping: str
    ) -> List[List[EntitySnapshot]]:
        if grouping == "single_object":
            return [list(entities)]
        if grouping == "individual":
            return [[entity] for entity in entities]

        remaining = list(entities)
        groups: List[List[EntitySnapshot]] = []
        while remaining:
            group = [remaining.pop(0)]
            endpoints = self._entity_endpoints(group[0])
            changed = True
            while changed:
                changed = False
                for entity in list(remaining):
                    other = self._entity_endpoints(entity)
                    if self._endpoint_sets_touch(endpoints, other):
                        group.append(entity)
                        endpoints.extend(other)
                        remaining.remove(entity)
                        changed = True
            groups.append(group)
        return groups

    @staticmethod
    def _entity_endpoints(entity: EntitySnapshot) -> List[Point2D]:
        geometry = entity.geometry
        if geometry.get("kind") == "line":
            return [tuple(geometry["start"][:2]), tuple(geometry["end"][:2])]
        vertices = geometry.get("vertices", [])
        if vertices:
            return [tuple(vertices[0][:2]), tuple(vertices[-1][:2])]
        return []

    def _endpoint_sets_touch(
        self, left: Sequence[Point2D], right: Sequence[Point2D]
    ) -> bool:
        return any(
            math.dist(a, b) <= self.snap_tolerance_mm for a in left for b in right
        )

    def _plan_create(self, change: TopologyChange) -> Dict[str, Any]:
        assert change.ontology_class is not None and change.geometry is not None
        geometry = self.validate_geometry(change.ontology_class, change.geometry)
        return {
            "kind": "create_managed",
            "semantic_id": change.semantic_id or self._new_semantic_id(),
            "ontology_class": change.ontology_class,
            "label": change.label or "",
            "geometry": geometry,
            "representation": change.representation,
        }

    def _plan_update(
        self, change: TopologyChange, nodes: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        assert change.semantic_id is not None
        node = nodes.get(change.semantic_id)
        if node is None:
            raise ValueError(f"Unknown semantic object '{change.semantic_id}'")
        if not node.get("cad:managed"):
            if change.geometry is not None:
                raise ValueError(
                    "Geometry updates are allowed only for MCP-managed objects"
                )
            return {
                "kind": "update_label",
                "semantic_id": change.semantic_id,
                "handles": node.get("cad:handles", []),
                "label": change.label or "",
            }
        ontology_class = node["@type"]
        geometry = node.get("cad:geometry", {})
        if change.geometry is not None:
            geometry = self.validate_geometry(ontology_class, change.geometry)
        old_handles = list(node.get("cad:handles", []))
        previous_wall_ids: List[str] = []
        if ontology_class == "top:Room":
            child_ids = {
                semantic_id
                for semantic_id, child in nodes.items()
                if child.get("@type") == "top:Wall"
                and change.semantic_id in self._wall_room_ids(child)
            }
            previous_wall_ids = sorted(child_ids)
            hosted = [
                dependent_id
                for dependent_id, dependent in nodes.items()
                if dependent.get("cad:hostWall") in child_ids
            ]
            if hosted:
                raise ValueError(
                    "Room geometry cannot be updated while its walls host openings: "
                    + ", ".join(sorted(hosted))
                )
        operation = {
            "kind": "replace_managed",
            "semantic_id": change.semantic_id,
            "ontology_class": ontology_class,
            "label": change.label
            if change.label is not None
            else node.get("rdfs:label", ""),
            "geometry": geometry,
            "representation": (
                node.get("cad:representation", "auto")
                if change.representation == "auto"
                else change.representation
            ),
            "old_handles": sorted(set(old_handles)),
        }
        if previous_wall_ids:
            operation["previous_wall_ids"] = previous_wall_ids
        return operation

    @staticmethod
    def _wall_room_ids(node: Dict[str, Any]) -> set[str]:
        """Read version-2 multi-room relations with legacy parent fallback."""
        room_ids = node.get("cad:boundingRooms", [])
        result = (
            {str(item) for item in room_ids if item}
            if isinstance(room_ids, list)
            else set()
        )
        if node.get("cad:parent"):
            result.add(str(node["cad:parent"]))
        return result

    def _plan_delete(
        self, change: TopologyChange, nodes: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        assert change.semantic_id is not None
        node = nodes.get(change.semantic_id)
        if node is None:
            raise ValueError(f"Unknown semantic object '{change.semantic_id}'")
        if not node.get("cad:managed"):
            raise ValueError("Refusing to delete untagged or legacy CAD geometry")

        shared_wall_updates: List[Dict[str, Any]] = []
        owned_ids: set[str] = set()
        if node.get("@type") == "top:Room":
            for semantic_id, child in nodes.items():
                if child.get("@type") != "top:Wall":
                    continue
                room_ids = self._wall_room_ids(child)
                if change.semantic_id not in room_ids:
                    continue
                remaining = sorted(room_ids - {change.semantic_id})
                if remaining:
                    shared_wall_updates.append(
                        {
                            "kind": "update_wall_rooms",
                            "semantic_id": semantic_id,
                            "ontology_class": "top:Wall",
                            "handles": list(child.get("cad:handles", [])),
                            "bounding_room_ids": remaining,
                            "aliases": sorted(
                                alias
                                for alias in child.get("cad:aliases", [])
                                if not str(alias).startswith(
                                    f"{change.semantic_id}:wall:"
                                )
                            ),
                        }
                    )
                else:
                    owned_ids.add(semantic_id)
        else:
            owned_ids = {
                semantic_id
                for semantic_id, child in nodes.items()
                if child.get("cad:parent") == change.semantic_id
            }
        host_ids = owned_ids | {change.semantic_id}
        dependent_ids = {
            semantic_id
            for semantic_id, dependent in nodes.items()
            if dependent.get("cad:hostWall") in host_ids
        }
        if dependent_ids and not change.cascade:
            raise ValueError(
                "Object has hosted openings; set cascade=true to delete them: "
                + ", ".join(sorted(dependent_ids))
            )

        delete_ids = {change.semantic_id} | owned_ids
        if change.cascade:
            delete_ids |= dependent_ids
        handles = {
            handle
            for semantic_id in delete_ids
            for handle in nodes[semantic_id].get("cad:handles", [])
        }
        return [
            {
                "kind": "delete_managed",
                "semantic_id": change.semantic_id,
                "ontology_class": node.get("@type"),
                "handles": sorted(handles),
                "deleted_semantic_ids": sorted(delete_ids),
                "cascade": change.cascade,
            },
            *shared_wall_updates,
        ]

    def _validate_operation_hosts(
        self,
        operations: Sequence[Dict[str, Any]],
        nodes: Dict[str, Dict[str, Any]],
    ) -> None:
        """Reject missing or geometrically invalid opening hosts at preview time."""
        wall_geometry: Dict[str, Dict[str, Any]] = {
            semantic_id: node.get("cad:geometry", {})
            for semantic_id, node in nodes.items()
            if node.get("@type") == "top:Wall"
        }

        for operation in operations:
            kind = operation["kind"]
            semantic_id = operation.get("semantic_id")
            ontology_class = operation.get("ontology_class")

            if kind == "delete_managed":
                for deleted_id in operation.get("deleted_semantic_ids", [semantic_id]):
                    wall_geometry.pop(deleted_id, None)
                continue

            if ontology_class == "top:Wall" and kind in {
                "annotate_handles",
                "create_managed",
                "replace_managed",
            }:
                if not isinstance(semantic_id, str):
                    raise ValueError("Wall operation is missing a semantic ID")
                wall_geometry[semantic_id] = operation["geometry"]
                continue

            if ontology_class not in {"top:Door", "top:Window"} or kind not in {
                "create_managed",
                "replace_managed",
            }:
                continue
            geometry = operation["geometry"]
            host_id = geometry["host_wall_id"]
            host_geometry = wall_geometry.get(host_id)
            if host_geometry is None:
                raise ValueError(
                    f"Missing host wall '{host_id}'; create or annotate it before "
                    "the opening"
                )
            host_length = self._wall_geometry_length(host_geometry)
            if float(geometry["offset"]) + float(geometry["width"]) > host_length:
                raise ValueError(
                    f"{ontology_class.removeprefix('top:').lower()} extends beyond "
                    f"host wall '{host_id}'"
                )

    @staticmethod
    def _wall_geometry_length(geometry: Dict[str, Any]) -> float:
        if "length" in geometry:
            return float(geometry["length"])
        start = geometry.get("start")
        end = geometry.get("end")
        if not start or not end:
            raise ValueError("Host wall does not have a usable straight centerline")
        return math.dist(start[:2], end[:2])

    @staticmethod
    def _operation_diff(operation: Dict[str, Any]) -> Dict[str, Any]:
        kind = operation["kind"]
        action = "update"
        if kind.startswith("create"):
            action = "create"
        elif kind.startswith("delete"):
            action = "delete"
        return {
            "action": action,
            "semantic_id": operation.get("semantic_id"),
            "ontology_class": operation.get("ontology_class"),
            "handles": operation.get("handles", operation.get("old_handles", [])),
            "operation": kind,
            "representation": operation.get("representation"),
        }

    @staticmethod
    def validate_geometry(
        ontology_class: str, geometry: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate and normalize the strict architectural geometry contract."""
        if not isinstance(geometry, dict):
            raise ValueError("geometry must be an object")
        value = dict(geometry)
        normalized: Dict[str, Any]

        def point(name: str) -> List[float]:
            raw = value.get(name)
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise ValueError(f"geometry.{name} must be [x, y]")
            return [float(raw[0]), float(raw[1])]

        def positive(name: str, default: float | None = None) -> float:
            raw = value.get(name, default)
            if raw is None or isinstance(raw, bool) or float(raw) <= 0:
                raise ValueError(f"geometry.{name} must be positive")
            return float(raw)

        if ontology_class == "top:Room":
            allowed = {
                "origin",
                "clear_width",
                "clear_depth",
                "wall_thickness",
                "rotation_deg",
                "wall_height",
                "wall_style",
            }
            normalized = {
                "origin": point("origin"),
                "clear_width": positive("clear_width"),
                "clear_depth": positive("clear_depth"),
                "wall_thickness": positive("wall_thickness", 200.0),
                "rotation_deg": float(value.get("rotation_deg", 0.0)),
                "wall_height": positive("wall_height", 3000.0),
                "wall_style": str(value.get("wall_style", "Standard")),
            }
        elif ontology_class == "top:Wall":
            allowed = {"start", "end", "thickness", "height", "style"}
            normalized = {
                "start": point("start"),
                "end": point("end"),
                "thickness": positive("thickness", 200.0),
                "height": positive("height", 3000.0),
                "style": str(value.get("style", "Standard")),
            }
            if normalized["start"] == normalized["end"]:
                raise ValueError("wall start and end must differ")
        elif ontology_class == "top:Door":
            allowed = {
                "host_wall_id",
                "offset",
                "width",
                "height",
                "style",
                "hinge",
                "swing",
                "swing_angle_deg",
            }
            if not value.get("host_wall_id"):
                raise ValueError("door geometry requires host_wall_id")
            if value.get("hinge") not in ("left", "right"):
                raise ValueError("door hinge must be left or right")
            if value.get("swing") not in ("in", "out"):
                raise ValueError("door swing must be in or out")
            normalized = {
                "host_wall_id": str(value["host_wall_id"]),
                "offset": float(value.get("offset", 0.0)),
                "width": positive("width", 900.0),
                "height": positive("height", 2100.0),
                "style": str(value.get("style", "Standard")),
                "hinge": value["hinge"],
                "swing": value["swing"],
                "swing_angle_deg": positive("swing_angle_deg", 90.0),
            }
            if normalized["offset"] < 0:
                raise ValueError("door offset cannot be negative")
        elif ontology_class == "top:Window":
            allowed = {
                "host_wall_id",
                "offset",
                "width",
                "height",
                "sill_height",
                "style",
            }
            if not value.get("host_wall_id"):
                raise ValueError("window geometry requires host_wall_id")
            normalized = {
                "host_wall_id": str(value["host_wall_id"]),
                "offset": float(value.get("offset", 0.0)),
                "width": positive("width", 1200.0),
                "height": positive("height", 1200.0),
                "sill_height": float(value.get("sill_height", 900.0)),
                "style": str(value.get("style", "Standard")),
            }
            if normalized["offset"] < 0:
                raise ValueError("window offset cannot be negative")
            if normalized["sill_height"] < 0:
                raise ValueError("window sill_height cannot be negative")
        else:
            raise ValueError(f"Unsupported ontology class '{ontology_class}'")
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown geometry fields: {', '.join(sorted(unknown))}")
        return normalized

    @staticmethod
    def _new_semantic_id() -> str:
        return f"urn:uuid:{uuid.uuid4()}"

    def to_jsonld(
        self,
        snapshot: DrawingSnapshot,
        nodes: Sequence[Dict[str, Any]],
        relations: Sequence[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Return deterministic JSON-LD for nodes and relationships."""
        graph_nodes = [json.loads(json.dumps(node)) for node in nodes]
        by_id = {node["@id"]: node for node in graph_nodes}
        for relation in relations:
            source = by_id.get(relation["subject"])
            if source is None:
                continue
            predicate = relation["predicate"]
            source.setdefault(predicate, []).append({"@id": relation["object"]})
        for node in graph_nodes:
            for key, value in list(node.items()):
                if (
                    isinstance(value, list)
                    and value
                    and isinstance(value[0], dict)
                    and "@id" in value[0]
                ):
                    node[key] = sorted(value, key=lambda item: item["@id"])

        drawing_hash = hashlib.sha256(
            (snapshot.full_name or snapshot.drawing_name).encode("utf-8")
        ).hexdigest()[:20]
        return {
            "@context": dict(self.CONTEXT),
            "@id": f"urn:topospatial:drawing:{drawing_hash}",
            "@type": "top:KnowledgeGraph",
            "cad:drawingName": snapshot.drawing_name,
            "cad:revision": snapshot.revision,
            "cad:units": snapshot.units,
            "@graph": sorted(graph_nodes, key=lambda item: item["@id"]),
        }

    def to_turtle(self, graph: Dict[str, Any]) -> str:
        """Serialize the supported JSON-LD subset without TopologicPy's slow path."""
        prefixes = [
            f"@prefix {prefix}: <{uri}> ."
            for prefix, uri in sorted(self.CONTEXT.items())
        ]
        triples: List[str] = []
        for node in sorted(graph.get("@graph", []), key=lambda item: item["@id"]):
            subject = self._turtle_resource(node["@id"])
            ontology_class = node.get("@type")
            if ontology_class:
                triples.append(f"{subject} a {self._turtle_resource(ontology_class)} .")
            for predicate, value in sorted(node.items()):
                if predicate in {"@id", "@type", "cad:geometry"}:
                    continue
                values = value if isinstance(value, list) else [value]
                for item in values:
                    pred = self._turtle_resource(predicate)
                    if isinstance(item, dict) and "@id" in item:
                        obj = self._turtle_resource(item["@id"])
                    elif isinstance(item, bool):
                        obj = f'"{str(item).lower()}"^^xsd:boolean'
                    elif isinstance(item, (int, float)):
                        obj = f'"{item}"^^xsd:double'
                    else:
                        obj = self._turtle_literal(item)
                    triples.append(f"{subject} {pred} {obj} .")
        return "\n".join(prefixes + [""] + sorted(set(triples))) + "\n"

    @staticmethod
    def _turtle_resource(value: str) -> str:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:[A-Za-z_][A-Za-z0-9_.-]*", value):
            return value
        return f"<{str(value).replace('>', '%3E')}>"

    @staticmethod
    def _turtle_literal(value: Any) -> str:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, separators=(",", ":"))
        escaped = (
            str(value)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
