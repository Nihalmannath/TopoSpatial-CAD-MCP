"""AutoCAD COM bridge for topology snapshots and transactional write-back."""

from __future__ import annotations

import json
import logging
import math
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from core.config import get_config

from .models import DrawingSnapshot, EntitySnapshot

logger = logging.getLogger(__name__)

XDATA_APP = "TOPOSPATIAL_TOPOLOGY"
XDATA_SCHEMA_VERSION = "1"
ROOM_LAYER = "AI-ROOMS"
WALL_LAYER = "AI-WALLS"
DOOR_LAYER = "AI-DOORS"
WINDOW_LAYER = "AI-WINDOWS"


class CADTopologyBridge:
    """Move primitive data between AutoCAD and the topology engine."""

    UNIT_NAMES = {
        0: "unitless",
        1: "in",
        2: "ft",
        4: "mm",
        5: "cm",
        6: "m",
    }

    def snapshot(self, adapter: Any, scope: str = "all") -> DrawingSnapshot:
        """Capture supported geometry and semantic XData on the current thread."""
        if scope not in {"all", "selected"}:
            raise ValueError("scope must be 'all' or 'selected'")
        document = adapter._get_document("topology_snapshot")
        entities = [
            self._snapshot_entity(entity)
            for entity in self._iter_entities(document, scope)
        ]
        units_code = self._safe_call(lambda: int(document.GetVariable("INSUNITS")), 0)
        return DrawingSnapshot(
            drawing_name=str(self._safe_get(document, "Name", "Drawing.dwg")),
            full_name=str(self._safe_get(document, "FullName", "")),
            units=self.UNIT_NAMES.get(units_code, f"insunits:{units_code}"),
            entities=entities,
        )

    @staticmethod
    def _iter_entities(document: Any, scope: str) -> Iterable[Any]:
        if scope == "all":
            model_space = document.ModelSpace
            for index in range(int(model_space.Count)):
                yield model_space.Item(index)
            return
        selection = document.PickfirstSelectionSet
        for index in range(int(selection.Count)):
            yield selection.Item(index)

    def _snapshot_entity(self, entity: Any) -> EntitySnapshot:
        object_type = str(self._safe_get(entity, "ObjectName", "Unknown"))
        return EntitySnapshot(
            handle=str(self._safe_get(entity, "Handle", "")),
            object_type=object_type,
            layer=str(self._safe_get(entity, "Layer", "0")).strip(),
            geometry=self._extract_geometry(entity, object_type),
            semantic=self.read_xdata(entity),
        )

    def _extract_geometry(self, entity: Any, object_type: str) -> Dict[str, Any]:
        upper = object_type.upper()
        if upper == "AECDBWALL":
            return {
                "kind": "line",
                "start": self._point(
                    self._safe_get(entity, "StartPoint", (0, 0, 0))
                ),
                "end": self._point(self._safe_get(entity, "EndPoint", (0, 0, 0))),
                "thickness": float(self._safe_get(entity, "Width", 0.0)),
                "height": float(self._safe_get(entity, "BaseHeight", 0.0)),
                "style": str(self._safe_get(entity, "StyleName", "")),
                "representation": "native_aec",
            }
        if upper in {"AECDBDOOR", "AECDBWINDOW"}:
            geometry = {
                "kind": "point",
                "position": self._point(
                    self._safe_get(entity, "Location", (0, 0, 0))
                ),
                "width": float(self._safe_get(entity, "Width", 0.0)),
                "height": float(self._safe_get(entity, "Height", 0.0)),
                "style": str(self._safe_get(entity, "StyleName", "")),
                "representation": "native_aec",
            }
            if upper == "AECDBWINDOW":
                geometry["sill_height"] = float(
                    self._safe_get(entity, "SillHeight", 0.0)
                )
            return geometry
        if "LINE" in upper and "POLY" not in upper:
            return {
                "kind": "line",
                "start": self._point(self._safe_get(entity, "StartPoint", (0, 0, 0))),
                "end": self._point(self._safe_get(entity, "EndPoint", (0, 0, 0))),
            }
        if "POLYLINE" in upper:
            coordinates = list(self._safe_get(entity, "Coordinates", []))
            vertices = self._polyline_vertices(coordinates, upper)
            return {
                "kind": "polyline",
                "vertices": vertices,
                "closed": bool(self._safe_get(entity, "Closed", False)),
            }
        if "ARC" in upper:
            return {
                "kind": "arc",
                "center": self._point(self._safe_get(entity, "Center", (0, 0, 0))),
                "radius": float(self._safe_get(entity, "Radius", 0.0)),
                "start_angle": math.degrees(
                    float(self._safe_get(entity, "StartAngle", 0.0))
                ),
                "end_angle": math.degrees(
                    float(self._safe_get(entity, "EndAngle", 0.0))
                ),
            }
        if "TEXT" in upper:
            return {
                "kind": "point",
                "position": self._point(
                    self._safe_get(
                        entity,
                        "InsertionPoint",
                        self._safe_get(entity, "TextAlignmentPoint", (0, 0, 0)),
                    )
                ),
                "text": str(self._safe_get(entity, "TextString", "")),
                "height": float(self._safe_get(entity, "Height", 0.0)),
                "rotation": math.degrees(
                    float(self._safe_get(entity, "Rotation", 0.0))
                ),
            }
        if "BLOCK" in upper or "INSERT" in upper:
            return {
                "kind": "point",
                "position": self._point(
                    self._safe_get(entity, "InsertionPoint", (0, 0, 0))
                ),
                "name": str(self._safe_get(entity, "Name", "")),
                "rotation": math.degrees(
                    float(self._safe_get(entity, "Rotation", 0.0))
                ),
            }
        return {"kind": "unsupported"}

    @staticmethod
    def _point(value: Any) -> List[float]:
        data = list(value) if value is not None else []
        while len(data) < 3:
            data.append(0.0)
        return [
            round(float(data[0]), 9),
            round(float(data[1]), 9),
            round(float(data[2]), 9),
        ]

    @staticmethod
    def _polyline_vertices(
        coordinates: Sequence[Any], object_type_upper: str
    ) -> List[List[float]]:
        if not coordinates:
            return []
        stride = 3 if "3D" in object_type_upper or "2D" in object_type_upper else 2
        if len(coordinates) % stride != 0:
            stride = 3 if len(coordinates) % 3 == 0 else 2
        vertices = []
        for index in range(0, len(coordinates), stride):
            values = list(coordinates[index : index + stride])
            if len(values) < stride:
                break
            if stride == 2:
                values.append(0.0)
            vertices.append([float(values[0]), float(values[1]), float(values[2])])
        return vertices

    def read_xdata(self, entity: Any) -> Dict[str, Any]:
        """Read and decode chunked semantic JSON from AutoCAD XData."""
        try:
            result = entity.GetXData(XDATA_APP)
        except Exception:
            return {}
        if not isinstance(result, tuple) or len(result) < 2:
            return {}
        values = result[-1]
        try:
            chunks = [str(value) for value in list(values)[1:] if value is not None]
            if not chunks:
                return {}
            payload = json.loads("".join(chunks))
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def write_xdata(self, document: Any, entity: Any, semantic: Dict[str, Any]) -> None:
        """Register and attach compact, chunked topology XData."""
        try:
            document.RegisteredApplications.Add(XDATA_APP)
        except Exception:
            pass
        payload = dict(semantic)
        payload.setdefault("schema_version", XDATA_SCHEMA_VERSION)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        chunks = [encoded[index : index + 250] for index in range(0, len(encoded), 250)]
        type_codes = [1001] + [1000] * len(chunks)
        values: List[Any] = [XDATA_APP] + chunks
        try:
            import pythoncom
            import win32com.client

            code_variant = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_I2, type_codes
            )
            value_variant = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, values
            )
            entity.SetXData(code_variant, value_variant)
        except ImportError:
            entity.SetXData(type_codes, values)

    def apply_operations(
        self, adapter: Any, operations: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply a previewed operation list inside one AutoCAD undo mark."""
        document = adapter._get_document("topology_apply")
        created: List[Dict[str, Any]] = []
        modified_handles: List[str] = []
        undo_started = False
        try:
            document.StartUndoMark()
            undo_started = True
        except Exception:
            logger.warning(
                "CAD does not expose StartUndoMark; continuing with best-effort undo"
            )

        try:
            for operation in operations:
                result = self._apply_operation(adapter, document, operation)
                created.extend(result.get("created", []))
                modified_handles.extend(result.get("modified_handles", []))
            if undo_started:
                document.EndUndoMark()
            adapter.refresh_view()
            return {
                "success": True,
                "created": created,
                "modified_handles": sorted(set(modified_handles)),
            }
        except Exception:
            if undo_started:
                try:
                    document.EndUndoMark()
                except Exception:
                    pass
            try:
                adapter.undo(1)
            except Exception:
                logger.exception("Failed to roll back topology transaction")
            raise

    def resolve_operation_representations(
        self,
        adapter: Any,
        operations: Sequence[Dict[str, Any]],
        analysis: Dict[str, Any],
    ) -> None:
        """Resolve ``auto`` during preview so apply is deterministic."""
        wall_representations = {
            node["@id"]: node.get("cad:representation", "standard")
            for node in analysis.get("graph", {}).get("@graph", [])
            if node.get("@type") == "top:Wall"
        }
        for operation in operations:
            kind = operation.get("kind")
            ontology_class = operation.get("ontology_class")
            if kind == "annotate_handles":
                if ontology_class == "top:Wall":
                    semantic_id = operation.get("semantic_id")
                    if semantic_id:
                        wall_representations[semantic_id] = operation.get(
                            "representation", "standard"
                        )
                continue
            if kind not in {"create_managed", "replace_managed"}:
                continue
            requested = operation.get("representation", "auto")
            selected = self._select_representation(adapter, requested)
            if ontology_class in {"top:Wall", "top:Room"}:
                operation["representation"] = selected
                semantic_id = operation.get("semantic_id")
                if ontology_class == "top:Wall" and semantic_id:
                    wall_representations[semantic_id] = selected
                elif ontology_class == "top:Room" and semantic_id:
                    for index in range(1, 5):
                        wall_representations[f"{semantic_id}:wall:{index}"] = selected
                continue
            if ontology_class not in {"top:Door", "top:Window"}:
                continue
            host_id = operation.get("geometry", {}).get("host_wall_id")
            host_representation = wall_representations.get(host_id, "standard")
            if selected == "native_aec" and host_representation != "native_aec":
                if requested == "native_aec":
                    raise ValueError(
                        "native_aec openings require a native AecDbWall host"
                    )
                selected = "standard"
            operation["representation"] = selected
        self._validate_native_styles(adapter, operations)

    @staticmethod
    def _validate_native_styles(
        adapter: Any, operations: Sequence[Dict[str, Any]]
    ) -> None:
        native_operations = [
            operation
            for operation in operations
            if operation.get("representation") == "native_aec"
            and operation.get("kind") in {"create_managed", "replace_managed"}
        ]
        if not native_operations:
            return
        capability_method = getattr(adapter, "get_architecture_capabilities", None)
        if not callable(capability_method):
            return
        capabilities = capability_method(include_styles=True)
        if capabilities.get("style_warning"):
            raise ValueError(
                "Could not validate AutoCAD Architecture styles: "
                + str(capabilities["style_warning"])
            )
        styles = capabilities.get("styles")
        if not isinstance(styles, dict):
            return

        style_contract = {
            "top:Room": ("wall", "wall_style"),
            "top:Wall": ("wall", "style"),
            "top:Door": ("door", "style"),
            "top:Window": ("window", "style"),
        }
        for operation in native_operations:
            ontology_class = str(operation.get("ontology_class", ""))
            contract = style_contract.get(ontology_class)
            if contract is None:
                continue
            collection_name, geometry_key = contract
            geometry = operation.get("geometry", {})
            requested = str(geometry.get(geometry_key, "Standard")).strip()
            available = [str(name) for name in styles.get(collection_name, [])]
            canonical = {name.casefold(): name for name in available}
            if requested.casefold() not in canonical:
                raise ValueError(
                    f"Unknown ACA {collection_name} style '{requested}'. "
                    f"Available styles: {', '.join(available) or '(none)'}"
                )
            geometry[geometry_key] = canonical[requested.casefold()]

    def _apply_operation(
        self, adapter: Any, document: Any, operation: Dict[str, Any]
    ) -> Dict[str, Any]:
        kind = operation["kind"]
        if kind == "annotate_handles":
            handles = operation["handles"]
            semantic = self._semantic_payload(operation, managed=False)
            for handle in handles:
                entity = document.HandleToObject(handle)
                existing = self.read_xdata(entity)
                existing.update(semantic)
                self.write_xdata(document, entity, existing)
            return {"modified_handles": handles, "created": []}
        if kind == "update_label":
            for handle in operation["handles"]:
                entity = document.HandleToObject(handle)
                semantic = self.read_xdata(entity)
                semantic["label"] = operation["label"]
                self.write_xdata(document, entity, semantic)
            return {"modified_handles": operation["handles"], "created": []}
        if kind == "create_room_boundary":
            created = self._create_room_boundary(
                adapter,
                document,
                operation["semantic_id"],
                operation.get("label", ""),
                operation["boundary"],
                managed=True,
            )
            return {"created": created, "modified_handles": []}
        if kind == "create_managed":
            created = self._create_managed(adapter, document, operation)
            return {"created": created, "modified_handles": []}
        if kind == "replace_managed":
            for handle in operation.get("old_handles", []):
                document.HandleToObject(handle).Delete()
            replacement = dict(operation)
            replacement["kind"] = "create_managed"
            created = self._create_managed(adapter, document, replacement)
            return {
                "created": created,
                "modified_handles": operation.get("old_handles", []),
            }
        if kind == "delete_managed":
            for handle in operation.get("handles", []):
                document.HandleToObject(handle).Delete()
            return {"created": [], "modified_handles": operation.get("handles", [])}
        raise ValueError(f"Unknown topology operation '{kind}'")

    def _create_managed(
        self, adapter: Any, document: Any, operation: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        ontology_class = operation["ontology_class"]
        semantic_id = operation["semantic_id"]
        geometry = operation["geometry"]
        label = operation.get("label", "")
        representation = operation.get("representation", "auto")
        if ontology_class == "top:Room":
            return self._create_room(
                adapter,
                document,
                semantic_id,
                label,
                geometry,
                representation=representation,
            )
        if ontology_class == "top:Wall":
            return self._create_wall(
                adapter,
                document,
                semantic_id,
                label,
                geometry,
                representation=representation,
            )
        if ontology_class == "top:Door":
            return self._create_door(
                adapter,
                document,
                semantic_id,
                label,
                geometry,
                representation=representation,
            )
        if ontology_class == "top:Window":
            return self._create_window(
                adapter,
                document,
                semantic_id,
                label,
                geometry,
                representation=representation,
            )
        raise ValueError(f"Unsupported managed class '{ontology_class}'")

    def _create_room(
        self,
        adapter: Any,
        document: Any,
        semantic_id: str,
        label: str,
        geometry: Dict[str, Any],
        representation: str = "auto",
    ) -> List[Dict[str, Any]]:
        origin = tuple(geometry["origin"])
        width = float(geometry["clear_width"])
        depth = float(geometry["clear_depth"])
        thickness = float(geometry["wall_thickness"])
        rotation = float(geometry.get("rotation_deg", 0.0))
        local_boundary = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]
        boundary = [
            self._transform(point, origin, rotation) for point in local_boundary
        ]
        room_geometry = dict(geometry)
        room_geometry["boundary"] = [[point[0], point[1]] for point in boundary]
        created = self._create_room_boundary(
            adapter,
            document,
            semantic_id,
            label,
            boundary,
            managed=True,
            geometry=room_geometry,
        )

        wall_specs = self._room_wall_specs(width, depth, thickness)
        for index, (local_start, local_end) in enumerate(wall_specs, start=1):
            wall_id = f"{semantic_id}:wall:{index}"
            wall_geometry = {
                "start": list(self._transform(local_start, origin, rotation)),
                "end": list(self._transform(local_end, origin, rotation)),
                "thickness": thickness,
                "height": float(geometry.get("wall_height", 3000.0)),
                "style": str(geometry.get("wall_style", "Standard")),
            }
            created.extend(
                self._create_wall(
                    adapter,
                    document,
                    wall_id,
                    f"{label} Wall {index}".strip(),
                    wall_geometry,
                    parent_id=semantic_id,
                    representation=representation,
                )
            )
        return created

    def _create_room_boundary(
        self,
        adapter: Any,
        document: Any,
        semantic_id: str,
        label: str,
        boundary: Sequence[Sequence[float]],
        managed: bool,
        geometry: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_layer(adapter, document, ROOM_LAYER, "green", plottable=False)
        handle = adapter.draw_polyline(
            [(point[0], point[1], 0.0) for point in boundary],
            closed=True,
            layer=ROOM_LAYER,
            color="green",
            lineweight=0,
            _skip_refresh=True,
        )
        semantic_geometry = geometry or {
            "boundary": [[float(point[0]), float(point[1])] for point in boundary]
        }
        payload = {
            "semantic_id": semantic_id,
            "ontology_class": "top:Room",
            "label": label,
            "group_id": semantic_id,
            "managed": managed,
            "geometry": semantic_geometry,
        }
        self.write_xdata(document, document.HandleToObject(handle), payload)
        return [
            {
                "semantic_id": semantic_id,
                "ontology_class": "top:Room",
                "handles": [handle],
            }
        ]

    def _create_wall(
        self,
        adapter: Any,
        document: Any,
        semantic_id: str,
        label: str,
        geometry: Dict[str, Any],
        parent_id: str | None = None,
        representation: str = "auto",
    ) -> List[Dict[str, Any]]:
        self._ensure_layer(adapter, document, WALL_LAYER, "white")
        actual_representation = self._select_representation(adapter, representation)
        object_type = "AcDbPolyline"
        if actual_representation == "native_aec":
            native = adapter.create_native_wall(
                (*geometry["start"], 0.0),
                (*geometry["end"], 0.0),
                width=float(geometry["thickness"]),
                height=float(geometry.get("height", 3000.0)),
                style=str(geometry.get("style", "Standard")),
                layer=WALL_LAYER,
            )
            handle = native["handle"]
            object_type = native["object_type"]
        else:
            polygon = self._wall_polygon(
                geometry["start"], geometry["end"], geometry["thickness"]
            )
            handle = adapter.draw_polyline(
                [(point[0], point[1], 0.0) for point in polygon],
                closed=True,
                layer=WALL_LAYER,
                color="white",
                lineweight=0,
                _skip_refresh=True,
            )
        payload = {
            "semantic_id": semantic_id,
            "ontology_class": "top:Wall",
            "label": label,
            "group_id": parent_id or semantic_id,
            "managed": True,
            "geometry": geometry,
            "representation": actual_representation,
        }
        if parent_id:
            payload["parent_id"] = parent_id
        self.write_xdata(document, document.HandleToObject(handle), payload)
        return [
            {
                "semantic_id": semantic_id,
                "ontology_class": "top:Wall",
                "handles": [handle],
                "representation": actual_representation,
                "object_type": object_type,
            }
        ]

    def _create_door(
        self,
        adapter: Any,
        document: Any,
        semantic_id: str,
        label: str,
        geometry: Dict[str, Any],
        representation: str = "auto",
    ) -> List[Dict[str, Any]]:
        self._ensure_layer(adapter, document, DOOR_LAYER, "yellow")
        wall = self._find_host_wall(document, geometry["host_wall_id"])
        start, direction, normal, length, _thickness = self._wall_frame(
            wall["geometry"]
        )
        offset = float(geometry["offset"])
        width = float(geometry["width"])
        if offset + width > length + 1e-6:
            raise ValueError("door extends beyond its host wall")
        actual_representation = self._select_opening_representation(
            adapter, representation, wall
        )
        if actual_representation == "native_aec":
            native = adapter.create_native_opening(
                "door",
                host_handle=wall["_entity_handle"],
                offset=offset,
                width=width,
                height=float(geometry.get("height", 2100.0)),
                style=str(geometry.get("style", "Standard")),
                layer=DOOR_LAYER,
                hinge=str(geometry["hinge"]),
                swing=str(geometry["swing"]),
                swing_angle=float(geometry.get("swing_angle_deg", 90.0)),
            )
            handles = [native["handle"]]
            object_type = native["object_type"]
        else:
            handles = []
            object_type = "AcDbLine+AcDbArc"
        hinge_at_start = geometry["hinge"] == "left"
        hinge_offset = offset if hinge_at_start else offset + width
        hinge = self._add(start, self._scale(direction, hinge_offset))
        closed_direction = direction if hinge_at_start else self._scale(direction, -1.0)
        swing_sign = 1.0 if geometry["swing"] == "in" else -1.0
        if not hinge_at_start:
            swing_sign *= -1.0
        open_direction = self._scale(normal, swing_sign)
        leaf_end = self._add(hinge, self._scale(open_direction, width))
        if actual_representation == "standard":
            leaf_handle = adapter.draw_line(
                (*hinge, 0.0),
                (*leaf_end, 0.0),
                layer=DOOR_LAYER,
                color="yellow",
                lineweight=0,
                _skip_refresh=True,
            )
            start_angle, end_angle = self._door_arc_angles(
                closed_direction, open_direction
            )
            arc_handle = adapter.draw_arc(
                (*hinge, 0.0),
                width,
                start_angle,
                end_angle,
                layer=DOOR_LAYER,
                color="yellow",
                lineweight=0,
                _skip_refresh=True,
            )
            handles = [leaf_handle, arc_handle]
        payload = {
            "semantic_id": semantic_id,
            "ontology_class": "top:Door",
            "label": label,
            "group_id": semantic_id,
            "managed": True,
            "host_wall_id": geometry["host_wall_id"],
            "geometry": geometry,
            "representation": actual_representation,
        }
        for handle in handles:
            self.write_xdata(document, document.HandleToObject(handle), payload)
        return [
            {
                "semantic_id": semantic_id,
                "ontology_class": "top:Door",
                "handles": handles,
                "representation": actual_representation,
                "object_type": object_type,
            }
        ]

    def _create_window(
        self,
        adapter: Any,
        document: Any,
        semantic_id: str,
        label: str,
        geometry: Dict[str, Any],
        representation: str = "auto",
    ) -> List[Dict[str, Any]]:
        self._ensure_layer(adapter, document, WINDOW_LAYER, "cyan")
        wall = self._find_host_wall(document, geometry["host_wall_id"])
        start, direction, normal, length, thickness = self._wall_frame(wall["geometry"])
        offset = float(geometry["offset"])
        width = float(geometry["width"])
        if offset + width > length + 1e-6:
            raise ValueError("window extends beyond its host wall")
        actual_representation = self._select_opening_representation(
            adapter, representation, wall
        )
        if actual_representation == "native_aec":
            native = adapter.create_native_opening(
                "window",
                host_handle=wall["_entity_handle"],
                offset=offset,
                width=width,
                height=float(geometry.get("height", 1200.0)),
                style=str(geometry.get("style", "Standard")),
                layer=WINDOW_LAYER,
                sill_height=float(geometry.get("sill_height", 900.0)),
            )
            handles = [native["handle"]]
            object_type = native["object_type"]
        else:
            handles = []
            object_type = "AcDbLine"
        start_point = self._add(start, self._scale(direction, offset))
        end_point = self._add(start_point, self._scale(direction, width))
        if actual_representation == "standard":
            for normal_offset in (-thickness / 6.0, thickness / 6.0):
                delta = self._scale(normal, normal_offset)
                line_start = self._add(start_point, delta)
                line_end = self._add(end_point, delta)
                handles.append(
                    adapter.draw_line(
                        (*line_start, 0.0),
                        (*line_end, 0.0),
                        layer=WINDOW_LAYER,
                        color="cyan",
                        lineweight=0,
                        _skip_refresh=True,
                    )
                )
        payload = {
            "semantic_id": semantic_id,
            "ontology_class": "top:Window",
            "label": label,
            "group_id": semantic_id,
            "managed": True,
            "host_wall_id": geometry["host_wall_id"],
            "geometry": geometry,
            "representation": actual_representation,
        }
        for handle in handles:
            self.write_xdata(document, document.HandleToObject(handle), payload)
        return [
            {
                "semantic_id": semantic_id,
                "ontology_class": "top:Window",
                "handles": handles,
                "representation": actual_representation,
                "object_type": object_type,
            }
        ]

    def _find_host_wall(self, document: Any, semantic_id: str) -> Dict[str, Any]:
        for entity in self._iter_entities(document, "all"):
            semantic = self.read_xdata(entity)
            if (
                semantic.get("semantic_id") == semantic_id
                and semantic.get("ontology_class") == "top:Wall"
            ):
                geometry = semantic.get("geometry")
                if isinstance(geometry, dict):
                    result = dict(semantic)
                    result["_entity_handle"] = str(
                        self._safe_get(entity, "Handle", "")
                    )
                    result["_object_type"] = str(
                        self._safe_get(entity, "ObjectName", "")
                    )
                    return result
        raise ValueError(f"Host wall '{semantic_id}' was not found")

    @staticmethod
    def _select_representation(adapter: Any, requested: str) -> str:
        if requested not in {"auto", "native_aec", "standard"}:
            raise ValueError(
                "representation must be auto, native_aec, or standard"
            )
        if requested == "standard":
            return "standard"
        capability_method = getattr(adapter, "get_architecture_capabilities", None)
        capabilities = (
            capability_method(include_styles=False)
            if callable(capability_method)
            else {}
        )
        native_available = bool(
            isinstance(capabilities, dict) and capabilities.get("native_aec")
        )
        if requested == "native_aec" and not native_available:
            raise ValueError(
                "native_aec representation requires AutoCAD Architecture"
            )
        return "native_aec" if native_available else "standard"

    @classmethod
    def _select_opening_representation(
        cls, adapter: Any, requested: str, wall: Dict[str, Any]
    ) -> str:
        selected = cls._select_representation(adapter, requested)
        host_is_native = (
            wall.get("representation") == "native_aec"
            and str(wall.get("_object_type", "")).upper() == "AECDBWALL"
        )
        if selected == "native_aec" and not host_is_native:
            if requested == "native_aec":
                raise ValueError(
                    "native_aec openings require a native AecDbWall host"
                )
            return "standard"
        return selected

    @staticmethod
    def _wall_frame(
        geometry: Dict[str, Any],
    ) -> Tuple[
        Tuple[float, float], Tuple[float, float], Tuple[float, float], float, float
    ]:
        start = (float(geometry["start"][0]), float(geometry["start"][1]))
        end = (float(geometry["end"][0]), float(geometry["end"][1]))
        vector = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*vector)
        if length <= 0:
            raise ValueError("Host wall has zero length")
        direction = (vector[0] / length, vector[1] / length)
        normal = (-direction[1], direction[0])
        return start, direction, normal, length, float(geometry["thickness"])

    @staticmethod
    def _wall_polygon(
        start: Sequence[float], end: Sequence[float], thickness: float
    ) -> List[Tuple[float, float]]:
        start_point = (float(start[0]), float(start[1]))
        end_point = (float(end[0]), float(end[1]))
        vector = (end_point[0] - start_point[0], end_point[1] - start_point[1])
        length = math.hypot(*vector)
        if length <= 0:
            raise ValueError("Wall start and end must differ")
        normal = (
            -vector[1] / length * thickness / 2.0,
            vector[0] / length * thickness / 2.0,
        )
        return [
            (start_point[0] + normal[0], start_point[1] + normal[1]),
            (end_point[0] + normal[0], end_point[1] + normal[1]),
            (end_point[0] - normal[0], end_point[1] - normal[1]),
            (start_point[0] - normal[0], start_point[1] - normal[1]),
        ]

    @staticmethod
    def _room_wall_specs(
        width: float, depth: float, thickness: float
    ) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Return outward wall centerlines around a clear-interior rectangle."""
        half = thickness / 2.0
        return [
            ((-thickness, -half), (width + thickness, -half)),
            ((width + half, -thickness), (width + half, depth + thickness)),
            ((width + thickness, depth + half), (-thickness, depth + half)),
            ((-half, depth + thickness), (-half, -thickness)),
        ]

    @staticmethod
    def _door_arc_angles(
        closed_direction: Sequence[float], open_direction: Sequence[float]
    ) -> Tuple[float, float]:
        """Return AutoCAD CCW angles for the visible quarter-circle swing."""
        closed_angle = (
            math.degrees(math.atan2(closed_direction[1], closed_direction[0])) % 360
        )
        open_angle = (
            math.degrees(math.atan2(open_direction[1], open_direction[0])) % 360
        )
        cross = (
            closed_direction[0] * open_direction[1]
            - closed_direction[1] * open_direction[0]
        )
        # AutoCAD always draws AddArc counter-clockwise. Swapping the endpoints
        # for a clockwise swing keeps the visible arc to a 90-degree sweep.
        if cross >= 0:
            return closed_angle, open_angle
        return open_angle, closed_angle

    @staticmethod
    def _transform(
        point: Sequence[float], origin: Sequence[float], rotation_deg: float
    ) -> Tuple[float, float]:
        angle = math.radians(rotation_deg)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        return (
            float(origin[0]) + point[0] * cos_a - point[1] * sin_a,
            float(origin[1]) + point[0] * sin_a + point[1] * cos_a,
        )

    @staticmethod
    def _add(left: Sequence[float], right: Sequence[float]) -> Tuple[float, float]:
        return (left[0] + right[0], left[1] + right[1])

    @staticmethod
    def _scale(vector: Sequence[float], factor: float) -> Tuple[float, float]:
        return (vector[0] * factor, vector[1] * factor)

    @staticmethod
    def _semantic_payload(operation: Dict[str, Any], managed: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "semantic_id": operation["semantic_id"],
            "ontology_class": operation["ontology_class"],
            "label": operation.get("label", ""),
            "group_id": operation.get("semantic_id"),
            "managed": managed,
        }
        if isinstance(operation.get("geometry"), dict):
            payload["geometry"] = operation["geometry"]
        if operation.get("representation") in {"native_aec", "standard"}:
            payload["representation"] = operation["representation"]
        return payload

    @staticmethod
    def _ensure_layer(
        adapter: Any,
        document: Any,
        name: str,
        color: str,
        plottable: bool = True,
    ) -> None:
        if name not in adapter.list_layers():
            if not adapter.create_layer(name, color=color, lineweight=0):
                raise RuntimeError(f"Failed to create layer '{name}'")
        try:
            document.Layers.Item(name).Plottable = plottable
        except Exception:
            logger.debug("Layer '%s' does not expose Plottable", name)

    def write_sidecars(
        self, snapshot: DrawingSnapshot, graph: Dict[str, Any], turtle: str
    ) -> Dict[str, str]:
        """Atomically write JSON-LD and Turtle to the configured safe directory."""
        output_dir = Path(get_config().output.directory).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(snapshot.drawing_name).stem or "drawing"
        json_path = output_dir / f"{stem}.topology.jsonld"
        ttl_path = output_dir / f"{stem}.topology.ttl"
        json_temp: Path | None = None
        ttl_temp: Path | None = None
        try:
            json_temp = self._write_temp(
                json_path, json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
            )
            ttl_temp = self._write_temp(ttl_path, turtle)
            json_temp.replace(json_path)
            ttl_temp.replace(ttl_path)
        finally:
            if json_temp is not None:
                json_temp.unlink(missing_ok=True)
            if ttl_temp is not None:
                ttl_temp.unlink(missing_ok=True)
        return {"jsonld": str(json_path), "ttl": str(ttl_path)}

    @staticmethod
    def _write_temp(path: Path, content: str) -> Path:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        return temp_path

    @staticmethod
    def _safe_get(obj: Any, name: str, default: Any) -> Any:
        try:
            return getattr(obj, name)
        except Exception:
            return default

    @staticmethod
    def _safe_call(call: Any, default: Any) -> Any:
        try:
            return call()
        except Exception:
            return default
