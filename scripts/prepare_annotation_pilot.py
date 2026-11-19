"""Select a reproducible, unreviewed 30-plan annotation pilot."""
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from style_survey import classify_style
    source = ROOT / "dataset/raw_233"
    output = ROOT / "dataset/pilot_30.json"
    if output.exists():
        raise SystemExit("Pilot already exists; preserve its assignments rather than silently resplitting.")
    rows = []
    for path in sorted(source.iterdir()):
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"):
            continue
        style, stats = classify_style(str(path))
        with Image.open(path) as image:
            thumbnail = np.asarray(ImageOps.exif_transpose(image).convert("L").resize((9,8)))
        bits = (thumbnail[:, 1:] > thumbnail[:, :-1]).ravel()
        dhash = sum(int(bit) << i for i, bit in enumerate(bits))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({"filename": path.name, "style": style, "source_sha256": digest,
                     "group_id": digest[:24], "dhash": dhash, **stats})
    # Conservative visual duplicate suggestions; human building review is still
    # mandatory because similar plans can have unrelated image hashes.
    for i, row in enumerate(rows):
        for earlier in rows[:i]:
            if (row["dhash"] ^ earlier["dhash"]).bit_count() <= 4:
                row["group_id"] = earlier["group_id"]
                break
    rng = random.Random(7)
    selected, groups = [], set()
    for style, count, val_count in (("line",15,3),("grey-filled",8,2),("render",6,1),("solid",1,0)):
        candidates = [row for row in rows if row["style"] == style]
        rng.shuffle(candidates)
        chosen = []
        for row in candidates:
            if row["group_id"] not in groups:
                groups.add(row["group_id"])
                chosen.append(row)
            if len(chosen) == count:
                break
        if len(chosen) != count:
            raise SystemExit(f"Not enough independent candidates for {style}: {len(chosen)}/{count}")
        for i, row in enumerate(chosen):
            selected.append({**row, "split": "val" if i < val_count else "train",
                             "annotation_complete": False, "group_verified": False})
    manifest = {"seed": 7, "status": "unreviewed annotation queue", "source_count": len(rows),
                "style_counts": dict(Counter(row["style"] for row in rows)),
                "grouping": "dHash suggestions only; verify building variants manually", "plans": selected}
    output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (ROOT / "dataset/survey/pilot_styles.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    sheet = Image.new("RGB", (5*260, 6*235), "#e8ecf0")
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(selected):
        with Image.open(source / row["filename"]) as image:
            thumb = ImageOps.exif_transpose(image).convert("RGB")
            thumb.thumbnail((244,190))
        x,y = (i%5)*260, (i//5)*235
        sheet.paste(thumb, (x+(260-thumb.width)//2,y+8))
        draw.text((x+8,y+200), f"{i+1:02d}  {row['style']}  /  {row['split']}", fill="#102030")
        draw.text((x+8,y+216), row["filename"][:32], fill="#304050")
    sheet.save(ROOT / "dataset/pilot_30_contact_sheet.png")
    print(json.dumps({"selected":len(selected),"split":dict(Counter(r['split'] for r in selected)),
                      "survey":manifest['style_counts'],"manifest":str(output)},indent=2))


if __name__ == "__main__":
    main()
