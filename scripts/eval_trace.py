"""End-to-end trace evaluation on a frozen set of held-out plans.

What it scores is what you would get in AutoCAD, not pixel IoU. Every model is
traced once per plan; the same trace is then scored at three stages:

  raw        - the recogniser's graph as returned (no cleanup)
  legacy     - after compile_trace(geometry_pipeline="legacy")   (shipped path)
  canonical  - after compile_trace(geometry_pipeline="canonical") (new path)

Metrics (tolerances are millimetres; with no scale they are pixels and every
mm figure is left empty):

  walls      points every 50 mm along IFC wall centrelines; recall = share within
             --wall-tol-mm of a traced wall, precision = share of traced wall length
             within --wall-tol-mm of an IFC wall or separator
  junctions  IFC L/T/X nodes vs traced L/T/X nodes, one-to-one within --junction-tol-mm
  doors /    opening centres one-to-one within --open-tol-mm (same kind, and
  windows    "any opening" ignoring type); for matched openings: width error (mm)
             and correct-host rate (traced host wall parallel to and over the
             IFC host wall)
  rooms      raw: closed regions between traced walls; legacy / canonical: the
             compiled clear room polygons. Found when IoU >= 0.5 (one-to-one);
             boundary error = mean distance between matched outlines (mm);
             adjacency = recall / precision of "room A touches room B" over matched
             rooms
  validity   compiled changes that fail the manage_design schema, and openings or
             rooms rejected during compilation
  runtime    trace and compile seconds per plan

Manifest: --freeze-manifest writes dataset/gt/eval_manifest.json from the current
training folder (sample_id, building_id, image, ground truth, scale, annotation
completeness, split) and checks that no building appears in two splits. Later
runs read that file, so every comparison uses the same plans. Tolerances and the
manifest hash are written into summary.json.

Usage (repo root):
  .venv\\Scripts\\python.exe scripts\\eval_trace.py --freeze-manifest
  .venv\\Scripts\\python.exe scripts\\eval_trace.py
  .venv\\Scripts\\python.exe scripts\\eval_trace.py --wall-bias 0,0.5,1 --stages canonical
  .venv\\Scripts\\python.exe scripts\\eval_trace.py --model round2=models\\checkpoints\\cubicasa_finetuned.pth --no-defaults
Output: dataset\\gt\\eval\\<time>\\index.html, results.csv, summary.json, overlays\\
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STAGES = ("raw", "legacy", "canonical")
JUNCTION_TYPES = {"L", "T", "X", "multi"}


# ---------------------------------------------------------------- geometry helpers
def seg_points(segs, step):
    pts = []
    for a, b in segs:
        a, b = np.asarray(a, float), np.asarray(b, float)
        n = max(1, int(math.ceil(np.linalg.norm(b - a) / step)))
        t = (np.arange(n) + 0.5)[:, None] / n
        pts.append(a + (b - a) * t)
    return np.concatenate(pts) if pts else np.zeros((0, 2))


def dist_to_segs(P, segs):
    if len(P) == 0:
        return np.zeros(0)
    if not segs:
        return np.full(len(P), np.inf)
    A = np.array([s[0] for s in segs], float)
    B = np.array([s[1] for s in segs], float)
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-9)
    out = np.full(len(P), np.inf)
    for i in range(0, len(P), 2048):
        p = P[i:i + 2048, None, :]
        t = np.clip(((p - A) * AB).sum(2) / L2, 0, 1)
        d = np.linalg.norm(p - (A + t[..., None] * AB), axis=2)
        out[i:i + 2048] = d.min(1)
    return out


def match_pairs(gt, pr, tol):
    """Greedy one-to-one nearest matching; returns [(gt index, pred index)]."""
    if not len(gt) or not len(pr):
        return []
    G, P = np.array(gt, float), np.array(pr, float)
    D = np.linalg.norm(G[:, None] - P[None], axis=2)
    pairs = sorted((D[i, j], i, j) for i, j in zip(*np.nonzero(D <= tol)))
    ug, up, out = set(), set(), []
    for _, i, j in pairs:
        if i not in ug and j not in up:
            ug.add(i); up.add(j); out.append((int(i), int(j)))
    return out


def prf(tp, n_gt, n_pr):
    r = tp / n_gt if n_gt else float("nan")
    p = tp / n_pr if n_pr else (float("nan") if n_gt == 0 else 0.0)
    f = 2 * p * r / (p + r) if (p == p and r == r and p + r > 0) else (0.0 if n_gt or n_pr else float("nan"))
    return p, r, f


def junction_type(dirs: Sequence[float]) -> str:
    n = len(dirs)
    if n <= 1:
        return "end"
    if n == 2:
        d = abs((dirs[0] - dirs[1] + math.pi) % (2 * math.pi) - math.pi)
        return "straight" if d > math.radians(170) else "L"
    return {3: "T", 4: "X"}.get(n, "multi")


def junctions_of(segs) -> List[Tuple[float, float]]:
    """L/T/X nodes of a segment soup (endpoints merged at 0.5 px)."""
    inc: Dict[Tuple[int, int], List[float]] = {}
    pos: Dict[Tuple[int, int], Tuple[float, float]] = {}
    for a, b in segs:
        for p, q in ((a, b), (b, a)):
            k = (int(round(p[0] * 2)), int(round(p[1] * 2)))
            pos[k] = (float(p[0]), float(p[1]))
            inc.setdefault(k, []).append(math.atan2(q[1] - p[1], q[0] - p[0]))
    return [pos[k] for k, d in inc.items() if junction_type(d) in JUNCTION_TYPES]


# ---------------------------------------------------------------- manifest
def build_manifest(data: Path) -> List[dict]:
    rows = []
    for d in sorted(p for p in data.iterdir() if (p / "graph.json").is_file() and (p / "image.png").is_file()):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").is_file() else {}
        ppm = (meta.get("registration") or {}).get("px_per_mm")
        rows.append({
            "sample_id": d.name,
            "building_id": meta.get("group_id") or meta.get("folder") or d.name,
            "image_path": f"{d.name}/image.png",
            "ground_truth_path": f"{d.name}/graph.json",
            "scale": {"px_per_mm": ppm, "source": meta.get("label_source", "unknown")} if ppm else None,
            "annotation_completeness": {
                "complete": bool(meta.get("annotation_complete")),
                "reviewed": bool(meta.get("reviewed")),
                "bucket": meta.get("bucket"),
            },
            "split": meta.get("split", "unassigned"),
        })
    return rows


def leakage(rows: List[dict]) -> Dict[str, List[str]]:
    splits: Dict[str, set] = {}
    for r in rows:
        splits.setdefault(r["building_id"], set()).add(r["split"])
    return {b: sorted(s) for b, s in splits.items() if len(s) > 1}


def manifest_hash(rows: List[dict]) -> str:
    return hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- ground truth
def load_gt(path: Path) -> dict:
    g = json.loads(path.read_text(encoding="utf-8"))["graph_px"]
    nodes = {n["id"]: (n["x"], n["y"]) for n in g["nodes"]}
    walls, seps, wall_by_nodes = [], [], {}
    for e in g["edges"]:
        if e["source"] in nodes and e["target"] in nodes:
            seg = (nodes[e["source"]], nodes[e["target"]])
            (seps if e.get("type") == "separator" else walls).append(seg)
            wall_by_nodes[frozenset((e["source"], e["target"]))] = seg
    ops = {"door": [], "window": []}
    for o in g.get("openings", []):
        if o.get("kind") in ops and o.get("ends_px"):
            a, b = o["ends_px"]
            host = wall_by_nodes.get(frozenset(o.get("edge_nodes") or ()))
            ops[o["kind"]].append({"c": ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
                                   "w": float(o.get("width_px") or math.dist(a, b)), "host": host})
    junctions = [nodes[n["id"]] for n in g["nodes"] if n.get("type") in JUNCTION_TYPES]
    zones = [z for z in g.get("zones", []) if len(z.get("polygon_px") or []) >= 3]
    return {"walls": walls, "seps": seps, "ops": ops, "junctions": junctions, "zones": zones}


# ---------------------------------------------------------------- predictions per stage
def _centre(o, nodes):
    a_id, b_id = o.edge_nodes
    if o.distance_from_lower_node is not None and a_id in nodes and b_id in nodes:
        a, b = nodes[a_id], nodes[b_id]
        L = math.dist(a, b) or 1.0
        return (a[0] + (b[0] - a[0]) / L * o.distance_from_lower_node,
                a[1] + (b[1] - a[1]) / L * o.distance_from_lower_node)
    return tuple(o.center) if o.center is not None else None


def pred_raw(trace) -> dict:
    nodes = {n.id: (n.x, n.y) for n in trace.walls_graph.nodes}
    walls, seps = [], []
    for e in trace.walls_graph.edges:
        if e.source in nodes and e.target in nodes:
            (seps if e.type == "separator" else walls).append((nodes[e.source], nodes[e.target], e.thickness))
    ops = {"door": [], "window": []}
    for kind, items in (("door", trace.doors_list), ("window", trace.windows_list)):
        for o in items:
            c = _centre(o, nodes)
            if c is None:
                continue
            a_id, b_id = o.edge_nodes
            host = (nodes[a_id], nodes[b_id]) if a_id in nodes and b_id in nodes else None
            ops[kind].append({"c": c, "w": o.length, "host": host})
    return {"walls": walls, "seps": seps, "ops": ops, "rooms": None, "invalid": 0, "rejected": 0}


def pred_compiled(compiled, frame) -> dict:
    """Compiled changes (mm, shifted) back into image pixels."""
    dx, dy = compiled.offset

    def px(p):
        return frame.from_mm((p[0] - dx, p[1] - dy))

    walls_by_id, walls = {}, []
    for c in compiled.changes:
        if c["@type"] == "top:Wall":
            g = c["geometry"]
            seg = (px(g["start"]), px(g["end"]), frame.length_from_mm(g["thickness"]))
            walls_by_id[c["@id"]] = seg
            walls.append(seg)
    net = compiled.network
    seps = [(frame.from_mm(net.nodes[w.a]), frame.from_mm(net.nodes[w.b]), frame.length_from_mm(w.thickness))
            for w in net.walls if w.type == "separator"]
    ops = {"door": [], "window": []}
    for c in compiled.changes:
        if c["@type"] in ("top:Door", "top:Window"):
            g = c["geometry"]
            host = walls_by_id.get(g.get("host_wall_id"))
            if host is None:
                continue
            a, b = np.array(host[0]), np.array(host[1])
            L = np.linalg.norm(b - a) or 1.0
            s = frame.length_from_mm(g["offset"] + g["width"] / 2)
            ops["door" if c["@type"] == "top:Door" else "window"].append(
                {"c": tuple(a + (b - a) / L * s), "w": frame.length_from_mm(g["width"]), "host": (host[0], host[1])})
    rooms = [[px(p) for p in c["geometry"]["boundary"]] for c in compiled.changes if c["@type"] == "top:Room"]
    invalid = 0
    try:
        from topology_engine.models import TopologyChange

        for c in compiled.changes:
            try:
                TopologyChange.model_validate(c)
            except Exception:
                invalid += 1
    except Exception:  # topology package unavailable: skip the schema check
        invalid = -1
    reg = getattr(compiled, "regularized", None)
    if reg is not None:
        rejected = sum(1 for h in reg.openings if not h.hosted) + sum(
            1 for d in reg.diagnostics if d.code in ("room_collapsed",))
    else:
        rejected = sum(1 for w in compiled.warnings if "skipped" in w.lower())
    return {"walls": walls, "seps": seps, "ops": ops, "rooms": rooms, "invalid": invalid, "rejected": rejected}


# ---------------------------------------------------------------- rooms
def raster_labels(shape, polys) -> np.ndarray:
    H, W = shape
    lab = np.zeros((H, W), np.int32)
    for k, poly in enumerate(polys, 1):
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).polygon([tuple(p) for p in poly], fill=1)
        lab[np.array(m).astype(bool)] = k
    return lab


def wall_regions(shape, segs_with_thickness, min_px):
    from scipy import ndimage
    H, W = shape
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for a, b, t in segs_with_thickness:
        d.line([tuple(a), tuple(b)], fill=255, width=max(2, int(round(t))))
    lab, n = ndimage.label(np.array(m) == 0)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    keep = [k for k in range(1, n + 1) if k not in border and sizes[k] >= min_px]
    lut = np.zeros(n + 1, np.int32)
    lut[keep] = np.arange(1, len(keep) + 1)
    return lut[lab]


def match_rooms(gt_lab, n_gt, pr_lab, n_pr):
    """One-to-one IoU >= 0.5 matching; returns ({gt: (pred, iou)}, mean IoU over GT)."""
    if n_gt == 0:
        return {}, float("nan")
    key = gt_lab.astype(np.int64) * (n_pr + 1) + pr_lab
    inter = np.bincount(key.ravel(), minlength=(n_gt + 1) * (n_pr + 1)).reshape(n_gt + 1, n_pr + 1).astype(float)
    ga = inter.sum(1)
    pa = inter.sum(0)
    iou = inter / np.maximum(ga[:, None] + pa[None, :] - inter, 1)
    pairs = sorted(((iou[i, j], i, j) for i in range(1, n_gt + 1) for j in range(1, n_pr + 1) if iou[i, j] >= 0.5),
                   reverse=True)
    ug, up, best = set(), set(), {}
    for v, i, j in pairs:
        if i not in ug and j not in up:
            ug.add(i); up.add(j); best[i] = (j, float(v))
    return best, float(np.mean([best[i][1] if i in best else 0.0 for i in range(1, n_gt + 1)]))


def adjacency(lab, gap_px, min_contact_px) -> set:
    """Pairs of labels within ``gap_px`` of each other along >= ``min_contact_px``."""
    counts: Dict[Tuple[int, int], int] = {}
    H, W = lab.shape
    for d in sorted({max(1, int(round(gap_px * f))) for f in (0.5, 1.0)}):
        for dy, dx in ((0, d), (d, 0)):
            a = lab[: H - dy, : W - dx]
            b = lab[dy:, dx:]
            m = (a > 0) & (b > 0) & (a != b)
            if not m.any():
                continue
            pa, pb = a[m], b[m]
            lo, hi = np.minimum(pa, pb), np.maximum(pa, pb)
            keys, cnt = np.unique(lo.astype(np.int64) * 100000 + hi, return_counts=True)
            for k, c in zip(keys, cnt):
                pair = (int(k // 100000), int(k % 100000))
                counts[pair] = max(counts.get(pair, 0), int(c))
    return {p for p, c in counts.items() if c >= min_contact_px}


def boundary_error(gt_poly, pr_poly, step):
    def ring(poly):
        return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
    g, p = ring(gt_poly), ring(pr_poly)
    d1 = dist_to_segs(seg_points(p, step), g)
    d2 = dist_to_segs(seg_points(g, step), p)
    return float(np.mean(np.concatenate([d1, d2]))) if len(d1) + len(d2) else float("nan")


# ---------------------------------------------------------------- scoring one stage
def score(gt: dict, pr: dict, shape, ppm: Optional[float], args) -> Tuple[dict, np.ndarray]:
    mm = (lambda v: v * ppm) if ppm else (lambda v: v)  # mm -> px when calibrated
    to_mm = (lambda v: v / ppm) if ppm else (lambda v: float("nan"))
    row: Dict[str, float] = {}
    pw = [(a, b) for a, b, _ in pr["walls"]]
    step, tol = mm(50.0) if ppm else 2.0, mm(args.wall_tol_mm) if ppm else args.wall_tol_px
    gpts, ppts = seg_points(gt["walls"], step), seg_points(pw, step)
    rec = float((dist_to_segs(gpts, pw) <= tol).mean()) if len(gpts) else float("nan")
    prec = float((dist_to_segs(ppts, gt["walls"] + gt["seps"]) <= tol).mean()) if len(ppts) else (0.0 if len(gpts) else float("nan"))
    row.update(wall_precision=prec, wall_recall=rec, wall_f1=(2 * prec * rec / (prec + rec) if prec + rec > 0 else 0.0),
               gt_wall_m=round(to_mm(len(gpts) * step) / 1000, 1) if ppm else float("nan"),
               pred_wall_m=round(to_mm(len(ppts) * step) / 1000, 1) if ppm else float("nan"))

    pj = junctions_of(pw + [(a, b) for a, b, _ in pr["seps"]])
    jt = len(match_pairs(gt["junctions"], pj, mm(args.junction_tol_mm) if ppm else args.wall_tol_px))
    p, r, f = prf(jt, len(gt["junctions"]), len(pj))
    row.update(junction_gt=len(gt["junctions"]), junction_pred=len(pj), junction_tp=jt,
               junction_precision=p, junction_recall=r, junction_f1=f)

    otol = mm(args.open_tol_mm) if ppm else args.open_tol_px
    width_err, host_ok, host_n = [], 0, 0
    sin10 = math.sin(math.radians(10))
    for kind in ("door", "window"):
        G, P = gt["ops"][kind], pr["ops"][kind]
        pairs = match_pairs([o["c"] for o in G], [o["c"] for o in P], otol)
        p, r, f = prf(len(pairs), len(G), len(P))
        row.update({f"{kind}_gt": len(G), f"{kind}_pred": len(P), f"{kind}_tp": len(pairs),
                    f"{kind}_precision": p, f"{kind}_recall": r, f"{kind}_f1": f})
        for i, j in pairs:
            width_err.append(abs(to_mm(P[j]["w"]) - to_mm(G[i]["w"])))
            gh, ph = G[i]["host"], P[j]["host"]
            if gh is None or ph is None:
                continue
            host_n += 1
            ug = np.subtract(gh[1], gh[0]); up_ = np.subtract(ph[1], ph[0])
            ng, npr = np.linalg.norm(ug), np.linalg.norm(up_)
            if ng == 0 or npr == 0:
                continue
            parallel = abs(ug[0] * up_[1] - ug[1] * up_[0]) / (ng * npr) <= sin10
            over = dist_to_segs(np.array([P[j]["c"]]), [gh])[0] <= tol
            host_ok += int(parallel and over)
    allg = gt["ops"]["door"] + gt["ops"]["window"]
    allp = pr["ops"]["door"] + pr["ops"]["window"]
    tp = len(match_pairs([o["c"] for o in allg], [o["c"] for o in allp], otol))
    p, r, f = prf(tp, len(allg), len(allp))
    row.update(opening_tp=tp, opening_gt=len(allg), opening_pred=len(allp), opening_precision=p, opening_recall=r,
               opening_f1=f, opening_width_err_mm=float(np.mean(width_err)) if width_err and ppm else float("nan"),
               opening_host_rate=host_ok / host_n if host_n else float("nan"))

    zones = gt["zones"]
    gt_lab = raster_labels(shape, [z["polygon_px"] for z in zones])
    if pr["rooms"] is None:
        pr_lab = wall_regions(shape, pr["walls"] + pr["seps"], (args.min_room_m2 * 1e6 * ppm * ppm) if ppm else 400)
        pr_polys = None
    else:
        pr_polys = pr["rooms"]
        pr_lab = raster_labels(shape, pr_polys)
    n_pr = int(pr_lab.max())
    best, miou = match_rooms(gt_lab, len(zones), pr_lab, n_pr)
    p, r, f = prf(len(best), len(zones), n_pr)
    berr = []
    if pr_polys is not None and ppm:
        for i, (j, _) in best.items():
            berr.append(to_mm(boundary_error(zones[i - 1]["polygon_px"], pr_polys[j - 1], mm(50.0))))
    gap, contact = (mm(400.0), mm(500.0)) if ppm else (8, 10)
    ga = adjacency(gt_lab, gap, contact)
    pa = adjacency(pr_lab, gap, contact)
    m = {i: j for i, (j, _) in best.items()}
    inv = {j: i for i, j in m.items()}
    g_pairs = [(a, b) for a, b in ga if a in m and b in m]
    p_pairs = [(a, b) for a, b in pa if a in inv and b in inv]
    adj_r = (sum(1 for a, b in g_pairs if tuple(sorted((m[a], m[b]))) in pa) / len(g_pairs)) if g_pairs else float("nan")
    adj_p = (sum(1 for a, b in p_pairs if tuple(sorted((inv[a], inv[b]))) in ga) / len(p_pairs)) if p_pairs else float("nan")
    row.update(room_gt=len(zones), room_pred=n_pr, room_tp=len(best), room_precision=p, room_recall=r, room_f1=f,
               room_mean_iou=miou, room_boundary_err_mm=float(np.mean(berr)) if berr else float("nan"),
               adjacency_recall=adj_r, adjacency_precision=adj_p,
               adj_gt_pairs=len(g_pairs), adj_pred_pairs=len(p_pairs),
               invalid_changes=pr["invalid"], rejected=pr["rejected"])
    return row, pr_lab


# ---------------------------------------------------------------- overlay
def overlay(img_path, out, gt, pr, room_lab):
    im = Image.open(img_path).convert("RGB")
    base = Image.blend(im, Image.new("RGB", im.size, (255, 255, 255)), 0.55).convert("RGBA")
    lay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    if room_lab is not None and room_lab.max() > 0:
        rng = np.random.default_rng(1)
        cols = rng.integers(60, 230, (room_lab.max() + 1, 3))
        rgba = np.zeros(room_lab.shape + (4,), np.uint8)
        m = room_lab > 0
        rgba[m, :3] = cols[room_lab[m]]
        rgba[m, 3] = 45
        lay = Image.alpha_composite(lay, Image.fromarray(rgba, "RGBA"))
    d = ImageDraw.Draw(lay)
    for a, b in gt["walls"]:
        d.line([tuple(a), tuple(b)], fill=(0, 170, 0, 255), width=7)
    for a, b in gt["seps"]:
        d.line([tuple(a), tuple(b)], fill=(0, 170, 0, 120), width=3)
    for a, b, _ in pr["walls"]:
        d.line([tuple(a), tuple(b)], fill=(220, 0, 0, 230), width=3)
    for a, b, _ in pr["seps"]:
        d.line([tuple(a), tuple(b)], fill=(150, 60, 150, 200), width=2)
    for kind, col in (("door", (0, 150, 0)), ("window", (0, 90, 255))):
        for o in gt["ops"][kind]:
            x, y = o["c"]
            d.ellipse([x - 9, y - 9, x + 9, y + 9], outline=col + (255,), width=3)
        for o in pr["ops"][kind]:
            x, y = o["c"]
            d.rectangle([x - 4, y - 4, x + 4, y + 4], fill=(220, 0, 0, 255) if kind == "door" else (255, 120, 0, 255))
    o = Image.alpha_composite(base, lay).convert("RGB")
    o.thumbnail((1100, 1100))
    out.parent.mkdir(parents=True, exist_ok=True)
    o.save(out, quality=82)


# ---------------------------------------------------------------- main
COLS = [("wall_f1", "Walls F1"), ("wall_recall", "Walls found"), ("wall_precision", "Walls correct"),
        ("junction_f1", "Junctions F1"), ("door_f1", "Doors F1"), ("window_f1", "Windows F1"),
        ("opening_f1", "Openings F1"), ("opening_host_rate", "Right host"), ("opening_width_err_mm", "Width err mm"),
        ("room_f1", "Rooms F1"), ("room_mean_iou", "Room IoU"), ("room_boundary_err_mm", "Room edge err mm"),
        ("adjacency_recall", "Adjacency found"), ("invalid_changes", "Invalid"), ("rejected", "Rejected"),
        ("seconds", "s / plan")]
LOWER_IS_BETTER = {"opening_width_err_mm", "room_boundary_err_mm", "invalid_changes", "rejected", "seconds"}


R2SEQ = "r2seq:"  # weights prefix marking a Raster2Seq checkpoint

def configs_from(args) -> List[Tuple[str, Optional[str], float]]:
    configs: List[Tuple[str, Optional[str], float]] = []
    if not args.no_defaults:
        configs.append(("classic", None, 0.0))
        pre = ROOT / "models" / "pretrained" / "model_best_val_loss_var.pkl"
        if pre.is_file():
            configs.append(("pretrained", str(pre), 0.0))
        for p in sorted((ROOT / "models" / "checkpoints").glob("cubicasa_finetuned*.pth")):
            configs.append((p.stem.replace("cubicasa_finetuned", "finetuned") or "finetuned", str(p), 0.0))
    for m in args.model:
        name, _, path = m.partition("=")
        configs.append((name, None if path in ("", "classic") else path, 0.0))
    biases = [float(b) for b in args.wall_bias.split(",")] if args.wall_bias else [0.0]
    out = []
    for name, weights, _ in configs:
        for b in (biases if weights and not str(weights).startswith(R2SEQ) else [0.0]):
            out.append((name if b == 0 else f"{name}+b{b:g}", weights, b))
    if getattr(args, "raster2seq", False):
        # Raster2Seq (vector polygon model) checkpoints, each scored on its own
        # (trace_plan picks between them per plan; here we want each one's number).
        from trace_engine.raster2seq_inference import DEFAULT_BEST, DEFAULT_PRETRAINED

        for rname, ck in (("r2seq_pretrained", DEFAULT_PRETRAINED), ("r2seq_pilot30", DEFAULT_BEST)):
            if ck.is_file():
                out.append((rname, R2SEQ + str(ck), 0.0))
            else:
                print(f"note: {rname} checkpoint missing: {ck}")
    if not args.no_defaults:
        # The setting trace_plan uses today (models/neural_profile.json): the bar
        # a new model has to beat before it is promoted.
        from trace_engine.neural import load_profile

        prof = load_profile()
        if prof:
            out.append((f"promoted+b{prof['wall_logit_bias']:g}", prof["weights"], prof["wall_logit_bias"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "dataset" / "gt" / "training"))
    ap.add_argument("--manifest", default=str(ROOT / "dataset" / "gt" / "eval_manifest.json"))
    ap.add_argument("--freeze-manifest", action="store_true", help="write --manifest from --data and exit")
    ap.add_argument("--split", default="val", help="val | test | train | all")
    ap.add_argument("--model", action="append", default=[], help="name=path to weights (repeatable); 'classic' for the classic engine")
    ap.add_argument("--no-defaults", action="store_true", help="only the --model entries")
    ap.add_argument("--raster2seq", action="store_true",
                    help="also score the Raster2Seq checkpoints (needs .venv-raster2seq)")
    ap.add_argument("--wall-bias", default="", help="comma list of neural wall logit biases, e.g. 0,0.5,1")
    ap.add_argument("--stages", default=",".join(STAGES), help="comma list of raw,legacy,canonical")
    ap.add_argument("--gap-repair-mm", type=float, default=None, help="canonical gap repair limit (default: automatic)")
    ap.add_argument("--close-rooms", action="store_true",
                    help="room closure from the raw masks for every neural model "
                         "(default: only for the promoted weights when neural_profile.json says so)")
    ap.add_argument("--require-door-evidence", choices=["auto", "on", "off"], default="auto",
                    help="empty wall gap is a door only with a drawn swing or the door mask "
                         "(auto: on for classic, off for neural)")
    ap.add_argument("--wall-tol-mm", type=float, default=150.0)
    ap.add_argument("--junction-tol-mm", type=float, default=250.0)
    ap.add_argument("--open-tol-mm", type=float, default=400.0)
    ap.add_argument("--wall-tol-px", type=float, default=6.0, help="used only for samples without a scale")
    ap.add_argument("--open-tol-px", type=float, default=16.0, help="used only for samples without a scale")
    ap.add_argument("--min-room-m2", type=float, default=1.0)
    ap.add_argument("--no-overlays", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    data = Path(args.data)
    manifest_path = Path(args.manifest)
    if args.freeze_manifest:
        rows = build_manifest(data)
        leaks = leakage(rows)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"data_root": str(data), "created": time.strftime("%Y-%m-%d %H:%M"),
                                             "samples": rows}, indent=1), encoding="utf-8")
        by = {}
        for r in rows:
            by[r["split"]] = by.get(r["split"], 0) + 1
        print(f"manifest: {manifest_path}  {len(rows)} samples {by}  hash {manifest_hash(rows)}")
        if leaks:
            print("WARNING building in more than one split:", leaks)
            return 2
        return 0
    if manifest_path.is_file():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows, data = man["samples"], Path(man.get("data_root") or data)
        if not data.is_dir():
            data = Path(args.data)
        frozen = True
    else:
        rows, frozen = build_manifest(data), False
        print(f"note: no frozen manifest at {manifest_path}; using the current folder (run --freeze-manifest)")
    leaks = leakage(rows)
    if leaks:
        print("WARNING building in more than one split (results are optimistic):", leaks)
    samples = [r for r in rows if args.split == "all" or r["split"] == args.split]
    if not samples:
        sys.exit(f"no {args.split} samples")
    stages = [s for s in args.stages.split(",") if s in STAGES]
    configs = configs_from(args)
    out = Path(args.out) if args.out else ROOT / "dataset" / "gt" / "eval" / time.strftime("%Y%m%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(samples)} {args.split} samples x {len(configs)} models x {len(stages)} stages -> {out}")

    from trace_engine.canonical import Frame
    from trace_engine.compiler import compile_trace
    from trace_engine.regularize import RegularizeConfig
    from trace_engine.vision import trace_image

    reg_cfg = RegularizeConfig(gap_repair_mm=args.gap_repair_mm)
    results = []
    for name, weights, bias in configs:
        print(f"\n== {name}" + (f"  ({weights})" if weights else ""))
        for s in samples:
            img = data / s["image_path"]
            ppm = (s.get("scale") or {}).get("px_per_mm")
            gt = load_gt(data / s["ground_truth_path"])
            with Image.open(img) as im:
                shape = (im.size[1], im.size[0])
            t0 = time.time()
            error, trace = "", None
            try:
                if weights and str(weights).startswith(R2SEQ):
                    from trace_engine.raster2seq_inference import trace_image as trace_r2seq

                    r2_mm_per_px = (1.0 / ppm) if ppm else 10.0
                    trace, _ = trace_r2seq(str(img), mm_per_px=r2_mm_per_px,
                                           weights_path=str(weights)[len(R2SEQ):])
                else:
                    trace, _ = trace_image(str(img), mm_per_px=(1.0 / ppm) if ppm else None, ocr=False,
                                           engine="classic" if weights is None else "neural", weights=weights,
                                           wall_logit_bias=bias,
                                           close_rooms=True if (args.close_rooms and weights is not None) else None,
                                           require_door_evidence={"auto": None, "on": True, "off": False}[args.require_door_evidence])
            except Exception as exc:  # a failed trace scores zero, it is not skipped
                error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc(limit=1)
            trace_s = time.time() - t0
            for stage in stages:
                t1 = time.time()
                row = {"model": name, "stage": stage, "sample": s["sample_id"], "building": s["building_id"],
                       "error": error}
                pr = {"walls": [], "seps": [], "ops": {"door": [], "window": []}, "rooms": None if stage == "raw" else [],
                      "invalid": 0, "rejected": 0}
                if trace is not None:
                    try:
                        if stage == "raw":
                            pr = pred_raw(trace)
                        else:
                            comp = compile_trace(trace, geometry_pipeline=stage, regularize_config=reg_cfg)
                            frame = Frame.of(trace)
                            if weights and str(weights).startswith(R2SEQ):
                                # the Raster2Seq bridge emits millimetres; score in image pixels like the others
                                import dataclasses

                                frame = dataclasses.replace(frame, mm_per_unit=r2_mm_per_px)
                            pr = pred_compiled(comp, frame)
                    except Exception as exc:
                        row["error"] += f" {stage}: {type(exc).__name__}: {exc}"
                        traceback.print_exc(limit=2)
                compile_s = time.time() - t1
                metrics, lab = score(gt, pr, shape, ppm, args)
                row.update(metrics)
                row.update(trace_seconds=round(trace_s, 2), compile_seconds=round(compile_s, 2),
                           seconds=round(trace_s + compile_s, 2))
                if not args.no_overlays:
                    ov = out / "overlays" / f"{name}__{stage}" / f"{s['sample_id']}.jpg"
                    try:
                        overlay(img, ov, gt, pr, lab)
                        row["overlay"] = str(ov.relative_to(out)).replace("\\", "/")
                    except Exception as exc:
                        row["overlay"] = ""
                        row["error"] += f" overlay: {exc}"
                else:
                    row["overlay"] = ""
                results.append(row)
                print(f"  {stage:9} {s['sample_id'][:40]:40} walls {row['wall_f1']:.2f}  doors {row['door_tp']}/{row['door_gt']}"
                      f"  windows {row['window_tp']}/{row['window_gt']}  rooms {row['room_tp']}/{row['room_gt']}"
                      + (f"  ERROR {row['error'][:60]}" if row["error"] else ""))

    keys = list(results[0].keys())
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(results)

    def summ(rs):
        def mean(k):
            v = [r[k] for r in rs if isinstance(r.get(k), (int, float)) and r[k] == r[k] and r[k] >= 0]
            return float(np.mean(v)) if v else float("nan")

        def pooled(kind):
            return prf(sum(r[f"{kind}_tp"] for r in rs), sum(r[f"{kind}_gt"] for r in rs),
                       sum(r[f"{kind}_pred"] for r in rs))
        s = {"samples": len(rs), "failed": sum(1 for r in rs if r["error"]),
             **{k: mean(k) for k in ("wall_precision", "wall_recall", "wall_f1", "room_mean_iou", "room_boundary_err_mm",
                                      "adjacency_recall", "adjacency_precision", "opening_host_rate",
                                      "opening_width_err_mm", "seconds", "trace_seconds", "compile_seconds")},
             "invalid_changes": int(sum(max(0, r["invalid_changes"]) for r in rs)),
             "rejected": int(sum(r["rejected"] for r in rs))}
        for kind in ("junction", "door", "window", "opening", "room"):
            p, r, f = pooled(kind)
            s.update({f"{kind}_precision": p, f"{kind}_recall": r, f"{kind}_f1": f})
        return s

    groups = [(n, st) for n, _, _ in configs for st in stages]
    summary = {
        "settings": {"manifest": str(manifest_path) if frozen else None, "manifest_hash": manifest_hash(rows),
                     "split": args.split, "samples": [s["sample_id"] for s in samples],
                     "wall_tol_mm": args.wall_tol_mm, "junction_tol_mm": args.junction_tol_mm,
                     "open_tol_mm": args.open_tol_mm, "min_room_m2": args.min_room_m2,
                     "gap_repair_mm": args.gap_repair_mm, "close_rooms": bool(args.close_rooms), "leakage": leaks},
        "results": {f"{n} | {st}": summ([r for r in results if r["model"] == n and r["stage"] == st]) for n, st in groups},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    print("\n" + f"{'model | stage':36}" + "".join(f"{c[1][:11]:>12}" for c in COLS))
    for key, s in summary["results"].items():
        print(f"{key:36}" + "".join(f"{s[k]:>12.2f}" for k, _ in COLS))

    fmt = lambda v, k: ("-" if v != v else (f"{v:.0f}" if k.endswith("_mm") or k in ("invalid_changes", "rejected")
                                            else f"{v:.1f}" if k == "seconds" else f"{v:.2f}"))
    best = {}
    for k, _ in COLS:
        vals = [s[k] for s in summary["results"].values() if s[k] == s[k]]
        if vals and k != "seconds":
            best[k] = min(vals) if k in LOWER_IS_BETTER else max(vals)
    th = "".join(f"<th>{html.escape(c[1])}</th>" for c in COLS)
    trs = "".join("<tr><td><b>" + html.escape(n) + "</b></td>" + "".join(
        f"<td class='{'best' if k in best and s[k] == best[k] else ''}'>{fmt(s[k], k)}</td>" for k, _ in COLS) + "</tr>"
        for n, s in summary["results"].items())
    by_sample: Dict[str, list] = {}
    for r in results:
        by_sample.setdefault(r["sample"], []).append(r)
    cards = ""
    for sname, rs in by_sample.items():
        cells = "".join(
            f"<div class=c><div class=h><b>{html.escape(r['model'])} · {r['stage']}</b> walls {fmt(r['wall_f1'], '')}"
            f" · doors {r['door_tp']}/{r['door_gt']} (+{r['door_pred'] - r['door_tp']}) · windows {r['window_tp']}/{r['window_gt']}"
            f" · rooms {r['room_tp']}/{r['room_gt']}"
            + (f" <span class=e>{html.escape(r['error'][:80])}</span>" if r["error"] else "") + "</div>"
            + (f"<a href='{r['overlay']}' target=_blank><img loading=lazy src='{r['overlay']}'></a>" if r["overlay"] else "")
            + "</div>" for r in rs)
        cards += f"<h3>{html.escape(sname)}</h3><div class=row>{cells}</div>"
    leak_html = f"<p class=e>Building in more than one split: {html.escape(str(leaks))}</p>" if leaks else ""
    page = f"""<!doctype html><meta charset=utf-8><title>Trace evaluation</title>
<style>body{{font:14px system-ui;margin:16px;background:#f6f6f8;color:#222}}table{{border-collapse:collapse;background:#fff}}
td,th{{border:1px solid #ddd;padding:5px 8px;text-align:right}}td:first-child,th:first-child{{text-align:left}}.best{{background:#dff5df;font-weight:600}}
.row{{display:flex;gap:10px;overflow-x:auto}}.c{{flex:0 0 400px;background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px}}.c img{{width:100%}}
.h{{font-size:12px;margin-bottom:4px}}.e{{color:#c33}}.k{{font-size:13px;color:#555;max-width:980px}}</style>
<h2>Trace evaluation — {len(samples)} {html.escape(args.split)} plans</h2>
<p class=k>Manifest {'frozen' if frozen else '<b>not frozen</b>'} (hash {manifest_hash(rows)}). Walls within {args.wall_tol_mm:.0f} mm,
junctions within {args.junction_tol_mm:.0f} mm, openings within {args.open_tol_mm:.0f} mm, rooms at IoU ≥ 0.5.
<b>raw</b> = recogniser output, <b>legacy</b> = shipped compile path, <b>canonical</b> = new pipeline. Best per column in green.</p>{leak_html}
<table><tr><th>model | stage</th>{th}</tr>{trs}</table>
<p class=k>Overlays: <b style="color:#0a0">green</b> = IFC truth, <b style="color:#d00">red</b> = traced walls, purple = separators,
squares = traced doors (red) / windows (orange), tints = traced rooms.</p>{cards}"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"\nreport: {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
