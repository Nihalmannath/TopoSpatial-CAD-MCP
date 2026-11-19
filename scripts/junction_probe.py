"""Measure what the network's 21 junction heatmaps do on our validation plans.

The CubiCasa model outputs 21 heatmaps (13 wall-junction types, 4 opening ends,
4 icon corners) that the tracer and the fine-tune both ignore so far. Before
training them we need to know, from the pretrained weights themselves:
which channel fires for which junction shape, how high and how wide its peaks
are, and how many of our labelled junctions it already finds.

    .venv\\Scripts\\python.exe scripts\\junction_probe.py

Reads dataset/gt/training (val split, samples with graph.json) and writes
dataset/gt/junction_probe/: table.json (one row per labelled junction / opening
end and model), patches.npz, maps/*.npz (compact maps), full/*.npz (all 21
channels for two plans). Nothing is trained or changed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

R_BIT, D_BIT, L_BIT, U_BIT = 1, 2, 4, 8  # image frame: x right, y down
BITS = (R_BIT, D_BIT, L_BIT, U_BIT)
PATCH = 10  # half-size of saved peak patches


def arm_bit(deg: float):
    """(bit, deviation in degrees) of the nearest image axis for an arm angle."""
    a = deg % 360.0
    k = int(round(a / 90.0)) % 4
    dev = abs(((a - 90.0 * k) + 180.0) % 360.0 - 180.0)
    return BITS[k], dev


def wall_nodes(graph: dict, axis_tol: float = 20.0) -> list:
    """Junctions of real walls (separators left out) with their arm bitmask."""
    pos = {n["id"]: (float(n["x"]), float(n["y"])) for n in graph["nodes"]}
    arms = {k: [] for k in pos}
    sep = {k: 0 for k in pos}
    thick = {k: [] for k in pos}
    for e in graph["edges"]:
        a, b = e["source"], e["target"]
        if a not in pos or b not in pos:
            continue
        if e.get("type") == "separator":
            sep[a] += 1
            sep[b] += 1
            continue
        (ax, ay), (bx, by) = pos[a], pos[b]
        arms[a].append(math.degrees(math.atan2(by - ay, bx - ax)))
        arms[b].append(math.degrees(math.atan2(ay - by, ax - bx)))
        thick[a].append(float(e.get("thickness_px", 0)))
        thick[b].append(float(e.get("thickness_px", 0)))
    out = []
    for k, (x, y) in pos.items():
        if not arms[k]:
            continue
        mask, ok = 0, True
        for d in arms[k]:
            bit, dev = arm_bit(d)
            ok = ok and dev <= axis_tol and not (mask & bit)
            mask |= bit
        out.append({"id": k, "x": x, "y": y, "mask": mask, "nwall": len(arms[k]), "nsep": sep[k],
                    "axis_ok": bool(ok), "thick": float(np.median(thick[k])) if thick[k] else 0.0})
    return out


def opening_ends(graph: dict) -> list:
    out = []
    for i, o in enumerate(graph.get("openings", [])):
        e = o.get("ends_px") or []
        if len(e) != 2:
            continue
        for j in (0, 1):
            (x, y), (ox, oy) = e[j], e[1 - j]
            bit, dev = arm_bit(math.degrees(math.atan2(oy - y, ox - x)))
            out.append({"opening": i, "end": j, "kind": o.get("kind"), "x": float(x), "y": float(y),
                        "toward": bit, "axis_ok": bool(dev <= 20.0), "width": float(o.get("width_px", 0))})
    return out


def window(arr: np.ndarray, x: float, y: float, r: int):
    h, w = arr.shape[-2:]
    cx, cy = int(round(x)), int(round(y))
    x0, x1, y0, y1 = max(0, cx - r), min(w, cx + r + 1), max(0, cy - r), min(h, cy + r + 1)
    if x0 >= x1 or y0 >= y1:
        return None, (cx, cy, x0, y0)
    return arr[..., y0:y1, x0:x1], (cx, cy, x0, y0)


def peaks(heat: np.ndarray, x: float, y: float, r: int):
    """Per-channel max within r px, the value at the point, and the best channel's peak position."""
    win, (cx, cy, x0, y0) = window(heat, x, y, r)
    c = heat.shape[0]
    if win is None:
        return [0.0] * c, [0.0] * c, None
    pk = win.reshape(c, -1).max(1)
    h, w = heat.shape[1:]
    at = heat[:, min(max(cy, 0), h - 1), min(max(cx, 0), w - 1)]
    best = int(pk.argmax())
    iy, ix = np.unravel_index(int(win[best].argmax()), win[best].shape)
    return [round(float(v), 3) for v in pk], [round(float(v), 3) for v in at], (best, int(x0 + ix), int(y0 + iy))


def patch(ch: np.ndarray, px: int, py: int) -> np.ndarray:
    out = np.zeros((2 * PATCH + 1, 2 * PATCH + 1), np.float16)
    h, w = ch.shape
    x0, x1, y0, y1 = max(0, px - PATCH), min(w, px + PATCH + 1), max(0, py - PATCH), min(h, py + PATCH + 1)
    out[y0 - (py - PATCH):y1 - (py - PATCH), x0 - (px - PATCH):x1 - (px - PATCH)] = ch[y0:y1, x0:x1]
    return out


def probe_sample(heat: np.ndarray, graph: dict, radius: int, tag: dict, valid: np.ndarray | None = None):
    """Rows + patches for one plan and one model. ``heat`` is (21, H, W)."""
    rows, wp, op = [], [], []
    wall, opening = heat[:13], heat[13:17]
    for n in wall_nodes(graph):
        pk, at, best = peaks(wall, n["x"], n["y"], radius)
        row = dict(tag, row="wall", **n, peak=pk, at=at)
        if best is not None:
            row.update(best=best[0], dx=best[1] - int(round(n["x"])), dy=best[2] - int(round(n["y"])), patch=len(wp))
            wp.append(patch(wall[best[0]], best[1], best[2]))
        rows.append(row)
    for e in opening_ends(graph):
        pk, at, best = peaks(opening, e["x"], e["y"], radius)
        row = dict(tag, row="opening", **e, peak=pk, at=at)
        if best is not None:
            row.update(best=best[0], patch=len(op))
            op.append(patch(opening[best[0]], best[1], best[2]))
        rows.append(row)
    region = heat if valid is None else heat[:, valid]
    flat = region.reshape(heat.shape[0], -1)
    stats = {"p50": np.percentile(flat, 50, axis=1), "p99": np.percentile(flat, 99, axis=1),
             "p999": np.percentile(flat, 99.9, axis=1), "max": flat.max(1), "min": flat.min(1)}
    stats = {k: [round(float(v), 3) for v in a] for k, a in stats.items()}
    return rows, wp, op, stats


def compact_maps(heat: np.ndarray) -> dict:
    return {"wallmax": heat[:13].max(0).astype(np.float16), "wallarg": heat[:13].argmax(0).astype(np.uint8),
            "openmax": heat[13:17].max(0).astype(np.float16), "openarg": heat[13:17].argmax(0).astype(np.uint8),
            "iconmax": heat[17:21].max(0).astype(np.float16)}


def val_samples(root: Path) -> list:
    out = []
    for d in sorted(p for p in root.iterdir() if (p / "graph.json").is_file() and (p / "image.png").is_file()):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").is_file() else {}
        if meta.get("split") == "val":
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(REPO / "dataset" / "gt" / "training"))
    ap.add_argument("--out", default=str(REPO / "dataset" / "gt" / "junction_probe"))
    ap.add_argument("--model", action="append", default=[], help="NAME=path (repeatable)")
    ap.add_argument("--radius", type=int, default=8, help="search radius in px around each labelled junction")
    ap.add_argument("--maps", type=int, default=16, help="plans to save compact maps for")
    ap.add_argument("--full", type=int, default=2, help="plans to save all 21 channels for")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import torch
    import torch.nn.functional as F
    from PIL import Image

    from finetune_cubicasa import SPLIT, build_model, to_tensor

    models = [m.split("=", 1) for m in args.model] or [
        ["pretrained", str(REPO / "models" / "pretrained" / "model_best_val_loss_var.pkl")],
        ["A", str(REPO / "models" / "checkpoints" / "cubicasa_finetuned_A.pth")]]
    models = [(n, Path(p)) for n, p in models if Path(p).is_file()]
    if not models:
        sys.exit("no model weights found")
    samples = val_samples(Path(args.data))
    if args.limit:
        samples = samples[:args.limit]
    if not samples:
        sys.exit(f"no validation samples with graph.json in {args.data}")
    by_size = sorted(samples, key=lambda d: (d / "image.png").stat().st_size)
    full_set = {d.name for d in by_size[len(by_size) // 2:len(by_size) // 2 + args.full]}
    map_set = {d.name for d in samples[::max(1, len(samples) // max(1, args.maps))][:args.maps]} | full_set
    out = Path(args.out)
    (out / "maps").mkdir(parents=True, exist_ok=True)
    (out / "full").mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device}; {len(samples)} validation plans; models: {[n for n, _ in models]}")

    rows, stats, wall_p, open_p = [], [], [], []
    for name, path in models:
        model = build_model(path).to(device)
        model.eval()
        t0 = time.time()
        for si, d in enumerate(samples):
            img = np.asarray(Image.open(d / "image.png").convert("RGB"), np.uint8)
            graph = json.loads((d / "graph.json").read_text(encoding="utf-8"))["graph_px"]
            h, w = img.shape[:2]
            x = F.pad(to_tensor(img)[None], (0, (-w) % 64, 0, (-h) % 64), value=1.0).to(device)
            with torch.no_grad():
                heat = model(x)[0, :SPLIT[0], :h, :w].float().cpu().numpy()
            valid = None
            if (d / "ignore.png").is_file():
                ig = np.asarray(Image.open(d / "ignore.png"))
                if ig.shape == (h, w) and (ig == 0).any():
                    valid = ig == 0
            r, wp, op, st = probe_sample(heat, graph, args.radius, {"model": name, "sample": d.name}, valid)
            for row in r:
                if "patch" in row:
                    row["patch"] += len(wall_p) if row["row"] == "wall" else len(open_p)
            rows += r
            wall_p += wp
            open_p += op
            stats.append({"model": name, "sample": d.name, "size": [w, h], **st})
            if d.name in map_set:
                np.savez_compressed(out / "maps" / f"{name}__{d.name}.npz", **compact_maps(heat))
            if d.name in full_set:
                np.savez_compressed(out / "full" / f"{name}__{d.name}.npz", heat=heat.astype(np.float16))
            print(f"  {name} {si + 1}/{len(samples)} {d.name[:40]}  wall-heat max {heat[:13].max():.2f}", flush=True)
        print(f"{name}: {time.time() - t0:.0f} s")
        del model
    (out / "table.json").write_text(json.dumps({"radius": args.radius, "rows": rows, "stats": stats,
                                                "samples": [d.name for d in samples],
                                                "models": [n for n, _ in models]}), encoding="utf-8")
    np.savez_compressed(out / "patches.npz", wall=np.stack(wall_p) if wall_p else np.zeros((0, 21, 21), np.float16),
                        opening=np.stack(open_p) if open_p else np.zeros((0, 21, 21), np.float16))
    print(f"done: {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
