"""Compile a trace into one TopoSpatial ``manage_design`` change batch.

walls  -> explicit ``top:Wall`` creates with stable ids
rooms  -> ``top:Room`` creates with clear-interior ``boundary`` and OCR names
doors / windows -> ``top:Door`` / ``top:Window`` hosted on the cleaned wall by
                   offset along its centerline (re-hosted geometrically, so
                   cleanup merges/splits never strand an opening)

Two geometry pipelines (``geometry_pipeline``):

* ``legacy``    - ``graph.clean`` + ``rooms.recover_rooms`` + geometric re-hosting
                  (the behaviour ``trace_plan`` has shipped with);
* ``canonical`` - ``regularize.regularize_trace``: one mm frame, lineage-based
                  opening hosting, bounded gap repair, exposure-resolved
                  thickness, structured diagnostics. ``compile_trace`` also
                  accepts an already regularized plan, so nothing is cleaned
                  twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from .graph import WallNetwork, clean, point_to_mm, to_mm
from .models import TraceOpening, TraceResult
from .rooms import Room, recover_rooms

if TYPE_CHECKING:  # pragma: no cover
    from .regularize import RegularizeConfig, RegularizedPlan

Point = Tuple[float, float]
URN = "urn:topospatial:"


@dataclass
class CompiledTrace:
    changes: List[Dict[str, Any]]
    network: WallNetwork
    rooms: List[Room]
    warnings: List[str] = field(default_factory=list)
    offset: Point = (0.0, 0.0)
    pipeline: str = "legacy"
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    regularized: Any = None  # RegularizedPlan for the canonical pipeline
    quality: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        for change in self.changes:
            kind = change["@type"].split(":")[1].lower()
            kinds[kind] = kinds.get(kind, 0) + 1
        xs = [p[0] for p in self.network.nodes.values()] or [0.0]
        ys = [p[1] for p in self.network.nodes.values()] or [0.0]
        return {
            "counts": kinds,
            "footprint_mm": [round(max(xs) - min(xs)), round(max(ys) - min(ys))],
            "rooms": [
                {
                    "name": room.label,
                    "clear_area_m2": round(room.area_mm2 / 1e6, 2),
                }
                for room in self.rooms
            ],
            "warnings": self.warnings,
            "geometry_pipeline": self.pipeline,
            "diagnostic_counts": _count_codes(self.diagnostics),
        }


def _count_codes(diagnostics: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for d in diagnostics:
        out[d["code"]] = out.get(d["code"], 0) + 1
    return out


def _host_opening(
    opening: TraceOpening,
    trace: TraceResult,
    raw_nodes: Dict[str, Point],
    network: WallNetwork,
) -> Tuple[Optional[int], float, bool, str]:
    """Return (wall index, offset_mm of opening start, reversed?, warning)."""
    scale = trace.mm_per_unit if trace.units == "px" else 1.0
    width = opening.length * scale
    a_id, b_id = opening.edge_nodes
    direction: Optional[Point] = None
    if a_id in raw_nodes and b_id in raw_nodes:
        a, b = raw_nodes[a_id], raw_nodes[b_id]
        length = math.dist(a, b)
        if length > 0:
            direction = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
    if opening.distance_from_lower_node is not None and direction is not None:
        d = opening.distance_from_lower_node * scale
        a = raw_nodes[a_id]
        center = (a[0] + direction[0] * d, a[1] + direction[1] * d)
    elif opening.center is not None:
        center = point_to_mm(opening.center, trace)
    else:
        return None, 0.0, False, "opening has no usable position"

    best = None
    for index, wall in enumerate(network.walls):
        if wall.type == "separator":
            continue
        p, q = network.nodes[wall.a], network.nodes[wall.b]
        length = math.dist(p, q)
        if length <= 0:
            continue
        u = ((q[0] - p[0]) / length, (q[1] - p[1]) / length)
        if direction is not None and abs(u[0] * direction[1] - u[1] * direction[0]) > math.sin(math.radians(10)):
            continue
        s = (center[0] - p[0]) * u[0] + (center[1] - p[1]) * u[1]
        perpendicular = abs((center[0] - p[0]) * u[1] - (center[1] - p[1]) * u[0])
        if s < -width / 2 or s > length + width / 2:
            continue
        if perpendicular > max(wall.thickness, 50.0):
            continue
        reversed_ = direction is not None and (u[0] * direction[0] + u[1] * direction[1]) < 0
        score = perpendicular
        if best is None or score < best[0]:
            best = (score, index, s, length, reversed_)
    if best is None:
        return None, 0.0, False, f"no wall found under opening at ({center[0]:.0f}, {center[1]:.0f})"
    _, index, s, length, reversed_ = best
    start = s - width / 2
    warning = ""
    if width > length:
        return None, 0.0, False, f"opening {width:.0f} mm wider than its {length:.0f} mm wall"
    if start < 0 or start + width > length:
        warning = f"opening clamped onto its {length:.0f} mm wall"
        start = min(max(start, 0.0), length - width)
    return index, start, reversed_, warning


def compile_trace(
    trace: "Union[TraceResult, RegularizedPlan]",
    *,
    representation: str = "auto",
    wall_height_mm: float = 3000.0,
    insertion_point: Point = (0.0, 0.0),
    id_prefix: str = "trace",
    door_height_mm: float = 2100.0,
    window_height_mm: float = 1200.0,
    sill_height_mm: float = 900.0,
    geometry_pipeline: str = "legacy",
    regularize_config: "Optional[RegularizeConfig]" = None,
) -> CompiledTrace:
    """Clean the trace and compile it into ``manage_design`` changes.

    Pass a ``RegularizedPlan`` (or ``geometry_pipeline="canonical"``) to use
    the canonical pipeline; the default ``legacy`` path is unchanged."""
    options = dict(representation=representation, wall_height_mm=wall_height_mm,
                   insertion_point=insertion_point, id_prefix=id_prefix, door_height_mm=door_height_mm,
                   window_height_mm=window_height_mm, sill_height_mm=sill_height_mm)
    if not isinstance(trace, TraceResult):
        from .regularize import RegularizedPlan

        if isinstance(trace, RegularizedPlan):
            return compile_regularized(trace, **options)
        trace = TraceResult.model_validate(trace)
    if geometry_pipeline == "canonical":
        from .regularize import regularize_trace

        return compile_regularized(regularize_trace(trace, regularize_config), **options)
    if geometry_pipeline != "legacy":
        raise ValueError(f"unknown geometry_pipeline {geometry_pipeline!r}; use 'legacy' or 'canonical'")
    raw_network, index = to_mm(trace)
    raw_nodes = {node_id: raw_network.nodes[i] for node_id, i in index.items()}
    if not raw_network.walls or not raw_network.nodes:
        return CompiledTrace([], raw_network, [], ["trace contains no walls"])
    network = clean(trace)
    warnings: List[str] = []
    if not network.walls or not network.nodes:
        return CompiledTrace([], network, [], ["trace contains no walls"])

    # Translate so the footprint's lower-left corner sits at insertion_point.
    valid_xs = [p[0] for p in network.nodes.values() if math.isfinite(p[0])]
    valid_ys = [p[1] for p in network.nodes.values() if math.isfinite(p[1])]
    min_x = min(valid_xs, default=0.0)
    min_y = min(valid_ys, default=0.0)
    dx = insertion_point[0] - min_x if math.isfinite(insertion_point[0] - min_x) else 0.0
    dy = insertion_point[1] - min_y if math.isfinite(insertion_point[1] - min_y) else 0.0

    def moved(p: Point) -> List[float]:
        return [round(p[0] + dx, 1), round(p[1] + dy, 1)]

    zones = [(z.name, point_to_mm(z.seed_point, trace)) for z in trace.zones_list]
    rooms, room_warnings = recover_rooms(network, zones)
    warnings += room_warnings

    changes: List[Dict[str, Any]] = []
    wall_ids: List[Optional[str]] = []
    k = 0
    for wall in network.walls:
        if wall.type == "separator":
            wall_ids.append(None)  # bounds rooms only; not built
            continue
        k += 1
        wall_id = f"{URN}wall:{id_prefix}-{k}"
        wall_ids.append(wall_id)
        changes.append(
            {
                "op": "create",
                "@id": wall_id,
                "@type": "top:Wall",
                "label": f"{wall.type.capitalize()} wall {k}",
                "representation": representation,
                "geometry": {
                    "start": moved(network.nodes[wall.a]),
                    "end": moved(network.nodes[wall.b]),
                    "thickness": round(wall.thickness),
                    "height": wall_height_mm,
                },
            }
        )
    for k, room in enumerate(rooms, start=1):
        changes.append(
            {
                "op": "create",
                "@id": f"{URN}room:{id_prefix}-{k}",
                "@type": "top:Room",
                "label": room.label,
                "geometry": {"boundary": [moved(p) for p in room.clear]},
            }
        )

    occupied: Dict[int, List[Tuple[float, float]]] = {}
    for kind, openings in (("door", trace.doors_list), ("window", trace.windows_list)):
        for k, opening in enumerate(openings, start=1):
            wall_index, start, reversed_, warning = _host_opening(
                opening, trace, raw_nodes, network
            )
            label = f"{kind.capitalize()} {k}"
            if wall_index is None:
                warnings.append(f"{label} skipped: {warning}")
                continue
            if warning:
                warnings.append(f"{label}: {warning}")
            scale = trace.mm_per_unit if trace.units == "px" else 1.0
            width = round(opening.length * scale)
            if any(start < e and start + width > s for s, e in occupied.get(wall_index, [])):
                warnings.append(f"{label} skipped: overlaps another opening on the same wall")
                continue
            occupied.setdefault(wall_index, []).append((start, start + width))
            host = network.walls[wall_index]
            host_length = math.dist(network.nodes[host.a], network.nodes[host.b])
            geometry: Dict[str, Any] = {
                "host_wall_id": wall_ids[wall_index],
                # Rounding must never push the opening past the wall end.
                "offset": max(0, min(round(start), math.floor(host_length - width))),
                "width": width,
            }
            if kind == "door":
                if opening.door_type in {"sliding", "opening"}:
                    warnings.append(
                        f"{label} is a {opening.door_type} door; created as a swing door"
                    )
                hinge, swing = opening.hinge, opening.swing
                if reversed_:
                    hinge = "end" if hinge == "start" else "start"
                    swing = "right" if swing == "left" else "left"
                geometry.update(
                    {
                        "height": (opening.height or 0) * scale or door_height_mm,
                        "hinge": "left" if hinge == "start" else "right",
                        "swing": "in" if swing == "left" else "out",
                    }
                )
            else:
                geometry.update(
                    {
                        "height": (opening.height or 0) * scale or window_height_mm,
                        "sill_height": (
                            opening.sill_height * scale
                            if opening.sill_height is not None
                            else sill_height_mm
                        ),
                    }
                )
            changes.append(
                {
                    "op": "create",
                    "@id": f"{URN}{kind}:{id_prefix}-{k}",
                    "@type": f"top:{kind.capitalize()}",
                    "label": label,
                    "representation": representation,
                    "geometry": geometry,
                }
            )
    return CompiledTrace(changes, network, rooms, warnings, (dx, dy))


def compile_regularized(
    reg: "RegularizedPlan",
    *,
    representation: str = "auto",
    wall_height_mm: float = 3000.0,
    insertion_point: Point = (0.0, 0.0),
    id_prefix: str = "trace",
    door_height_mm: float = 2100.0,
    window_height_mm: float = 1200.0,
    sill_height_mm: float = 900.0,
) -> CompiledTrace:
    """Compile a regularized plan; no cleanup or room recovery is repeated."""
    network = reg.network
    diagnostics = [d.model_dump() for d in reg.diagnostics]
    warnings = [d["message"] for d in diagnostics if d["severity"] in ("warning", "error")]
    if not network.walls or not network.nodes:
        return CompiledTrace([], network, [], warnings + ["trace contains no walls"], pipeline="canonical",
                             diagnostics=diagnostics, regularized=reg)
    xs = [p[0] for p in network.nodes.values()]
    ys = [p[1] for p in network.nodes.values()]
    dx, dy = insertion_point[0] - min(xs), insertion_point[1] - min(ys)

    def moved(p: Point) -> List[float]:
        return [round(p[0] + dx, 1), round(p[1] + dy, 1)]

    changes: List[Dict[str, Any]] = []
    wall_urns: List[Optional[str]] = []
    k = 0
    for i, wall in enumerate(network.walls):
        if wall.type == "separator":
            wall_urns.append(None)
            continue
        k += 1
        urn = f"{URN}wall:{id_prefix}-{k}"
        wall_urns.append(urn)
        changes.append({
            "op": "create", "@id": urn, "@type": "top:Wall",
            "label": f"{wall.type.capitalize()} wall {k}",
            "representation": representation,
            "geometry": {
                "start": moved(network.nodes[wall.a]), "end": moved(network.nodes[wall.b]),
                "thickness": round(wall.thickness), "height": wall_height_mm,
            },
        })
    rooms: List[Room] = []
    unnamed = 0
    for room in sorted(reg.rooms, key=lambda r: (-r.clear[0][1], r.clear[0][0])):
        names = list(room.names)
        if not names:
            unnamed += 1
            names = [f"Space {unnamed}"]
        rooms.append(Room(room.centerline, room.clear, room.wall_indices, names))
        changes.append({
            "op": "create", "@id": f"{URN}room:{id_prefix}-{room.room_id}", "@type": "top:Room",
            "label": " / ".join(names),
            "geometry": {"boundary": [moved(p) for p in room.clear]},
        })
    counters = {"door": 0, "window": 0}
    for h in reg.openings:
        counters[h.kind] += 1
        label = f"{h.kind.capitalize()} {counters[h.kind]}"
        if not h.hosted:
            continue  # the reason is in reg.diagnostics
        host = network.walls[h.wall_index]
        length = math.dist(network.nodes[host.a], network.nodes[host.b])
        width = round(h.width)
        geometry: Dict[str, Any] = {
            "host_wall_id": wall_urns[h.wall_index],
            "offset": max(0, min(round(h.start_offset), math.floor(length - width))),
            "width": width,
        }
        if h.kind == "door":
            if h.door_type in {"sliding", "opening"}:
                warnings.append(f"{label} is a {h.door_type} door; created as a swing door")
            geometry.update({
                "height": h.height or door_height_mm,
                "hinge": "left" if h.hinge == "start" else "right",
                "swing": "in" if h.swing == "left" else "out",
            })
        else:
            geometry.update({
                "height": h.height or window_height_mm,
                "sill_height": h.sill_height if h.sill_height is not None else sill_height_mm,
            })
        changes.append({
            "op": "create", "@id": f"{URN}{h.kind}:{id_prefix}-{counters[h.kind]}",
            "@type": f"top:{h.kind.capitalize()}", "label": label,
            "representation": representation, "geometry": geometry,
        })
    return CompiledTrace(changes, network, rooms, warnings, (dx, dy), pipeline="canonical",
                         diagnostics=diagnostics, regularized=reg)
