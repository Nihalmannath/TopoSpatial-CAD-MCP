"""Diagnose a neural trace on one plan: what the network sees, what the crop keeps.

Writes to scratch/diag_<image stem>/:
  mask_b<bias>.png         network wall mask (red) over the plan, crop box (blue)
  overlay_<variant>.png    traced walls / rooms / openings for each variant
  trace_<variant>.json     the raw trace (pixels)
  summary.json             counts, crop box, wall pixels per image third

Variants: the promoted setting (bias 2), bias 0 and 3, and bias 2 with the
post-segmentation crop disabled (whole image kept).

    .venv\\Scripts\\python.exe scripts\\diagnose_trace.py scratch\\ground_floor_plan.jpg --mm-per-px 12.2
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_engine import compile_trace, neural, vision  # noqa: E402
from trace_engine._cv import cv2  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", default=str(ROOT / "scratch" / "ground_floor_plan.jpg"))
    ap.add_argument("--mm-per-px", type=float, default=None)
    args = ap.parse_args(argv)
    img_path = Path(args.image)
    if not img_path.is_absolute():
        img_path = ROOT / img_path
    out = ROOT / "scratch" / f"diag_{img_path.stem}"
    out.mkdir(parents=True, exist_ok=True)
    weights = neural.find_weights(None)
    print("image:", img_path, "\nweights:", weights, "\nout:", out)
    image = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
    h, w = image.shape[:2]
    base = Image.open(img_path).convert("RGB")
    summary = {"image": str(img_path), "size": [w, h], "weights": str(weights), "masks": {}, "variants": {}}

    # 1. Raw network masks (full image, no crop) per bias.
    for bias in (0.0, 2.0, 3.0):
        seg = neural.segment(image, None, weights, wall_logit_bias=bias)
        walls = seg["walls"].astype(bool)
        thirds = [int(walls[:, k * w // 3:(k + 1) * w // 3].sum()) for k in range(3)]
        try:
            t = vision.estimate_wall_thickness(seg["walls"], min_px=4)
        except ValueError:
            t = 10.0
        try:
            box = vision.plan_region(seg["walls"], t)
        except ValueError:
            box = None
        vis = np.array(base).copy()
        vis[walls] = (0.4 * vis[walls] + 0.6 * np.array([230, 0, 0])).astype(np.uint8)
        im = Image.fromarray(vis)
        if box:
            from PIL import ImageDraw

            x, y, bw, bh = box
            ImageDraw.Draw(im).rectangle([x, y, x + bw, y + bh], outline=(0, 90, 255), width=3)
        im.save(out / f"mask_b{bias:g}.png")
        summary["masks"][f"b{bias:g}"] = {"wall_px_by_third_left_to_right": thirds, "thickness_px": round(t, 2),
                                          "plan_region_xywh": list(box) if box else None,
                                          "windows_px": int(seg["windows"].sum()), "doors_px": int(seg["doors"].sum())}
        print(f"bias {bias:g}: wall px by third {thirds}, crop box {box}")

    # 2. Full traces for the variants.
    orig_region = vision.plan_region
    variants = [("b2", 2.0, False), ("b0", 0.0, False), ("b3", 3.0, False), ("b2_nocrop", 2.0, True)]
    for name, bias, nocrop in variants:
        try:
            if nocrop:
                vision.plan_region = lambda walls, thickness: (0, 0, walls.shape[1], walls.shape[0])
            trace, rep = vision.trace_image(str(img_path), mm_per_px=args.mm_per_px, ocr=True, engine="neural",
                                            weights=str(weights), wall_logit_bias=bias)
            comp = compile_trace(trace, geometry_pipeline="canonical")
            vision.render_overlay(str(img_path), trace, comp, str(out / f"overlay_{name}.png"))
            (out / f"trace_{name}.json").write_text(json.dumps(trace.model_dump(by_alias=True)), encoding="utf-8")
            s = comp.summary()
            summary["variants"][name] = {"counts": s["counts"], "rooms": s["rooms"], "bbox": list(rep.plan_bbox),
                                         "scale_mm_per_px": rep.mm_per_px, "scale_from": rep.scale_method,
                                         "labels": [(lab.text, lab.size_cm) for lab in rep.labels],
                                         "warnings": s["warnings"][:15]}
            print(f"{name}: {s['counts']}  bbox {rep.plan_bbox}  scale {rep.mm_per_px:.2f} ({rep.scale_method})")
        except Exception as exc:
            summary["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            traceback.print_exc()
        finally:
            vision.plan_region = orig_region
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print("\nwrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
