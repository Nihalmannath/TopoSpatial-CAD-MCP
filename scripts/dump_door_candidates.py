"""Dump every door candidate the tracer checks for a swing, labelled true/false.

Hooks ``vision._detect_swing`` while tracing the ResPlan Test B images, crops
the dark mask around each door candidate and labels it against ground truth:

  true_door   a ground-truth door centre is within --tol-mm
  window      a ground-truth window is there instead
  false       nothing true nearby (a bare gap)

The swing detector can then be tuned offline on the dump without re-tracing
(see scripts/measure_swing.py).

    .venv\\Scripts\\python.exe scripts\\dump_door_candidates.py --limit 200
    .venv\\Scripts\\python.exe scripts\\dump_door_candidates.py --limit 60 --model J=models\\checkpoints\\cubicasa_finetuned_J.pth

Output: dataset/resplan_gt/door_candidates_<model>.pkl.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import pickle
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "dataset" / "resplan_gt"))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--tol-mm", type=float, default=400.0)
    ap.add_argument("--model", default="classic", help="classic, or name=weights for the neural engine")
    args = ap.parse_args()

    from trace_engine import vision

    data = Path(args.data)
    man = json.loads((data / "eval_manifest.json").read_text(encoding="utf-8"))
    samples = [s for s in man["samples"] if s["split"] == "test"][: args.limit or None]
    name, _, weights = args.model.partition("=")
    weights = weights or None

    original = vision._detect_swing
    current: dict = {}
    records: list = []

    def recorder(dark, op, t):
        original(dark, op, t)
        w = op.hi - op.lo
        m = int(math.ceil(w * 1.4 + t + 8))
        mid = (op.lo + op.hi) / 2
        cx, cy = (mid, op.c) if op.axis == "h" else (op.c, mid)
        x0, y0 = max(0, int(cx) - m), max(0, int(cy) - m)
        x1, y1 = min(dark.shape[1], int(cx) + m + 1), min(dark.shape[0], int(cy) + m + 1)
        crop = np.packbits(dark[y0:y1, x0:x1] > 0, axis=None)
        current["calls"].append({
            "sample": current["id"], "axis": op.axis, "c": float(op.c), "lo": float(op.lo), "hi": float(op.hi),
            "t": float(t), "origin": (x0, y0), "shape": (y1 - y0, x1 - x0), "crop_bits": crop,
            "detected": bool(op.swing_detected), "centre": (float(cx), float(cy)),
        })

    vision._detect_swing = recorder
    t0 = time.time()
    for n, s in enumerate(samples, 1):
        ppm = s["scale"]["px_per_mm"]
        tol = args.tol_mm * ppm
        gt = json.loads((data / s["ground_truth_path"]).read_text())["graph_px"]["openings"]
        gt_c = [(o["kind"], ((o["ends_px"][0][0] + o["ends_px"][1][0]) / 2, (o["ends_px"][0][1] + o["ends_px"][1][1]) / 2),
                 o["width_px"]) for o in gt]
        current.update(id=s["sample_id"], calls=[])
        try:
            vision.trace_image(str(data / s["image_path"]), mm_per_px=1.0 / ppm, ocr=False,
                               engine="classic" if weights is None else "neural", weights=weights,
                               close_rooms=True if weights else None)
        except Exception as exc:
            print(f"  {s['sample_id']}: {type(exc).__name__}: {exc}")
        for rec in current["calls"]:
            best = min(((math.dist(rec["centre"], c), k, w) for k, c, w in gt_c), default=(1e9, None, None))
            rec["label"] = ("true_door" if best[1] == "door" else "window") if best[0] <= tol else "false"
            rec["gt_width"] = best[2] if best[0] <= tol else None
            rec["px_per_mm"] = ppm
            records.append(rec)
        if n % 20 == 0:
            print(f"  {n}/{len(samples)}  candidates {len(records)}  {time.time() - t0:.0f}s")
    vision._detect_swing = original

    out = data / f"door_candidates_{name}.pkl.gz"
    with gzip.open(out, "wb") as f:
        pickle.dump(records, f)
    labels = {}
    for r in records:
        key = (r["label"], r["detected"])
        labels[key] = labels.get(key, 0) + 1
    print("label, swing found by current detector -> count")
    for k in sorted(labels):
        print("  ", k, labels[k])
    print(f"saved {out}")


if __name__ == "__main__":
    main()
