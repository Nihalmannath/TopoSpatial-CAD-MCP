"""Show what each recognition stage keeps, so a lost wall can be traced to the step that lost it.

Stages written to scratch/stages_<image stem>_<engine>/ :
  1_mask.png      what the segmenter (or the dark-pixel test, for classic) calls wall           (red)
  2_opened.png    after the opening filter; pixels removed here                                (orange)
  3_runs.png      straight wall runs extracted (blue); mask pixels no run covers               (orange)
  4_final.png     runs after gap bridging + separators (purple) + openings; still uncovered    (orange)
  sheet.png       the four above side by side
  lost_walls.txt  every uncovered mask blob with its size and the likely rule that dropped it

    .venv\\Scripts\\python.exe scripts\\stage_trace.py scratch\\ground_floor_plan2.jpg --engine neural --mm-per-px 12.2
    .venv\\Scripts\\python.exe scripts\\stage_trace.py scratch\\ground_floor_plan2.jpg --engine classic
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_engine import vision  # noqa: E402
from trace_engine._cv import cv2  # noqa: E402

RED, ORANGE, BLUE, PURPLE, GREEN = (40, 40, 220), (0, 140, 255), (200, 80, 20), (160, 50, 160), (60, 160, 60)  # BGR


# ----------------------------------------------------------------------------- pure helpers
def runs_mask(runs, shape: Tuple[int, int], grow: int = 0) -> np.ndarray:
    """Boolean image covered by wall runs (each run drawn at its own thickness, plus ``grow`` px)."""
    out = np.zeros(shape, np.uint8)
    for r in runs:
        half = r.t / 2.0 + grow
        if r.axis == "h":
            x0, x1, y0, y1 = r.lo - grow, r.hi + grow, r.c - half, r.c + half
        else:
            x0, x1, y0, y1 = r.c - half, r.c + half, r.lo - grow, r.hi + grow
        cv2.rectangle(out, (int(round(x0)), int(round(y0))), (int(round(x1)), int(round(y1))), 1, -1)
    return out.astype(bool)


def lost_blobs(mask: np.ndarray, covered: np.ndarray, thickness: float, min_area: float = 0.0) -> List[Dict]:
    """Connected blobs of mask pixels that no run covers, each with the rule that most likely dropped it."""
    uncovered = (mask.astype(bool) & ~covered).astype(np.uint8)
    min_area = min_area or thickness ** 2
    n, _, stats, _ = cv2.connectedComponentsWithStats(uncovered)
    out = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < min_area:
            continue
        thin, length = min(w, h), max(w, h)
        if thin < 0.5 * thickness:
            why = f"thinner than 0.5 x wall thickness ({thin} < {0.5 * thickness:.1f} px): _bands drops it"
        elif length < 1.6 * thickness:
            why = f"shorter than 1.6 x thickness ({length} < {1.6 * thickness:.0f} px): opening filter drops it"
        elif thin > 2.5 * thickness and length < 4 * thickness:
            why = "blob (column / furniture / filled area), not a wall run"
        else:
            why = "wide enough and long enough: lost in band matching or end snapping"
        out.append({"x": x, "y": y, "w": w, "h": h, "area": area, "why": why})
    return sorted(out, key=lambda b: -b["area"])


def file_sha256(path: Optional[str], limit: int = 12) -> Optional[str]:
    import hashlib

    if not path or not Path(path).is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:limit]


def stage_counts(stages: Dict, compiled_counts: Optional[Dict] = None) -> Dict[str, object]:
    """Object counts after every stage, so the first stage that loses things stands out."""
    ops = stages.get("openings", [])
    c = {"mask_wall_px": int(np.count_nonzero(stages["raw_walls"])),
         "opened_wall_px": int(np.count_nonzero(stages["walls"])),
         "wall_thickness_px": round(float(stages["thickness"]), 2),
         "runs_extracted": len(stages.get("raw_runs", [])), "runs_bridged": len(stages.get("runs", [])),
         "separators": len(stages.get("separators", [])),
         "doors_found": sum(1 for o in ops if getattr(o, "kind", "") == "door"),
         "windows_found": sum(1 for o in ops if getattr(o, "kind", "") == "window")}
    if "raw_windows" in stages:
        c["mask_window_px"] = int(np.count_nonzero(stages["raw_windows"]))
        c["mask_door_px"] = int(np.count_nonzero(stages["raw_doors"]))
    if compiled_counts is not None:
        c.update({f"compiled_{k}": v for k, v in compiled_counts.items()})
    return c


def run_record(img_path: Path, args, stages: Dict, trace) -> Dict[str, object]:
    """Compile like the audit does (canonical) and describe exactly what ran."""
    compiled, error = None, None
    try:
        from trace_engine import compile_trace

        comp = compile_trace(trace, geometry_pipeline="canonical")
        compiled = {k: comp.summary()["counts"].get(k, 0) for k in ("wall", "room", "door", "window")}
    except Exception as exc:  # report, don't hide: a compile failure is itself a finding
        error = f"{type(exc).__name__}: {exc}"
    weights = stages.get("weights_path")
    return {"image": str(img_path), "engine": args.engine, "mm_per_px_given": args.mm_per_px,
            "weights": weights, "weights_sha256": file_sha256(weights),
            "wall_logit_bias": stages.get("wall_logit_bias"), "network_region": stages.get("network_region"),
            "thickness_source": stages.get("thickness_source"),
            "plan_bbox": [int(v) for v in stages["bbox"]], "early_exit": stages.get("early_exit"),
            "compile_error": error, "counts": stage_counts(stages, compiled)}


def save_stage_data(out: Path, stages: Dict) -> None:
    """Exact stage inputs, so extraction can be replayed without the network:
    mask_raw.png / mask_opened.png (0 or 255) and stages.json (thickness, scale, box, runs)."""
    import json

    cv2.imwrite(str(out / "mask_raw.png"), (np.asarray(stages["raw_walls"]) > 0).astype(np.uint8) * 255)
    cv2.imwrite(str(out / "mask_opened.png"), (np.asarray(stages["walls"]) > 0).astype(np.uint8) * 255)
    for key in ("raw_windows", "raw_doors"):
        if key in stages:
            cv2.imwrite(str(out / f"mask_{key[4:]}.png"), (np.asarray(stages[key]) > 0).astype(np.uint8) * 255)

    def run(r):
        return {"axis": r.axis, "c": float(r.c), "lo": float(r.lo), "hi": float(r.hi), "t": float(r.t)}

    data = {"thickness_px": float(stages["thickness"]), "mm_per_px": float(stages["mm_per_px"]),
            "bbox": [int(v) for v in stages["bbox"]],
            "raw_runs": [run(r) for r in stages["raw_runs"]], "runs": [run(r) for r in stages["runs"]],
            "separators": [[[float(v) for v in s[0]], [float(v) for v in s[1]]] for s in stages["separators"]],
            "openings": [{"kind": getattr(o, "kind", ""), "axis": o.axis, "c": float(o.c), "lo": float(o.lo),
                          "hi": float(o.hi)} for o in stages["openings"]]}
    (out / "stages.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


# ----------------------------------------------------------------------------- drawing
def faded(image: np.ndarray, amount: float = 0.7) -> np.ndarray:
    return cv2.addWeighted(image, 1.0 - amount, np.full_like(image, 255), amount, 0)


def paint(canvas: np.ndarray, mask: np.ndarray, color, alpha: float = 0.85) -> None:
    m = mask.astype(bool)
    canvas[m] = (canvas[m] * (1 - alpha) + np.array(color) * alpha).astype(np.uint8)


def label(canvas: np.ndarray, text: str) -> np.ndarray:
    out = canvas.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 26), (255, 255, 255), -1)
    cv2.putText(out, text, (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--engine", choices=("classic", "neural"), default="classic")
    ap.add_argument("--mm-per-px", type=float, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--weights", default=None, help="neural checkpoint (default: the promoted profile)")
    ap.add_argument("--bias", type=float, default=None, help="wall logit bias (default: the promoted value)")
    args = ap.parse_args(argv)

    img_path = Path(args.image)
    if not img_path.is_absolute():
        img_path = ROOT / img_path
    out = Path(args.out) if args.out else ROOT / "scratch" / f"stages_{img_path.stem}_{args.engine}"
    out.mkdir(parents=True, exist_ok=True)
    image = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
    base = faded(image)

    stages: Dict = {}
    trace, report = vision.trace_image(str(img_path), mm_per_px=args.mm_per_px, engine=args.engine,
                                       ocr=not args.no_ocr, stages=stages, weights=args.weights,
                                       wall_logit_bias=args.bias)
    record = run_record(img_path, args, stages, trace)
    print(json.dumps({k: record[k] for k in ("weights", "weights_sha256", "wall_logit_bias", "early_exit", "counts")}))
    shape = image.shape[:2]
    t = float(stages["thickness"])
    raw, opened = stages["raw_walls"].astype(bool), stages["walls"].astype(bool)
    raw_cover = runs_mask(stages["raw_runs"], shape, grow=3)
    cover_u8 = runs_mask(stages["runs"], shape, grow=3).astype(np.uint8)
    # bridged gaps and separators also stand for wall: count them as covered in the last stage
    for sep in stages["separators"]:
        cv2.line(cover_u8, (int(sep[0][0]), int(sep[0][1])), (int(sep[1][0]), int(sep[1][1])), 1, int(max(3, t)))
    final_cover = cover_u8.astype(bool)
    save_stage_data(out, stages)
    (out / "run.json").write_text(json.dumps(record, indent=1), encoding="utf-8")

    p1 = base.copy(); paint(p1, raw, RED)
    p2 = base.copy(); paint(p2, opened, RED); paint(p2, raw & ~opened, ORANGE)
    p3 = base.copy(); paint(p3, opened & raw_cover, RED); paint(p3, opened & ~raw_cover, ORANGE)
    p4 = base.copy(); paint(p4, opened & final_cover, RED); paint(p4, opened & ~final_cover, ORANGE)
    for r in stages["raw_runs"]:
        a, b = ((int(r.lo), int(r.c)), (int(r.hi), int(r.c))) if r.axis == "h" else ((int(r.c), int(r.lo)), (int(r.c), int(r.hi)))
        cv2.line(p3, a, b, BLUE, 2)
    for r in stages["runs"]:
        a, b = ((int(r.lo), int(r.c)), (int(r.hi), int(r.c))) if r.axis == "h" else ((int(r.c), int(r.lo)), (int(r.c), int(r.hi)))
        cv2.line(p4, a, b, BLUE, 2)
    for sep in stages["separators"]:
        cv2.line(p4, (int(sep[0][0]), int(sep[0][1])), (int(sep[1][0]), int(sep[1][1])), PURPLE, 2)
    for op in stages["openings"]:
        a, b = (op.lo, op.c) if op.axis == "h" else (op.c, op.lo), (op.hi, op.c) if op.axis == "h" else (op.c, op.hi)
        cv2.line(p4, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), GREEN, 4)

    names = ["1_mask", "2_opened", "3_runs", "4_final"]
    titles = [f"1 mask ({args.engine}): wall pixels", "2 after opening filter (orange = removed)",
              f"3 runs extracted: {len(stages['raw_runs'])} (orange = uncovered)",
              f"4 after bridging: {len(stages['runs'])} runs, {len(stages['separators'])} separators, {len(stages['openings'])} openings"]
    panels = []
    for name, title, p in zip(names, titles, (p1, p2, p3, p4)):
        cv2.imwrite(str(out / f"{name}.png"), p)
        panels.append(label(p, title))
    top, bottom = np.hstack(panels[:2]), np.hstack(panels[2:])
    cv2.imwrite(str(out / "sheet.png"), np.vstack([top, bottom]))

    lost = lost_blobs(opened, final_cover, t)
    lines = [f"image {img_path.name}  engine {args.engine}  wall thickness {t:.1f} px  scale {stages['mm_per_px']:.2f} mm/px",
             f"mask px {int(raw.sum())}  opened {int(opened.sum())}  runs {len(stages['raw_runs'])} -> {len(stages['runs'])}",
             f"{len(lost)} wall blobs in the opened mask that the final runs do not cover (largest first):", ""]
    lines += [f"  at x={b['x']:>5} y={b['y']:>5}  {b['w']:>4} x {b['h']:<4} px ({b['w'] * stages['mm_per_px']:.0f} x {b['h'] * stages['mm_per_px']:.0f} mm)  {b['why']}"
              for b in lost[:60]]
    (out / "lost_walls.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:30]))
    print("\nwrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
