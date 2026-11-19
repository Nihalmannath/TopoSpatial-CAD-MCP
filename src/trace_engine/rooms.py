"""Room recovery: closed cycles of the wall network, inset to clear interiors.

Rooms are never taken from pixels. Every bounded face of the planar wall
network is a room; its clear boundary is the centerline face pushed inwards
by half of each bounding wall's thickness; its name comes from whichever
zone seed points fall inside it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .graph import Wall, WallNetwork

Point = Tuple[float, float]


@dataclass
class Room:
    centerline: List[Point]
    clear: List[Point]
    wall_indices: List[int]
    names: List[str] = field(default_factory=list)

    @property
    def area_mm2(self) -> float:
        return abs(signed_area(self.clear))

    @property
    def label(self) -> str:
        return " / ".join(self.names)


def signed_area(polygon: Sequence[Point]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, list(polygon[1:]) + [polygon[0]]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def _without_dangling(network: WallNetwork) -> List[int]:
    """Indices of walls that can bound a room (iteratively drop stubs)."""
    alive = set(range(len(network.walls)))
    while True:
        degree: Dict[int, int] = {}
        for i in alive:
            w = network.walls[i]
            degree[w.a] = degree.get(w.a, 0) + 1
            degree[w.b] = degree.get(w.b, 0) + 1
        stubs = {
            i for i in alive
            if degree[network.walls[i].a] == 1 or degree[network.walls[i].b] == 1
        }
        if not stubs:
            return sorted(alive)
        alive -= stubs


def find_faces(network: WallNetwork) -> List[Tuple[List[int], List[int]]]:
    """Return bounded faces as (node cycle, wall index per edge), CCW."""
    usable = _without_dangling(network)
    out: Dict[int, List[Tuple[float, int, int]]] = {}
    for i in usable:
        w = network.walls[i]
        for u, v in ((w.a, w.b), (w.b, w.a)):
            (x1, y1), (x2, y2) = network.nodes[u], network.nodes[v]
            out.setdefault(u, []).append((math.atan2(y2 - y1, x2 - x1), v, i))
    for edges in out.values():
        edges.sort()
    visited = set()
    faces = []
    for u, edges in out.items():
        for _, v, wi in edges:
            if (u, v) in visited:
                continue
            cycle, walls = [], []
            a, b, w = u, v, wi
            for _guard in range(10000):
                if (a, b) in visited:
                    break
                visited.add((a, b))
                cycle.append(a)
                walls.append(w)
                # At b, take the next edge clockwise from the reverse edge b->a:
                # that keeps the face on the left (CCW bounded faces).
                (bx, by), (ax, ay) = network.nodes[b], network.nodes[a]
                back = math.atan2(ay - by, ax - bx)
                candidates = out[b]
                k = max(
                    (j for j, (ang, _, _) in enumerate(candidates) if ang < back - 1e-12),
                    default=len(candidates) - 1,
                )
                _, c, wc = candidates[k]
                a, b, w = b, c, wc
            if len(cycle) >= 3:
                poly = [network.nodes[n] for n in cycle]
                if signed_area(poly) > 0:
                    faces.append((cycle, walls))
    return faces


def _line_intersection(p1: Point, d1: Point, p2: Point, d2: Point) -> Optional[Point]:
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denom) < 1e-9:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / denom
    return (p1[0] + t * d1[0], p1[1] + t * d1[1])


def inset_face(points: List[Point], half_widths: List[float]) -> List[Point]:
    """Offset a CCW polygon inwards; edge i (points[i]->points[i+1]) moves by
    half_widths[i]. Collinear neighbours of equal width are merged; collinear
    neighbours of different width get a small step."""
    # A zero-length edge (two coincident corners) has no direction: drop it.
    keep = [i for i in range(len(points)) if math.dist(points[i], points[(i + 1) % len(points)]) > 1e-9]
    if len(keep) < 3:
        return []
    points = [points[i] for i in keep]
    half_widths = [half_widths[i] for i in keep]
    n = len(points)
    lines = []
    for i in range(n):
        (x1, y1), (x2, y2) = points[i], points[(i + 1) % n]
        length = math.hypot(x2 - x1, y2 - y1)
        d = ((x2 - x1) / length, (y2 - y1) / length)
        normal = (-d[1], d[0])  # left of travel = inside for a CCW face
        h = half_widths[i]
        lines.append(((x1 + normal[0] * h, y1 + normal[1] * h), d, normal, h))
    result: List[Point] = []
    for i in range(n):
        prev = lines[i - 1]
        cur = lines[i]
        p = _line_intersection(prev[0], prev[1], cur[0], cur[1])
        if p is not None:
            result.append(p)
            continue
        # Collinear: same direction -> either nothing (equal width) or a step.
        if abs(prev[3] - cur[3]) < 1e-6:
            continue
        corner = points[i]
        result.append((corner[0] + prev[2][0] * prev[3], corner[1] + prev[2][1] * prev[3]))
        result.append((corner[0] + cur[2][0] * cur[3], corner[1] + cur[2][1] * cur[3]))
    return _drop_colinear(result)


def _drop_colinear(points: List[Point], eps: float = 1e-6) -> List[Point]:
    cleaned = []
    n = len(points)
    for i in range(n):
        p0, p1, p2 = points[i - 1], points[i], points[(i + 1) % n]
        cross = (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])
        if abs(cross) > eps * max(1.0, math.dist(p0, p1) * math.dist(p1, p2)) and math.dist(p0, p1) > eps:
            cleaned.append(p1)
    return cleaned


def recover_rooms(
    network: WallNetwork,
    zones: Sequence[Tuple[str, Point]] = (),
    min_area_mm2: float = 500_000.0,
    min_clear_dimension_mm: float = 500.0,
) -> Tuple[List[Room], List[str]]:
    """Return rooms (named from zone seeds) and warnings."""
    warnings: List[str] = []
    rooms: List[Room] = []
    for cycle, walls in find_faces(network):
        centerline = [network.nodes[n] for n in cycle]
        clear = inset_face(centerline, [network.walls[w].thickness / 2.0 for w in walls])
        if len(clear) < 3 or signed_area(clear) <= 0:
            warnings.append("Skipped a wall loop that collapses when inset by wall thickness")
            continue
        xs = [p[0] for p in clear]
        ys = [p[1] for p in clear]
        if abs(signed_area(clear)) < min_area_mm2 or min(max(xs) - min(xs), max(ys) - min(ys)) < min_clear_dimension_mm:
            warnings.append(
                f"Skipped a {abs(signed_area(clear)) / 1e6:.2f} m2 loop (shaft or wall pocket)"
            )
            continue
        rooms.append(Room(centerline, clear, walls))
    unmatched = []
    for name, seed in zones:
        hosts = [room for room in rooms if point_in_polygon(seed, room.centerline)]
        if not hosts:
            unmatched.append(name)
            continue
        # Nested faces cannot occur in a planar face set, but guard anyway.
        min(hosts, key=lambda r: r.area_mm2).names.append(name)
    if unmatched:
        warnings.append("Room labels outside every wall loop: " + ", ".join(unmatched))
    unnamed = 0
    for room in sorted(rooms, key=lambda r: (-r.clear[0][1], r.clear[0][0])):
        if not room.names:
            unnamed += 1
            room.names.append(f"Space {unnamed}")
    if unnamed:
        warnings.append(f"{unnamed} wall loop(s) had no room label and were named 'Space N'")
    return rooms, warnings
