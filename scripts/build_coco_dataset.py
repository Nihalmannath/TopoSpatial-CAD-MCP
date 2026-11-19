"""Assemble COCO train and val annotations from pilot predictions."""

import json
import math
import os
from pathlib import Path
import numpy as np
from PIL import Image
from shapely.geometry import Polygon, LineString

from trace_engine.raster2seq_bridge import clean_polygon, unproject_point

BASE_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = BASE_DIR / "dataset"
PILOT_JSON = DATASET_DIR / "pilot_30.json"
COCO_DIR = DATASET_DIR / "annotated_coco"
JSONS_DIR = BASE_DIR / "models" / "pretrained" / "raster2seq" / "jsons"

CATEGORIES = [
    {"id": 0, "name": "Outdoor", "supercategory": "floorplan"},
    {"id": 1, "name": "Kitchen", "supercategory": "floorplan"},
    {"id": 2, "name": "Living Room", "supercategory": "floorplan"},
    {"id": 3, "name": "Bed Room", "supercategory": "floorplan"},
    {"id": 4, "name": "Bath", "supercategory": "floorplan"},
    {"id": 5, "name": "Entry", "supercategory": "floorplan"},
    {"id": 6, "name": "Storage", "supercategory": "floorplan"},
    {"id": 7, "name": "Garage", "supercategory": "floorplan"},
    {"id": 8, "name": "Undefined", "supercategory": "floorplan"},
    {"id": 9, "name": "Window", "supercategory": "floorplan"},
    {"id": 10, "name": "Door", "supercategory": "floorplan"},
]


def expand_line_to_box(p1, p2, thickness=6.0):
    """Expand a 2-point opening line segment into a 4-point thin polygon."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1e-3:
        return [
            p1[0] - thickness, p1[1] - thickness,
            p1[0] + thickness, p1[1] - thickness,
            p1[0] + thickness, p1[1] + thickness,
            p1[0] - thickness, p1[1] + thickness,
        ]
    nx = -dy / length * (thickness / 2.0)
    ny = dx / length * (thickness / 2.0)
    poly = [
        round(p1[0] + nx, 1), round(p1[1] + ny, 1),
        round(p2[0] + nx, 1), round(p2[1] + ny, 1),
        round(p2[0] - nx, 1), round(p2[1] - ny, 1),
        round(p1[0] - nx, 1), round(p1[1] - ny, 1),
    ]
    return poly


def build_coco():
    with open(PILOT_JSON, "r", encoding="utf-8") as f:
        pilot = json.load(f)

    ann_dir = COCO_DIR / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)

    splits = {"train": {"images": [], "annotations": [], "categories": CATEGORIES},
              "val": {"images": [], "annotations": [], "categories": CATEGORIES}}

    ann_id = 1
    image_id_counter = {"train": 1, "val": 1}

    for plan in pilot["plans"]:
        fn = plan["filename"]
        split = plan["split"]
        stem = Path(fn).stem

        img_path = COCO_DIR / split / fn
        if not img_path.exists():
            continue

        im = Image.open(img_path)
        w, h = im.size
        img_id = image_id_counter[split]
        image_id_counter[split] += 1

        splits[split]["images"].append({
            "id": img_id,
            "file_name": fn,
            "width": w,
            "height": h,
            "style": plan.get("style", "unknown"),
        })

        pred_file = JSONS_DIR / f"{stem}.json"
        if not pred_file.exists():
            continue

        with open(pred_file, "r", encoding="utf-8") as f:
            items = json.load(f)

        for item in items:
            cat_id = item.get("category_id")
            if cat_id is None or cat_id > 10:
                continue

            raw_pts = item.get("segmentation", [])
            if not raw_pts:
                continue

            if cat_id in (9, 10):
                # Opening line segment
                if len(raw_pts) < 2:
                    continue
                p1 = unproject_point(raw_pts[0][0], raw_pts[0][1], w, h)
                p2 = unproject_point(raw_pts[-1][0], raw_pts[-1][1], w, h)
                flat_poly = expand_line_to_box(p1, p2, thickness=8.0)
            else:
                # Room polygon
                if len(raw_pts) < 3:
                    continue
                unproj = [unproject_point(p[0], p[1], w, h) for p in raw_pts]
                poly = clean_polygon(unproj)
                if poly is None or poly.area < 25.0:
                    continue
                coords = list(poly.exterior.coords)[:-1]
                if len(coords) < 3:
                    continue
                flat_poly = [round(v, 1) for pt in coords for v in pt]

            # Bounding box
            xs = flat_poly[0::2]
            ys = flat_poly[1::2]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            bbox = [min_x, min_y, round(max_x - min_x, 1), round(max_y - min_y, 1)]
            area = bbox[2] * bbox[3]

            splits[split]["annotations"].append({
                "id": ann_id,
                "image_id": img_id,
                "category_id": cat_id,
                "segmentation": [flat_poly],
                "bbox": bbox,
                "area": round(area, 1),
                "iscrowd": 0,
            })
            ann_id += 1

    for split in ("train", "val"):
        out_path = ann_dir / f"{split}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(splits[split], f, indent=2)
        print(f"Saved {split}: {len(splits[split]['images'])} images, {len(splits[split]['annotations'])} annotations to {out_path}")


if __name__ == "__main__":
    build_coco()
