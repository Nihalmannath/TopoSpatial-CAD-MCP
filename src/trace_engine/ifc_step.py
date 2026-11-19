"""Minimal, dependency-free reader for IFC (ISO 10303-21 / STEP) files.

Only what the ground-truth builder needs: entity parsing, placement chains
and 2D plan footprints of swept solids / tessellations. It is written for
the regular IFC4 files produced by web-ifc based exporters (one entity per
statement, millimetre units), but parses general STEP syntax.

Nothing here depends on ifcopenshell, shapely or scipy, so it runs in the
MCP server's venv as-is.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

Point = Tuple[float, float]


# --------------------------------------------------------------- tokens
class Ref(int):
    """An entity reference ``#123``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"#{int(self)}"


class Enum(str):
    """An enumeration value ``.FOO.``."""


@dataclass
class Typed:
    """A typed parameter such as ``IFCLABEL('x')``."""

    type: str
    value: object


@dataclass
class Entity:
    id: int
    type: str
    args: list

    def __getitem__(self, i: int):
        return self.args[i] if i < len(self.args) else None


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_KW = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _decode_string(raw: str) -> str:
    """Decode STEP string escapes (\\X2\\...\\X0\\, \\X\\hh, '')."""
    s = raw.replace("''", "'")

    def x2(m: re.Match) -> str:
        hexs = m.group(1)
        return "".join(chr(int(hexs[i:i + 4], 16)) for i in range(0, len(hexs), 4))

    s = re.sub(r"\\X2\\([0-9A-Fa-f]*)\\X0\\", x2, s)
    s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    return s


def _parse_args(text: str, pos: int) -> Tuple[list, int]:
    """Parse a parenthesised parameter list starting at ``text[pos] == '('``."""
    assert text[pos] == "("
    pos += 1
    out: list = []
    n = len(text)
    while pos < n:
        c = text[pos]
        if c in " \t\r\n,":
            pos += 1
        elif c == ")":
            return out, pos + 1
        elif c == "(":
            sub, pos = _parse_args(text, pos)
            out.append(sub)
        elif c == "'":
            j = pos + 1
            while True:
                j = text.index("'", j)
                if j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                break
            out.append(_decode_string(text[pos + 1:j]))
            pos = j + 1
        elif c == "#":
            m = _NUM.match(text, pos + 1)
            out.append(Ref(int(m.group(0))))
            pos = m.end()
        elif c == "$" or c == "*":
            out.append(None)
            pos += 1
        elif c == ".":
            if pos + 1 < n and (text[pos + 1].isdigit()):
                m = _NUM.match(text, pos)
                out.append(float(m.group(0)))
                pos = m.end()
            else:
                j = text.index(".", pos + 1)
                out.append(Enum(text[pos + 1:j]))
                pos = j + 1
        elif c.isdigit() or c in "+-":
            m = _NUM.match(text, pos)
            tok = m.group(0)
            out.append(float(tok) if any(ch in tok for ch in ".eE") else int(tok))
            pos = m.end()
        elif c.isalpha():
            m = _KW.match(text, pos)
            name = m.group(0).upper()
            pos = m.end()
            while pos < n and text[pos] in " \t\r\n":
                pos += 1
            if pos < n and text[pos] == "(":
                sub, pos = _parse_args(text, pos)
                out.append(Typed(name, sub[0] if len(sub) == 1 else sub))
            else:
                out.append(Enum(name))
        else:
            raise ValueError(f"unexpected character {c!r} at {pos}")
    raise ValueError("unterminated parameter list")


def _statements(data: str) -> Iterator[str]:
    """Split the DATA section on ``;`` outside strings."""
    start = 0
    in_str = False
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == "'":
            if in_str and i + 1 < n and data[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
        elif c == ";" and not in_str:
            yield data[start:i]
            start = i + 1
        i += 1


_HEAD = re.compile(r"\s*#(\d+)\s*=\s*([A-Za-z0-9_]+)\s*\(")


class IfcFile:
    """Parsed IFC file: ``file[123]`` -> :class:`Entity`."""

    def __init__(self, text: str, path: str = "") -> None:
        self.path = path
        self.header = text[: text.find("DATA;")] if "DATA;" in text else ""
        body = text[text.find("DATA;") + 5:] if "DATA;" in text else text
        end = body.rfind("ENDSEC")
        body = body[:end] if end >= 0 else body
        self.entities: Dict[int, Entity] = {}
        self.by_type: Dict[str, List[Entity]] = {}
        for stmt in _statements(body):
            m = _HEAD.match(stmt)
            if not m:
                continue
            args, _ = _parse_args(stmt, m.end() - 1)
            ent = Entity(int(m.group(1)), m.group(2).upper(), args)
            self.entities[ent.id] = ent
            self.by_type.setdefault(ent.type, []).append(ent)
        self.length_scale = self._length_scale()
        self._placement_cache: Dict[int, np.ndarray] = {}

    @classmethod
    def open(cls, path: str | Path) -> "IfcFile":
        p = Path(path)
        return cls(p.read_text(encoding="latin-1"), str(p))

    def __getitem__(self, ref: int) -> Entity:
        return self.entities[int(ref)]

    def get(self, ref) -> Optional[Entity]:
        return None if ref is None else self.entities.get(int(ref))

    def of_type(self, *types: str) -> List[Entity]:
        out: List[Entity] = []
        for t in types:
            out.extend(self.by_type.get(t.upper(), []))
        return out

    @property
    def originating_system(self) -> str:
        m = re.search(r"FILE_NAME\((.*?)\);", self.header, re.S)
        return m.group(1) if m else ""

    # ---------------------------------------------------------- units
    def _length_scale(self) -> float:
        """Factor that converts file length units to millimetres."""
        prefixes = {"MILLI": 1.0, "CENTI": 10.0, "DECI": 100.0, None: 1000.0, "KILO": 1e6}
        for unit in self.of_type("IFCSIUNIT"):
            if unit[1] == "LENGTHUNIT":
                return prefixes.get(unit[2], 1.0)
        return 1.0

    # ------------------------------------------------------ placements
    def _axis2(self, ref) -> np.ndarray:
        """4x4 matrix of an IfcAxis2Placement3D/2D (identity if missing)."""
        m = np.eye(4)
        ent = self.get(ref)
        if ent is None:
            return m
        loc = self.get(ent[0])
        if loc is not None:
            c = list(loc[0]) + [0.0] * (3 - len(loc[0]))
            m[:3, 3] = np.array(c[:3], float) * self.length_scale
        if ent.type == "IFCAXIS2PLACEMENT2D":
            ref_dir = self._direction(ent[1], (1.0, 0.0, 0.0))
            z = np.array([0.0, 0.0, 1.0])
        else:
            z = self._direction(ent[1], (0.0, 0.0, 1.0))
            ref_dir = self._direction(ent[2], (1.0, 0.0, 0.0))
        z = z / (np.linalg.norm(z) or 1.0)
        x = ref_dir - np.dot(ref_dir, z) * z
        nx = np.linalg.norm(x)
        if nx < 1e-9:  # ref direction parallel to axis
            x = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
            x = x - np.dot(x, z) * z
            nx = np.linalg.norm(x)
        x = x / nx
        y = np.cross(z, x)
        m[:3, 0], m[:3, 1], m[:3, 2] = x, y, z
        return m

    def _direction(self, ref, default) -> np.ndarray:
        ent = self.get(ref)
        if ent is None:
            return np.array(default, float)
        v = list(ent[0]) + [0.0] * (3 - len(ent[0]))
        return np.array(v[:3], float)

    def placement(self, ref) -> np.ndarray:
        """World matrix of an IfcLocalPlacement chain."""
        if ref is None:
            return np.eye(4)
        key = int(ref)
        if key in self._placement_cache:
            return self._placement_cache[key]
        ent = self[key]
        if ent.type != "IFCLOCALPLACEMENT":
            m = np.eye(4)
        else:
            parent = self.placement(ent[0]) if ent[0] is not None else np.eye(4)
            m = parent @ self._axis2(ent[1])
        self._placement_cache[key] = m
        return m

    # -------------------------------------------------------- geometry
    def _curve_points(self, ref) -> List[Point]:
        ent = self.get(ref)
        if ent is None:
            return []
        if ent.type == "IFCPOLYLINE":
            pts = []
            for r in ent[0]:
                c = self[r][0]
                pts.append((float(c[0]) * self.length_scale, float(c[1]) * self.length_scale))
            return pts
        if ent.type == "IFCINDEXEDPOLYCURVE":
            plist = self[ent[0]][0]
            return [(float(p[0]) * self.length_scale, float(p[1]) * self.length_scale) for p in plist]
        return []

    def _profile(self, ref) -> List[List[Point]]:
        """Outer (and inner) loops of a profile in profile coordinates."""
        ent = self.get(ref)
        if ent is None:
            return []
        if ent.type in ("IFCARBITRARYCLOSEDPROFILEDEF", "IFCARBITRARYPROFILEDEFWITHVOIDS"):
            return [self._curve_points(ent[2])]
        if ent.type == "IFCRECTANGLEPROFILEDEF":
            m = self._axis2(ent[2])
            hx, hy = float(ent[3]) * self.length_scale / 2, float(ent[4]) * self.length_scale / 2
            pts = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
            return [[tuple((m @ np.array([x, y, 0, 1]))[:2]) for x, y in pts]]
        return []

    def footprints(self, product: Entity) -> List[List[Point]]:
        """Plan (XY) outlines of every body item of a product, in world mm.

        Vertical extrusions give their profile polygon; anything else
        (sloped extrusion, tessellation) gives the convex hull of its
        projected vertices.
        """
        world = self.placement(product[5])
        shape = self.get(product[6])
        out: List[List[Point]] = []
        if shape is None:
            return out
        for rep_ref in shape[2] or []:
            rep = self[rep_ref]
            for item_ref in rep[3] or []:
                out.extend(self._item_footprints(self[item_ref], world))
        return out

    def _item_footprints(self, item: Entity, world: np.ndarray) -> List[List[Point]]:
        if item.type == "IFCEXTRUDEDAREASOLID":
            m = world @ self._axis2(item[1])
            loops = self._profile(item[0])
            if not loops or not loops[0]:
                return []
            d = self._direction(item[2], (0, 0, 1))
            d_world = m[:3, :3] @ d
            pts = loops[0]
            if pts and pts[0] == pts[-1]:
                pts = pts[:-1]
            base = [tuple((m @ np.array([x, y, 0.0, 1.0]))[:2]) for x, y in pts]
            horizontal = math.hypot(d_world[0], d_world[1]) / (np.linalg.norm(d_world) or 1.0)
            if horizontal < 1e-3:
                return [base]
            depth = float(item[3]) * self.length_scale
            shift = d_world / (np.linalg.norm(d_world) or 1.0) * depth
            top = [tuple((m @ np.array([x, y, 0.0, 1.0]))[:2] + shift[:2]) for x, y in pts]
            return [convex_hull(base + top)]
        if item.type in ("IFCTRIANGULATEDFACESET", "IFCPOLYGONALFACESET"):
            coords = self[item[0]][0]
            pts = [tuple((world @ np.array([c[0] * self.length_scale, c[1] * self.length_scale,
                                             c[2] * self.length_scale, 1.0]))[:2]) for c in coords]
            return [convex_hull(pts)] if len(pts) >= 3 else []
        if item.type == "IFCMAPPEDITEM":
            source = self[item[0]]
            origin = self._axis2(source[0])
            target = self.get(item[1])
            t = np.eye(4)
            if target is not None and target.type.startswith("IFCCARTESIANTRANSFORMATIONOPERATOR"):
                t = self._operator(target)
            rep = self[source[1]]
            out = []
            for sub in rep[3] or []:
                out.extend(self._item_footprints(self[sub], world @ t @ origin))
            return out
        if item.type == "IFCBOOLEANCLIPPINGRESULT" or item.type == "IFCBOOLEANRESULT":
            first = self.get(item[1])
            return self._item_footprints(first, world) if first is not None else []
        return []

    def _operator(self, op: Entity) -> np.ndarray:
        m = np.eye(4)
        x = self._direction(op[0], (1, 0, 0))
        y = self._direction(op[1], (0, 1, 0))
        origin = self.get(op[2])
        scale = float(op[3]) if isinstance(op[3], (int, float)) else 1.0
        z = np.cross(x, y)
        m[:3, 0], m[:3, 1], m[:3, 2] = x * scale, y * scale, z * scale
        if origin is not None:
            c = list(origin[0]) + [0.0] * (3 - len(origin[0]))
            m[:3, 3] = np.array(c[:3]) * self.length_scale
        return m


# ------------------------------------------------------------ helpers
def convex_hull(points: Sequence[Point]) -> List[Point]:
    """Monotone-chain convex hull (counter-clockwise)."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return list(pts)

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
    return lower[:-1] + upper[:-1]


def polygon_area(poly: Sequence[Point]) -> float:
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def iter_refs(value) -> Iterable[int]:
    if isinstance(value, Ref):
        yield int(value)
    elif isinstance(value, list):
        for v in value:
            yield from iter_refs(v)
