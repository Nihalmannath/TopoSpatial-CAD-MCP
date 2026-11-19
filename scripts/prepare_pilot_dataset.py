"""Prepare the 30-plan pilot dataset in COCO format for Raster2Seq fine-tuning."""

import json
import os
import shutil
from pathlib import Path
from PIL import Image
from shapely.geometry import Polygon

BASE_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = BASE_DIR / "dataset"
RAW_DIR = DATASET_DIR / "raw_233"
PILOT_JSON = DATASET_DIR / "pilot_30.json"
COCO_DIR = DATASET_DIR / "annotated_coco"

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


def prepare_images():
    with open(PILOT_JSON, "r", encoding="utf-8") as f:
        pilot = json.load(f)

    train_dir = COCO_DIR / "train"
    val_dir = COCO_DIR / "val"
    ann_dir = COCO_DIR / "annotations"

    train_dir.mkdir(parents=True, exist_ok=True)
    val_dir.mkdir(parents=True, exist_ok=True)
    ann_dir.mkdir(parents=True, exist_ok=True)

    pilot_plans = pilot["plans"]
    print(f"Total pilot plans: {len(pilot_plans)}")

    for plan in pilot_plans:
        fn = plan["filename"]
        split = plan["split"]
        src = RAW_DIR / fn
        dst = (train_dir if split == "train" else val_dir) / fn
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)

    print(f"Copied images to {train_dir} and {val_dir}")


if __name__ == "__main__":
    prepare_images()
