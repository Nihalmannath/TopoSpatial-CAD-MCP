"""One regularization service: canonical plan -> clean, hosted, room-solved plan.

    regularize_trace(trace, config) -> RegularizedPlan

Order (every step in canonical millimetres, y-up; see ``canonical.normalize``):

1. validate + transform (``normalize``)                  - one frame for everything
2. clean graph (``graph`` steps, with lineage)           - merge / snap / split / join
3. repair small, well-supported gaps (bounded, logged)   - dangling ends only
4. recover faces (half-edge traversal, ``rooms``)        - centerline faces
5. classify exposure (``geometry_kernel.solve_wall_exposure``) and split
   walls where it changes along their length
6. resolve thickness (measured / user value kept; defaults by exposure)
7. host openings by lineage, bounded geometric fallback  - nothing dropped silently
8. clear room boundaries, label seeds, identity

The result keeps:

* ``node_map``: input node id -> canonical node index (``None`` when the node
  was absorbed into the interior of a merged wall),
* ``edge_lineage``: input edge id -> [(wall index, s0_mm, s1_mm)] intervals of
  each resulting wall that the input edge contributes to (handles merges and
  splits),
* ``openings``: one ``HostedOpening`` per input opening, hosted or rejected
  with a diagnostic,
* ``rooms``: ``RoomRecord`` with centerline face, clear interior, label point,
  canonical geometry hash and a persistent semantic id,
* ``diagnostics``: structured notes (``models.Diagnostic``).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from . import graph as g
from .canonical import CanonicalPlan, COpening, Frame, normalize
from .geometry_kernel import DEFAULT_WALL_THICKNESS, compute_face_content_hash, solve_wall_exposure
from .models import Diagnostic, TraceResult
from .rooms import find_faces, inset_face, point_in_polygon, signed_area

Point = Tuple[float, float]

OUTDOOR_WORDS = ("balcony", "terrace", "courtyard", "patio", "deck", "sit out", "sitout", "verandah",
                 "veranda", "porch", "open to sky", "ots", "lawn", "garden")


@dataclass
class RegularizeConfig:
    """All tolerances are millimetres or degrees."""

    snap_tolerance_mm: Optional[float] = None  # None: max(10, 0.5 x median wall thickness)
    axis_snap_deg: float = 3.0
    colinear_deg: float = 1.0
    thickness_ratio: float = 0.1
    # Gap repair: a dangling wall end is extended (along the wall) or joined to
    # another dangling end only within this distance. Keep it below the
    # narrowest doorway so real openings are never closed. None: clamp(1.5 x
    # median thickness, 150, 500).
    gap_repair_mm: Optional[float] = None
    repair_gaps: bool = True
    opening_angle_deg: float = 10.0
    opening_search_mm: float = 400.0
    opening_end_clearance_mm: float = 0.0
    min_room_area_mm2: float = 500_000.0
    min_clear_dimension_mm: float = 500.0
    boundary_seed_mm: float = 100.0
    exposure_probe_mm: float = 40.0
    exposure_probes: int = 5
    default_internal_mm: float = DEFAULT_WALL_THICKNESS["internalMm"]
    default_external_mm: float = DEFAULT_WALL_THICKNESS["externalMm"]
    outdoor_words: Tuple[str, ...] = OUTDOOR_WORDS


@dataclass
class HostedOpening:
    id: str
    kind: str
    width: float
    wall_index: Optional[int] = None
    start_offset: float = 0.0       # from the host wall's start node (CAD offset)
    center_offset: float = 0.0      # from the host wall's start node
    reversed: bool = False          # host wall runs opposite to the reported edge
    method: str = ""                # lineage | fallback | rejected
    door_type: str = "swing"
    hinge: str = "start"            # relative to the host wall start -> end
    swing: str = "left"
    height: Optional[float] = None
    sill_height: Optional[float] = None
    position: Point = (0.0, 0.0)
    confidence: Optional[float] = None

    @property
    def hosted(self) -> bool:
        return self.wall_index is not None


@dataclass
class RoomRecord:
    centerline: List[Point]
    clear: List[Point]
    wall_indices: List[int]
    names: List[str] = field(default_factory=list)
    label_point: Point = (0.0, 0.0)
    geometry_hash: str = ""
    room_id: str = ""
    outdoor: bool = False
    flags: List[str] = field(default_factory=list)

    @property
    def classified(self) -> bool:
        return bool(self.names)

    @property
    def area_mm2(self) -> float:
        return abs(signed_area(self.clear))

    @property
    def label(self) -> str:
        return " / ".join(self.names)


@dataclass
class RegularizedPlan:
    plan: CanonicalPlan
    network: g.WallNetwork
    wall_ids: List[str]
    exposure: List[str]
    node_map: Dict[str, Optional[int]]
    edge_lineage: Dict[str, List[Tuple[int, float, float]]]
    openings: List[HostedOpening]
    rooms: List[RoomRecord]
    faces: List[Tuple[List[int], List[int]]]
    diagnostics: List[Diagnostic]
    config: RegularizeConfig
    tolerance_mm: float

    @property
    def frame(self) -> Frame:
        return self.plan.frame

    def wall_length(self, index: int) -> float:
        w = self.network.walls[index]
        return math.dist(self.network.nodes[w.a], self.network.nodes[w.b])

    def dangling_endpoints(self) -> List[int]:
        return _dangling(self.network)


def _dangling(net: g.WallNetwork) -> List[int]:
    """Nodes with exactly one wall (separators count: they close rooms)."""
    deg: Dict[int, int] = {}
    for w in net.walls:
        deg[w.a] = deg.get(w.a, 0) + 1
        deg[w.b] = deg.get(w.b, 0) + 1
    return sorted(n for n, d in deg.items() if d == 1)


# ------------------------------------------------------------------ helpers
def _diag(diags: List[Diagnostic], code: str, message: str, entity: Optional[str] = None,
          location: Optional[Point] = None, severity: str = "warning") -> None:
    diags.append(Diagnostic(severity=severity, code=code, message=message, entity=entity,
                            location=(round(location[0], 1), round(location[1], 1)) if location else None))


def _unit(a: Point, b: Point) -> Tuple[Point, float]:
    length = math.dist(a, b)
    if length <= 0:
        return (0.0, 0.0), 0.0
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length), length


def _ray_segment(p: Point, d: Point, a: Point, b: Point) -> Optional[Tuple[float, float]]:
    """Ray p + t d (t > 0) against segment a-b: return (t, u) with u in [0, 1]."""
    s = (b[0] - a[0], b[1] - a[1])
    denom = d[0] * s[1] - d[1] * s[0]
    if abs(denom) < 1e-12:
        return None
    q = (a[0] - p[0], a[1] - p[1])
    t = (q[0] * s[1] - q[1] * s[0]) / denom
    u = (q[0] * d[1] - q[1] * d[0]) / denom
    if t > 1e-6 and -1e-9 <= u <= 1 + 1e-9:
        return t, u
    return None


def _to_network(plan: CanonicalPlan) -> g.WallNetwork:
    net = g.WallNetwork()
    index: Dict[str, int] = {}
    for i, (nid, p) in enumerate(plan.nodes.items()):
        index[nid] = i
        net.nodes[i] = p
    for e in plan.edges:
        net.walls.append(g.Wall(index[e.a], index[e.b], e.thickness, e.type,
                                frozenset({e.id}), e.thickness_source, e.provenance))
    return net


def _clean(net: g.WallNetwork, tol: float, cfg: RegularizeConfig, notes: List[g.Note]) -> g.WallNetwork:
    net = g.merge_nodes(net, tol, notes)
    net = g.snap_axes(net, cfg.axis_snap_deg)
    net = g.merge_nodes(net, tol, notes)
    net = g.split_edges(net, tol, notes)
    net = g.snap_axes(net, cfg.axis_snap_deg)
    net = g.merge_nodes(net, tol, notes)
    net = g.merge_colinear(net, cfg.colinear_deg, cfg.thickness_ratio, notes)
    return g.prune_isolated(net)


# ------------------------------------------------------------- gap repair
def _repair_gaps(net: g.WallNetwork, max_gap: float, diags: List[Diagnostic]) -> int:
    """Close small gaps at dangling wall ends. Returns the number of repairs.

    1. Extend a dangling end straight on (along its own wall) to the first wall
       it hits within ``max_gap``; the extension inherits the wall's lineage.
    2. Extend two dangling ends whose rays meet within ``max_gap`` (an L corner
       where both walls stop short) to that corner.
    3. Otherwise join two dangling ends closer than ``max_gap`` with a
       separator (bounds rooms, never built as a wall).
    """
    if max_gap <= 0:
        return 0
    deg: Dict[int, int] = {}
    for w in net.walls:
        deg[w.a] = deg.get(w.a, 0) + 1
        deg[w.b] = deg.get(w.b, 0) + 1
    dangling = [n for n, d in deg.items() if d == 1]
    if not dangling:
        return 0
    incident = {n: next(w for w in net.walls if n in (w.a, w.b)) for n in dangling}
    next_id = max(net.nodes, default=-1) + 1
    repaired = 0
    done = set()
    new_walls: List[g.Wall] = []
    # Pass 1: straight extensions (a real wall that stops short).
    for n in dangling:
        wall = incident[n]
        if wall.type == "separator":
            continue
        other = wall.b if wall.a == n else wall.a
        d, length = _unit(net.nodes[other], net.nodes[n])
        if length <= 0:
            continue
        p = net.nodes[n]
        best = None
        for w in net.walls + new_walls:
            if w is wall or n in (w.a, w.b):
                continue
            hit = _ray_segment(p, d, net.nodes[w.a], net.nodes[w.b])
            if hit and hit[0] <= max_gap and (best is None or hit[0] < best[0]):
                best = (hit[0], hit[1], w)
        # A collinear wall end straight ahead (a wall interrupted by a short
        # gap, shorter than any doorway) continues the same wall.
        for m in dangling:
            if m == n or incident[m] is wall:
                continue
            v = (net.nodes[m][0] - p[0], net.nodes[m][1] - p[1])
            t_ahead = v[0] * d[0] + v[1] * d[1]
            perp = abs(v[0] * d[1] - v[1] * d[0])
            if 0 < t_ahead <= max_gap and perp <= max(1.0, 0.5 * wall.thickness) \
                    and (best is None or t_ahead < best[0]):
                best = (t_ahead, None, m)
        if best is None:
            continue
        t, u, host = best
        q = (p[0] + d[0] * t, p[1] + d[1] * t)
        target = None  # land on an existing node when the hit is next to one
        if isinstance(host, int):
            target, q = host, net.nodes[host]
        for cand in (() if target is not None else (host.a, host.b)):
            if math.dist(q, net.nodes[cand]) <= max(1.0, 0.05 * max_gap):
                target = cand
        if target is None:
            target = next_id
            next_id += 1
            net.nodes[target] = q
        new_walls.append(g.Wall(n, target, wall.thickness, wall.type, wall.sources,
                                wall.thickness_source, "gap_repair"))
        _diag(diags, "gap_extended", f"wall end extended {t:.0f} mm to meet a wall", None, q, "info")
        done.add(n)
        if target in incident:
            done.add(target)  # the end it reached is no longer dangling
        repaired += 1
    # Pass 2: L-corner completion - two dangling ends whose rays meet within
    # the limit (both walls stop short of a shared corner) are extended to
    # the meeting point.
    sin_min = math.sin(math.radians(30))
    for n in dangling:
        if n in done or incident[n].type == "separator":
            continue
        wn = incident[n]
        dn, ln = _unit(net.nodes[wn.b if wn.a == n else wn.a], net.nodes[n])
        if ln <= 0:
            continue
        pn = net.nodes[n]
        best = None
        for m in dangling:
            if m == n or m in done or incident[m] is wn or incident[m].type == "separator":
                continue
            wm = incident[m]
            dm, lm = _unit(net.nodes[wm.b if wm.a == m else wm.a], net.nodes[m])
            cross = dn[0] * dm[1] - dn[1] * dm[0]
            if lm <= 0 or abs(cross) < sin_min:
                continue
            pm = net.nodes[m]
            q = (pm[0] - pn[0], pm[1] - pn[1])
            t = (q[0] * dm[1] - q[1] * dm[0]) / cross
            u = (q[0] * dn[1] - q[1] * dn[0]) / cross
            if -1.0 <= t <= max_gap and -1.0 <= u <= max_gap and (best is None or max(t, u) < best[0]):
                best = (max(t, u), m, (pn[0] + dn[0] * t, pn[1] + dn[1] * t))
        if best is None:
            continue
        _, m, corner = best
        k = next_id
        next_id += 1
        net.nodes[k] = corner
        for end in (n, m):
            w = incident[end]
            if math.dist(net.nodes[end], corner) > 1e-6:
                new_walls.append(g.Wall(end, k, w.thickness, w.type, w.sources, w.thickness_source, "gap_repair"))
        _diag(diags, "gap_corner", "two wall ends extended to their common corner", None, corner, "info")
        done.update({n, m})
        repaired += 1
    # Pass 3: join two remaining dangling ends with a separator.
    for n in dangling:
        if n in done or incident[n].type == "separator":
            continue
        wall = incident[n]
        p = net.nodes[n]
        partner, pd = None, max_gap
        for m in dangling:
            if m == n or m in done or incident[m] is wall:
                continue
            dist = math.dist(p, net.nodes[m])
            if dist <= pd:
                partner, pd = m, dist
        if partner is not None:
            new_walls.append(g.Wall(n, partner, max(1.0, min(wall.thickness, incident[partner].thickness)),
                                    "separator", frozenset(), "default", "gap_repair"))
            mid = ((p[0] + net.nodes[partner][0]) / 2, (p[1] + net.nodes[partner][1]) / 2)
            _diag(diags, "gap_joined", f"two wall ends {pd:.0f} mm apart joined by a separator", None, mid, "info")
            done.update({n, partner})
            repaired += 1
    net.walls.extend(new_walls)
    return repaired


# ---------------------------------------------------------------- lineage
def _lineage(plan: CanonicalPlan, net: g.WallNetwork, tol: float) -> Dict[str, List[Tuple[int, float, float]]]:
    """For every input edge: the intervals [s0, s1] (mm along each resulting
    wall from its start node) that the input edge covers."""
    by_id = {e.id: e for e in plan.edges}
    out: Dict[str, List[Tuple[int, float, float]]] = {e.id: [] for e in plan.edges}
    for i, w in enumerate(net.walls):
        a, b = net.nodes[w.a], net.nodes[w.b]
        u, length = _unit(a, b)
        if length <= 0:
            continue
        for src in sorted(w.sources):
            e = by_id.get(src)
            if e is None:
                continue
            s = sorted(((plan.nodes[n][0] - a[0]) * u[0] + (plan.nodes[n][1] - a[1]) * u[1]) for n in (e.a, e.b))
            s0, s1 = max(0.0, s[0]), min(length, s[1])
            if s1 - s0 > tol or (w.provenance == "gap_repair"):
                out[src].append((i, round(max(0.0, s0), 1), round(max(s0, s1), 1)))
    return out


def _node_map(plan: CanonicalPlan, net: g.WallNetwork, tol: float) -> Dict[str, Optional[int]]:
    result: Dict[str, Optional[int]] = {}
    for nid, p in plan.nodes.items():
        best, bd = None, 2.0 * tol
        for k, q in net.nodes.items():
            d = math.dist(p, q)
            if d <= bd:
                best, bd = k, d
        result[nid] = best
    return result


# --------------------------------------------------------------- exposure
def _exposure(net: g.WallNetwork, faces, outdoor_faces: set, cfg: RegularizeConfig,
              diags: List[Diagnostic], wall_ids: List[str]) -> List[str]:
    polys = [[net.nodes[n] for n in cycle] for k, (cycle, _) in enumerate(faces) if k not in outdoor_faces]
    node_map = {str(k): {"x": p[0], "y": p[1]} for k, p in net.nodes.items()}
    walls = [{"id": str(i), "startNodeId": str(w.a), "endNodeId": str(w.b), "type": w.type,
              "thickness": w.thickness, "thicknessSource": w.thickness_source}
             for i, w in enumerate(net.walls)]
    result = solve_wall_exposure(walls, node_map, polys, cfg.exposure_probe_mm, cfg.exposure_probes)
    exposure = []
    for i, r in enumerate(result):
        exposure.append(r.get("exposure", "unknown"))
        if r.get("exposure") == "mixed":
            _diag(diags, "exposure_mixed", "wall faces a room on part of its length only",
                  wall_ids[i] if i < len(wall_ids) else None)
    return exposure


def _split_mixed(net: g.WallNetwork, faces, outdoor_faces, cfg) -> bool:
    """Split walls whose exposure changes along their length at the change."""
    polys = [[net.nodes[n] for n in cycle] for k, (cycle, _) in enumerate(faces) if k not in outdoor_faces]
    node_map = {str(k): {"x": p[0], "y": p[1]} for k, p in net.nodes.items()}
    walls = [{"id": str(i), "startNodeId": str(w.a), "endNodeId": str(w.b), "type": w.type}
             for i, w in enumerate(net.walls)]
    result = solve_wall_exposure(walls, node_map, polys, cfg.exposure_probe_mm, max(cfg.exposure_probes, 9))
    changed = False
    next_id = max(net.nodes, default=-1) + 1
    out: List[g.Wall] = []
    for w, r in zip(net.walls, result):
        cuts = r.get("exposureTransitions") or []
        if r.get("exposure") != "mixed" or not cuts:
            out.append(w)
            continue
        a, b = net.nodes[w.a], net.nodes[w.b]
        chain = [w.a]
        for f in cuts:
            net.nodes[next_id] = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            chain.append(next_id)
            next_id += 1
        chain.append(w.b)
        for u, v in zip(chain, chain[1:]):
            out.append(g.Wall(u, v, w.thickness, w.type, w.sources, w.thickness_source, w.provenance))
        changed = True
    net.walls = out
    return changed


# ---------------------------------------------------------------- rooms
def representative_point(poly: Sequence[Point], samples: int = 24) -> Point:
    """A point well inside ``poly`` (also for L / U shapes): the grid sample
    farthest from the boundary among those inside."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    best, bd = None, -1.0
    n = len(poly)
    for i in range(1, samples):
        for j in range(1, samples):
            q = (min(xs) + (max(xs) - min(xs)) * i / samples, min(ys) + (max(ys) - min(ys)) * j / samples)
            if not point_in_polygon(q, poly):
                continue
            d = min(_seg_dist(q, poly[k], poly[(k + 1) % n]) for k in range(n))
            if d > bd:
                best, bd = q, d
    if best is None:
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return best


def _seg_dist(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def canonical_face_hash(vertices: Sequence[Point], quantum_mm: float = 1.0) -> str:
    """Geometry hash independent of winding and start vertex.

    Vertices are quantised to ``quantum_mm`` (default 1 mm), duplicate
    closing vertices and collinear points are dropped, the ring is made
    counter-clockwise and rotated to start at its smallest (x, y) vertex, then
    hashed with ``geometry_kernel.compute_face_content_hash``. The hash changes
    when the room changes shape; use ``RoomRecord.room_id`` for identity.
    """
    q = [(round(x / quantum_mm) * quantum_mm, round(y / quantum_mm) * quantum_mm) for x, y in vertices]
    if len(q) > 1 and q[0] == q[-1]:
        q = q[:-1]
    ring: List[Point] = []
    for p in q:
        if not ring or p != ring[-1]:
            ring.append(p)
    cleaned = []
    n = len(ring)
    for i in range(n):
        p0, p1, p2 = ring[i - 1], ring[i], ring[(i + 1) % n]
        if (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0]) != 0:
            cleaned.append(p1)
    ring = cleaned or ring
    if signed_area(ring) < 0:
        ring = ring[::-1]
    k = min(range(len(ring)), key=lambda i: ring[i])
    ring = ring[k:] + ring[:k]
    return compute_face_content_hash(ring)


def _is_outdoor(names: Sequence[str], words: Sequence[str]) -> bool:
    text = " ".join(names).lower()
    return any(w.lower() in text for w in words)


def _solve_rooms(net: g.WallNetwork, faces, seeds: Sequence[Tuple[str, Point]], cfg: RegularizeConfig,
                 diags: List[Diagnostic], previous: Optional[Mapping[str, Sequence[Point]]]) -> List[RoomRecord]:
    rooms: List[RoomRecord] = []
    for cycle, walls in faces:
        centerline = [net.nodes[n] for n in cycle]
        clear = inset_face(centerline, [net.walls[w].thickness / 2.0 for w in walls])
        if len(clear) < 3 or signed_area(clear) <= 0:
            _diag(diags, "room_collapsed", "a wall loop collapses when inset by wall thickness",
                  location=centerline[0])
            continue
        xs = [p[0] for p in clear]
        ys = [p[1] for p in clear]
        area = abs(signed_area(clear))
        if area < cfg.min_room_area_mm2 or min(max(xs) - min(xs), max(ys) - min(ys)) < cfg.min_clear_dimension_mm:
            _diag(diags, "room_too_small", f"skipped a {area / 1e6:.2f} m2 loop (shaft or wall pocket)",
                  location=centerline[0], severity="info")
            continue
        rooms.append(RoomRecord(centerline, clear, list(walls)))
    unmatched = []
    for name, seed in seeds:
        hosts = [r for r in rooms if point_in_polygon(seed, r.centerline)]
        if not hosts:
            unmatched.append(name)
            continue
        room = min(hosts, key=lambda r: r.area_mm2)
        n = len(room.clear)
        if min(_seg_dist(seed, room.clear[k], room.clear[(k + 1) % n]) for k in range(n)) <= cfg.boundary_seed_mm \
                or not point_in_polygon(seed, room.clear):
            room.flags.append(f"label '{name}' sits on the room boundary")
            _diag(diags, "label_on_boundary", f"label '{name}' is within {cfg.boundary_seed_mm:.0f} mm of a wall",
                  location=seed, severity="info")
        room.names.append(name)
    if unmatched:
        _diag(diags, "label_outside_rooms", "room labels outside every wall loop: " + ", ".join(unmatched))
    for room in rooms:
        if len(room.names) > 1:
            room.flags.append("conflicting labels")
            _diag(diags, "label_conflict", "one room carries several labels: " + " / ".join(room.names),
                  location=room.centerline[0])
        room.label_point = representative_point(room.clear)
        room.geometry_hash = canonical_face_hash(room.clear)
        room.outdoor = _is_outdoor(room.names, cfg.outdoor_words)
    unclassified = sum(1 for r in rooms if not r.names)
    if unclassified:
        _diag(diags, "room_unclassified", f"{unclassified} room(s) have no label and are left unclassified",
              severity="info")
    _assign_room_ids(rooms, previous)
    return rooms


def _assign_room_ids(rooms: List[RoomRecord], previous: Optional[Mapping[str, Sequence[Point]]]) -> None:
    """Persistent semantic ids: reuse a previous id when the previous room's
    interior point lies in this room and the areas agree within 2x; new rooms
    get an id derived from their first geometry hash."""
    used = set()
    prev = dict(previous or {})
    for room in sorted(rooms, key=lambda r: -r.area_mm2):
        chosen = None
        for rid, poly in prev.items():
            if rid in used or len(poly) < 3:
                continue
            area = abs(signed_area(list(poly)))
            if area <= 0 or not (0.5 <= room.area_mm2 / area <= 2.0):
                continue
            if point_in_polygon(representative_point(list(poly)), room.clear):
                chosen = rid
                break
        if chosen is None:
            base = "room-" + hashlib.sha1(room.geometry_hash.encode()).hexdigest()[:10]
            chosen, k = base, 1
            while chosen in used or chosen in prev:
                k += 1
                chosen = f"{base}-{k}"
        used.add(chosen)
        room.room_id = chosen


# ---------------------------------------------------------------- openings
def _host(op: COpening, net: g.WallNetwork, wall_ids: List[str], lineage, cfg: RegularizeConfig,
          diags: List[Diagnostic]) -> HostedOpening:
    out = HostedOpening(op.id, op.kind, op.width, door_type=op.door_type, hinge=op.hinge, swing=op.swing,
                        height=op.height, sill_height=op.sill_height, position=op.position,
                        confidence=op.confidence)
    sin_tol = math.sin(math.radians(cfg.opening_angle_deg))

    def evaluate(index: int, limit: float):
        w = net.walls[index]
        if w.type == "separator":
            return None
        u, length = _unit(net.nodes[w.a], net.nodes[w.b])
        if length <= 0:
            return None
        if op.host_direction is not None and abs(u[0] * op.host_direction[1] - u[1] * op.host_direction[0]) > sin_tol:
            return None
        p = net.nodes[w.a]
        s = (op.position[0] - p[0]) * u[0] + (op.position[1] - p[1]) * u[1]
        perp = abs((op.position[0] - p[0]) * u[1] - (op.position[1] - p[1]) * u[0])
        if perp > limit or s < -op.width / 2 or s > length + op.width / 2:
            return None
        inside = 0 <= s <= length
        return (0 if inside else 1, perp, index, s, length, u)

    candidates = []
    if op.host_edge is not None:
        for index, _s0, _s1 in lineage.get(op.host_edge, []):
            c = evaluate(index, max(net.walls[index].thickness, 50.0))
            if c:
                candidates.append(c)
        out.method = "lineage"
    if not candidates:
        for index in range(len(net.walls)):
            c = evaluate(index, min(cfg.opening_search_mm, max(net.walls[index].thickness, 50.0) * 2))
            if c:
                candidates.append(c)
        out.method = "fallback"
        candidates.sort()
        if len(candidates) > 1 and op.host_direction is None:
            a, b = candidates[0], candidates[1]
            parallel = abs(a[5][0] * b[5][1] - a[5][1] * b[5][0]) <= sin_tol
            if parallel and b[0] == a[0] and b[1] <= 1.5 * a[1] + 20 and abs(b[1] - a[1]) > 1.0:
                _diag(diags, "opening_host_ambiguous",
                      f"{op.kind} lies between parallel walls {wall_ids[a[2]]} and {wall_ids[b[2]]}; "
                      "chose the nearer", op.id, op.position)
    if not candidates:
        out.method = "rejected"
        _diag(diags, "opening_no_host", f"no wall under {op.kind}", op.id, op.position)
        return out
    candidates.sort()
    _, _perp, index, s, length, u = candidates[0]
    if op.width > length:
        out.method = "rejected"
        _diag(diags, "opening_wider_than_wall",
              f"{op.kind} {op.width:.0f} mm is wider than its {length:.0f} mm wall", op.id, op.position)
        return out
    start = s - op.width / 2
    clear = cfg.opening_end_clearance_mm
    lo, hi = clear, length - op.width - clear
    if hi < lo:
        lo = hi = (length - op.width) / 2
    if start < lo or start > hi:
        _diag(diags, "opening_clamped", f"{op.kind} moved {abs(min(max(start, lo), hi) - start):.0f} mm to fit "
              f"its {length:.0f} mm wall", op.id, op.position, "info")
        start = min(max(start, lo), hi)
    out.wall_index = index
    out.start_offset = start
    out.center_offset = start + op.width / 2
    out.reversed = op.host_direction is not None and (u[0] * op.host_direction[0] + u[1] * op.host_direction[1]) < 0
    if out.reversed:
        out.hinge = "end" if op.hinge == "start" else "start"
        out.swing = "right" if op.swing == "left" else "left"
    return out


def _host_all(plan: CanonicalPlan, net: g.WallNetwork, wall_ids, lineage, cfg, diags) -> List[HostedOpening]:
    hosted = [_host(op, net, wall_ids, lineage, cfg, diags) for op in plan.openings]
    occupied: Dict[int, List[Tuple[float, float, str]]] = {}
    for h in hosted:
        if not h.hosted:
            continue
        clash = next((o for o in occupied.get(h.wall_index, [])
                      if h.start_offset < o[1] and h.start_offset + h.width > o[0]), None)
        if clash is not None:
            _diag(diags, "opening_overlap", f"{h.kind} overlaps {clash[2]} on the same wall; not placed",
                  h.id, h.position)
            h.wall_index = None
            h.method = "rejected"
            continue
        occupied.setdefault(h.wall_index, []).append((h.start_offset, h.start_offset + h.width, h.id))
    return hosted


# ------------------------------------------------------------------ ids
def _wall_ids(net: g.WallNetwork) -> List[str]:
    """Deterministic ids from lineage: a wall made of one input edge keeps its
    id (``e3``; pieces of a split edge become ``e3.1``, ``e3.2`` in order along
    the edge); merged walls join their sources (``e3+e7``)."""
    pieces: Dict[str, List[int]] = {}
    for i, w in enumerate(net.walls):
        if len(w.sources) == 1:
            pieces.setdefault(next(iter(w.sources)), []).append(i)
    ids: List[str] = []
    seen = set()
    for i, w in enumerate(net.walls):
        if not w.sources:
            base = f"{w.provenance or 'w'}{i}"
        elif len(w.sources) == 1:
            src = next(iter(w.sources))
            group = pieces[src]
            base = src if len(group) == 1 else f"{src}.{sorted(group, key=lambda k: min(net.nodes[net.walls[k].a], net.nodes[net.walls[k].b])).index(i) + 1}"
        else:
            srcs = sorted(w.sources)
            base = "+".join(srcs[:4]) + (f"+{len(srcs) - 4}more" if len(srcs) > 4 else "")
        if w.provenance == "gap_repair" and w.sources:
            base += "~gap"
        cand, k = base, 1
        while cand in seen:
            k += 1
            cand = f"{base}#{k}"
        seen.add(cand)
        ids.append(cand)
    return ids


# --------------------------------------------------------------- entry point
def regularize_trace(
    trace: "TraceResult | dict | CanonicalPlan",
    config: Optional[RegularizeConfig] = None,
    *,
    previous_rooms: Optional[Mapping[str, Sequence[Point]]] = None,
) -> RegularizedPlan:
    """Run the shared regularization pipeline (see module docstring)."""
    cfg = config or RegularizeConfig()
    plan = trace if isinstance(trace, CanonicalPlan) else normalize(trace)
    diags: List[Diagnostic] = list(plan.diagnostics)
    if not plan.frame.calibrated:
        _diag(diags, "scale_uncalibrated",
              f"scale {plan.frame.mm_per_unit:.3f} mm/unit is a nominal guess"
              + (f" ({plan.frame.scale_source})" if plan.frame.scale_source else ""), severity="info")
    net = _to_network(plan)
    thick = sorted(w.thickness for w in net.walls if w.type != "separator") or [100.0]
    median = thick[len(thick) // 2]
    tol = cfg.snap_tolerance_mm or max(10.0, 0.5 * median)
    notes: List[g.Note] = []
    if net.walls:
        net = _clean(net, tol, cfg, notes)
        if cfg.repair_gaps:
            gap = cfg.gap_repair_mm if cfg.gap_repair_mm is not None else min(500.0, max(150.0, 1.5 * median))
            for _round in range(4):  # a repair can expose the next end (e.g. the third arm of a T)
                if not _repair_gaps(net, gap, diags):
                    break
                net = _clean(net, tol, cfg, notes)
    for code, message, loc in notes:
        _diag(diags, code, message, location=loc)

    faces = find_faces(net)
    seeds = list(plan.seeds)

    def outdoor_faces(faces_):
        result = set()
        for k, (cycle, _) in enumerate(faces_):
            poly = [net.nodes[n] for n in cycle]
            names = [name for name, p in seeds if point_in_polygon(p, poly)]
            if names and _is_outdoor(names, cfg.outdoor_words):
                result.add(k)
        return result

    if faces and _split_mixed(net, faces, outdoor_faces(faces), cfg):
        net = g.merge_nodes(net, 1e-6)
        faces = find_faces(net)

    wall_ids = _wall_ids(net)
    exposure = _exposure(net, faces, outdoor_faces(faces), cfg, diags, wall_ids) if net.walls else []
    # Resolve thickness and type from exposure (explicit values are kept).
    for i, w in enumerate(net.walls):
        e = exposure[i] if i < len(exposure) else "unknown"
        if w.type == "separator" or e not in ("internal", "external"):
            continue
        if w.thickness_source == "default":
            # Nominal wall: exposure decides both type and thickness.
            w.type = e
            w.thickness = cfg.default_external_mm if e == "external" else cfg.default_internal_mm
        elif w.type == "internal" and e == "external":
            # Measured / user thickness is kept; only the label follows the evidence.
            w.type = "external"
    dangling = _dangling(net)
    if dangling:
        _diag(diags, "dangling_wall_ends", f"{len(dangling)} wall end(s) connect to nothing", severity="info")

    lineage = _lineage(plan, net, tol)
    for eid, spans in lineage.items():
        if not spans:
            _diag(diags, "edge_absorbed", "input edge left no wall after cleanup (zero length or merged away)",
                  eid, severity="info")
    hosted = _host_all(plan, net, wall_ids, lineage, cfg, diags)
    rooms = _solve_rooms(net, faces, seeds, cfg, diags, previous_rooms)
    return RegularizedPlan(plan, net, wall_ids, exposure, _node_map(plan, net, tol), lineage, hosted, rooms,
                           faces, diags, cfg, tol)
