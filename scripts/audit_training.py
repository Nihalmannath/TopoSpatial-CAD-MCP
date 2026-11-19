"""Audit where a fine-tuned model goes wrong, stage by stage, on the frozen val plans.

For every val plan in the frozen manifest it compares the original pretrained model
(with the promoted wall bias) and a fine-tuned checkpoint on:

    1. the original image
    2. the annotation (rooms.png / icons.png the model was trained on)
    3. pretrained wall/window/door mask   vs the annotation   (green = agree, red = missed, blue = extra)
    4. fine-tuned mask                    vs the annotation
    5. final geometry from the pretrained model (canonical pipeline)
    6. final geometry from the fine-tuned model

It ranks plans by how much the fine-tuned model lost against the pretrained one and writes a
contact sheet (PNG) for the worst N plus ``index.html`` and ``audit.json`` with per-plan pixel
IoU, annotation statistics and registration quality (from meta.json). The first stage where the
numbers or pictures go wrong is the stage to fix.

    .venv\\Scripts\\python.exe scripts\\audit_training.py --finetuned models\\checkpoints\\cubicasa_finetuned.pth
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

IGNORE = 255
PANEL_W = 560
DIFF_COLORS = {"agree": (40, 170, 70), "missed": (220, 40, 40), "extra": (40, 90, 230)}


# ----------------------------------------------------------------------------- pure helpers
def iou(gt: np.ndarray, pred: np.ndarray, valid: Optional[np.ndarray] = None) -> float:
    """Intersection over union of two boolean masks; NaN when both are empty."""
    gt, pred = gt.astype(bool), pred.astype(bool)
    if valid is not None:
        gt, pred = gt & valid, pred & valid
    union = int((gt | pred).sum())
    return float((gt & pred).sum()) / union if union else float("nan")


def diff_panel(image: np.ndarray, gt: np.ndarray, pred: np.ndarray, valid: Optional[np.ndarray] = None) -> np.ndarray:
    """Faded image with agree / missed / extra pixels coloured."""
    gt, pred = gt.astype(bool), pred.astype(bool)
    if valid is not None:
        gt, pred = gt & valid, pred & valid
    canvas = (0.35 * image.astype(np.float32) + 0.65 * 255).astype(np.uint8)
    for key, m in (("agree", gt & pred), ("missed", gt & ~pred), ("extra", pred & ~gt)):
        canvas[m] = DIFF_COLORS[key]
    return canvas


def annotation_panel(image: np.ndarray, wall: np.ndarray, window: np.ndarray, door: np.ndarray) -> np.ndarray:
    canvas = (0.35 * image.astype(np.float32) + 0.65 * 255).astype(np.uint8)
    canvas[wall.astype(bool)] = (200, 40, 40)
    canvas[window.astype(bool)] = (40, 120, 230)
    canvas[door.astype(bool)] = (40, 170, 70)
    return canvas


def annotation_stats(wall: np.ndarray, valid: np.ndarray, px_per_mm: Optional[float]) -> Dict[str, float]:
    """Wall pixel share and (if scaled) mean wall thickness in mm from a distance-free estimate."""
    n = int(wall.sum())
    stats: Dict[str, float] = {"wall_px": n, "wall_share": n / max(1, wall.size),
                               "ignored_share": 1.0 - float(valid.mean())}
    if px_per_mm and n:
        # run-length thickness along rows and columns; the smaller median is the wall thickness
        def runs(m: np.ndarray) -> np.ndarray:
            d = np.diff(np.pad(m.astype(np.int8), ((0, 0), (1, 1))), axis=1)
            starts = np.argwhere(d == 1)
            ends = np.argwhere(d == -1)
            return (ends[:, 1] - starts[:, 1]) if len(starts) == len(ends) and len(starts) else np.array([0])
        t = min(float(np.median(runs(wall))), float(np.median(runs(wall.T))))
        stats["wall_thickness_mm"] = t / px_per_mm
    return stats


def pad_to_width(img: np.ndarray, width: int = PANEL_W) -> Image.Image:
    im = Image.fromarray(img)
    h = max(1, round(im.height * width / im.width))
    return im.resize((width, h), Image.BILINEAR)


def sheet(panels: Dict[str, Image.Image], cols: int = 3) -> Image.Image:
    names = list(panels)
    rows = (len(names) + cols - 1) // cols
    cell_h = max(p.height for p in panels.values()) + 22
    out = Image.new("RGB", (cols * PANEL_W, rows * cell_h), (255, 255, 255))
    draw = ImageDraw.Draw(out)
    for i, name in enumerate(names):
        x, y = (i % cols) * PANEL_W, (i // cols) * cell_h
        draw.text((x + 6, y + 4), name, fill=(0, 0, 0))
        out.paste(panels[name], (x, y + 22))
    return out


def rank_regressions(rows: Dict[str, dict], key: str = "wall_iou") -> list:
    """Sample ids ordered by (fine-tuned - pretrained) on ``key``, worst first; NaNs last."""
    def delta(item):
        r = item[1]
        a, b = r.get(f"ft_{key}"), r.get(f"pre_{key}")
        if a is None or b is None or a != a or b != b:
            return float("inf")
        return a - b
    return [k for k, _ in sorted(rows.items(), key=delta)]


# ----------------------------------------------------------------------------- model runs
def predict_masks(image_bgr: np.ndarray, weights: Path, bias: float) -> Dict[str, np.ndarray]:
    from trace_engine import neural

    return neural.segment(image_bgr, None, weights, wall_logit_bias=bias)


def geometry_overlay(img_path: Path, weights: Path, bias: float, mm_per_px: Optional[float], out: Path) -> Tuple[str, Optional[Image.Image]]:
    from trace_engine import compile_trace, vision

    try:
        trace, _ = vision.trace_image(str(img_path), mm_per_px=mm_per_px, ocr=False, engine="neural",
                                      weights=str(weights), wall_logit_bias=bias)
        comp = compile_trace(trace, geometry_pipeline="canonical")
        vision.render_overlay(str(img_path), trace, comp, str(out))
        s = comp.summary()["counts"]
        return f"walls {s.get('wall', 0)}, rooms {s.get('room', 0)}, doors {s.get('door', 0)}, windows {s.get('window', 0)}", Image.open(out).convert("RGB")
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(ROOT / "dataset" / "gt" / "eval_manifest.json"))
    ap.add_argument("--pretrained", default=str(ROOT / "models" / "pretrained" / "model_best_val_loss_var.pkl"))
    ap.add_argument("--finetuned", default=str(ROOT / "models" / "checkpoints" / "cubicasa_finetuned.pth"))
    ap.add_argument("--pre-bias", type=float, default=2.0, help="wall bias for the pretrained model (promoted setting)")
    ap.add_argument("--ft-bias", type=float, default=0.0)
    ap.add_argument("--split", default="val")
    ap.add_argument("--worst", type=int, default=5, help="plans to render as contact sheets")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    from trace_engine import neural

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    data = Path(manifest.get("data_root") or ROOT / "dataset" / "gt" / "training")
    if not data.is_dir():
        data = ROOT / "dataset" / "gt" / "training"
    samples = [s for s in manifest["samples"] if args.split == "all" or s["split"] == args.split]
    pre_w, ft_w = Path(args.pretrained), Path(args.finetuned)
    for label, p in (("pretrained", pre_w), ("fine-tuned", ft_w)):
        if not p.is_file():
            sys.exit(f"{label} weights not found: {p}")
    out = Path(args.out) if args.out else ROOT / "dataset" / "gt" / "audit" / time.strftime("%Y%m%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(samples)} {args.split} plans; pretrained+b{args.pre_bias:g} vs {ft_w.name}+b{args.ft_bias:g} -> {out}")

    wall_cls = neural.ROOM_CLASSES.index("Wall")
    win_cls, door_cls = neural.ICON_CLASSES.index("Window"), neural.ICON_CLASSES.index("Door")
    rows: Dict[str, dict] = {}
    cache: Dict[str, dict] = {}
    for s in samples:
        sid = s["sample_id"]
        folder = data / Path(s["image_path"]).parent
        try:
            image = np.asarray(Image.open(folder / "image.png").convert("RGB"), np.uint8)
            rooms = np.array(Image.open(folder / "rooms.png"))
            icons = np.array(Image.open(folder / "icons.png"))
            ign = folder / "ignore.png"
            ignore = np.asarray(Image.open(ign)) != 0 if ign.is_file() else np.zeros(rooms.shape, bool)
            valid = (rooms != IGNORE) & ~ignore
            gt = {"wall": rooms == wall_cls, "window": icons == win_cls, "door": icons == door_cls}
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8")) if (folder / "meta.json").is_file() else {}
            reg = meta.get("registration", {})
            ppm = (s.get("scale") or {}).get("px_per_mm")
            bgr = np.ascontiguousarray(image[:, :, ::-1])
            row = {"sample": sid, "building": s.get("building_id"), "bucket": meta.get("bucket"),
                   "reg_precision": reg.get("precision"), "reg_recall": reg.get("recall"), "reg_support": reg.get("support"),
                   "reg_score": reg.get("score"), "px_per_mm": ppm}
            row.update({f"gt_{k}": v for k, v in annotation_stats(gt["wall"], valid, ppm).items()})
            masks = {}
            for tag, w, b in (("pre", pre_w, args.pre_bias), ("ft", ft_w, args.ft_bias)):
                m = predict_masks(bgr, w, b)
                masks[tag] = {"wall": m["walls"].astype(bool), "window": m["windows"].astype(bool), "door": m["doors"].astype(bool)}
                for k in ("wall", "window", "door"):
                    row[f"{tag}_{k}_iou"] = iou(gt[k], masks[tag][k], valid)
                row[f"{tag}_wall_px_ratio"] = float(masks[tag]["wall"][valid].sum()) / max(1, int(gt["wall"][valid].sum()))
            rows[sid] = row
            cache[sid] = {"image": image, "gt": gt, "valid": valid, "masks": masks, "folder": folder, "ppm": ppm, "bgr": bgr}
            print(f"  {sid[:44]:44} wall IoU pre {row['pre_wall_iou']:.2f} ft {row['ft_wall_iou']:.2f}"
                  f"  door pre {row['pre_door_iou']:.2f} ft {row['ft_door_iou']:.2f}  reg P/R {row['reg_precision']}/{row['reg_recall']}")
        except Exception as exc:
            traceback.print_exc(limit=2)
            rows[sid] = {"sample": sid, "error": f"{type(exc).__name__}: {exc}"}

    order = [k for k in rank_regressions({k: v for k, v in rows.items() if "error" not in v}) if k in cache]
    cards = []
    for sid in order[: args.worst]:
        c, row = cache[sid], rows[sid]
        panels = {
            "1 original image": pad_to_width(c["image"]),
            "2 annotation (red wall, blue window, green door)": pad_to_width(annotation_panel(c["image"], **{k: c["gt"][k] for k in ("wall", "window", "door")})),
            f"3 pretrained+b{args.pre_bias:g} wall vs annotation (red missed, blue extra)": pad_to_width(diff_panel(c["image"], c["gt"]["wall"], c["masks"]["pre"]["wall"], c["valid"])),
            "4 fine-tuned wall vs annotation": pad_to_width(diff_panel(c["image"], c["gt"]["wall"], c["masks"]["ft"]["wall"], c["valid"])),
        }
        mm = (1.0 / c["ppm"]) if c["ppm"] else None
        ov_dir = out / "geometry"
        ov_dir.mkdir(exist_ok=True)
        for tag, w, b, n in (("pre", pre_w, args.pre_bias, 5), ("ft", ft_w, args.ft_bias, 6)):
            note, im = geometry_overlay(c["folder"] / "image.png", w, b, mm, ov_dir / f"{sid[:60]}__{tag}.png")
            row[f"{tag}_geometry"] = note
            if im is not None:
                panels[f"{n} {'pretrained' if tag == 'pre' else 'fine-tuned'} geometry: {note}"[:90]] = pad_to_width(np.asarray(im))
        name = f"{len(cards) + 1:02d}_{sid[:50]}.png".replace("(", "").replace(")", "")
        sheet(panels).save(out / name)
        cards.append((sid, name, row))

    (out / "audit.json").write_text(json.dumps({"settings": vars(args), "rows": rows, "worst": [c[0] for c in cards]}, indent=1, default=str), encoding="utf-8")

    def f(v, nd=2):
        return "-" if v is None or v != v else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

    trs = "".join(
        f"<tr><td>{html.escape(r['sample'][:48])}</td>" + "".join(f"<td>{f(r.get(k))}</td>" for k in (
            "pre_wall_iou", "ft_wall_iou", "pre_window_iou", "ft_window_iou", "pre_door_iou", "ft_door_iou",
            "pre_wall_px_ratio", "ft_wall_px_ratio", "reg_precision", "reg_recall", "gt_wall_thickness_mm", "gt_ignored_share")) + "</tr>"
        for k in order for r in [rows[k]])
    errs = "".join(f"<li>{html.escape(r['sample'][:48])}: {html.escape(r['error'])}</li>" for r in rows.values() if "error" in r)
    body = "".join(f"<h3>{i + 1}. {html.escape(sid)}</h3><p>{html.escape(str({k: v for k, v in row.items() if k.startswith(('reg_', 'pre_geom', 'ft_geom'))}))}</p><img src='{n}' style='max-width:100%'>"
                   for i, (sid, n, row) in enumerate(cards))
    (out / "index.html").write_text(
        "<meta charset=utf-8><title>Training audit</title><style>body{font:14px sans-serif;margin:20px}td,th{border:1px solid #ccc;padding:3px 7px;text-align:right}td:first-child{text-align:left}</style>"
        f"<h2>Audit: pretrained+b{args.pre_bias:g} vs {html.escape(ft_w.name)}+b{args.ft_bias:g} ({len(samples)} {args.split} plans)</h2>"
        "<p>Plans sorted worst-regression first (fine-tuned minus pretrained wall IoU). <b>wall_px_ratio</b> = predicted wall pixels / annotated wall pixels (&gt;1 draws too much). "
        "<b>reg_precision / reg_recall</b> = how well the IFC walls registered to the image when the annotation was built (low = suspect annotation).</p>"
        "<table><tr><th>plan</th><th>wall IoU pre</th><th>ft</th><th>window IoU pre</th><th>ft</th><th>door IoU pre</th><th>ft</th><th>wall px ratio pre</th><th>ft</th>"
        "<th>reg prec</th><th>reg recall</th><th>annot. wall mm</th><th>ignored</th></tr>" + trs + "</table>"
        + (f"<h3>Errors</h3><ul>{errs}</ul>" if errs else "") + body, encoding="utf-8")
    print("\nwrote", out / "index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
