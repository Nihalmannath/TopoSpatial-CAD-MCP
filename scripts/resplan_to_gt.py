"""Test B data: render ResPlan plans as images with exact ground truth.

ResPlan (https://github.com/m-agour/ResPlan, data CC BY 4.0) has vector plans
but no images. This draws each plan as a clean floor-plan image (black walls,
door leaves and swings, glazed windows, room names) and writes the answer in
the format ``scripts/eval_trace.py`` already scores:

    dataset/resplan_gt/<id>/image.png
    dataset/resplan_gt/<id>/graph.json   graph_px: wall centrelines, openings, room zones
    dataset/resplan_gt/<id>/meta.json    px_per_mm, split=test, annotation_complete

Wall centrelines are taken from the wall polygons (with openings filled in) by
splitting them into horizontal and vertical bands; ResPlan walls are almost all
axis-aligned. Only the test split is converted; never train on it.

    .venv\\Scripts\\python.exe scripts\\resplan_to_gt.py --resplan ..\\ResPlan --limit 200
    .venv\\Scripts\\python.exe scripts\\eval_trace.py --data dataset\\resplan_gt --manifest dataset\\resplan_gt\\eval_manifest.json --freeze-manifest
    .venv\\Scripts\\python.exe scripts\\eval_trace.py --data dataset\\resplan_gt --manifest dataset\\resplan_gt\\eval_manifest.json --split test --stages canonical --no-defaults --model classic=classic --model J=models\\checkpoints\\cubicasa_finetuned_J.pth --close-rooms

Rendered images are cleaner than real drawings, so these scores are a best case.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
ROOM_TYPES = ("living", "kitchen", "bedroom", "bathroom", "balcony", "storage", "stair")
NAMES = {"living": "LIVING", "kitchen": "KITCHEN", "bedroom": "BEDROOM", "bathroom": "BATH",
         "balcony": "BALCONY", "storage": "STORE", "stair": "STAIR"}
FINE_MM = 10.0  # raster resolution for centreline extraction


# ---------------------------------------------------------------- loading
def load_plans(resplan: Path):
    pkl = resplan / "ResPlan.pkl"
    if not pkl.is_file():
        with zipfile.ZipFile(resplan / "ResPlan.zip") as f:
            f.extract("ResPlan.pkl", resplan)
    import shapely.io

    class SafeUnpickler(pickle.Unpickler):
        def find_class(self, module, name):
            if (module, name) == ("shapely.io", "from_wkb"):
                return shapely.io.from_wkb
            if module.startswith("numpy") and name in {"scalar", "dtype", "_reconstruct", "ndarray"}:
                return super().find_class(module, name)
            raise pickle.UnpicklingError(f"blocked unexpected class {module}.{name}")

    with open(pkl, "rb") as f:
        return SafeUnpickler(f).load()


def rings_of(geom):
    """Shapely (Multi)Polygon -> list of polygons, each [exterior, *holes] as coordinate lists."""
    if geom is None or geom.is_empty:
        return []
    out = []
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type == "Polygon" and not part.is_empty:
            out.append([list(part.exterior.coords)] + [list(r.coords) for r in part.interiors])
    return out


def area(ring):
    a = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        a += x0 * y1 - x1 * y0
    return abs(a) / 2


# ---------------------------------------------------------------- conversion
class Frame:
    """ResPlan units -> mm -> image px (y down)."""

    def __init__(self, polys, mm_per_unit, max_px, margin):
        xs = [p[0] for poly in polys for p in poly[0]]
        ys = [p[1] for poly in polys for p in poly[0]]
        self.k = mm_per_unit
        self.x0, self.y1 = min(xs) * self.k, max(ys) * self.k
        w_mm, h_mm = (max(xs) - min(xs)) * self.k, (max(ys) - min(ys)) * self.k
        self.mm_per_px = max(w_mm, h_mm) / (max_px - 2 * margin)
        self.m = margin
        self.size = (int(math.ceil(w_mm / self.mm_per_px + 2 * margin)), int(math.ceil(h_mm / self.mm_per_px + 2 * margin)))
        self.w_mm, self.h_mm = w_mm, h_mm

    def px(self, x, y):
        return ((x * self.k - self.x0) / self.mm_per_px + self.m, (self.y1 - y * self.k) / self.mm_per_px + self.m)

    def mm_local(self, x, y):  # mm, origin top-left, y down
        return (x * self.k - self.x0, self.y1 - y * self.k)

    def local_to_px(self, x, y):
        return (x / self.mm_per_px + self.m, y / self.mm_per_px + self.m)


def fill(draw, polys, conv, value):
    for poly in polys:
        draw.polygon([conv(*p) for p in poly[0]], fill=value)
        for hole in poly[1:]:
            draw.polygon([conv(*p) for p in hole], fill=0 if value else 1)


def centrelines(solid_polys, frame):
    """Horizontal and vertical wall centrelines (mm, local frame) from wall polygons."""
    from scipy import ndimage

    W, H = int(frame.w_mm / FINE_MM) + 4, int(frame.h_mm / FINE_MM) + 4
    im = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(im)
    fill(d, solid_polys, lambda x, y: tuple(v / FINE_MM + 2 for v in frame.mm_local(x, y)), 1)
    mask = np.array(im, bool)
    if not mask.any():
        return [], 0.0
    dt = ndimage.distance_transform_edt(mask)
    t = max(2.0, 2 * float(np.percentile(dt[mask], 90)))  # wall thickness in fine px
    L = int(round(1.6 * t)) + 1
    segs = []
    for horizontal in (True, False):
        k = np.ones((1, L) if horizontal else (L, 1), bool)
        band = ndimage.binary_opening(mask, structure=k)
        lab, n = ndimage.label(band)
        for sl in ndimage.find_objects(lab):
            ys, xs = sl
            h, w = ys.stop - ys.start, xs.stop - xs.start
            if (w if horizontal else h) < L:
                continue
            if horizontal:
                y = (ys.start + ys.stop) / 2
                a, b = (xs.start, y), (xs.stop, y)
            else:
                x = (xs.start + xs.stop) / 2
                a, b = (x, ys.start), (x, ys.stop)
            segs.append(tuple(((p[0] - 2) * FINE_MM, (p[1] - 2) * FINE_MM) for p in (a, b)))
    return segs, t * FINE_MM


def opening(poly, frame):
    """(ends_px, width_px) along the long side of a door/window rectangle."""
    pts = [frame.px(*p) for p in poly[0]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    if dx >= dy:
        return [[min(xs), cy], [max(xs), cy]], dx, dy
    return [[cx, min(ys)], [cx, max(ys)]], dy, dx


def convert(plan, out_dir: Path, max_px=1000, margin=40):
    get = lambda k: rings_of(plan.get(k))
    inner = get("inner")
    walls, doors, windows = get("wall"), get("door"), get("window")
    rooms = [(t, p) for t in ROOM_TYPES for p in get(t)]
    if not walls or not rooms:
        return None
    inner_area = sum(area(p[0]) for p in inner) or sum(area(p[0]) for _, p in rooms)
    mm_per_unit = math.sqrt(float(plan["area"]) * 1e6 / inner_area)
    frame = Frame(walls + [p for _, p in rooms] + doors + windows, mm_per_unit, max_px, margin)

    # ---- image
    img = Image.new("L", frame.size, 255)
    d = ImageDraw.Draw(img)
    fill(d, walls, frame.px, 0)
    for poly in doors + windows:  # openings are gaps in the wall
        d.polygon([frame.px(*p) for p in poly[0]], fill=255)
    for poly in windows:
        (a, b), wlen, thick = opening(poly, frame)
        d.polygon([frame.px(*p) for p in poly[0]], outline=0)
        d.line([tuple(a), tuple(b)], fill=0, width=1)
    for poly in doors:
        (a, b), wlen, thick = opening(poly, frame)
        ux, uy = (b[0] - a[0]) / (wlen or 1), (b[1] - a[1]) / (wlen or 1)
        nx, ny = -uy, ux
        tip = (a[0] + nx * wlen, a[1] + ny * wlen)
        d.line([tuple(a), tip], fill=0, width=2)
        au, an = math.degrees(math.atan2(uy, ux)), math.degrees(math.atan2(ny, nx))
        s, e = (au, an) if (an - au) % 360 == 90 else (an, au)
        d.arc([a[0] - wlen, a[1] - wlen, a[0] + wlen, a[1] + wlen], s, e, fill=0, width=1)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    zones = []
    for t, poly in rooms:
        ring = [frame.px(*p) for p in poly[0]]
        zones.append({"name": NAMES[t], "type": t, "polygon_px": [list(p) for p in ring[:-1]]})
        cx = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
        cy = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)
        d.text((cx, cy), NAMES[t], fill=0, font=font, anchor="mm")

    # ---- ground truth
    segs, thick_mm = centrelines(walls + doors + windows, frame)
    nodes, edges = [], []
    for i, (a, b) in enumerate(segs):
        pa, pb = frame.local_to_px(*a), frame.local_to_px(*b)
        nodes += [{"id": f"n{2*i}", "x": pa[0], "y": pa[1]}, {"id": f"n{2*i+1}", "x": pb[0], "y": pb[1]}]
        edges.append({"source": f"n{2*i}", "target": f"n{2*i+1}", "type": "wall",
                      "thickness_px": thick_mm / frame.mm_per_px})
    openings = []
    for kind, polys in (("door", doors), ("window", windows)):
        for poly in polys:
            ends, wlen, _ = opening(poly, frame)
            openings.append({"kind": kind, "ends_px": ends, "width_px": wlen})
    out_dir.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_dir / "image.png")
    (out_dir / "graph.json").write_text(json.dumps({"graph_px": {"nodes": nodes, "edges": edges,
                                                                 "openings": openings, "zones": zones}}))
    (out_dir / "meta.json").write_text(json.dumps({
        "registration": {"px_per_mm": 1.0 / frame.mm_per_px}, "label_source": "resplan",
        "annotation_complete": True, "reviewed": True, "split": "test",
        "group_id": f"resplan_{plan['id']}", "resplan_id": int(plan["id"]),
        "wall_thickness_mm": round(thick_mm), "licence": "ResPlan data CC BY 4.0",
    }))
    return {"walls": len(segs), "doors": len(doors), "windows": len(windows), "rooms": len(zones)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resplan", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=str(REPO / "dataset" / "resplan_gt"))
    ap.add_argument("--max-px", type=int, default=1000, help="longest image side")
    args = ap.parse_args()
    resplan = Path(args.resplan).resolve()
    test = {int(x) for x in json.loads((resplan / "split.json").read_text())["test"]}
    plans = sorted((p for p in load_plans(resplan) if int(p.get("id", -1)) in test), key=lambda p: int(p["id"]))
    if args.limit:
        plans = plans[: args.limit]
    out, done, skipped = Path(args.out), 0, 0
    for plan in plans:
        try:
            r = convert(plan, out / f"resplan_{int(plan['id']):05d}", args.max_px)
        except Exception as exc:
            print(f"  {plan['id']}: {type(exc).__name__}: {exc}")
            r = None
        if r:
            done += 1
        else:
            skipped += 1
    print(f"wrote {done} samples to {out} ({skipped} skipped)")


if __name__ == "__main__":
    main()
