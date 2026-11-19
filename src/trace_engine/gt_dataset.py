"""Registered IFC storeys -> training samples (CubiCasa masks + graph labels).

One sample per (image, storey) registration:

* ``image.png``     - the plan image (EXIF-normalised RGB, original size)
* ``rooms.png``     - CubiCasa room classes: spaces by name, walls (true
                      thickness) = Wall, railings = Railing
* ``icons.png``     - CubiCasa icon classes: Window / Door from the opening
                      footprints
* ``ignore.png``    - 255 where labels are unknown: beyond 600 mm outside the
                      traced building (untraced drawing parts, title blocks)
* ``graph.json``    - storey ground truth in mm + the same graph in image px
                      (nodes with junction type, typed edges with thickness,
                      hosted openings, zones) + the registration
* ``overlay.png``   - registration check image
* ``meta.json``     - provenance, metrics, bucket, ``annotation_complete``

``accept_samples`` copies reviewed candidates into the training folder with
``annotation_complete: true`` and a folder-level train/val split, which is
what ``scripts/finetune_cubicasa.py`` requires.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .dataset import room_class
from .ifc_groundtruth import StoreyGT
from .neural import ICON_CLASSES, ROOM_CLASSES
from .register import Registration, render_overlay

Point = Tuple[float, float]
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


# ------------------------------------------------------------ discovery
def folder_images(folder: str | Path) -> List[Path]:
    """Plan images directly in a project folder (not previews / crops)."""
    out = []
    for p in sorted(Path(folder).iterdir(), key=lambda q: q.name.lower()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(("preview_", "crop_", "overlay")):
            out.append(p)
    return out


def slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in text.strip())
    while "__" in keep:
        keep = keep.replace("__", "_")
    return keep.strip("_")[:60] or "x"


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ rasterise
def rasterize(gt: StoreyGT, reg: Registration, ignore_margin_mm: float = 600.0
              ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, int]]:
    """(rooms, icons, ignore, counts) in original image pixels."""
    from PIL import Image, ImageDraw

    w, h = reg.image_size
    rooms = Image.new("L", (w, h), ROOM_CLASSES.index("Background"))
    icons = Image.new("L", (w, h), ICON_CLASSES.index("No Icon"))
    known = Image.new("L", (w, h), 0)
    rd, idr, kd = ImageDraw.Draw(rooms), ImageDraw.Draw(icons), ImageDraw.Draw(known)
    px = lambda poly: [reg.to_px(p) for p in poly]  # noqa: E731

    outdoor = ROOM_CLASSES.index("Outdoor")
    named = 0
    # Larger spaces first so nested/overlapping small ones stay visible.
    for sp in sorted(gt.spaces, key=lambda s: -abs(_area(s.polygon))):
        cls = outdoor if sp.outdoor else room_class(sp.name)
        named += cls != ROOM_CLASSES.index("Undefined")
        rd.polygon(px(sp.polygon), fill=cls)
    wall, railing = ROOM_CLASSES.index("Wall"), ROOM_CLASSES.index("Railing")
    for kind, poly in gt.wall_polygons:
        rd.polygon(px(poly), fill=wall if kind == "wall" else railing)
    counts = {"window": 0, "door": 0}
    for op in gt.openings:
        host = gt.segments[op.host_wall] if op.host_wall is not None and op.host_wall < len(gt.segments) else None
        poly = _trim_to_wall(op.polygon, host.thickness if host else None)
        idr.polygon(px(poly), fill=ICON_CLASSES.index("Window" if op.kind == "window" else "Door"))
        counts[op.kind] += 1

    # Known region: the building footprint grown by ``ignore_margin_mm``.
    grow = max(1, int(round(ignore_margin_mm * reg.px_per_mm)))
    for sp in gt.spaces:
        kd.polygon(px(sp.polygon), fill=255)
    for _, poly in gt.wall_polygons:
        kd.polygon(px(poly), fill=255)
    from ._cv import cv2

    known_arr = cv2.dilate(np.asarray(known, np.uint8), np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
    ignore = np.where(known_arr > 0, 0, 255).astype(np.uint8)
    stats = {"spaces": len(gt.spaces), "named_spaces": named, "walls": len(gt.wall_polygons),
             "windows": counts["window"], "doors": counts["door"],
             "ignored_fraction": round(float((ignore > 0).mean()), 3)}
    return np.asarray(rooms, np.uint8), np.asarray(icons, np.uint8), ignore, stats


def _trim_to_wall(poly: Sequence[Point], thickness: float | None) -> List[Point]:
    """Opening footprint, with its depth cut to the host wall thickness.

    Exporter opening shapes are deeper than the wall (~200 mm vs 100-230 mm),
    which makes door/window labels bleed into the rooms either side.
    """
    if not thickness or len(poly) < 3:
        return list(poly)
    pts = np.asarray(poly, np.float32)
    (cx, cy), (rw, rh), ang = cv2_min_area_rect(pts)
    if min(rw, rh) <= thickness * 1.02:
        return list(poly)
    if rw < rh:
        rw = thickness
    else:
        rh = thickness
    a = np.deg2rad(ang)
    ux, uy = np.cos(a), np.sin(a)
    vx, vy = -np.sin(a), np.cos(a)
    return [(cx + sx * rw / 2 * ux + sy * rh / 2 * vx, cy + sx * rw / 2 * uy + sy * rh / 2 * vy)
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def cv2_min_area_rect(pts: np.ndarray):
    from ._cv import cv2

    return cv2.minAreaRect(pts)


def _area(poly: Sequence[Point]) -> float:
    a = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        a += x1 * y2 - x2 * y1
    return a / 2.0


# ------------------------------------------------------------ graph labels
def junction_type(dirs: Sequence[float]) -> str:
    """'end', 'straight', 'L', 'T', 'X' or 'multi' from incident edge angles (rad)."""
    n = len(dirs)
    if n <= 1:
        return "end"
    if n == 2:
        d = abs((dirs[0] - dirs[1] + math.pi) % (2 * math.pi) - math.pi)
        return "straight" if d > math.radians(170) else "L"
    if n == 3:
        return "T"
    if n == 4:
        return "X"
    return "multi"


def graph_px(gt: StoreyGT, reg: Registration) -> Dict[str, object]:
    """The storey's wall graph, openings and zones in image pixels."""
    net = gt.network
    nodes_px = {n: reg.to_px(p) for n, p in net.nodes.items()}
    incident: Dict[int, List[float]] = {n: [] for n in net.nodes}
    for w in net.walls:
        a, b = nodes_px[w.a], nodes_px[w.b]
        incident[w.a].append(math.atan2(b[1] - a[1], b[0] - a[0]))
        incident[w.b].append(math.atan2(a[1] - b[1], a[0] - b[0]))
    nodes = [{"id": f"n{n}", "x": round(p[0], 2), "y": round(p[1], 2),
              "degree": len(incident[n]), "type": junction_type(incident[n]),
              "directions_deg": [round(math.degrees(d) % 360, 1) for d in incident[n]]}
             for n, p in nodes_px.items()]
    edges = [{"source": f"n{w.a}", "target": f"n{w.b}", "type": w.type,
              "thickness_px": round(w.thickness * reg.px_per_mm, 2), "thickness_mm": round(w.thickness, 1)}
             for w in net.walls]
    openings = []
    if gt.trace is not None:
        for kind, items in (("door", gt.trace.doors_list), ("window", gt.trace.windows_list)):
            for o in items:
                a = net.nodes[int(o.edge_nodes[0][1:])]
                b = net.nodes[int(o.edge_nodes[1][1:])]
                L = math.dist(a, b) or 1.0
                u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
                c = (a[0] + u[0] * o.distance_from_lower_node, a[1] + u[1] * o.distance_from_lower_node)
                e0 = (c[0] - u[0] * o.length / 2, c[1] - u[1] * o.length / 2)
                e1 = (c[0] + u[0] * o.length / 2, c[1] + u[1] * o.length / 2)
                openings.append({"kind": kind, "door_type": o.door_type, "edge_nodes": list(o.edge_nodes),
                                 "offset_ratio": round(o.distance_from_lower_node / L, 4),
                                 "width_px": round(o.length * reg.px_per_mm, 2),
                                 "ends_px": [list(map(lambda v: round(v, 2), reg.to_px(e0))),
                                             list(map(lambda v: round(v, 2), reg.to_px(e1)))]})
    zones = [{"name": sp.name, "class": ROOM_CLASSES[ROOM_CLASSES.index("Outdoor") if sp.outdoor else room_class(sp.name)],
              "seed_px": list(map(lambda v: round(v, 2), reg.to_px(sp.seed))),
              "polygon_px": [list(map(lambda v: round(v, 1), reg.to_px(p))) for p in sp.polygon]}
             for sp in gt.spaces]
    return {"nodes": nodes, "edges": edges, "openings": openings, "zones": zones}


# ------------------------------------------------------------ write
def write_sample(gt: StoreyGT, reg: Registration, out_dir: str | Path, folder_name: str,
                 overlay: bool = True) -> Dict[str, object]:
    from PIL import Image, ImageOps

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    img = ImageOps.exif_transpose(Image.open(reg.image)).convert("RGB")
    img.save(out / "image.png")
    rooms, icons, ignore, stats = rasterize(gt, reg)
    Image.fromarray(rooms).save(out / "rooms.png")
    Image.fromarray(icons).save(out / "icons.png")
    Image.fromarray(ignore).save(out / "ignore.png")
    graph = {"storey_mm": gt.to_json(), "graph_px": graph_px(gt, reg), "registration": reg.to_json()}
    (out / "graph.json").write_text(json.dumps(graph, indent=1), encoding="utf-8")
    if overlay:
        render_overlay(reg, gt, out / "overlay.png")
    meta = {
        "source_image": str(reg.image),
        "source_sha256": sha256(reg.image),
        "folder": folder_name,
        "group_id": slug(folder_name),
        "storey": gt.key,
        "storey_name": gt.name,
        "ifc": gt.ifc_path,
        "bucket": reg.bucket,
        "registration": {k: round(v, 4) if isinstance(v, float) else v for k, v in {
            "support": reg.support, "score": reg.score, "precision": reg.inlier, "recall": reg.recall,
            "margin": reg.margin, "match": reg.match, "rotation": reg.rotation, "mirror": reg.mirror,
            "px_per_mm": reg.px_per_mm}.items()},
        "notes": reg.notes,
        "storey_stats": gt.stats,
        "label_stats": stats,
        "image_size": list(reg.image_size),
        "room_classes": ROOM_CLASSES,
        "icon_classes": ICON_CLASSES,
        "label_source": "ifc_registration",
        "annotation_complete": False,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def sample_id(folder_name: str, image: str | Path) -> str:
    return f"{slug(folder_name)}__{slug(Path(image).stem)}"


# ------------------------------------------------------------ review + accept
def write_review_page(candidates_dir: str | Path, out_html: str | Path) -> int:
    """Static HTML contact sheet: overlay per candidate, tick to accept,
    download ``accepted.txt`` for :func:`accept_samples`."""
    cands = []
    for d in sorted(Path(candidates_dir).iterdir()):
        mp = d / "meta.json"
        if mp.is_file():
            m = json.loads(mp.read_text(encoding="utf-8"))
            cands.append((d.name, m))
    order = {"auto_ok": 0, "review": 1, "reject": 2}
    cands.sort(key=lambda c: (order.get(c[1].get("bucket"), 3), c[0]))
    rel = lambda name: Path(candidates_dir).name + "/" + name + "/overlay.png"  # noqa: E731
    cards = []
    for name, m in cands:
        r = m.get("registration", {})
        checked = "checked" if m.get("bucket") == "auto_ok" else ""
        cards.append(
            f'<label class="card {m.get("bucket")}"><img loading="lazy" src="{rel(name)}">'
            f'<div><input type="checkbox" value="{name}" {checked}> <b>{m.get("bucket")}</b> '
            f'support {r.get("support", 0):.2f} · score {r.get("score", 0):.2f} · margin {r.get("margin", 0):.2f}'
            f'<br><small>{name} → {m.get("storey")}</small></div></label>')
    html = """<!doctype html><meta charset="utf-8"><title>GT review</title>
<style>body{font:13px system-ui;margin:16px;background:#fafafa}.bar{position:sticky;top:0;background:#fff;padding:8px;border-bottom:1px solid #ddd;z-index:2}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px;margin-top:12px}
.card{display:block;background:#fff;border:2px solid #ddd;border-radius:8px;padding:6px;cursor:pointer}
.card img{width:100%;display:block}.auto_ok{border-color:#3a3}.review{border-color:#e90}.reject{border-color:#c33;opacity:.7}</style>
<div class="bar">Tick the overlays where the red walls, blue windows and green doors sit on the drawing. Then
<button onclick="dl()">download accepted.txt</button> and run
<code>python scripts/build_gt_dataset.py accept --list accepted.txt</code>. <span id="n"></span></div>
<div class="grid">""" + "\n".join(cards) + """</div>
<script>function sel(){return [...document.querySelectorAll('input:checked')].map(i=>i.value)}
function upd(){document.getElementById('n').textContent=sel().length+' selected'}
document.addEventListener('change',upd);upd();
function dl(){const b=new Blob([sel().join('\\n')+'\\n'],{type:'text/plain'});const a=document.createElement('a');
a.href=URL.createObjectURL(b);a.download='accepted.txt';a.click()}</script>"""
    Path(out_html).write_text(html, encoding="utf-8")
    return len(cands)


def default_val_groups(groups: Iterable[str], every: int = 5) -> List[str]:
    """Deterministic folder-level hold-out: every ``every``-th group."""
    g = sorted(set(groups))
    return [x for i, x in enumerate(g) if i % every == every - 1]


def accept_samples(candidates_dir: str | Path, training_dir: str | Path, names: Sequence[str],
                   val_groups: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """Copy reviewed candidates into the training folder, marked complete,
    with a folder-level split. Duplicate source images are kept once."""
    cdir, tdir = Path(candidates_dir), Path(training_dir)
    tdir.mkdir(parents=True, exist_ok=True)
    metas = {}
    for n in names:
        mp = cdir / n / "meta.json"
        if mp.is_file():
            metas[n] = json.loads(mp.read_text(encoding="utf-8"))
    all_groups = [m["group_id"] for m in metas.values()]
    val = set(val_groups) if val_groups is not None else set(default_val_groups(all_groups))
    seen: Dict[str, str] = {}
    for d in tdir.iterdir():
        mp = d / "meta.json"
        if mp.is_file():
            m = json.loads(mp.read_text(encoding="utf-8"))
            seen[m.get("source_sha256", d.name)] = d.name
    copied, skipped = [], []
    for n, m in metas.items():
        sha = m.get("source_sha256", n)
        if sha in seen and seen[sha] != n:
            skipped.append((n, f"duplicate of {seen[sha]}"))
            continue
        dst = tdir / n
        if dst.exists():
            shutil.copytree(cdir / n, dst, dirs_exist_ok=True)
        else:
            shutil.copytree(cdir / n, dst)
        m["annotation_complete"] = True
        m["split"] = "val" if m["group_id"] in val else "train"
        m["reviewed"] = True
        (dst / "meta.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
        seen[sha] = n
        copied.append(n)
    missing = [n for n in names if n not in metas]
    return {"copied": copied, "skipped": skipped, "missing": missing, "val_groups": sorted(val),
            "train": sum(1 for n in copied if json.loads((tdir / n / "meta.json").read_text())["split"] == "train"),
            "val": sum(1 for n in copied if json.loads((tdir / n / "meta.json").read_text())["split"] == "val")}
