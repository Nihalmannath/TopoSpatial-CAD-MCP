"""Reproject reviewed/pseudo COCO annotations onto the audited plan crops.

This never overwrites the original annotation set. Polygons are clipped to the
plan crop, translated to crop-local coordinates, and tiny mostly-clipped
objects (usually brochure/footer false positives) are discarded.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, box

ROOT = Path(__file__).resolve().parents[1]


def _polygon_parts(flat):
    points = list(zip(flat[0::2], flat[1::2]))
    if len(points) < 3:
        return []
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return []
    if isinstance(polygon, Polygon):
        return [polygon]
    if isinstance(polygon, MultiPolygon):
        return list(polygon.geoms)
    return []


def convert_split(source_json: Path, target_json: Path, crop_by_name, min_retained: float):
    data = json.loads(source_json.read_text(encoding="utf-8"))
    images = {image["id"]: image for image in data["images"]}
    converted_images = []
    converted_annotations = []
    stats = {"input": len(data["annotations"]), "kept": 0, "dropped": 0, "clipped": 0}

    for image in data["images"]:
        record = crop_by_name.get(image["file_name"])
        if record is None:
            raise ValueError(f"no audited crop for {image['file_name']}")
        x1, y1, x2, y2 = record["crop_box"]
        updated = copy.deepcopy(image)
        updated["width"], updated["height"] = record["cropped_size"]
        updated["crop_box_in_source"] = record["crop_box"]
        converted_images.append(updated)

    for annotation in data["annotations"]:
        image = images[annotation["image_id"]]
        record = crop_by_name[image["file_name"]]
        x1, y1, x2, y2 = record["crop_box"]
        clip = box(x1, y1, x2, y2)
        output_parts = []
        original_area = 0.0
        retained_area = 0.0
        for segmentation in annotation.get("segmentation", []):
            for polygon in _polygon_parts(segmentation):
                original_area += polygon.area
                clipped = polygon.intersection(clip)
                parts = [clipped] if isinstance(clipped, Polygon) else (
                    list(clipped.geoms) if isinstance(clipped, MultiPolygon) else []
                )
                for part in parts:
                    if part.area < 4.0:
                        continue
                    retained_area += part.area
                    coords = list(part.exterior.coords)[:-1]
                    output_parts.append([value for x, y in coords for value in (round(x - x1, 2), round(y - y1, 2))])
        retained_ratio = retained_area / original_area if original_area else 0.0
        if not output_parts or retained_ratio < min_retained:
            stats["dropped"] += 1
            continue
        updated = copy.deepcopy(annotation)
        updated["segmentation"] = output_parts
        updated["area"] = round(retained_area, 2)
        points = [(flat[i], flat[i + 1]) for flat in output_parts for i in range(0, len(flat), 2)]
        xs, ys = zip(*points)
        updated["bbox"] = [round(min(xs), 2), round(min(ys), 2), round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)]
        updated["crop_retained_ratio"] = round(retained_ratio, 4)
        converted_annotations.append(updated)
        stats["kept"] += 1
        if retained_ratio < 0.999:
            stats["clipped"] += 1

    output = copy.deepcopy(data)
    output["images"] = converted_images
    output["annotations"] = converted_annotations
    output.setdefault("info", {})["crop_reprojection"] = {
        "source": str(source_json),
        "minimum_retained_area_ratio": min_retained,
        **stats,
    }
    target_json.parent.mkdir(parents=True, exist_ok=True)
    target_json.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "dataset" / "annotated_coco")
    parser.add_argument("--output", type=Path, default=ROOT / "dataset" / "annotated_coco_cropped")
    parser.add_argument("--min-retained", type=float, default=0.25)
    args = parser.parse_args()
    manifest = json.loads((ROOT / "dataset" / "crop_manifest.json").read_text(encoding="utf-8"))
    crop_by_name = {item["filename"]: item for item in manifest["plans"]}
    cropped_images = ROOT / "dataset" / "cropped_plans"
    results = {}
    for split in ("train", "val"):
        results[split] = convert_split(
            args.source / "annotations" / f"{split}.json",
            args.output / "annotations" / f"{split}.json",
            crop_by_name,
            args.min_retained,
        )
        destination = args.output / split
        destination.mkdir(parents=True, exist_ok=True)
        data = json.loads((args.output / "annotations" / f"{split}.json").read_text(encoding="utf-8"))
        for image in data["images"]:
            shutil.copy2(cropped_images / image["file_name"], destination / image["file_name"])
    (args.output / "crop_conversion_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
