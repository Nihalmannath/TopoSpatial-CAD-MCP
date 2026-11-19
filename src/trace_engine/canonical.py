"""Canonical geometry: one validated millimetre, y-up representation.

Every recogniser (classic, neural, Raster2Seq, the geometry kernel) produces a
``TraceResult`` in its own units. ``normalize`` applies the single coordinate
transform into the canonical frame, so cleanup, room recovery, exposure and
opening hosting all work on the same numbers:

    perception output (px or mm, y-down or y-up, detections + confidence)
          |  normalize()  - the only place scale / origin / y-flip are applied
          v
    CanonicalPlan (mm, y-up, stable node / edge / opening ids, label seeds)

Conventions in the canonical frame

* Lengths (thickness, opening width, heights) are millimetres.
* An opening stores its physical centre ``position`` and the direction of the
  host edge it was reported on (``edge_nodes[0] -> edge_nodes[1]``).
  ``hinge`` / ``swing`` are relative to that direction, with "left" meaning the
  left-hand side in y-up space (``vision._detect_swing`` already reports it
  that way for y-down images, so no flip is applied here).
* Only uniform scale, translation and Y inversion are supported. Anisotropic
  scaling changes angles and is rejected rather than applied silently.

``normalize`` never modifies its input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Diagnostic, TraceOpening, TraceResult, upgrade_trace

Point = Tuple[float, float]


@dataclass(frozen=True)
class Frame:
    """Source -> canonical transform: uniform scale, translation, optional y flip."""

    mm_per_unit: float = 1.0
    y_down: bool = False
    origin: Point = (0.0, 0.0)
    source_units: str = "mm"
    calibrated: bool = True
    scale_source: Optional[str] = None

    def __post_init__(self) -> None:
        if not (math.isfinite(self.mm_per_unit) and self.mm_per_unit > 0):
            raise ValueError(f"mm_per_unit must be a positive finite number, got {self.mm_per_unit!r}")

    @classmethod
    def of(cls, trace: TraceResult) -> "Frame":
        scale = trace.mm_per_unit if trace.units == "px" else 1.0
        meta = trace.frame
        return cls(
            mm_per_unit=scale,
            y_down=trace.y_down,
            origin=tuple(meta.origin) if meta else (0.0, 0.0),
            source_units=trace.units,
            calibrated=meta.calibrated if meta else True,
            scale_source=meta.scale_source if meta else None,
        )

    @property
    def _sy(self) -> float:
        return -1.0 if self.y_down else 1.0

    def to_mm(self, p: Sequence[float]) -> Point:
        return ((float(p[0]) - self.origin[0]) * self.mm_per_unit,
                self._sy * (float(p[1]) - self.origin[1]) * self.mm_per_unit)

    def from_mm(self, p: Sequence[float]) -> Point:
        return (float(p[0]) / self.mm_per_unit + self.origin[0],
                self._sy * float(p[1]) / self.mm_per_unit + self.origin[1])

    def length_to_mm(self, value: float) -> float:
        return float(value) * self.mm_per_unit

    def length_from_mm(self, value: float) -> float:
        return float(value) / self.mm_per_unit

    def describe(self) -> Dict[str, object]:
        return {
            "source_units": self.source_units,
            "mm_per_unit": round(self.mm_per_unit, 6),
            "y_down": self.y_down,
            "origin": list(self.origin),
            "calibrated": self.calibrated,
            "scale_source": self.scale_source,
        }


@dataclass
class CEdge:
    id: str
    a: str
    b: str
    thickness: float
    type: str = "internal"
    thickness_source: str = "measured"
    confidence: Optional[float] = None
    provenance: Optional[str] = None


@dataclass
class COpening:
    id: str
    kind: str  # "door" | "window"
    position: Point
    width: float
    host_edge: Optional[str] = None
    host_direction: Optional[Point] = None  # unit vector edge_nodes[0] -> [1]
    door_type: str = "swing"
    hinge: str = "start"
    swing: str = "left"
    height: Optional[float] = None
    sill_height: Optional[float] = None
    confidence: Optional[float] = None
    provenance: Optional[str] = None


@dataclass
class CanonicalPlan:
    nodes: Dict[str, Point]
    edges: List[CEdge]
    openings: List[COpening]
    seeds: List[Tuple[str, Point]]
    frame: Frame
    diagnostics: List[Diagnostic] = field(default_factory=list)
    source: Optional[str] = None

    def edge(self, edge_id: str) -> Optional[CEdge]:
        for e in self.edges:
            if e.id == edge_id:
                return e
        return None


def _finite(p: Sequence[float]) -> bool:
    return len(p) >= 2 and math.isfinite(p[0]) and math.isfinite(p[1])


def _opening(op: TraceOpening, kind: str, trace: TraceResult, frame: Frame,
             raw_nodes: Dict[str, Point], edge_by_nodes: Dict[frozenset, str],
             diags: List[Diagnostic]) -> Optional[COpening]:
    a_id, b_id = op.edge_nodes
    direction: Optional[Point] = None
    host = edge_by_nodes.get(frozenset((a_id, b_id)))
    if a_id in raw_nodes and b_id in raw_nodes:
        pa, pb = frame.to_mm(raw_nodes[a_id]), frame.to_mm(raw_nodes[b_id])
        length = math.dist(pa, pb)
        if length > 0:
            direction = ((pb[0] - pa[0]) / length, (pb[1] - pa[1]) / length)
    position: Optional[Point] = None
    if op.distance_from_lower_node is not None and direction is not None:
        d = frame.length_to_mm(op.distance_from_lower_node)
        pa = frame.to_mm(raw_nodes[a_id])
        position = (pa[0] + direction[0] * d, pa[1] + direction[1] * d)
    elif op.center is not None and _finite(op.center):
        position = frame.to_mm(op.center)
    if position is None:
        diags.append(Diagnostic(code="opening_unpositioned", entity=op.id,
                                message=f"{kind} has neither a usable centre nor a host offset"))
        return None
    if host is None:
        diags.append(Diagnostic(severity="info", code="opening_host_unknown", entity=op.id,
                                message=f"{kind} edge_nodes {a_id}-{b_id} name no edge; hosting by position",
                                location=position))
    return COpening(
        id=op.id or f"{kind}?", kind=kind, position=position,
        width=frame.length_to_mm(op.length), host_edge=host, host_direction=direction,
        door_type=op.door_type, hinge=op.hinge, swing=op.swing,
        height=frame.length_to_mm(op.height) if op.height is not None else None,
        sill_height=frame.length_to_mm(op.sill_height) if op.sill_height is not None else None,
        confidence=op.confidence, provenance=op.provenance,
    )


def normalize(trace: "TraceResult | dict") -> CanonicalPlan:
    """Validate ``trace`` and express it in the canonical mm, y-up frame."""
    model = upgrade_trace(trace)
    frame = Frame.of(model)
    diags: List[Diagnostic] = list(model.diagnostics)
    raw_nodes: Dict[str, Point] = {}
    nodes: Dict[str, Point] = {}
    for node in model.walls_graph.nodes:
        if not _finite((node.x, node.y)):
            diags.append(Diagnostic(code="node_not_finite", entity=node.id,
                                    message="node coordinates are not finite; dropped"))
            continue
        raw_nodes[node.id] = (node.x, node.y)
        nodes[node.id] = frame.to_mm((node.x, node.y))
    edges: List[CEdge] = []
    edge_by_nodes: Dict[frozenset, str] = {}
    for e in model.walls_graph.edges:
        if e.source not in nodes or e.target not in nodes:
            diags.append(Diagnostic(code="edge_dangling_reference", entity=e.id,
                                    message="edge references a dropped node"))
            continue
        thickness = frame.length_to_mm(e.thickness)
        if not (math.isfinite(thickness) and thickness > 0):
            diags.append(Diagnostic(code="edge_bad_thickness", entity=e.id,
                                    message="thickness is not positive; marked as default"))
            thickness, source = 150.0, "default"
        else:
            source = e.thickness_source
        if math.dist(nodes[e.source], nodes[e.target]) <= 1e-6:
            diags.append(Diagnostic(code="edge_zero_length", entity=e.id, message="zero-length edge dropped"))
            continue
        edges.append(CEdge(e.id, e.source, e.target, thickness, e.type, source, e.confidence, e.provenance))
        edge_by_nodes.setdefault(frozenset((e.source, e.target)), e.id)
    openings: List[COpening] = []
    for kind, items in (("door", model.doors_list), ("window", model.windows_list)):
        for op in items:
            c = _opening(op, kind, model, frame, raw_nodes, edge_by_nodes, diags)
            if c is not None:
                openings.append(c)
    seeds = [(z.name, frame.to_mm(z.seed_point)) for z in model.zones_list if _finite(z.seed_point)]
    return CanonicalPlan(nodes, edges, openings, seeds, frame, diags, model.source)


def to_trace_result(plan: CanonicalPlan, *, source: Optional[str] = None) -> TraceResult:
    """Write a canonical plan back as a millimetre, y-up v2 ``TraceResult``."""
    node_ids = set(plan.nodes)
    data = {
        "walls_graph": {
            "nodes": [{"id": n, "x": p[0], "y": p[1]} for n, p in plan.nodes.items()],
            "edges": [
                {"id": e.id, "source": e.a, "target": e.b, "type": e.type, "thickness": e.thickness,
                 "thickness_source": e.thickness_source, "confidence": e.confidence, "provenance": e.provenance}
                for e in plan.edges if e.a in node_ids and e.b in node_ids
            ],
        },
        "doors_list": [], "windows_list": [],
        "zones_list": [{"name": n, "seedPoint": list(p)} for n, p in plan.seeds],
        "units": "mm", "mm_per_unit": 1.0, "y_down": False,
        "source": source or plan.source, "schema_version": 2,
        "frame": {"origin": [0.0, 0.0], "calibrated": plan.frame.calibrated,
                  "scale_source": plan.frame.scale_source},
        "diagnostics": [d.model_dump() for d in plan.diagnostics],
    }
    by_id = {e.id: e for e in plan.edges}
    for op in plan.openings:
        item = {"id": op.id, "length": op.width, "center": list(op.position),
                "confidence": op.confidence, "provenance": op.provenance}
        host = by_id.get(op.host_edge) if op.host_edge else None
        if host is not None:
            a, b = plan.nodes[host.a], plan.nodes[host.b]
            item["edge_nodes"] = [host.a, host.b]
            u = op.host_direction
            reversed_ = u is not None and ((b[0] - a[0]) * u[0] + (b[1] - a[1]) * u[1]) < 0
            item["distance_from_lower_node"] = max(0.0, (op.position[0] - a[0]) * (b[0] - a[0]) / max(math.dist(a, b), 1e-9)
                                                   + (op.position[1] - a[1]) * (b[1] - a[1]) / max(math.dist(a, b), 1e-9))
        else:
            item["edge_nodes"] = ["?", "?"]
            reversed_ = False
        if op.kind == "door":
            hinge, swing = op.hinge, op.swing
            if reversed_:
                hinge = "end" if hinge == "start" else "start"
                swing = "right" if swing == "left" else "left"
            item.update(door_type=op.door_type, hinge=hinge, swing=swing)
            if op.height is not None:
                item["height"] = op.height
            data["doors_list"].append(item)
        else:
            if op.height is not None:
                item["height"] = op.height
            if op.sill_height is not None:
                item["sill_height"] = op.sill_height
            data["windows_list"].append(item)
    return TraceResult.model_validate(data)
