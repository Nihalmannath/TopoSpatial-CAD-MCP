"""Where do doors and windows get lost? Trace -> compile, stage by stage.

Runs on the samples of an eval manifest (e.g. the ResPlan Test B set) and, per
model, counts for every ground-truth door/window whether it was

  found_raw       detected by the recogniser (centre within --open-tol-mm)
  placed          still present after compile (canonical pipeline)
  lost_<code>     detected, then rejected while hosting it on a wall
                  (opening_no_host / opening_wider_than_wall / opening_overlap)
  lost_other      detected, then missing after compile for another reason

plus false detections and the width ratio (detected / true) of matched doors.

    .venv\\Scripts\\python.exe scripts\\diagnose_openings.py --data dataset\\resplan_gt --manifest dataset\\resplan_gt\\eval_manifest.json --limit 60

Output: printed table and diagnose_openings.json next to the manifest.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_trace as ev  # noqa: E402

REJECT_CODES = ("opening_no_host", "opening_wider_than_wall", "opening_overlap")


def nearest(points, p, tol):
    if not points:
        return None, None
    d = [math.dist(p, q) for q in points]
    i = int(np.argmin(d))
    return (i, d[i]) if d[i] <= tol else (None, None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "dataset" / "resplan_gt"))
    ap.add_argument("--manifest", default=str(ROOT / "dataset" / "resplan_gt" / "eval_manifest.json"))
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--open-tol-mm", type=float, default=400.0)
    ap.add_argument("--model", action="append", default=[],
                    help="name=weights; default: classic and J (models/checkpoints/cubicasa_finetuned_J.pth)")
    args = ap.parse_args()

    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    data = Path(man.get("data_root") or args.data)
    if not data.is_dir():
        data = Path(args.data)
    samples = [s for s in man["samples"] if args.split == "all" or s["split"] == args.split][: args.limit or None]
    models = [m.partition("=")[::2] for m in args.model] or [
        ("classic", ""), ("J", str(ROOT / "models" / "checkpoints" / "cubicasa_finetuned_J.pth"))]

    from trace_engine.canonical import Frame
    from trace_engine.compiler import compile_trace
    from trace_engine.vision import trace_image

    report = {}
    for name, weights in models:
        weights = None if weights in ("", "classic") else weights
        tally = {k: collections.Counter() for k in ("door", "window")}
        diag_codes = collections.Counter()
        width_ratio = {"door": [], "window": []}
        examples = collections.defaultdict(list)
        print(f"\n== {name}")
        for s in samples:
            ppm = (s.get("scale") or {}).get("px_per_mm")
            tol = args.open_tol_mm * ppm if ppm else 16.0
            gt = ev.load_gt(data / s["ground_truth_path"])
            img = data / s["image_path"]
            try:
                trace, _ = trace_image(str(img), mm_per_px=(1.0 / ppm) if ppm else None, ocr=False,
                                       engine="classic" if weights is None else "neural", weights=weights,
                                       close_rooms=True if weights else None)
                comp = compile_trace(trace, geometry_pipeline="canonical")
                frame = Frame.of(trace)
            except Exception as exc:
                print(f"  {s['sample_id']}: trace/compile failed: {type(exc).__name__}: {exc}")
                for kind in tally:
                    tally[kind]["trace_failed"] += len(gt["ops"][kind])
                continue
            raw = ev.pred_raw(trace)["ops"]
            placed = ev.pred_compiled(comp, frame)["ops"]
            reg = getattr(comp, "regularized", None)
            rejects = []
            for d in (reg.diagnostics if reg is not None else []):
                diag_codes[d.code] += 1
                if d.code in REJECT_CODES and d.location is not None:
                    rejects.append((d.code, frame.from_mm(d.location), d.message))
            for kind in ("door", "window"):
                g_c = [o["c"] for o in gt["ops"][kind]]
                r_c = [o["c"] for o in raw[kind]]
                p_c = [o["c"] for o in placed[kind]]
                matched_raw = set()
                for gi, go in enumerate(gt["ops"][kind]):
                    ri, _ = nearest(r_c, go["c"], tol)
                    if ri is None:
                        tally[kind]["missed_by_recogniser"] += 1
                        continue
                    matched_raw.add(ri)
                    tally[kind]["found_raw"] += 1
                    if go["w"]:
                        width_ratio[kind].append(raw[kind][ri]["w"] / go["w"])
                    pi, _ = nearest(p_c, go["c"], tol)
                    if pi is not None:
                        tally[kind]["placed"] += 1
                        continue
                    rj, _ = nearest([r[1] for r in rejects], r_c[ri], tol)
                    code = rejects[rj][0] if rj is not None else "other"
                    tally[kind][f"lost_{code}"] += 1
                    if len(examples[f"{kind}:{code}"]) < 6:
                        examples[f"{kind}:{code}"].append(
                            f"{s['sample_id']}: {rejects[rj][2] if rj is not None else 'no diagnostic'}")
                for ri, rc in enumerate(r_c):
                    if ri in matched_raw:
                        continue
                    tally[kind]["false_raw"] += 1
                    dmin = min((math.dist(rc, q) for q in g_c), default=None)
                    mm_d = (dmin / ppm) if (dmin is not None and ppm) else None
                    if mm_d is None:
                        tally[kind]["false_far_from_any_true"] += 1
                    elif mm_d < 900:
                        tally[kind]["false_near_miss_under_900mm"] += 1  # a true opening is close: position/width error
                    else:
                        tally[kind]["false_far_from_any_true"] += 1      # nothing true nearby: a spurious gap
                tally[kind]["gt"] += len(g_c)
        ratios = {k: {"median": round(float(np.median(v)), 2), "p10": round(float(np.percentile(v, 10)), 2),
                      "p90": round(float(np.percentile(v, 90)), 2)} if v else None for k, v in width_ratio.items()}
        report[name] = {"openings": {k: dict(v) for k, v in tally.items()}, "width_ratio_raw_vs_true": ratios,
                        "diagnostic_codes": dict(diag_codes.most_common()), "examples": dict(examples)}
        for kind in ("door", "window"):
            t = tally[kind]
            print(f"  {kind:6} true {t['gt']:4}  recogniser found {t['found_raw']:4}  placed {t['placed']:4}  "
                  + "  ".join(f"{k} {v}" for k, v in sorted(t.items()) if k.startswith("lost_"))
                  + f"  false detections {t['false_raw']} (near a true one {t['false_near_miss_under_900mm']},"
                  f" nothing true nearby {t['false_far_from_any_true']})")
        print("  width ratio (detected/true):", ratios)
        print("  diagnostics:", dict(diag_codes.most_common(8)))
    out = Path(args.manifest).with_name("diagnose_openings.json")
    out.write_text(json.dumps({"samples": len(samples), "open_tol_mm": args.open_tol_mm, "models": report}, indent=1))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
