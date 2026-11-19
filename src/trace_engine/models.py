"""Typed trace format: the hand-off between plan recognition and CAD planning.

A trace describes a floor plan the way a recogniser sees it, not the way CAD
stores it:

* ``walls_graph`` - wall *centerlines* as a node/edge network with thickness,
* ``doors_list`` / ``windows_list`` - openings hosted on one graph edge,
  positioned by their distance from the edge's first node,
* ``zones_list`` - room labels with a seed point somewhere inside the room.

Rooms are never drawn from pixels; they are recovered later as the closed
cycles of the cleaned wall network and named by the seed points they contain.

Coordinates may be in pixels (``units="px"``) together with
``mm_per_unit``, or already in millimetres (``units="mm"``). Image traces use
y-down pixel coordinates; set ``y_down=True`` so the compiler flips them into
CAD's y-up space.

Schema version 2 (this file) adds optional, backward-compatible fields; every
version-1 payload still validates unchanged:

* stable ``id`` on edges and openings (``upgrade_trace`` fills missing ones),
* ``confidence`` / ``provenance`` on edges and openings,
* ``thickness_source`` on edges: ``measured`` (from the image), ``user_supplied``
  or ``default`` (a nominal value the kernel may replace by exposure),
* ``frame`` metadata on the result (origin, calibration source),
* ``diagnostics``: structured notes on rejected or ambiguous entities.

Opening position: ``distance_from_lower_node`` is, as before, the distance from
``edge_nodes[0]`` to the opening *centre* (``center_offset``). The CAD
``offset`` is the distance to the opening *start* (``start_offset`` =
centre offset - width / 2). Both helpers are on ``TraceOpening``.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

Point = Tuple[float, float]

SCHEMA_VERSION = 2
ThicknessSource = Literal["measured", "user_supplied", "default"]


class TraceNode(BaseModel):
    """A wall junction or wall end."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    x: float
    y: float


class TraceEdge(BaseModel):
    """A straight wall centerline between two nodes."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    type: Literal["internal", "external", "separator"] = Field(
        "internal",
        description="'separator' bounds rooms (open-plan split, drawn partition line) "
        "but is not built as a wall",
    )
    thickness: float = Field(gt=0, description="Wall thickness in trace units")
    id: Optional[str] = Field(default=None, min_length=1, description="Stable edge id")
    thickness_source: ThicknessSource = "measured"
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    provenance: Optional[str] = None


class WallsGraph(BaseModel):
    """Wall centerline network."""

    model_config = ConfigDict(extra="forbid")

    nodes: List[TraceNode] = Field(default_factory=list)
    edges: List[TraceEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _edges_reference_nodes(self) -> "WallsGraph":
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("walls_graph node ids must be unique")
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                raise ValueError(
                    f"edge {edge.source}->{edge.target} references an unknown node"
                )
            if edge.source == edge.target:
                raise ValueError("a wall edge must join two different nodes")
        edge_ids = [edge.id for edge in self.edges if edge.id is not None]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("walls_graph edge ids must be unique")
        return self


class TraceOpening(BaseModel):
    """A door or window hosted on one wall edge.

    ``edge_nodes`` names the host edge (in either order). Position is given by
    ``distance_from_lower_node`` - the distance from ``edge_nodes[0]`` to the
    opening's *centre* - or, failing that, by ``center``.
    """

    model_config = ConfigDict(extra="forbid")

    edge_nodes: Tuple[str, str]
    length: float = Field(gt=0, description="Opening width in trace units")
    distance_from_lower_node: Optional[float] = Field(default=None, ge=0)
    center: Optional[Point] = None
    # Doors only. hinge: which end of the opening (seen from edge_nodes[0]
    # towards edge_nodes[1]) carries the hinge. swing: towards the left-hand
    # side of that direction ("in") or the right-hand side ("out").
    door_type: Literal["swing", "sliding", "double", "opening"] = "swing"
    hinge: Literal["start", "end"] = "start"
    swing: Literal["left", "right"] = "left"
    height: Optional[float] = Field(default=None, gt=0)
    sill_height: Optional[float] = Field(default=None, ge=0)
    id: Optional[str] = Field(default=None, min_length=1, description="Stable opening id")
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    provenance: Optional[str] = None

    @model_validator(mode="after")
    def _has_position(self) -> "TraceOpening":
        if self.distance_from_lower_node is None and self.center is None:
            raise ValueError("an opening needs distance_from_lower_node or center")
        return self

    @property
    def center_offset(self) -> Optional[float]:
        """Distance from ``edge_nodes[0]`` to the opening centre (trace units)."""
        return self.distance_from_lower_node

    @property
    def start_offset(self) -> Optional[float]:
        """Distance from ``edge_nodes[0]`` to the opening start (trace units)."""
        if self.distance_from_lower_node is None:
            return None
        return self.distance_from_lower_node - self.length / 2.0


class TraceZone(BaseModel):
    """A room name and a point inside that room (e.g. its OCR label)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=128)
    seed_point: Point = Field(alias="seedPoint")


class FurnitureItem(BaseModel):
    """An oriented furniture box (only kept when furniture output is enabled)."""

    model_config = ConfigDict(extra="forbid")

    cls: str = Field(min_length=1, description="Furniture class, e.g. bed_double")
    center: Point
    size: Tuple[float, float]
    rotation_deg: float = 0.0
    room_id: Optional[str] = None


class Diagnostic(BaseModel):
    """A structured note about a rejected, repaired or ambiguous entity."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "error"] = "warning"
    code: str = Field(min_length=1, description="Stable machine-readable code")
    message: str = ""
    entity: Optional[str] = Field(default=None, description="Id of the entity concerned")
    location: Optional[Point] = None


class CoordinateFrame(BaseModel):
    """How trace coordinates relate to the canonical millimetre, y-up frame.

    canonical = (x * s - origin_x * s, sy * (y - origin_y) * s) with
    s = mm_per_unit and sy = -1 for y-down traces. ``calibrated`` is False when
    the scale is a nominal guess (e.g. assumed wall thickness).
    """

    model_config = ConfigDict(extra="forbid")

    origin: Point = (0.0, 0.0)
    calibrated: bool = True
    scale_source: Optional[str] = None


class TraceResult(BaseModel):
    """Complete recogniser output for one floor."""

    model_config = ConfigDict(extra="forbid")

    walls_graph: WallsGraph
    doors_list: List[TraceOpening] = Field(default_factory=list)
    windows_list: List[TraceOpening] = Field(default_factory=list)
    zones_list: List[TraceZone] = Field(default_factory=list)
    units: Literal["px", "mm"] = "mm"
    mm_per_unit: float = Field(default=1.0, gt=0)
    y_down: bool = False
    source: Optional[str] = Field(default=None, description="Provenance note")
    schema_version: int = Field(default=1, ge=1, le=SCHEMA_VERSION)
    frame: Optional[CoordinateFrame] = None
    diagnostics: List[Diagnostic] = Field(default_factory=list)
    furniture_list: List[FurnitureItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _scale_is_explicit_for_pixels(self) -> "TraceResult":
        if self.units == "px" and self.mm_per_unit == 1.0:
            raise ValueError(
                "pixel traces need mm_per_unit (millimetres per pixel); "
                "calibrate from a known dimension first"
            )
        return self


def upgrade_trace(trace: "TraceResult | dict") -> TraceResult:
    """Return a schema-v2 copy of ``trace`` with stable ids on every edge and
    opening. Existing ids are kept; missing ones become ``e<i>`` / ``d<i>`` /
    ``w<i>`` (index in the input list, so they are deterministic). The input
    is never modified."""
    model = trace if isinstance(trace, TraceResult) else TraceResult.model_validate(trace)
    data = model.model_dump(by_alias=True)
    used = {e["id"] for e in data["walls_graph"]["edges"] if e.get("id")}
    for i, edge in enumerate(data["walls_graph"]["edges"]):
        if not edge.get("id"):
            candidate, k = f"e{i}", 0
            while candidate in used:
                k += 1
                candidate = f"e{i}_{k}"
            edge["id"] = candidate
            used.add(candidate)
    for key, prefix in (("doors_list", "d"), ("windows_list", "w")):
        taken = {o["id"] for o in data[key] if o.get("id")}
        for i, opening in enumerate(data[key]):
            if not opening.get("id"):
                candidate, k = f"{prefix}{i}", 0
                while candidate in taken:
                    k += 1
                    candidate = f"{prefix}{i}_{k}"
                opening["id"] = candidate
                taken.add(candidate)
    data["schema_version"] = SCHEMA_VERSION
    return TraceResult.model_validate(data)
