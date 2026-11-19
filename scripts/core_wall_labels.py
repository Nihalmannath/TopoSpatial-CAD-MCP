"""Make training_v3: copy dataset/gt/training and, for the Synaps-labelled plans, keep only the CORE of every wall.

Synaps draws walls at three fixed thicknesses (350 / 200 / 50 mm) whatever the image shows, so the full painted wall
teaches the model the wrong thickness. Here the wall centre band (about 3 px) stays labelled wall; the rest of each
wall in rooms.png becomes 255 (ignore) so it is neither rewarded nor penalised. icons.png (doors, windows) is untouched.
CubiCasa hand-labelled plans (CC__*) are copied unchanged: their thickness was drawn by people from the image.
Originals are not modified."""
import argparse, json, shutil, time
from pathlib import Path
import numpy as np, cv2
from PIL import Image

WALL, IGNORE = 2, 255

def core_band(wall: np.ndarray, half: int = 1) -> np.ndarray:
    m = wall.astype(np.uint8)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 3)
    ridge = (dt >= cv2.dilate(dt, np.ones((5, 5), np.uint8)) - 0.5) & (dt >= 1.0)
    core = cv2.dilate(ridge.astype(np.uint8), np.ones((2 * half + 1, 2 * half + 1), np.uint8)) > 0
    return core & wall

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="dataset/gt/training"); ap.add_argument("--dst", default="dataset/gt/training_v3")
    ap.add_argument("--budget", type=float, default=100.0, help="seconds per run")
    a = ap.parse_args()
    t0 = time.time()
    src, dst = Path(a.src), Path(a.dst)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in sorted(p for p in src.iterdir() if (p / "rooms.png").is_file()):
        out = dst / d.name
        done = out / "meta.json"
        if done.is_file() and (d.name.startswith("CC__") or json.loads(done.read_text(encoding="utf-8")).get("wall_label")):
            continue
        if time.time() - t0 > a.budget: print("time budget reached; run again to continue"); break
        shutil.copytree(d, out, dirs_exist_ok=True)
        if d.name.startswith("CC__"): continue
        r = np.array(Image.open(d / "rooms.png"))
        wall = r == WALL
        keep = core_band(wall)
        r2 = r.copy(); r2[wall & ~keep] = IGNORE
        Image.fromarray(r2).save(out / "rooms.png")
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        meta["wall_label"] = "core_only (Synaps thickness ignored)"
        (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        n += 1
    print(f"{n} Synaps plans relabelled -> {dst}")

if __name__ == "__main__":
    main()
