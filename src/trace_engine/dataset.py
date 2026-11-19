"""Turn a traced-and-corrected drawing into a CubiCasa-style training example.

Workflow: ``trace_plan`` writes ``<image>_align.json`` (pixel <-> drawing
transform). You fix the drawing in AutoCAD. ``export_training_example``
reads walls, doors, windows, room outlines and room names back from the
drawing and rasterises them in the image's pixel frame:

* ``rooms.png``  - uint8 class map, CubiCasa room classes (Wall = 2)
* ``icons.png``  - uint8 class map, CubiCasa icon classes (Window = 1, Door = 2)
* ``image.png``  - the plan image
* ``meta.json``  - counts, scale, source

The geometry here is plain data, so this module is testable without CAD.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .neural import ICON_CLASSES, ROOM_CLASSES

Point = Tuple[float, float]

# Name keywords -> CubiCasa room class. First match wins.
_ROOM_RULES = [
    (r"toilet|bath|wc|wash|powder", "Bath"),
    (r"kitchen|pantry|work ?area|utility", "Kitchen"),
    (r"bed|master", "Bed Room"),
    (r"living|drawing|dining|lounge|family|hall|swing|formal", "Living Room"),
    (r"sit ?out|verandah|veranda|porch|balcony|terrace|deck|court", "Outdoor"),
    (r"entr|foyer|lobby|passage|corridor", "Entry"),
    (r"store|dress|closet|wardrobe|laundry|l ?au ?ndry", "Storage"),
    (r"garage|car ?porch|parking", "Garage"),
]


def room_class(name: str) -> int:
    text = (name or "").lower()
    for pattern, cls in _ROOM_RULES:
        if re.search(pattern, text):
            return ROOM_CLASSES.index(cls)
    return ROOM_CLASSES.index("Undefined")


@dataclass
class Alignment:
    """Drawing mm (y-up) <-> image px (y-down), as written by trace_plan."""

    mm_per_px: float
    offset_mm: Tuple[float, float]
    image_size: Tuple[int, int]  # width, height
    image: str = ""
    # Optional full 2x3 drawing-mm -> image-px transform (rotation / mirror),
    # as written by trace_engine.register. Overrides mm_per_px + offset_mm.
    matrix: Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = None

    def to_px(self, p: Sequence[float]) -> Point:
        if self.matrix is not None:
            m = self.matrix
            return (m[0][0] * p[0] + m[0][1] * p[1] + m[0][2], m[1][0] * p[0] + m[1][1] * p[1] + m[1][2])
        return ((p[0] - self.offset_mm[0]) / self.mm_per_px, -(p[1] - self.offset_mm[1]) / self.mm_per_px)

    @classmethod
    def load(cls, path: Path) -> "Alignment":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        matrix = data.get("mm_to_px")
        offset = data.get("offset_mm") or (0.0, 0.0)
        return cls(float(data["mm_per_px"]), tuple(offset), tuple(data["image_size"]), data.get("image", ""),
                   tuple(tuple(float(v) for v in row) for row in matrix) if matrix else None)


@dataclass
class DrawingContent:
    """What the export reads from CAD (all in drawing mm)."""

    walls: List[Tuple[Point, Point, float]] = field(default_factory=list)  # start, end, width
    openings: List[Tuple[str, Point, Point]] = field(default_factory=list)  # kind, bbox min, bbox max
    rooms: List[List[Point]] = field(default_factory=list)  # closed outlines
    texts: List[Tuple[str, Point]] = field(default_factory=list)


def _wall_quad(a: Point, b: Point, width: float) -> List[Point]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * width / 2, dx / length * width / 2
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


def _opening_quad(lo: Point, hi: Point, walls: Sequence[Tuple[Point, Point, float]]) -> Optional[List[Point]]:
    """Clip an opening's extents (door extents include the swing) to the wall
    band that hosts it: the part of the host wall between the extents."""
    best = None
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    for a, b, width in walls:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length
        corners = [(lo[0], lo[1]), (hi[0], lo[1]), (lo[0], hi[1]), (hi[0], hi[1])]
        s = [(x - a[0]) * ux + (y - a[1]) * uy for x, y in corners]
        d = [(x - a[0]) * -uy + (y - a[1]) * ux for x, y in corners]
        s0, s1 = max(0.0, min(s)), min(length, max(s))
        # the wall centreline must pass through the opening extents
        if s1 - s0 < 200 or min(d) > width / 2 or max(d) < -width / 2:
            continue
        dist = abs((cx - a[0]) * -uy + (cy - a[1]) * ux)
        if best is None or dist < best[0]:
            p0 = (a[0] + ux * s0, a[1] + uy * s0)
            p1 = (a[0] + ux * s1, a[1] + uy * s1)
            best = (dist, _wall_quad(p0, p1, width * 1.05))
    return None if best is None else best[1]


def _point_in(poly: Sequence[Point], p: Point) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > p[1]) != (yj > p[1]) and p[0] < (xj - xi) * (p[1] - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def rasterize(content: DrawingContent, align: Alignment) -> Tuple[np.ndarray, np.ndarray, Dict[str, int]]:
    """Return (room class map, icon class map, counts) in image pixels."""
    from PIL import Image, ImageDraw

    w, h = align.image_size
    rooms_img = Image.new("L", (w, h), ROOM_CLASSES.index("Background"))
    icons_img = Image.new("L", (w, h), ICON_CLASSES.index("No Icon"))
    rd, idr = ImageDraw.Draw(rooms_img), ImageDraw.Draw(icons_img)
    px = lambda pts: [align.to_px(p) for p in pts]  # noqa: E731

    named = 0
    for outline in content.rooms:
        names = [t for t, pos in content.texts if _point_in(outline, pos)]
        cls = room_class(" ".join(names)) if names else ROOM_CLASSES.index("Undefined")
        named += bool(names)
        rd.polygon(px(outline), fill=cls)
    for a, b, width in content.walls:
        rd.polygon(px(_wall_quad(a, b, width)), fill=ROOM_CLASSES.index("Wall"))
    placed = {"window": 0, "door": 0}
    for kind, lo, hi in content.openings:
        quad = _opening_quad(lo, hi, content.walls)
        if quad is None:
            continue
        idr.polygon(px(quad), fill=ICON_CLASSES.index("Window" if kind == "window" else "Door"))
        placed[kind] += 1
    counts = {"walls": len(content.walls), "rooms": len(content.rooms), "named_rooms": named,
              "windows": placed["window"], "doors": placed["door"]}
    return np.asarray(rooms_img), np.asarray(icons_img), counts


def write_example(content: DrawingContent, align: Alignment, image_path: str, out_root: Path,
                  name: Optional[str] = None) -> Dict[str, object]:
    from PIL import Image

    rooms, icons, counts = rasterize(content, align)
    name = name or Path(image_path).stem
    folder = Path(out_root) / name
    folder.mkdir(parents=True, exist_ok=True)
    Image.open(image_path).convert("RGB").save(folder / "image.png")
    Image.fromarray(rooms).save(folder / "rooms.png")
    Image.fromarray(icons).save(folder / "icons.png")
    # Colour preview for eyeballing the labels.
    palette = np.array([[255, 255, 255], [200, 230, 200], [30, 30, 30], [250, 200, 120], [180, 210, 250],
                        [230, 180, 230], [150, 220, 230], [240, 240, 150], [120, 120, 120], [210, 180, 150],
                        [170, 170, 200], [220, 220, 220]], np.uint8)
    preview = palette[np.clip(rooms, 0, len(palette) - 1)]
    preview[icons == ICON_CLASSES.index("Window")] = (0, 160, 255)
    preview[icons == ICON_CLASSES.index("Door")] = (0, 170, 0)
    Image.fromarray(preview).save(folder / "preview.png")
    meta = {"source_image": str(image_path), "mm_per_px": align.mm_per_px, "counts": counts}
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return {"folder": str(folder), **counts}
