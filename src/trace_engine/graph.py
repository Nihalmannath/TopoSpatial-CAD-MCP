"""Wall-graph cleanup: turn raw recogniser output into a clean planar network.

Steps (all in millimetres, y-up):

1. ``to_mm``          - scale/flip trace coordinates.
2. ``merge_nodes``    - collapse nodes closer than the snap tolerance.
3. ``snap_axes``      - make near-horizontal / near-vertical walls exact by
                        sharing one x (or y) per connected axis group.
4. ``split_edges``    - split walls at T-junctions and at crossings so the
                        network is planar (every intersection is a node).
5. ``merge_colinear`` - join consecutive collinear walls through degree-2
                        nodes when type and thickness agree.

Lineage: every ``Wall`` carries ``sources`` - the ids of the input edges it
was built from. Merges union them, splits copy them, so after cleanup each
input edge can be followed to the walls that replaced it. The legacy compiler
still re-hosts openings geometrically; ``regularize.regularize_trace`` uses the
lineage first and geometry only as a bounded fallback.

Optional ``notes`` lists collect (code, message, location) tuples for
conflicts the steps resolve (e.g. duplicate walls with different explicit
thicknesses), so nothing is resolved silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .models import TraceResult

Point = Tuple[float, float]


@dataclass
class Wall:
    """One straight wall centerline in millimetres."""

    a: int
    b: int
    thickness: float
    type: str = "internal"
    sources: FrozenSet[str] = frozenset()
    thickness_source: str = "measured"
    provenance: Optional[str] = None


Note = Tuple[str, str, Optional[Point]]
EXPLICIT_THICKNESS = ("measured", "user_supplied")


@dataclass
class WallNetwork:
    """Cleaned planar wall network."""

    nodes: Dict[int, Point] = field(default_factory=dict)
    walls: List[Wall] = field(default_factory=list)

    def length(self, wall: Wall) -> float:
        return math.dist(self.nodes[wall.a], self.nodes[wall.b])

    def degree(self) -> Dict[int, int]:
        deg = {n: 0 for n in self.nodes}
        for wall in self.walls:
            deg[wall.a] += 1
            deg[wall.b] += 1
        return deg


class _UnionFind:
    def __init__(self, items: Iterable[int]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# ----------------------------------------------------------------- 1. units
def to_mm(trace: TraceResult) -> Tuple[WallNetwork, Dict[str, int]]:
    """Convert the trace graph to a millimetre, y-up ``WallNetwork``."""
    scale = trace.mm_per_unit if (trace.units == "px" and math.isfinite(trace.mm_per_unit) and trace.mm_per_unit > 0) else 1.0
    sign = -1.0 if trace.y_down else 1.0
    index: Dict[str, int] = {}
    network = WallNetwork()
    for i, node in enumerate(trace.walls_graph.nodes):
        index[node.id] = i
        x = node.x * scale if math.isfinite(node.x) else 0.0
        y = sign * node.y * scale if math.isfinite(node.y) else 0.0
        network.nodes[i] = (x, y)
    for k, edge in enumerate(trace.walls_graph.edges):
        if edge.source in index and edge.target in index:
            t = edge.thickness * scale if math.isfinite(edge.thickness) else 100.0
            network.walls.append(
                Wall(index[edge.source], index[edge.target], t, edge.type,
                     frozenset({edge.id or f"e{k}"}), edge.thickness_source, edge.provenance)
            )
    return network, index


def point_to_mm(point: Sequence[float], trace: TraceResult) -> Point:
    scale = trace.mm_per_unit if (trace.units == "px" and math.isfinite(trace.mm_per_unit) and trace.mm_per_unit > 0) else 1.0
    sign = -1.0 if trace.y_down else 1.0
    px = float(point[0]) * scale if len(point) > 0 and math.isfinite(point[0]) else 0.0
    py = sign * float(point[1]) * scale if len(point) > 1 and math.isfinite(point[1]) else 0.0
    return (px, py)


# ----------------------------------------------------------- 2. merge nodes
def merge_nodes(network: WallNetwork, tolerance: float, notes: Optional[List[Note]] = None) -> WallNetwork:
    """Collapse nodes within ``tolerance`` of each other (single linkage)."""
    ids = sorted(network.nodes)
    uf = _UnionFind(ids)
    # Grid bucketing keeps this near-linear for large traces.
    cell = max(tolerance, 1e-9) if (math.isfinite(tolerance) and tolerance > 0) else 10.0
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for n in ids:
        x, y = network.nodes[n]
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        key = (int(math.floor(x / cell)), int(math.floor(y / cell)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((key[0] + dx, key[1] + dy), []):
                    if math.dist(network.nodes[n], network.nodes[other]) <= tolerance:
                        uf.union(n, other)
        buckets.setdefault(key, []).append(n)
    groups: Dict[int, List[int]] = {}
    for n in ids:
        groups.setdefault(uf.find(n), []).append(n)
    merged = WallNetwork()
    remap: Dict[int, int] = {}
    for root, members in groups.items():
        xs = [network.nodes[m][0] for m in members]
        ys = [network.nodes[m][1] for m in members]
        merged.nodes[root] = (sum(xs) / len(xs), sum(ys) / len(ys))
        for m in members:
            remap[m] = root
    for wall in network.walls:
        a, b = remap[wall.a], remap[wall.b]
        if a != b:
            merged.walls.append(replace(wall, a=a, b=b))
    return _dedupe_walls(merged, notes)


def _dedupe_walls(network: WallNetwork, notes: Optional[List[Note]] = None) -> WallNetwork:
    best: Dict[Tuple[int, int], Wall] = {}
    for wall in network.walls:
        key = (min(wall.a, wall.b), max(wall.a, wall.b))
        kept = best.get(key)
        if kept is None or (wall.type == "separator") < (kept.type == "separator") or (
            (wall.type == "separator") == (kept.type == "separator") and wall.thickness > kept.thickness
        ):
            best[key] = wall
        if kept is not None:
            winner = best[key]
            loser = kept if winner is wall else wall
            if (
                notes is not None
                and winner.type != "separator" and loser.type != "separator"
                and winner.thickness_source in EXPLICIT_THICKNESS
                and loser.thickness_source in EXPLICIT_THICKNESS
                and abs(winner.thickness - loser.thickness) > 0.1 * max(winner.thickness, loser.thickness)
            ):
                pa, pb = network.nodes[key[0]], network.nodes[key[1]]
                notes.append((
                    "duplicate_wall_thickness_conflict",
                    f"duplicate walls {sorted(winner.sources)} / {sorted(loser.sources)} disagree on "
                    f"thickness ({winner.thickness:.0f} vs {loser.thickness:.0f} mm); kept the thicker",
                    ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2),
                ))
            best[key] = replace(
                winner,
                sources=winner.sources | loser.sources,
                type="external" if "external" in (wall.type, kept.type) else winner.type,
            )
    network.walls = [best[key] for key in sorted(best)]
    return network


# ------------------------------------------------------------- 3. snap axes
def snap_axes(network: WallNetwork, angle_tolerance_deg: float = 3.0) -> WallNetwork:
    """Make near-orthogonal walls exactly horizontal/vertical.

    Nodes joined by a near-vertical wall share one x; nodes joined by a
    near-horizontal wall share one y. Shared values are the group mean, so a
    long wall traced slightly skewed is straightened about its middle.
    """
    tol = math.radians(angle_tolerance_deg)
    ids = list(network.nodes)
    ux, uy = _UnionFind(ids), _UnionFind(ids)
    for wall in network.walls:
        (x1, y1), (x2, y2) = network.nodes[wall.a], network.nodes[wall.b]
        angle = math.atan2(y2 - y1, x2 - x1) % math.pi
        if min(angle, math.pi - angle) <= tol:
            uy.union(wall.a, wall.b)
        elif abs(angle - math.pi / 2) <= tol:
            ux.union(wall.a, wall.b)
    for uf, axis in ((ux, 0), (uy, 1)):
        groups: Dict[int, List[int]] = {}
        for n in ids:
            groups.setdefault(uf.find(n), []).append(n)
        for members in groups.values():
            if len(members) < 2:
                continue
            mean = sum(network.nodes[m][axis] for m in members) / len(members)
            for m in members:
                x, y = network.nodes[m]
                network.nodes[m] = (mean, y) if axis == 0 else (x, mean)
    return network


# ---------------------------------------------------------- 4. split edges
def _project(p: Point, a: Point, b: Point) -> Tuple[float, float]:
    """Return (t along a->b in [0..1] unclamped, perpendicular distance)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return 0.0, math.dist(p, a)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length_sq
    qx, qy = ax + t * dx, ay + t * dy
    return t, math.dist(p, (qx, qy))


def _intersection(a: Point, b: Point, c: Point, d: Point) -> Optional[Point]:
    """Proper intersection of segments ab and cd (interiors only)."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < 1e-9:
        return None
    qp = (c[0] - a[0], c[1] - a[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / denom
    u = (qp[0] * r[1] - qp[1] * r[0]) / denom
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def split_edges(network: WallNetwork, tolerance: float, notes: Optional[List[Note]] = None) -> WallNetwork:
    """Insert nodes at T-junctions and crossings so the graph is planar."""
    # Crossings: add a node at every proper intersection of two walls.
    next_id = max(network.nodes, default=-1) + 1
    walls = list(network.walls)
    for i in range(len(walls)):
        for j in range(i + 1, len(walls)):
            w1, w2 = walls[i], walls[j]
            if {w1.a, w1.b} & {w2.a, w2.b}:
                continue
            p = _intersection(
                network.nodes[w1.a], network.nodes[w1.b],
                network.nodes[w2.a], network.nodes[w2.b],
            )
            if p is not None and all(math.dist(p, q) > tolerance for q in network.nodes.values()):
                network.nodes[next_id] = p
                next_id += 1
    # T-junctions (and the crossing nodes just added): split every wall that
    # has a node lying on its interior.
    changed = True
    while changed:
        changed = False
        result: List[Wall] = []
        for wall in network.walls:
            a, b = network.nodes[wall.a], network.nodes[wall.b]
            length = math.dist(a, b)
            on_wall = []
            for n, p in network.nodes.items():
                if n in (wall.a, wall.b):
                    continue
                t, dist = _project(p, a, b)
                if dist <= tolerance and tolerance / length < t < 1 - tolerance / length:
                    on_wall.append((t, n))
            if not on_wall:
                result.append(wall)
                continue
            changed = True
            chain = [wall.a] + [n for _, n in sorted(on_wall)] + [wall.b]
            for u, v in zip(chain, chain[1:]):
                result.append(replace(wall, a=u, b=v))
        network.walls = result
    # Snap split nodes exactly onto their host line for straight geometry.
    return _dedupe_walls(network, notes)


# ------------------------------------------------------- 5. merge colinear
def merge_colinear(
    network: WallNetwork, angle_tolerance_deg: float = 1.0, thickness_ratio: float = 0.1,
    notes: Optional[List[Note]] = None,
) -> WallNetwork:
    """Join two walls meeting at a degree-2 node when they continue straight
    on with the same type and (within ``thickness_ratio``) the same thickness."""
    tol = math.radians(angle_tolerance_deg)
    kept_apart = set()
    changed = True
    while changed:
        changed = False
        incident: Dict[int, List[int]] = {}
        for idx, wall in enumerate(network.walls):
            incident.setdefault(wall.a, []).append(idx)
            incident.setdefault(wall.b, []).append(idx)
        for node, idxs in incident.items():
            if len(idxs) != 2:
                continue
            w1, w2 = network.walls[idxs[0]], network.walls[idxs[1]]
            if w1.type != w2.type:
                continue
            far1 = w1.b if w1.a == node else w1.a
            far2 = w2.b if w2.a == node else w2.a
            p, q, r = network.nodes[far1], network.nodes[node], network.nodes[far2]
            v1 = (q[0] - p[0], q[1] - p[1])
            v2 = (r[0] - q[0], r[1] - q[1])
            angle = abs(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))
            if angle > tol:
                continue
            if abs(w1.thickness - w2.thickness) > thickness_ratio * max(w1.thickness, w2.thickness):
                # Different explicit wall specifications on one straight run:
                # keep both walls and say so instead of silently averaging.
                if (notes is not None and node not in kept_apart
                        and w1.thickness_source in EXPLICIT_THICKNESS
                        and w2.thickness_source in EXPLICIT_THICKNESS):
                    kept_apart.add(node)
                    notes.append(("colinear_kept_apart",
                                  f"straight run kept as two walls: thickness {w1.thickness:.0f} vs "
                                  f"{w2.thickness:.0f} mm", q))
                continue
            thick = w1 if w1.thickness >= w2.thickness else w2
            merged = replace(
                thick, a=far1, b=far2, sources=w1.sources | w2.sources,
                thickness_source=_merge_source(w1.thickness_source, w2.thickness_source),
                provenance=w1.provenance if w1.provenance == w2.provenance else None,
            )
            network.walls = [w for k, w in enumerate(network.walls) if k not in idxs] + [merged]
            del network.nodes[node]
            changed = True
            break
    return network


def _merge_source(a: str, b: str) -> str:
    for kind in ("user_supplied", "measured", "default"):
        if kind in (a, b):
            return kind
    return a


def prune_isolated(network: WallNetwork) -> WallNetwork:
    used = {n for wall in network.walls for n in (wall.a, wall.b)}
    network.nodes = {n: p for n, p in network.nodes.items() if n in used}
    return network


def clean(trace: TraceResult, snap_tolerance_mm: Optional[float] = None) -> WallNetwork:
    """Run the full cleanup pipeline and return a planar mm network."""
    network, _ = to_mm(trace)
    if not network.walls or not network.nodes:
        return network
    thicknesses = sorted(w.thickness for w in network.walls if w.type != "separator") or [100.0]
    tolerance = snap_tolerance_mm or max(10.0, 0.5 * thicknesses[len(thicknesses) // 2])
    if not (math.isfinite(tolerance) and tolerance > 0):
        tolerance = 10.0
    network = merge_nodes(network, tolerance)
    network = snap_axes(network)
    network = merge_nodes(network, tolerance)
    network = split_edges(network, tolerance)
    # Junction nodes found while splitting carry the detector's noise; snap
    # again so every split piece is exactly on its wall's axis.
    network = snap_axes(network)
    network = merge_nodes(network, tolerance)
    network = merge_colinear(network)
    return prune_isolated(network)
