"""Apply align_fixes.json (from align_weak/index.html) to weak candidate masks.
usage: python apply_alignment.py <gt_dir> <align_fixes.json>"""
import json, os, sys, shutil
import numpy as np
from PIL import Image
G, F = sys.argv[1], sys.argv[2]
fixes = json.load(open(F))
def affine(t, W, H):
    cx, cy = W / 2, H / 2
    # output q = c + s*(p-c) + t  ->  p = (q - c - t)/s + c
    a, e = 1 / t["sx"], 1 / t["sy"]
    return (a, 0, cx - (cx + t["tx"]) * a, 0, e, cy - (cy + t["ty"]) * e)
ok = []
for cid, t in fixes.items():
    if t.get("st") != "ok": continue
    d = f"{G}/candidates/{cid}"
    if not os.path.isdir(d): print("missing", cid); continue
    o = f"{d}/_orig"
    if not os.path.isdir(o):
        os.makedirs(o); [shutil.copy2(f"{d}/{n}", o) for n in ("rooms.png", "icons.png", "ignore.png", "overlay.png", "meta.json", "graph.json") if os.path.exists(f"{d}/{n}")]
    W, H = Image.open(f"{d}/image.png").size; A = affine(t, W, H)
    tr = lambda im, fill: im.transform((W, H), Image.AFFINE, A, resample=Image.NEAREST, fillcolor=fill)
    rooms = tr(Image.open(f"{o}/rooms.png"), 0); icons = tr(Image.open(f"{o}/icons.png"), 0)
    cover = np.array(tr(Image.new("L", (W, H), 255), 0)) > 0
    ig = np.array(tr(Image.open(f"{o}/ignore.png"), 255)) if os.path.exists(f"{o}/ignore.png") else np.zeros((H, W), np.uint8)
    ig[~cover] = 255
    rooms.save(f"{d}/rooms.png"); icons.save(f"{d}/icons.png"); Image.fromarray(ig.astype(np.uint8)).save(f"{d}/ignore.png")
    img = Image.open(f"{d}/image.png").convert("RGB"); r = np.array(rooms); c = np.array(icons); a = np.array(img).copy()
    a[r == 2] = (a[r == 2] * 0.3 + np.array([230, 0, 0]) * 0.7).astype(np.uint8)
    a[c == 1] = (0, 110, 255); a[c == 2] = (0, 190, 0)
    Image.fromarray(a).save(f"{d}/overlay.png")
    # move the ground-truth wall graph (used by eval_trace.py) the same way
    gpath = f"{o}/graph.json" if os.path.exists(f"{o}/graph.json") else None
    if gpath is None and os.path.exists(f"{d}/graph.json"):
        shutil.copy2(f"{d}/graph.json", o); gpath = f"{o}/graph.json"
    if gpath:
        cx, cy = W / 2, H / 2
        P = lambda p: [round(cx + t["sx"] * (p[0] - cx) + t["tx"], 2), round(cy + t["sy"] * (p[1] - cy) + t["ty"], 2)]
        g = json.load(open(gpath)); gp = g["graph_px"]
        for n in gp.get("nodes", []): n["x"], n["y"] = P((n["x"], n["y"]))
        for e in gp.get("edges", []):
            if "thickness_px" in e: e["thickness_px"] = round(e["thickness_px"] * (abs(t["sx"]) + abs(t["sy"])) / 2, 2)
        for op in gp.get("openings", []):
            if "ends_px" in op: op["ends_px"] = [P(p) for p in op["ends_px"]]
        for z in gp.get("zones", []):
            if "seed_px" in z: z["seed_px"] = P(z["seed_px"])
            if "polygon_px" in z: z["polygon_px"] = [P(p) for p in z["polygon_px"]]
        g["manual_alignment"] = t
        json.dump(g, open(f"{d}/graph.json", "w"), indent=1)
    m = json.load(open(f"{d}/meta.json")); m["manual_alignment"] = t; m["bucket"] = "manual_aligned"
    json.dump(m, open(f"{d}/meta.json", "w"), indent=1)
    ok.append(cid)
open(f"{G}/weak_accepted.txt", "w").write("\n".join(ok) + "\n")
print(f"aligned {len(ok)} samples -> {G}/weak_accepted.txt  (originals kept in candidates/<id>/_orig)")
