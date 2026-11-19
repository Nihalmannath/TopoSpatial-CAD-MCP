"""Save what the network outputs for each validation plan, exactly as the tracer sees it.

    .venv\\Scripts\\python.exe scripts\\dump_segments.py

For every val plan it runs ``trace_image`` (neural engine) and records the
``neural.segment`` result of that call: wall / window / door masks, room and
icon maps, the network crop, and the junction heatmaps (uint8). Output:
dataset/gt/segments/<model>/<sample>.npz. The tracer can then be developed and
scored offline by replaying these files instead of running the network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(REPO / "dataset" / "gt" / "training"))
    ap.add_argument("--out", default=str(REPO / "dataset" / "gt" / "segments"))
    ap.add_argument("--model", action="append", default=[], help="NAME=path[@bias] (repeatable)")
    ap.add_argument("--split", default="val")
    args = ap.parse_args()

    from trace_engine import neural, vision

    models = []
    for m in args.model or ["J=" + str(REPO / "models" / "checkpoints" / "cubicasa_finetuned_J.pth")]:
        name, rest = m.split("=", 1)
        path, _, bias = rest.partition("@")
        models.append((name, Path(path), float(bias) if bias else 0.0))
    samples = []
    for d in sorted(p for p in Path(args.data).iterdir() if (p / "image.png").is_file() and (p / "meta.json").is_file()):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if args.split == "all" or meta.get("split") == args.split:
            samples.append((d, (meta.get("registration") or {}).get("px_per_mm")))
    if not samples:
        sys.exit(f"no {args.split} samples in {args.data}")
    original = neural.segment
    captured = {}

    def recording_segment(image, bbox, weights, **kw):
        seg = original(image, bbox, weights, return_heat=True, **kw)
        captured.update(seg=seg, bbox=bbox, kw=kw, shape=image.shape[:2])
        return {k: v for k, v in seg.items() if k != "heat"}

    neural.segment = recording_segment
    try:
        for name, path, bias in models:
            if not path.is_file():
                print(f"missing weights: {path}")
                continue
            out = Path(args.out) / name
            out.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            for i, (d, ppm) in enumerate(samples):
                captured.clear()
                error = ""
                try:
                    vision.trace_image(str(d / "image.png"), mm_per_px=(1.0 / ppm) if ppm else None, ocr=False,
                                       engine="neural", weights=str(path), wall_logit_bias=bias)
                except Exception as exc:  # the segment result is still saved
                    error = f"{type(exc).__name__}: {exc}"
                if "seg" not in captured:
                    print(f"  {name} {i + 1}/{len(samples)} {d.name[:44]}  NOT RECORDED {error}")
                    continue
                seg = captured["seg"]
                bbox = captured["bbox"]
                np.savez_compressed(
                    out / f"{d.name}.npz", walls=seg["walls"], windows=seg["windows"], doors=seg["doors"],
                    rooms=seg["rooms"], icons=seg["icons"], heat=seg["heat"],
                    bbox=np.asarray(bbox if bbox is not None else (-1, -1, -1, -1), np.int64),
                    wall_logit_bias=np.float32(captured["kw"].get("wall_logit_bias", 0.0)),
                    shape=np.asarray(captured["shape"], np.int64), error=np.asarray(error))
                print(f"  {name} {i + 1}/{len(samples)} {d.name[:44]}  heat max {int(seg['heat'][:13].max())}/255"
                      + (f"  trace error: {error}" if error else ""), flush=True)
            (out / "_info.json").write_text(json.dumps({"weights": str(path), "wall_logit_bias": bias,
                                                        "split": args.split, "samples": [d.name for d, _ in samples]}),
                                            encoding="utf-8")
            print(f"{name}: {time.time() - t0:.0f} s -> {out}")
    finally:
        neural.segment = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
