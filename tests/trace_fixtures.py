"""Render the reference plan as a brochure-style raster for tracer tests.

Solid dark walls with real door gaps, quarter-circle door swings, glazed
window bands, outlined furniture, room names with "W x D" size labels and a
dimension line: the clutter a tracer must ignore.
"""

from __future__ import annotations

import math
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

from test_trace_engine import DOORS, EXT, INT, ROOMS, WALLS, WINDOWS

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _get_font(size: int = 18):
    for f in ("arial.ttf", "calibri.ttf", FONT):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_reference_plan(mm_per_px: float = 10.0, margin_px: int = 120) -> Tuple[Image.Image, Dict]:
    width_mm, height_mm = 15240, 12192
    w = int(width_mm / mm_per_px) + 2 * margin_px
    h = int(height_mm / mm_per_px) + 2 * margin_px
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)

    def px(p):  # mm (y-up) -> px (y-down)
        return (margin_px + p[0] / mm_per_px, margin_px + (height_mm - p[1]) / mm_per_px)

    # Openings to cut out of each wall, in mm along the wall.
    cuts: Dict[str, list] = {}
    for wall, off, width in DOORS + WINDOWS:
        cuts.setdefault(wall, []).append((off, off + width))

    for name, (a, b, t) in WALLS.items():
        length = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        nx, ny = -uy, ux
        ext = t / 2  # extend through corners so joints are solid
        spans, cursor = [], -ext
        for s, e in sorted(cuts.get(name, [])):
            spans.append((cursor, s))
            cursor = e
        spans.append((cursor, length + ext))
        for s, e in spans:
            p0 = (a[0] + ux * s, a[1] + uy * s)
            p1 = (a[0] + ux * e, a[1] + uy * e)
            poly = [
                (p0[0] + nx * t / 2, p0[1] + ny * t / 2), (p1[0] + nx * t / 2, p1[1] + ny * t / 2),
                (p1[0] - nx * t / 2, p1[1] - ny * t / 2), (p0[0] - nx * t / 2, p0[1] - ny * t / 2),
            ]
            d.polygon([px(p) for p in poly], fill=(60, 60, 60))

    # Windows: two thin glazing lines inside the gap.
    for wall, off, width in WINDOWS:
        a, b, t = WALLS[wall]
        length = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        nx, ny = -uy, ux
        for k in (-0.25, 0.25):
            p0 = (a[0] + ux * off + nx * t * k, a[1] + uy * off + ny * t * k)
            p1 = (a[0] + ux * (off + width) + nx * t * k, a[1] + uy * (off + width) + ny * t * k)
            d.line([px(p0), px(p1)], fill=(90, 90, 90), width=1)

    # Doors: leaf + quarter swing from the hinge (start of the opening),
    # swinging to the wall's left-hand side.
    for wall, off, width in DOORS:
        a, b, t = WALLS[wall]
        length = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        nx, ny = -uy, ux
        hinge = (a[0] + ux * off, a[1] + uy * off)
        leaf_end = (hinge[0] + nx * width, hinge[1] + ny * width)
        d.line([px(hinge), px(leaf_end)], fill=(30, 30, 30), width=2)
        pts = []
        for k in range(31):
            ang = k / 30 * math.pi / 2
            v = (ux * math.cos(ang) + nx * math.sin(ang), uy * math.cos(ang) + ny * math.sin(ang))
            pts.append(px((hinge[0] + v[0] * width, hinge[1] + v[1] * width)))
        d.line(pts, fill=(30, 30, 30), width=1)

    # Furniture outlines and labels (clutter).
    font = _get_font(18)
    small = _get_font(14)
    for name, (xl, xr, yb, yt) in ROOMS.items():
        cx, cy = (xl + xr) / 2, (yb + yt) / 2
        if (xr - xl) > 3000 and (yt - yb) > 3000:
            d.rectangle([px((xl + 400, yt - 400)), px((xl + 2000, yt - 2400))], outline=(120, 120, 160), width=1)
        label = name.upper()
        tw = d.textlength(label, font=font)
        d.text((px((cx, cy))[0] - tw / 2, px((cx, cy))[1] - 22), label, fill=(20, 20, 20), font=font)
        size = f"{round((xr - xl) / 10)} x {round((yt - yb) / 10)}"
        sw = d.textlength(size, font=small)
        d.text((px((cx, cy))[0] - sw / 2, px((cx, cy))[1] + 2), size, fill=(20, 20, 20), font=small)

    # Overall dimension line under the plan.
    y = px((0, -800))[1]
    d.line([px((0, -800)), px((width_mm, -800))], fill=(0, 0, 0), width=1)
    d.text((px((width_mm / 2, -800))[0] - 30, y + 4), "15240", fill=(0, 0, 0), font=small)

    truth = {
        "mm_per_px": mm_per_px,
        "origin_px": px((0, 0)),
        "walls": WALLS,
        "rooms": ROOMS,
        "doors": DOORS,
        "windows": WINDOWS,
        "thickness": {"external": EXT, "internal": INT},
    }
    return img, truth


if __name__ == "__main__":
    image, _ = render_reference_plan()
    image.save("/tmp/reference_plan.png")
    print(image.size)
