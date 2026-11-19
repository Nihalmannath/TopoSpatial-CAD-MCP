"""Reviewed room-instance polygons in Raster2Seq's CubiCasa category space."""
import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

CATEGORIES = ["Outdoor", "Kitchen", "Living Room", "Bed Room", "Bath", "Entry",
              "Storage", "Garage", "Undefined", "Window", "Door"]


def validate_polygons(items, size):
    result = []
    w, h = size
    for item in items:
        category = item.get("category_id")
        points = item.get("points", [])
        if type(category) is not int or not 0 <= category < len(CATEGORIES):
            raise ValueError("Invalid Raster2Seq category ID")
        if len(points) < 3 or len(points) > 512:
            raise ValueError("Each polygon needs 3–512 vertices")
        for p in points:
            if len(p) != 2 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in p):
                raise ValueError("Polygon coordinates must be finite x/y pairs")
            if not 0 <= p[0] <= w or not 0 <= p[1] <= h:
                raise ValueError("Polygon extends beyond the image")
        poly = Polygon(points)
        if not poly.is_valid or poly.area <= 0:
            raise ValueError("Polygon must have positive area and no self-intersections")
        result.append({"category_id": category, "points": [list(p) for p in poly.exterior.coords[:-1]]})
    return result


def export_coco(store, output):
    """Export only explicitly reviewed complete instance annotations."""
    from .labeling import atomic_write, png_bytes
    output = Path(output)
    categories = [{"id": i, "name": name, "supercategory": "floorplan"} for i, name in enumerate(CATEGORIES)]
    documents = {split: {"images": [], "annotations": [], "categories": categories} for split in ("train", "val")}
    groups, annotation_id = {}, 1
    for image_id, key in enumerate(sorted(store.entries), 1):
        meta = store.metadata(key)
        if not meta.get("polygons_complete"):
            continue
        if not meta.get("group_verified"):
            raise ValueError("Review building/group assignments before COCO export")
        split = meta["split"]
        group = meta["group_id"]
        if group in groups and groups[group] != split:
            raise ValueError("A building/group crosses train and validation")
        groups[group] = split
        image = store.image(key)
        polygons = validate_polygons(meta.get("polygons", []), image.size)
        if not polygons:
            raise ValueError("A reviewed plan has no polygon instances")
        document = documents[split]
        filename = key + ".png"
        document["images"].append({"id": image_id, "file_name": filename, "width": image.width,
                                   "height": image.height, "source_sha256": store.entry(key)["sha256"],
                                   "group_id": group, "annotation_complete": True, "group_verified": True})
        atomic_write(output / split / filename, png_bytes(np.asarray(image)))
        for item in polygons:
            poly = Polygon(item["points"])
            lo_x, lo_y, hi_x, hi_y = poly.bounds
            document["annotations"].append({"id": annotation_id, "image_id": image_id,
                "category_id": item["category_id"], "segmentation": [[v for p in item["points"] for v in p]],
                "bbox": [lo_x, lo_y, hi_x-lo_x, hi_y-lo_y], "area": poly.area, "iscrowd": 0})
            annotation_id += 1
    for split, document in documents.items():
        atomic_write(output / "annotations" / f"{split}.json", json.dumps(document, indent=2).encode())
    return {split: len(doc["images"]) for split, doc in documents.items()}
