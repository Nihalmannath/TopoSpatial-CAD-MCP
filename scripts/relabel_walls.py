"""Rebuild wall labels from what the plan actually draws (writes a NEW training folder).

Why: ``dataset/gt/training/*/rooms.png`` paints every IFC wall at its nominal IFC thickness
(350 mm external / 200 mm internal). Brochure plans draw thinner walls, so a model trained on
those labels learns fat wall bands; the fat masks then merge and the geometry step loses rooms
(audit of 2 Nov 2026: pixel IoU rose, rooms fell). This script keeps an annotated wall pixel only
where the image shows a wall:

    evidence  = dark pixels (Otsu) and/or the pretrained network's wall mask (hatched walls)
    new wall  = evidence  INSIDE the annotated wall band grown by ``slack`` px  (opened to drop specks)
    leftover  = annotated wall pixels with no drawn evidence  ->  255 (ignore; the label is unknown)

Only ``rooms.png`` changes. Everything else (image, icons, ignore, graph, meta) is copied, so the
old folder stays valid and nothing is overwritten.

    .venv\\Scripts\\python.exe scripts\\relabel_walls.py --limit 3          # try three plans first
    .venv\\Scripts\\python.exe scripts\\relabel_walls.py                      # all plans -> dataset\\gt\\training_v2
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

IGNORE = 255
WALL = 2  # ROOM_CLASSES.index("Wall")


# ----------------------------------------------------------------------------- pure core
def grow(mask: np.ndarray, radius: float) -> np.ndarray:
    """Boolean mask dilated by ``radius`` pixels (Euclidean)."""
    from scipy import ndimage

    if radius <= 0:
        return mask.astype(bool)
    return ndimage.distance_transform_edt(~mask.astype(bool)) <= radius


def drop_specks(mask: np.ndarray, size: int = 3) -> np.ndarray:
    from scipy import ndimage

    return ndimage.binary_opening(mask.astype(bool), structure=np.ones((size, size), bool))


def relabel_walls(rooms: np.ndarray, evidence: np.ndarray, slack_px: float, wall_cls: int = WALL,
                  ignore: int = IGNORE, speck: int = 3) -> Tuple[np.ndarray, Dict[str, float]]:
    """New rooms map: wall only where ``evidence`` shows a wall near an annotated wall."""
    old = rooms == wall_cls
    band = grow(old, slack_px)
    new = drop_specks(evidence.astype(bool) & band, speck)
    out = rooms.copy()
    out[old & ~new] = ignore          # annotated wall with no drawn wall: unknown
    out[new] = wall_cls
    n_old, n_new = int(old.sum()), int(new.sum())
    stats = {"wall_px_old": n_old, "wall_px_new": n_new,
             "wall_px_ratio": n_new / n_old if n_old else float("nan"),
             "newly_ignored_px": int((old & ~new).sum()),
             "newly_wall_px": int((new & ~old).sum())}
    return out, stats


def slack_from_graph(graph: dict, default: float = 6.0) -> float:
    """A quarter of the median annotated wall thickness in px (misregistration tolerance)."""
    try:
        th = [e["thickness_px"] for e in graph["graph_px"]["edges"] if e.get("type") != "separator" and e.get("thickness_px")]
        return max(3.0, 0.25 * statistics.median(th)) if th else default
    except (KeyError, TypeError):
        return default


# ----------------------------------------------------------------------------- evidence
def drawn_evidence(image_rgb: np.ndarray, mode: str, weights: Optional[Path]) -> np.ndarray:
    from trace_engine._cv import cv2
    from trace_engine.vision import _dark_mask

    ev = np.zeros(image_rgb.shape[:2], bool)
    if mode in ("dark", "both"):
        gray = cv2.cvtColor(np.ascontiguousarray(image_rgb[:, :, ::-1]), cv2.COLOR_BGR2GRAY)
        ev |= _dark_mask(gray).astype(bool)
    if mode in ("model", "both"):
        from trace_engine import neural

        seg = neural.segment(np.ascontiguousarray(image_rgb[:, :, ::-1]), None, weights, wall_logit_bias=0.0)
        ev |= seg["walls"].astype(bool)
    return ev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(ROOT / "dataset" / "gt" / "training"))
    ap.add_argument("--dst", default=str(ROOT / "dataset" / "gt" / "training_v2"))
    ap.add_argument("--weights", default=str(ROOT / "models" / "pretrained" / "model_best_val_loss_var.pkl"))
    ap.add_argument("--evidence", choices=("dark", "model", "both"), default="both")
    ap.add_argument("--slack-px", type=float, default=None, help="override; default = 1/4 of the median wall thickness per plan")
    ap.add_argument("--limit", type=int, default=0, help="only the first N plans (try before the full run)")
    ap.add_argument("--only", default="", help="only plans whose folder name contains this text")
    args = ap.parse_args(argv)

    src, dst = Path(args.src), Path(args.dst)
    if dst.resolve() == src.resolve():
        sys.exit("--dst must differ from --src (the original labels are never overwritten)")
    weights = Path(args.weights)
    if args.evidence != "dark" and not weights.is_file():
        sys.exit(f"weights not found: {weights} (use --evidence dark to skip the network)")
    folders = sorted(p for p in src.iterdir() if (p / "rooms.png").is_file() and args.only in p.name)
    if args.limit:
        folders = folders[: args.limit]
    dst.mkdir(parents=True, exist_ok=True)
    print(f"{len(folders)} plans: {src} -> {dst}  evidence={args.evidence}")

    report, t0 = {}, time.time()
    for i, f in enumerate(folders, 1):
        try:
            out = dst / f.name
            out.mkdir(exist_ok=True)
            for item in f.iterdir():
                if item.is_file() and item.name != "rooms.png":
                    shutil.copy2(item, out / item.name)
            image = np.asarray(Image.open(f / "image.png").convert("RGB"), np.uint8)
            rooms = np.array(Image.open(f / "rooms.png"))
            graph = json.loads((f / "graph.json").read_text(encoding="utf-8")) if (f / "graph.json").is_file() else {}
            slack = args.slack_px if args.slack_px is not None else slack_from_graph(graph)
            ev = drawn_evidence(image, args.evidence, weights)
            new_rooms, stats = relabel_walls(rooms, ev, slack)
            Image.fromarray(new_rooms).save(out / "rooms.png")
            meta_path = out / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            meta["wall_relabel"] = {"evidence": args.evidence, "slack_px": round(slack, 2), **{k: round(v, 3) if isinstance(v, float) else v for k, v in stats.items()},
                                    "note": "wall labels restricted to drawn wall pixels; leftover IFC band = ignore (255)"}
            meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            report[f.name] = {"slack_px": round(slack, 2), **stats}
            print(f"  [{i}/{len(folders)}] {f.name[:46]:46} wall px {stats['wall_px_old']:>7} -> {stats['wall_px_new']:>7}"
                  f"  (x{stats['wall_px_ratio']:.2f})  ignored +{stats['newly_ignored_px']}  {time.time() - t0:.0f}s")
        except Exception as exc:
            traceback.print_exc(limit=2)
            report[f.name] = {"error": f"{type(exc).__name__}: {exc}"}
    (dst / "relabel_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    ratios = [r["wall_px_ratio"] for r in report.values() if r.get("wall_px_ratio") == r.get("wall_px_ratio") and "error" not in r]
    if ratios:
        print(f"\nwall pixels kept: median x{statistics.median(ratios):.2f} (min {min(ratios):.2f}, max {max(ratios):.2f}); "
              f"{sum(1 for r in report.values() if 'error' in r)} errors")
    print("wrote", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
