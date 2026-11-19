"""Convert CubiCasa5K plans (model.svg + F1_scaled.png) to this repo's gt sample format
(image.png, rooms.png, icons.png, meta.json) so finetune_cubicasa.py can mix them with the Synaps-labelled plans.
CubiCasa5K is CC BY-NC 4.0 - non-commercial use only."""
from __future__ import annotations
import argparse, json, re, hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np, cv2
from PIL import Image

ROOM = ["Background", "Outdoor", "Wall", "Kitchen", "Living Room", "Bed Room", "Bath", "Entry", "Railing", "Storage", "Garage", "Undefined"]
ICON = ["No Icon", "Window", "Door", "Closet", "Electrical Appliance", "Toilet", "Sink", "Sauna Bench", "Fire Place", "Bathtub", "Chimney"]
NS = "{http://www.w3.org/2000/svg}"

def space_class(cls: str) -> int:
    w = cls.split()[1:] ; k = " ".join(w)
    if k.startswith("Outdoor"): return 1
    if k.startswith("Kitchen"): return 3
    if k.startswith(("LivingRoom", "Dining", "Den")): return 4
    if k.startswith("Bedroom"): return 5
    if k.startswith("Bath"): return 6
    if k.startswith(("Entry", "DraughtLobby")): return 7
    if k.startswith(("Storage", "Utility", "Closet", "DressingRoom")): return 9
    if k.startswith(("Garage", "CarPort")): return 10
    return 11

def icon_class(cls: str) -> int:
    p = cls.split()
    if p[0] == "Window": return 1
    if p[0] in ("Door", "Doors"): return 2
    if p[0] == "FixedFurniture" and len(p) > 1:
        k = p[1]
        if k in ("Closet", "CoatCloset"): return 3
        if k == "ElectricalAppliance": return 4
        if k == "Toilet": return 5
        if "Sink" in k: return 6
        if k.startswith("SaunaBench"): return 7
        if k.startswith("Fireplace") or k == "WoodStove": return 8
        if k == "Bathtub": return 9
        if k == "Chimney": return 10
    return 0

def mat(s):
    m = re.match(r"matrix\(([^)]*)\)", s or "")
    if not m: return np.eye(3)
    a, b, c, d, e, f = [float(x) for x in re.split(r"[ ,]+", m.group(1).strip())]
    return np.array([[a, c, e], [b, d, f], [0, 0, 1.0]])

def pts(el, T):
    v = [float(x) for x in re.split(r"[ ,]+", el.get("points", "").strip()) if x]
    if len(v) < 6: return None
    P = np.array(v[: len(v) // 2 * 2]).reshape(-1, 2)
    H = np.c_[P, np.ones(len(P))] @ T.T
    return H[:, :2]

def walk(el, T, out):
    """collect (class_string, [polygons]) for the elements we label; recurse otherwise."""
    cls = el.get("class", "")
    T = T @ mat(el.get("transform"))
    tag = el.tag.replace(NS, "")
    if tag == "g" and cls:
        head = cls.split()[0]
        if head in ("Space", "Wall", "Railing", "Window", "Door", "Doors", "FixedFurniture", "Stairs"):
            polys = []
            for ch in el:
                if ch.tag.replace(NS, "") == "polygon":
                    p = pts(ch, T)
                    if p is not None: polys.append(p)
            if not polys and head == "FixedFurniture":
                for ch in el.iter(NS + "polygon"):
                    p = pts(ch, T)
                    if p is not None: polys.append(p)
            out.append((cls, polys))
    for ch in el: walk(ch, T, out)

def convert(sample_dir: Path, out_dir: Path, max_side: int = 1400) -> dict:
    img = Image.open(sample_dir / "F1_scaled.png").convert("RGB")
    W, H = img.size
    s = min(1.0, max_side / max(W, H))
    w, h = max(1, round(W * s)), max(1, round(H * s))
    rooms = np.zeros((h, w), np.uint8); icons = np.zeros((h, w), np.uint8)
    root = ET.parse(sample_dir / "model.svg").getroot()
    items = []
    walk(root, np.eye(3), items)
    S = np.diag([s, s])
    def fill(m, polys, v):
        for p in polys:
            cv2.fillPoly(m, [np.round(p @ S).astype(np.int32)], int(v))
    for cls, polys in items:
        if cls.startswith("Space"): fill(rooms, polys, space_class(cls))
    for cls, polys in items:
        if cls.startswith("Wall"): fill(rooms, polys, 2)
    for cls, polys in items:
        if cls.startswith("Railing"): fill(rooms, polys, 8)
    for cls, polys in items:
        c = icon_class(cls)
        if c: fill(icons, polys, c)
    out_dir.mkdir(parents=True, exist_ok=True)
    img.resize((w, h), Image.LANCZOS if s < 1 else Image.NEAREST).save(out_dir / "image.png")
    Image.fromarray(rooms).save(out_dir / "rooms.png"); Image.fromarray(icons).save(out_dir / "icons.png")
    return {"size": [w, h], "walls_px": int((rooms == 2).sum()), "n_items": len(items)}



def build_mix(dataset_root: Path, training: Path, per_category: int = 50, seed: int = 42) -> None:
    """Add a stratified sample of CubiCasa5K *train-list* plans to the training folder (ids start with CC__)."""
    import random
    cc = dataset_root / "SAMPLE DATA SET 1"
    trainlist = [l.strip().strip("/") for l in (cc / "train.txt").read_text().splitlines() if l.strip()]
    used = set()
    mf = dataset_root / "synaps_finetuning_200" / "manifest.json"
    if mf.is_file():
        for m in json.loads(mf.read_text(encoding="utf-8")):
            used.add(Path(m["source_png"]).parent.as_posix().replace("SAMPLE DATA SET 1/", ""))
    rng = random.Random(seed)
    for cat in ("colorful", "high_quality", "high_quality_architectural"):
        pool = [p for p in trainlist if p.startswith(cat + "/") and p not in used and (cc / p / "model.svg").is_file()]
        rng.shuffle(pool)
        done = 0
        for p in pool:
            if done >= per_category: break
            sid = "CC__" + p.replace("/", "_")
            out = training / sid
            if (out / "meta.json").is_file(): done += 1; continue
            try:
                st = convert(cc / p, out)
            except Exception as exc:  # bad svg: skip
                print("skip", p, exc); continue
            if st["walls_px"] < 2000:
                import shutil; shutil.rmtree(out, ignore_errors=True); continue
            sha = hashlib.sha256((cc / p / "F1_scaled.png").read_bytes()).hexdigest()
            meta = {"source_image": str(cc / p / "F1_scaled.png"), "source_sha256": sha, "folder": "cubicasa_" + cat,
                    "group_id": "cc_" + p.replace("/", "_"), "storey": "F1", "label_source": "cubicasa5k_svg",
                    "annotation_complete": True, "reviewed": True, "split": "train", "image_size": st["size"],
                    "room_classes": ROOM, "icon_classes": ICON, "notes": ["CubiCasa5K CC BY-NC 4.0, non-commercial"]}
            (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            done += 1
        print(cat, done, "plans")



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sample", nargs="?"); ap.add_argument("out", nargs="?")
    ap.add_argument("--mix", action="store_true", help="add a CubiCasa sample to dataset/gt/training")
    ap.add_argument("--per-category", type=int, default=50)
    ap.add_argument("--dataset", default=str(Path(__file__).resolve().parents[1] / "dataset"))
    a = ap.parse_args()
    if a.mix:
        ds = Path(a.dataset); build_mix(ds, ds / "gt" / "training", a.per_category)
    else:
        print(convert(Path(a.sample), Path(a.out)))

