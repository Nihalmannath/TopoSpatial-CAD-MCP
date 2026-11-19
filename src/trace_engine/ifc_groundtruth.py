"""IFC storey -> ground-truth ``TraceResult`` (walls graph, hosted openings, zones).

Turns each storey of an IFC model (e.g. the per-project exports of a plan
tracer) into the same typed trace the recogniser produces, so it can be
rasterised into training labels or compared with a predicted trace.

Steps per storey (all millimetres, y-up):

1. collect walls / railings / stairs (``IfcRelContainedInSpatialStructure``)
   and spaces (``IfcRelAggregates``);
2. wall footprint -> centreline + thickness (long axis of the outline);
3. extend or trim every centreline end to the centreline of the wall it
   abuts, so footprints that only meet face-to-face become one connected
   graph; then run :func:`graph.clean` (merge, snap, T/X split, merge runs);
4. classify walls internal / external by probing the spaces on each side;
5. host doors / windows: ``IfcRelFillsElement`` -> opening ->
   ``IfcRelVoidsElement`` -> wall, positioned on the cleaned wall by
   projecting the opening footprint;
6. zones from ``IfcSpace`` (``LongName`` or ``Name``), seeded inside the
   space outline.

Only numpy + the local STEP reader are required.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .graph import Wall, WallNetwork, clean
from .ifc_step import Entity, IfcFile, convex_hull, iter_refs, polygon_area
from .models import TraceEdge, TraceNode, TraceOpening, TraceResult, TraceZone, WallsGraph
from .rooms import point_in_polygon, recover_rooms

Point = Tuple[float, float]

OUTDOOR_WORDS = ("terrace", "balcony", "sit out", "sitout", "verandah", "veranda", "porch",
                 "deck", "open", "court", "garden", "lawn", "yard", "car porch", "portico")


# ------------------------------------------------------------- records
@dataclass
class Segment:
    a: Point
    b: Point
    thickness: float
    kind: str  # "wall" | "railing"
    source_id: str = ""


@dataclass
class OpeningGT:
    kind: str  # "door" | "window"
    polygon: List[Point]
    host_wall: Optional[int]  # index into StoreyGT.segments (raw)
    width: float
    height: Optional[float]
    door_type: str = "swing"
    source_id: str = ""


@dataclass
class SpaceGT:
    name: str
    number: str
    polygon: List[Point]
    seed: Point
    outdoor: bool


@dataclass
class StoreyGT:
    """Ground truth for one storey."""

    ifc_path: str
    index: int
    name: str
    elevation: float
    segments: List[Segment] = field(default_factory=list)
    wall_polygons: List[Tuple[str, List[Point]]] = field(default_factory=list)
    openings: List[OpeningGT] = field(default_factory=list)
    spaces: List[SpaceGT] = field(default_factory=list)
    stairs: List[List[Point]] = field(default_factory=list)
    network: WallNetwork = field(default_factory=WallNetwork)
    trace: Optional[TraceResult] = None
    stats: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{Path(self.ifc_path).stem}#{self.index}"

    def bounds(self) -> Tuple[float, float, float, float]:
        pts = [p for _, poly in self.wall_polygons for p in poly]
        if not pts:
            return (0.0, 0.0, 0.0, 0.0)
        xs, ys = zip(*pts)
        return (min(xs), min(ys), max(xs), max(ys))

    def to_json(self) -> Dict[str, object]:
        return {
            "ifc": self.ifc_path,
            "storey_index": self.index,
            "storey": self.name,
            "elevation_mm": self.elevation,
            "units": "mm",
            "y_up": True,
            "trace": self.trace.model_dump(by_alias=True) if self.trace else None,
            "spaces": [{"name": s.name, "number": s.number, "outdoor": s.outdoor,
                        "polygon": [list(p) for p in s.polygon], "seed": list(s.seed)} for s in self.spaces],
            "openings": [{"kind": o.kind, "door_type": o.door_type, "width": o.width, "height": o.height,
                          "polygon": [list(p) for p in o.polygon], "source_id": o.source_id} for o in self.openings],
            "wall_polygons": [{"kind": k, "polygon": [list(p) for p in poly]} for k, poly in self.wall_polygons],
            "stairs": [[list(p) for p in poly] for poly in self.stairs],
            "stats": self.stats,
            "warnings": self.warnings,
        }


# ------------------------------------------------------ small geometry
def _f(p) -> Point:
    return (float(p[0]), float(p[1]))


def footprint_to_centerline(poly: Sequence[Point]) -> Optional[Tuple[Point, Point, float]]:
    """Centreline (a, b) and thickness of a wall outline.

    The axis is the direction of the outline's longest edge; thickness is
    the outline's extent across that axis; the ends are where the axis
    leaves the outline (for mitred or pointed ends this is the junction
    point, for square ends the end face).
    """
    pts = [_f(p) for p in poly]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    best, u = -1.0, (1.0, 0.0)
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        length = math.hypot(x2 - x1, y2 - y1)
        if length > best:
            best, u = length, ((x2 - x1) / (length or 1.0), (y2 - y1) / (length or 1.0))
    n = (-u[1], u[0])
    s = [p[0] * u[0] + p[1] * u[1] for p in pts]
    t = [p[0] * n[0] + p[1] * n[1] for p in pts]
    thickness = max(t) - min(t)
    tm = (max(t) + min(t)) / 2.0
    # Axis x polygon boundary intersections.
    hits: List[float] = []
    for i in range(len(pts)):
        s1, t1 = s[i], t[i]
        s2, t2 = s[(i + 1) % len(pts)], t[(i + 1) % len(pts)]
        if (t1 - tm) * (t2 - tm) <= 0 and t1 != t2:
            hits.append(s1 + (tm - t1) / (t2 - t1) * (s2 - s1))
    s0, s1_ = (min(hits), max(hits)) if len(hits) >= 2 else (min(s), max(s))
    if s1_ - s0 <= 1e-6 or thickness <= 0:
        return None
    a = (u[0] * s0 + n[0] * tm, u[1] * s0 + n[1] * tm)
    b = (u[0] * s1_ + n[0] * tm, u[1] * s1_ + n[1] * tm)
    return a, b, thickness


def inside_point(poly: Sequence[Point]) -> Point:
    """A point guaranteed inside a simple polygon (centroid when it is)."""
    pts = [_f(p) for p in poly]
    a = polygon_area(pts)
    if abs(a) > 1e-9:
        cx = cy = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            cr = x1 * y2 - x2 * y1
            cx += (x1 + x2) * cr
            cy += (y1 + y2) * cr
        c = (cx / (6 * a), cy / (6 * a))
        if point_in_polygon(c, pts):
            return c
    ys = sorted(set(p[1] for p in pts))
    best: Optional[Tuple[float, Point]] = None
    for k in range(len(ys) - 1):
        y = (ys[k] + ys[k + 1]) / 2
        xs = []
        for i in range(len(pts)):
            (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
            if (y1 > y) != (y2 > y):
                xs.append(x1 + (y - y1) / (y2 - y1) * (x2 - x1))
        xs.sort()
        for j in range(0, len(xs) - 1, 2):
            w = xs[j + 1] - xs[j]
            if best is None or w > best[0]:
                best = (w, ((xs[j] + xs[j + 1]) / 2, y))
    return best[1] if best else pts[0]


def _line_hit(p: Point, v: Point, a: Point, b: Point) -> Optional[Tuple[float, float]]:
    """Ray p + lam*v against line a->b: (lam, mu) with mu along a->b (0..1)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = v[0] * dy - v[1] * dx
    if abs(den) < 1e-12:
        return None
    wx, wy = a[0] - p[0], a[1] - p[1]
    lam = (wx * dy - wy * dx) / den
    mu = (wx * v[1] - wy * v[0]) / den
    return lam, mu


def connect_ends(segments: List[Segment], tolerance: float = 30.0) -> List[Segment]:
    """Move each centreline end onto the centreline of the wall it abuts.

    An end is moved along its own axis by at most half the other wall's
    thickness plus ``tolerance`` (forwards for a T-junction stub that stops
    at a face, backwards for an L-corner wall that runs to the outer face).
    Only clearly non-parallel walls are considered.
    """
    dividers = [s for s in segments if s.kind == "divider"]
    if dividers:
        solid = connect_ends([s for s in segments if s.kind != "divider"], tolerance)
        return solid + [_snap_divider(d, solid) for d in dividers]
    out = [Segment(s.a, s.b, s.thickness, s.kind, s.source_id) for s in segments]
    for i, seg in enumerate(segments):
        length = math.dist(seg.a, seg.b)
        if length <= 0:
            continue
        u = ((seg.b[0] - seg.a[0]) / length, (seg.b[1] - seg.a[1]) / length)
        for end in ("a", "b"):
            p = seg.a if end == "a" else seg.b
            v = (-u[0], -u[1]) if end == "a" else u
            best: Optional[Tuple[float, Point]] = None
            for j, other in enumerate(segments):
                if j == i:
                    continue
                olen = math.dist(other.a, other.b)
                if olen <= 0:
                    continue
                ou = ((other.b[0] - other.a[0]) / olen, (other.b[1] - other.a[1]) / olen)
                if abs(u[0] * ou[1] - u[1] * ou[0]) < 0.5:  # near-parallel
                    continue
                hit = _line_hit(p, v, other.a, other.b)
                if hit is None:
                    continue
                lam, mu = hit
                reach = other.thickness / 2.0 + tolerance + (120.0 if seg.kind == "divider" else 0.0)
                slack = (seg.thickness / 2.0 + tolerance) / olen
                if not (-reach <= lam <= reach and -slack <= mu <= 1 + slack):
                    continue
                if lam < 0 and -lam > length * 0.45:  # never trim a wall away
                    continue
                if best is None or abs(lam) < abs(best[0]):
                    best = (lam, (p[0] + v[0] * lam, p[1] + v[1] * lam))
            if best is not None:
                if end == "a":
                    out[i].a = best[1]
                else:
                    out[i].b = best[1]
    return out


def _snap_divider(seg: Segment, solid: Sequence[Segment], reach: float = 450.0,
                  corner: float = 150.0) -> Segment:
    """Snap both ends of a divider onto the (already connected) wall centrelines.

    Dividers end on a wall face at any angle, so each end goes to the nearest
    centreline point - or to that wall's end node when it is close, so the
    divider shares the junction instead of landing just beside it.
    """
    out = Segment(seg.a, seg.b, seg.thickness, seg.kind, seg.source_id)
    for end in ("a", "b"):
        p = seg.a if end == "a" else seg.b
        best_d, best_q = reach, None
        for other in solid:
            q = _closest_on_segment(p, other.a, other.b)
            d = math.dist(p, q)
            if d < best_d:
                best_d, best_q = d, q
                for node in (other.a, other.b):
                    if math.dist(q, node) < corner:
                        best_q = node
        if best_q is not None:
            setattr(out, end, best_q)
    return out


def _closest_on_segment(p: Point, a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return (a[0] + t * dx, a[1] + t * dy)


def _components_and_dead_ends(network: WallNetwork) -> Tuple[int, int]:
    walls = [w for w in network.walls if w.type != "separator"]
    nodes = {n for w in walls for n in (w.a, w.b)}
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    deg: Dict[int, int] = {n: 0 for n in nodes}
    for w in walls:
        parent[find(w.a)] = find(w.b)
        deg[w.a] += 1
        deg[w.b] += 1
    comps = len({find(n) for n in nodes})
    return comps, sum(1 for d in deg.values() if d == 1)


def polygon_iou(p: Sequence[Point], q: Sequence[Point], cell: float = 25.0) -> float:
    from PIL import Image, ImageDraw

    pts = list(p) + list(q)
    x0, y0 = min(x for x, _ in pts), min(y for _, y in pts)
    x1, y1 = max(x for x, _ in pts), max(y for _, y in pts)
    w, h = int((x1 - x0) / cell) + 3, int((y1 - y0) / cell) + 3
    if w * h > 4_000_000:
        cell *= math.sqrt(w * h / 4_000_000)
        w, h = int((x1 - x0) / cell) + 3, int((y1 - y0) / cell) + 3
    tr = lambda poly: [((x - x0) / cell + 1, (y - y0) / cell + 1) for x, y in poly]  # noqa: E731
    a, b = Image.new("1", (w, h)), Image.new("1", (w, h))
    ImageDraw.Draw(a).polygon(tr(p), fill=1)
    ImageDraw.Draw(b).polygon(tr(q), fill=1)
    A, B = np.asarray(a, bool), np.asarray(b, bool)
    union = (A | B).sum()
    return float((A & B).sum() / union) if union else 0.0


# --------------------------------------------------------------- reader
def _relations(ifc: IfcFile):
    contained: Dict[int, List[int]] = {}
    for rel in ifc.of_type("IFCRELCONTAINEDINSPATIALSTRUCTURE"):
        contained.setdefault(int(rel[5]), []).extend(iter_refs(rel[4]))
    aggregated: Dict[int, List[int]] = {}
    for rel in ifc.of_type("IFCRELAGGREGATES"):
        aggregated.setdefault(int(rel[4]), []).extend(iter_refs(rel[5]))
    voids: Dict[int, int] = {}  # opening -> host element
    for rel in ifc.of_type("IFCRELVOIDSELEMENT"):
        voids[int(rel[5])] = int(rel[4])
    fills: Dict[int, int] = {}  # opening -> filling element
    for rel in ifc.of_type("IFCRELFILLSELEMENT"):
        fills[int(rel[4])] = int(rel[5])
    return contained, aggregated, voids, fills


def load_storeys(path: str | Path, build: bool = True) -> List[StoreyGT]:
    """Read every storey of an IFC file (sorted by elevation)."""
    ifc = IfcFile.open(path)
    contained, aggregated, voids, fills = _relations(ifc)
    storeys = sorted(ifc.of_type("IFCBUILDINGSTOREY"),
                     key=lambda s: float(s[9]) if isinstance(s[9], (int, float)) else 0.0)
    out: List[StoreyGT] = []
    for idx, st in enumerate(storeys):
        elev = float(st[9]) * ifc.length_scale if isinstance(st[9], (int, float)) else 0.0
        gt = StoreyGT(str(path), idx, str(st[2] or f"Storey {idx}"), elev)
        members = [ifc[r] for r in contained.get(st.id, []) if r in ifc.entities]
        members += [ifc[r] for r in aggregated.get(st.id, []) if r in ifc.entities]
        wall_index: Dict[int, int] = {}
        for ent in members:
            if ent.type in ("IFCWALL", "IFCWALLSTANDARDCASE", "IFCRAILING", "IFCCURTAINWALL"):
                kind = "railing" if ent.type == "IFCRAILING" else "wall"
                polys = ifc.footprints(ent)
                if not polys:
                    continue
                # A wall body is often split into several extrusions around its
                # openings (sides, sill, lintel); a straight wall's full outline is
                # the hull of all of them.
                poly = convex_hull([p for fp in polys for p in fp]) if len(polys) > 1 else polys[0]
                gt.wall_polygons.append((kind, [_f(p) for p in poly]))
                cl = footprint_to_centerline(poly)
                if cl is None:
                    gt.warnings.append(f"{kind} {ent[7] or ent.id}: degenerate outline")
                    continue
                wall_index[ent.id] = len(gt.segments)
                gt.segments.append(Segment(cl[0], cl[1], cl[2], kind, str(ent[7] or ent.id)))
            elif ent.type == "IFCSTAIR" or ent.type == "IFCSTAIRFLIGHT":
                gt.stairs.extend([[_f(p) for p in poly] for poly in ifc.footprints(ent)])
            elif ent.type == "IFCSPACE":
                polys = ifc.footprints(ent)
                if not polys:
                    continue
                poly = [_f(p) for p in max(polys, key=lambda p: abs(polygon_area(p)))]
                long_name = ent[7] if isinstance(ent[7], str) else ""
                short = ent[2] if isinstance(ent[2], str) else ""
                name = (long_name or short or "Space").strip()
                outdoor = any(w in name.lower() for w in OUTDOOR_WORDS)
                gt.spaces.append(SpaceGT(name, short, poly, inside_point(poly), outdoor))
        # openings hosted on this storey's walls
        for opening_id, host_id in voids.items():
            if host_id not in wall_index or opening_id not in ifc.entities:
                continue
            opening = ifc[opening_id]
            polys = ifc.footprints(opening)
            if not polys:
                continue
            poly = [_f(p) for p in max(polys, key=lambda p: abs(polygon_area(p)))]
            filler = ifc.get(fills.get(opening_id))
            if filler is not None and filler.type in ("IFCWINDOW", "IFCWINDOWSTANDARDCASE"):
                kind, door_type = "window", "swing"
            elif filler is not None and filler.type in ("IFCDOOR", "IFCDOORSTANDARDCASE"):
                kind, door_type = "door", "swing"
            else:
                kind, door_type = "door", "opening"
            seg = gt.segments[wall_index[host_id]]
            L = math.dist(seg.a, seg.b) or 1.0
            u = ((seg.b[0] - seg.a[0]) / L, (seg.b[1] - seg.a[1]) / L)
            s = [p[0] * u[0] + p[1] * u[1] for p in poly]
            width = max(s) - min(s)
            height = None
            if filler is not None and isinstance(filler[8], (int, float)):
                height = float(filler[8]) * ifc.length_scale
            gt.openings.append(OpeningGT(kind, poly, wall_index[host_id], width, height, door_type,
                                         str(filler[7] if filler is not None else opening[7] or opening.id)))
        if build:
            build_storey(gt)
        out.append(gt)
    return out


# --------------------------------------------------------------- build
def _host(network: WallNetwork, center: Point, direction: Point, width: float) -> Optional[Tuple[Wall, float, float]]:
    """Best cleaned wall for an opening: (wall, distance from wall.a, perpendicular offset)."""
    best = None
    for wall in network.walls:
        if wall.type == "separator":
            continue
        a, b = network.nodes[wall.a], network.nodes[wall.b]
        L = math.dist(a, b)
        if L <= 0:
            continue
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        if abs(u[0] * direction[0] + u[1] * direction[1]) < 0.95:
            continue
        s = (center[0] - a[0]) * u[0] + (center[1] - a[1]) * u[1]
        d = abs((center[0] - a[0]) * -u[1] + (center[1] - a[1]) * u[0])
        if s < -width / 2 or s > L + width / 2 or d > wall.thickness / 2 + 60:
            continue
        overrun = max(0.0, width / 2 - s, s + width / 2 - L)
        score = d + 2 * overrun
        if best is None or score < best[0]:
            best = (score, wall, min(max(s, min(width / 2, L / 2)), max(L - width / 2, L / 2)), d)
    return None if best is None else (best[1], best[2], best[3])


def _dist_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def space_dividers(gt: StoreyGT, segments: Sequence[Segment], step: float = 50.0,
                   min_length: float = 300.0) -> List[Segment]:
    """Stretches of space outlines with no wall or railing behind them.

    Open-plan rooms and drawn room dividers bound a space without a wall;
    they become ``separator`` edges so every space is a closed face of the
    wall graph (the same role separators play in a predicted trace).
    """
    out: List[Segment] = []
    for sp in gt.spaces:
        poly = sp.polygon
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            L = math.dist(a, b)
            if L < min_length:
                continue
            k = max(2, int(L / step) + 1)
            samples = [(a[0] + (b[0] - a[0]) * j / (k - 1), a[1] + (b[1] - a[1]) * j / (k - 1)) for j in range(k)]
            open_ = [all(_dist_to_segment(q, sg.a, sg.b) > sg.thickness / 2 + 80.0 for sg in segments) for q in samples]
            j = 0
            while j < k:
                if not open_[j]:
                    j += 1
                    continue
                e = j
                while e + 1 < k and open_[e + 1]:
                    e += 1
                lo, hi = max(0, j - 1), min(k - 1, e + 1)
                if math.dist(samples[lo], samples[hi]) >= min_length:
                    out.append(Segment(samples[lo], samples[hi], 10.0, "divider", f"space:{sp.number or sp.name}"))
                j = e + 1
    # drop duplicates from the two spaces sharing an open boundary
    uniq: List[Segment] = []
    for sg in out:
        if any(_dist_to_segment(sg.a, u.a, u.b) < 60 and _dist_to_segment(sg.b, u.a, u.b) < 60 for u in uniq):
            continue
        uniq.append(sg)
    return uniq


def build_storey(gt: StoreyGT) -> StoreyGT:
    """Fill ``gt.network``, ``gt.trace`` and ``gt.stats`` from the raw records."""
    if not gt.segments:
        gt.stats = {"walls": 0}
        return gt
    dividers = space_dividers(gt, gt.segments)
    segs = connect_ends(list(gt.segments) + dividers)
    nodes: List[TraceNode] = []
    edges: List[TraceEdge] = []
    for k, s in enumerate(segs):
        if math.dist(s.a, s.b) < 1.0:
            continue
        na, nb = f"r{k}a", f"r{k}b"
        nodes += [TraceNode(id=na, x=s.a[0], y=s.a[1]), TraceNode(id=nb, x=s.b[0], y=s.b[1])]
        edges.append(TraceEdge(source=na, target=nb, thickness=max(s.thickness, 1.0),
                               type="separator" if s.kind in ("railing", "divider") else "internal"))
    raw = TraceResult(walls_graph=WallsGraph(nodes=nodes, edges=edges), units="mm", source="ifc")
    walls_only = sorted(s.thickness for s in segs if s.kind == "wall") or [100.0]
    tol = max(10.0, min(60.0, 0.25 * walls_only[len(walls_only) // 2]))
    network = clean(raw, snap_tolerance_mm=tol)

    # exterior / interior
    indoor = [sp.polygon for sp in gt.spaces if not sp.outdoor]
    for wall in network.walls:
        if wall.type == "separator":
            continue
        a, b = network.nodes[wall.a], network.nodes[wall.b]
        L = math.dist(a, b) or 1.0
        n = (-(b[1] - a[1]) / L, (b[0] - a[0]) / L)
        m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        d = wall.thickness / 2 + 120.0
        left = any(point_in_polygon((m[0] + n[0] * d, m[1] + n[1] * d), poly) for poly in indoor)
        right = any(point_in_polygon((m[0] - n[0] * d, m[1] - n[1] * d), poly) for poly in indoor)
        wall.type = "internal" if (left and right) else "external"
    gt.network = network

    ids = {n: f"n{n}" for n in network.nodes}
    t_nodes = [TraceNode(id=ids[n], x=p[0], y=p[1]) for n, p in network.nodes.items()]
    t_edges = [TraceEdge(source=ids[w.a], target=ids[w.b], type=w.type, thickness=w.thickness)
               for w in network.walls]
    doors: List[TraceOpening] = []
    windows: List[TraceOpening] = []
    unhosted = 0
    for op in gt.openings:
        seg = gt.segments[op.host_wall] if op.host_wall is not None else None
        if seg is None:
            unhosted += 1
            continue
        L = math.dist(seg.a, seg.b) or 1.0
        u = ((seg.b[0] - seg.a[0]) / L, (seg.b[1] - seg.a[1]) / L)
        s = [p[0] * u[0] + p[1] * u[1] for p in op.polygon]
        t = [p[0] * -u[1] + p[1] * u[0] for p in op.polygon]
        sc, tc = (max(s) + min(s)) / 2, (max(t) + min(t)) / 2
        center = (u[0] * sc - u[1] * tc, u[1] * sc + u[0] * tc)
        hosted = _host(network, center, u, op.width)
        if hosted is None:
            unhosted += 1
            continue
        wall, dist, _ = hosted
        rec = TraceOpening(edge_nodes=(ids[wall.a], ids[wall.b]), length=max(op.width, 1.0),
                           distance_from_lower_node=max(dist, 0.0), center=center,
                           door_type=op.door_type if op.kind == "door" else "swing",
                           height=op.height if (op.height or 0) > 0 else None)
        (windows if op.kind == "window" else doors).append(rec)
    zones = [TraceZone(name=sp.name[:128] or "Space", seedPoint=sp.seed) for sp in gt.spaces]
    gt.trace = TraceResult(walls_graph=WallsGraph(nodes=t_nodes, edges=t_edges), doors_list=doors,
                           windows_list=windows, zones_list=zones, units="mm", y_down=False,
                           source=f"ifc:{Path(gt.ifc_path).name}#{gt.index}:{gt.name}")

    comps, dead = _components_and_dead_ends(network)
    rooms, room_warnings = recover_rooms(network, [(sp.name, sp.seed) for sp in gt.spaces])
    matched = 0
    for sp in gt.spaces:
        hosts = [r for r in rooms if point_in_polygon(sp.seed, r.centerline)]
        if hosts and polygon_iou(sp.polygon, min(hosts, key=lambda r: r.area_mm2).clear) >= 0.7:
            matched += 1
    gt.stats = {
        "walls": sum(1 for w in network.walls if w.type != "separator"),
        "separators": sum(1 for w in network.walls if w.type == "separator"),
        "dividers_added": len(dividers),
        "external": sum(1 for w in network.walls if w.type == "external"),
        "nodes": len(network.nodes),
        "components": comps,
        "dead_ends": dead,
        "doors": len(doors),
        "windows": len(windows),
        "openings_unhosted": unhosted,
        "spaces": len(gt.spaces),
        "rooms_recovered": len(rooms),
        "spaces_matched": matched,
    }
    if unhosted:
        gt.warnings.append(f"{unhosted} opening(s) could not be hosted on a cleaned wall")
    return gt


def find_ifcs(folder: str | Path) -> List[Path]:
    """Every .ifc under a project folder (recursively), stable order."""
    return sorted(Path(folder).rglob("*.ifc"), key=lambda p: str(p).lower())


def storeys_for_folder(folder: str | Path) -> List[StoreyGT]:
    """All non-empty storeys of all IFCs in a folder (candidates for matching)."""
    out: List[StoreyGT] = []
    for ifc in find_ifcs(folder):
        out.extend(s for s in load_storeys(ifc) if s.stats.get("walls", 0) > 0)
    return out


def save_storey(gt: StoreyGT, path: str | Path) -> None:
    Path(path).write_text(json.dumps(gt.to_json(), indent=1), encoding="utf-8")
