"""Observable quality indicators for a compiled trace (no ground truth needed).

``assess`` replaces the old "how many rooms did we find" engine score in
``trace_plan``. Each indicator is something you can see in the result:

* ``dangling_ratio``      - wall ends connected to nothing / all wall ends
* ``face_coverage``       - area enclosed by recovered rooms / footprint hull area
* ``opening_host_rate``   - openings placed on a wall / openings reported
* ``unresolved_overlaps`` - openings dropped because they overlap another
* ``size_agreement``      - for named rooms with a printed size (``room_sizes``):
                            1 - mean relative error of the clear width x depth
* ``short_wall_ratio``    - walls shorter than their own thickness (noise)

``score`` combines them into 0..1. During normal inference it is a heuristic
for choosing between engines, not proof of correctness; offline, use
``scripts/eval_trace.py`` against ground truth.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def _hull_area(points: Sequence[Point]) -> float:
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(hull[i][0] * hull[(i + 1) % len(hull)][1] - hull[(i + 1) % len(hull)][0] * hull[i][1]
                   for i in range(len(hull)))) / 2.0


def _polygon_area(poly: Sequence[Point]) -> float:
    n = len(poly)
    return abs(sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))) / 2.0


def assess(compiled: Any, room_sizes: Optional[Sequence[Tuple[str, float, float]]] = None,
           openings_reported: Optional[int] = None) -> Dict[str, Any]:
    """Quality indicators for a ``CompiledTrace`` (legacy or canonical)."""
    net = compiled.network
    walls = [w for w in net.walls]
    deg: Dict[int, int] = {}
    for w in walls:
        deg[w.a] = deg.get(w.a, 0) + 1
        deg[w.b] = deg.get(w.b, 0) + 1
    ends = sum(deg.values())
    dangling = sum(1 for d in deg.values() if d == 1)
    hull = _hull_area(list(net.nodes.values()))
    enclosed = sum(_polygon_area(r.centerline) for r in compiled.rooms)
    placed = sum(1 for c in compiled.changes if c["@type"] in ("top:Door", "top:Window"))
    if openings_reported is None:
        reg = getattr(compiled, "regularized", None)
        openings_reported = len(reg.openings) if reg is not None else placed + sum(
            1 for w in compiled.warnings if " skipped:" in w)
    overlaps = sum(1 for d in compiled.diagnostics if d.get("code") == "opening_overlap") + sum(
        1 for w in compiled.warnings if "overlaps another opening" in w)
    short = sum(1 for w in walls if w.type != "separator"
                and math.dist(net.nodes[w.a], net.nodes[w.b]) < max(w.thickness, 1.0))
    real = sum(1 for w in walls if w.type != "separator")

    agreement = None
    if room_sizes:
        errors = []
        for name, w_cm, d_cm in room_sizes:
            room = next((r for r in compiled.rooms if name.lower() in r.label.lower()), None)
            if room is None:
                continue
            xs = [p[0] for p in room.clear]
            ys = [p[1] for p in room.clear]
            got = sorted((max(xs) - min(xs), max(ys) - min(ys)))
            want = sorted((w_cm * 10.0, d_cm * 10.0))
            errors.append(sum(abs(g - t) / t for g, t in zip(got, want)) / 2)
        agreement = max(0.0, 1.0 - sum(errors) / len(errors)) if errors else 0.0

    ind = {
        "dangling_ratio": round(dangling / ends, 3) if ends else 1.0,
        "face_coverage": round(min(1.0, enclosed / hull), 3) if hull > 0 else 0.0,
        "opening_host_rate": round(placed / openings_reported, 3) if openings_reported else None,
        "unresolved_overlaps": overlaps,
        "size_agreement": None if agreement is None else round(agreement, 3),
        "short_wall_ratio": round(short / real, 3) if real else 0.0,
        "rooms": len(compiled.rooms),
        "walls": real,
    }
    parts = [(1.0 - ind["dangling_ratio"], 1.0), (ind["face_coverage"], 2.0), (1.0 - ind["short_wall_ratio"], 0.5)]
    if ind["opening_host_rate"] is not None:
        parts.append((ind["opening_host_rate"], 1.0))
    if agreement is not None:
        parts.append((agreement, 2.0))
    score = sum(v * w for v, w in parts) / sum(w for _, w in parts) if real else 0.0
    ind["score"] = round(score, 3)
    return ind
