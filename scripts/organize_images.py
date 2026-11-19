"""Copy plan images into the project folder of the IFC they belong to (non-destructive).

  python scripts/organize_images.py [--list reviewed.txt]

Reads dataset/gt/manual_report.json (from `build_gt_dataset.py manual`). Pairs in --list
(default: all verified `manual_ok`) are copied to
  dataset/raw_vector_exports_fixed/PDF plan/<IFC folder>/<image>
Other pairs go to .../_to_check/<IFC folder>/. Originals are never moved or deleted;
a pairs.tsv records image folder -> IFC folder -> storey. Stdlib only.
"""
import argparse, json, os, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data = Path(os.environ.get("TOPOSPATIAL_DATA") or ROOT / "dataset")
ap = argparse.ArgumentParser()
ap.add_argument("--list")
ap.add_argument("--report", default=str(data / "gt" / "manual_report.json"))
ap.add_argument("--src", default=str(data / "raw_vector_exports" / "PDF plan"))
ap.add_argument("--out", default=str(data / "raw_vector_exports_fixed" / "PDF plan"))
a = ap.parse_args()
rep = [r for r in json.loads(Path(a.report).read_text(encoding="utf-8")) if r.get("id")]
keep = set(Path(a.list).read_text(encoding="utf-8").split()) if a.list else {r["id"] for r in rep if r["status"] == "manual_ok"}
out = Path(a.out)
rows, n_ok, n_chk = [], 0, 0
for r in rep:
    good = r["id"] in keep
    dest = (out if good else out / "_to_check") / r["ifc_folder"]
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(a.src) / r["image_folder"] / r["image"]
    target = dest / r["image"]
    if target.exists() and target.stat().st_size == src.stat().st_size:
        pass
    elif target.exists():  # same image used for a second storey, or a name clash
        pass
    else:
        shutil.copy2(src, target)
    rows.append("\t".join([("ok" if good else "check"), r["image_folder"], r["image"], r["ifc_folder"], r["storey"]]))
    n_ok += good; n_chk += not good
(out / "pairs.tsv").write_text("status\timage_folder\timage\tifc_folder\tstorey\n" + "\n".join(rows) + "\n", encoding="utf-8")
print(f"{n_ok} pairs filed under their IFC folder, {n_chk} in _to_check -> {out}")
