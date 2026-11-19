"""Build training ground truth from plan images + IFC exports.

Replaces scripts/compile_ground_truth_dataset.py and
scripts/match_all_exported_batches.py (PDF scraping + dHash matching).

For each project folder (images + one or more .ifc files):
  1. read every IFC storey as a wall graph with hosted openings and spaces;
  2. register every image against every storey and assign them 1:1;
  3. write a candidate sample per assigned pair (masks, graph, overlay).

Usage (from the repo root):

  .venv\\Scripts\\python.exe scripts\\build_gt_dataset.py build
  .venv\\Scripts\\python.exe scripts\\build_gt_dataset.py build --folders "1 FIRST 6" "3 THIRD 23"
  # open dataset\\gt\\review.html, tick good overlays, download accepted.txt
  .venv\\Scripts\\python.exe scripts\\build_gt_dataset.py accept --list accepted.txt
  .venv\\Scripts\\python.exe scripts\\build_gt_dataset.py accept --auto   # accept every auto_ok
  .venv\\Scripts\\python.exe scripts\\finetune_cubicasa.py --data dataset\\gt\\training --epochs 60

Nothing is written into the source folders.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_engine import paths  # noqa: E402
from trace_engine.gt_dataset import (  # noqa: E402
    accept_samples,
    folder_images,
    sample_id,
    write_review_page,
    write_sample,
)
from trace_engine.ifc_groundtruth import find_ifcs, storeys_for_folder  # noqa: E402
from trace_engine.register import match_folder  # noqa: E402


def process_folder(folder: str, candidates: str, write_rejects: bool, overlays: bool) -> dict:
    t0 = time.time()
    folder_p = Path(folder)
    report = {"folder": folder_p.name, "ifcs": [str(p.relative_to(folder_p)) for p in find_ifcs(folder_p)],
              "images": 0, "storeys": 0, "samples": [], "unassigned": [], "errors": []}
    try:
        storeys = storeys_for_folder(folder_p)
        images = folder_images(folder_p)
        report["images"], report["storeys"] = len(images), len(storeys)
        if not storeys or not images:
            report["errors"].append("no storeys" if not storeys else "no images")
            return report
        regs, _ = match_folder(images, storeys, log=None)
        by_key = {s.key: s for s in storeys}
        assigned = {r.image for r in regs}
        report["unassigned"] = [Path(p).name for p in images if str(p) not in assigned]
        for reg in regs:
            if reg.bucket == "reject" and not write_rejects:
                report["samples"].append({"image": Path(reg.image).name, "storey": reg.storey,
                                          "bucket": "reject", "written": False})
                continue
            sid = sample_id(folder_p.name, reg.image)
            write_sample(by_key[reg.storey], reg, Path(candidates) / sid, folder_p.name, overlay=overlays)
            report["samples"].append({"id": sid, "image": Path(reg.image).name, "storey": reg.storey,
                                      "bucket": reg.bucket, "support": round(reg.support, 3),
                                      "score": round(reg.score, 3), "margin": round(reg.margin, 3),
                                      "written": True})
    except Exception as exc:  # keep the batch going; report per folder
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["seconds"] = round(time.time() - t0, 1)
    return report


def cmd_build(args) -> int:
    root = Path(args.root)
    out = Path(args.out)
    candidates = out / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    folders = [p for p in sorted(root.iterdir(), key=lambda p: p.name) if p.is_dir()]
    if args.folders:
        wanted = set(args.folders)
        folders = [p for p in folders if p.name in wanted]
    print(f"{len(folders)} folder(s) under {root} -> {candidates}")
    reports = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(process_folder, str(f), str(candidates), args.write_rejects, not args.no_overlays): f
                for f in folders}
        for fut in as_completed(futs):
            rep = fut.result()
            reports.append(rep)
            b = {k: sum(1 for s in rep["samples"] if s["bucket"] == k) for k in ("auto_ok", "review", "reject")}
            print(f"  {rep['folder']}: {rep['images']} images, {rep['storeys']} storeys -> "
                  f"auto_ok {b['auto_ok']}, review {b['review']}, reject {b['reject']}, "
                  f"unassigned {len(rep['unassigned'])} ({rep.get('seconds', 0)} s)"
                  + (f"  ERRORS: {rep['errors']}" if rep["errors"] else ""))
    reports.sort(key=lambda r: r["folder"])
    (out / "build_report.json").write_text(json.dumps(reports, indent=1), encoding="utf-8")
    n = write_review_page(candidates, out / "review.html")
    tot = {k: sum(1 for r in reports for s in r["samples"] if s["bucket"] == k) for k in ("auto_ok", "review", "reject")}
    print(f"done: {tot}; review page with {n} candidates: {out / 'review.html'}")
    return 0


def _parse_matches(path: Path, extra) -> list:
    """(image_folder, image, floor_index|None, ifc_folder|None, storey_key|None) per line.

    3 columns (match sheet):  image_folder, image, "Building frame-floor-3" / "floor_03"
    4 columns (cross sheet):  image_folder, image, ifc_folder, storey_key
    --add uses '|' instead of tabs.
    """
    import re

    lines = [ln.rstrip("\n") for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    lines += [e.replace("|", "\t") for e in (extra or [])]
    out = []
    for ln in lines:
        c = [x.strip() for x in ln.split("\t")]
        if len(c) >= 4:
            out.append((c[0], c[1], None, c[2], c[3]))
        elif len(c) == 3:
            m = re.search(r"(\d+)\s*$", c[2])
            if m:
                out.append((c[0], c[1], int(m.group(1)), None, None))
            else:
                print(f"  cannot read floor number in: {ln}")
    return out


def cmd_manual(args) -> int:
    """Samples from hand-made image -> floor matches; the IFC folder is found by registration."""
    import re
    from trace_engine.register import IMAGE_FRAME_POSES, POSES, load_image, load_image_edges, prepare_model, register

    root = Path(args.root)
    out = Path(args.out)
    candidates = out / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    folders = {p.name: p for p in root.iterdir() if p.is_dir()}

    def fno(n: str) -> int:
        m = re.match(r"\s*(\d+)", n)
        return int(m.group(1)) if m else -99

    pool_cache: dict = {}

    def storeys(folder: str):
        if folder not in pool_cache:
            try:
                gts = storeys_for_folder(folders[folder])
            except Exception as exc:
                print(f"  {folder}: IFC error {exc}")
                gts = []
            pool_cache[folder] = [(gt, prepare_model(gt)) for gt in gts]
        return pool_cache[folder]

    matches = _parse_matches(Path(args.matches), args.add)
    print(f"{len(matches)} manual match(es)")
    report, accepted = [], []
    for img_folder, img_name, idx, ifc_folder, key in matches:
        img = folders[img_folder] / img_name if img_folder in folders else None
        if img is None or not img.is_file():
            print(f"  MISSING image {img_folder}/{img_name}")
            report.append({"image_folder": img_folder, "image": img_name, "status": "missing_image"})
            continue
        ev = load_image(img)
        ev_edge = load_image_edges(img)
        near = [f for f in folders if abs(fno(f) - fno(img_folder)) <= args.window and list(folders[f].rglob("*.ifc"))]
        if ifc_folder:
            tests = [(ifc_folder, gt, m) for gt, m in storeys(ifc_folder) if gt.key == key]
        else:
            tests = [(f, gt, m) for f in near for gt, m in storeys(f) if gt.index == idx]

        def best_of(cands, poses):
            best = None
            for f, gt, m in cands:
                reg = register(ev, gt, m, top=3, poses=poses)
                if reg is None or reg.score < args.min_score:
                    # coloured / photo plans: retry on edge evidence, keep the better score
                    alt = register(ev_edge, gt, m, top=3, poses=poses)
                    if alt is not None and (reg is None or alt.score > reg.score):
                        alt.notes.append("edge_evidence")
                        reg = alt
                if reg is not None and (best is None or reg.match > best[1].match):
                    best = (f, reg, gt)
            return best

        best = best_of(tests, IMAGE_FRAME_POSES)
        how = "label"
        if (best is None or best[1].match < args.min_match) and not ifc_folder:
            wide = [(f, gt, m) for f in near for gt, m in storeys(f)]
            alt = best_of(wide, IMAGE_FRAME_POSES)
            if alt is not None and (best is None or alt[1].match > best[1].match):
                best, how = alt, "searched"
        if best is None:
            print(f"  NO FIT {img_folder}/{img_name[:12]}")
            report.append({"image_folder": img_folder, "image": img_name, "status": "no_fit"})
            continue
        f, reg, gt = best
        ok = reg.match >= args.min_match and reg.score >= args.min_score
        reg.bucket = "manual_ok" if ok else "manual_weak"
        if how == "searched":
            reg.notes.append("storey_searched")
        sid = f"{sample_id(img_folder, img)}__{gt.key.replace(' ', '_').replace('#', '_')}"
        write_sample(gt, reg, candidates / sid, f, overlay=True)
        row = {"id": sid, "image_folder": img_folder, "image": img_name, "ifc_folder": f, "storey": gt.key,
               "how": how, "match": round(reg.match, 3), "score": round(reg.score, 3),
               "support": round(reg.support, 3), "status": reg.bucket,
               "moved": f != img_folder}
        report.append(row)
        if ok:
            accepted.append(sid)
        print(f"  {img_folder[:10]}/{img_name[:10]} -> {f[:10]} {gt.key} ({how}) match {reg.match:.2f} "
              f"score {reg.score:.2f} {reg.bucket}" + ("  [folder differs]" if f != img_folder else ""))
    (out / "manual_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (out / "manual_accepted.txt").write_text("\n".join(accepted) + "\n", encoding="utf-8")
    n = write_review_page(candidates, out / "review.html")
    weak = [r for r in report if r.get("status") == "manual_weak"]
    print(f"done: {len(accepted)} verified, {len(weak)} weak (see review.html), "
          f"{sum(1 for r in report if r.get('status') in ('no_fit', 'missing_image'))} failed")
    print(f"accept the verified ones with:  accept --list {out / 'manual_accepted.txt'}")
    return 0


def cmd_accept(args) -> int:
    out = Path(args.out)
    candidates, training = out / "candidates", out / "training"
    if args.auto:
        names = []
        for d in sorted(candidates.iterdir()):
            mp = d / "meta.json"
            if mp.is_file() and json.loads(mp.read_text(encoding="utf-8")).get("bucket") == "auto_ok":
                names.append(d.name)
    else:
        names = [ln.strip() for ln in Path(args.list).read_text(encoding="utf-8").splitlines() if ln.strip()]
    res = accept_samples(candidates, training, names, val_groups=args.val_groups)
    print(f"accepted {len(res['copied'])} -> {training} (train {res['train']}, val {res['val']}); "
          f"val groups: {res['val_groups']}")
    for n, why in res["skipped"]:
        print(f"  skipped {n}: {why}")
    for n in res["missing"]:
        print(f"  not found: {n}")
    return 0


def cmd_review(args) -> int:
    out = Path(args.out)
    n = write_review_page(out / "candidates", out / "review.html")
    print(f"{n} candidates -> {out / 'review.html'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(paths.gt_root()), help="output root (default: dataset/gt)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="register images to IFC storeys and write candidates")
    b.add_argument("--root", default=str(paths.vector_exports_root()), help="folder of project folders")
    b.add_argument("--folders", nargs="*", help="only these project folder names")
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--write-rejects", action="store_true", help="also write rejected pairs (for inspection)")
    b.add_argument("--no-overlays", action="store_true")
    b.set_defaults(func=cmd_build)
    m = sub.add_parser("manual", help="build samples from hand-made matches.txt (finds the right IFC folder)")
    m.add_argument("--matches", required=True, help="matches.txt from the match / cross page")
    m.add_argument("--root", default=str(paths.vector_exports_root()))
    m.add_argument("--window", type=int, default=2, help="also look in IFCs of folders N-window..N+window")
    m.add_argument("--min-match", type=float, default=0.30)
    m.add_argument("--min-score", type=float, default=0.15)
    m.add_argument("--add", nargs="*", help='extra matches as "image_folder|image|Building frame-floor-0"')
    m.set_defaults(func=cmd_manual)
    a = sub.add_parser("accept", help="copy reviewed candidates into the training folder")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", help="text file with one candidate id per line (from review.html)")
    g.add_argument("--auto", action="store_true", help="accept every auto_ok candidate")
    a.add_argument("--val-groups", nargs="*", help="group ids held out for validation (default: every 5th folder)")
    a.set_defaults(func=cmd_accept)
    r = sub.add_parser("review", help="rebuild review.html from the candidates")
    r.set_defaults(func=cmd_review)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
