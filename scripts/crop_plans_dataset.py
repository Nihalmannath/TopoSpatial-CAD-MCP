"""Crop floor-plan only regions from template brochures across the 233 dataset images.

Removes:
- Top template header banner ("HOUSE PLAN ...") and brown category tag
- Bottom "HOUSE FEATURES" section with 6 room icons
- Bottom dark charcoal/brown branding footer band
- Blank outer sheet margins

Saves:
- Cropped images to `dataset/cropped_plans/`
- Full reversible geometric audit manifest to `dataset/crop_manifest.json`
- 3x3 before-and-after visual montage to `dataset/crop_sample_montage.png`
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytesseract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crop_plans")

BASE_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = BASE_DIR / "dataset"
RAW_DIR = DATASET_DIR / "raw_233"
CROPPED_DIR = DATASET_DIR / "cropped_plans"
MANIFEST_FILE = DATASET_DIR / "crop_manifest.json"
MONTAGE_FILE = DATASET_DIR / "crop_sample_montage.png"


def detect_plan_crop(img_path: Path) -> Tuple[int, int, int, int]:
    """Detect precise floor-plan bounding box (x1, y1, x2, y2) eliminating template clutter."""
    im = Image.open(img_path)
    w, h = im.size
    gray = np.array(im.convert("L"))

    # 1. Top limit: Header text & brown tag are strictly within y in [0, 160].
    # White space gap follows. Scan from y=160 downwards to find top of plan drawing.
    top_plan = None
    for r in range(160, int(h * 0.70)):
        if (gray[r] < 235).sum() >= 10:
            top_plan = r
            break
    if top_plan is None:
        top_plan = 200

    # 2. Bottom limit:
    # A) Check for bottom dark branding footer
    branding_top = h - 90
    for r in range(h - 1, int(h * 0.85), -1):
        if gray[r].min() < 100 and gray[r].mean() < 200:
            branding_top = r
        elif gray[r].mean() > 248 and branding_top < h - 90:
            break

    # B) Check for HOUSE FEATURES via OCR in lower section [int(h*0.55), branding_top]
    search_top = int(h * 0.55)
    lower_crop = im.crop((0, search_top, w, branding_top))
    data = pytesseract.image_to_data(lower_crop, output_type=pytesseract.Output.DICT)

    hf_top = None
    for i in range(len(data["text"])):
        txt = data["text"][i].strip().upper()
        if "FEATURE" in txt or (txt == "HOUSE" and data["top"][i] > 100):
            actual_top = search_top + data["top"][i]
            if actual_top > int(h * 0.60):
                if hf_top is None or actual_top < hf_top:
                    hf_top = actual_top

    # Fallback: check for icons text (Bedrooms, Bathrooms, Carpourch, Balcony)
    if hf_top is None:
        for i in range(len(data["text"])):
            txt = data["text"][i].strip().upper()
            if any(k in txt for k in ("CARPOURCH", "BALCONY", "BEDROOMS", "BATHROOMS")):
                actual_top = search_top + data["top"][i]
                if actual_top > int(h * 0.65):
                    candidate_hf = actual_top - 120
                    if hf_top is None or candidate_hf < hf_top:
                        hf_top = candidate_hf

    if hf_top is not None:
        bot_search_limit = min(hf_top - 15, branding_top - 10)
    else:
        bot_search_limit = branding_top - 10

    # Scan upwards from bot_search_limit to find bottom of plan drawing
    bot_plan = None
    for r in range(bot_search_limit, top_plan, -1):
        if (gray[r] < 235).sum() >= 10:
            bot_plan = r
            break
    if bot_plan is None:
        bot_plan = bot_search_limit

    # 3. Find left and right bounds of the plan content within [top_plan, bot_plan]
    sub = gray[top_plan : bot_plan + 1, :]
    col_counts = (sub < 235).sum(axis=0)
    valid_cols = np.where(col_counts >= 5)[0]

    left_plan = 0 if len(valid_cols) == 0 else valid_cols[0]
    right_plan = w if len(valid_cols) == 0 else valid_cols[-1]

    # Add comfortable margin (25 pixels)
    margin = 25
    y1 = max(0, top_plan - margin)
    y2 = min(h, bot_plan + margin)
    x1 = max(0, left_plan - margin)
    x2 = min(w, right_plan + margin)

    return int(x1), int(y1), int(x2), int(y2)


def process_plan(img_path: Path) -> Dict[str, Any]:
    """Process a single image: detect crop box, crop, and save to CROPPED_DIR."""
    x1, y1, x2, y2 = detect_plan_crop(img_path)

    im = Image.open(img_path)
    orig_w, orig_h = im.size
    cropped = im.crop((x1, y1, x2, y2))

    out_path = CROPPED_DIR / img_path.name
    # Save as high quality JPEG to preserve full architectural linework
    cropped.save(out_path, quality=98)

    crop_w = x2 - x1
    crop_h = y2 - y1

    return {
        "filename": img_path.name,
        "orig_size": [orig_w, orig_h],
        "crop_box": [x1, y1, x2, y2],
        "cropped_size": [crop_w, crop_h],
        "aspect_ratio": round(crop_w / crop_h, 3),
        "removed_margins": {
            "top_px": y1,
            "bottom_px": orig_h - y2,
            "left_px": x1,
            "right_px": orig_w - x2,
        },
    }


def generate_montage(sample_records: List[Dict[str, Any]]):
    """Generate a clean side-by-side comparison montage of 4 representative plans."""
    sample_records = sample_records[:4]
    thumb_w, thumb_h = 300, 424

    montage = Image.new("RGB", (thumb_w * 4 + 50, thumb_h * 2 + 80), "white")
    draw = ImageDraw.Draw(montage)

    try:
        font = ImageFont.truetype("arial.ttf", 16)
        small_font = ImageFont.truetype("arial.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()

    draw.text((20, 15), "ORIGINAL TEMPLATE BROCHURE (Top Row)", fill=(40, 40, 40), font=font)
    draw.text((20, thumb_h + 45), "CROPPED PLAN-ONLY PORTION (Bottom Row)", fill=(10, 120, 40), font=font)

    for i, rec in enumerate(sample_records):
        fn = rec["filename"]
        raw_p = RAW_DIR / fn
        crop_p = CROPPED_DIR / fn

        # Top row: original with red crop box overlay
        raw_im = Image.open(raw_p)
        raw_draw = ImageDraw.Draw(raw_im)
        box = rec["crop_box"]
        raw_draw.rectangle(box, outline="red", width=6)
        raw_thumb = raw_im.resize((thumb_w, thumb_h))
        montage.paste(raw_thumb, (10 + i * (thumb_w + 10), 40))

        # Bottom row: cropped plan only
        crop_im = Image.open(crop_p)
        crop_thumb = crop_im.copy()
        crop_thumb.thumbnail((thumb_w, thumb_h))
        # Center in cell
        offset_x = (thumb_w - crop_thumb.width) // 2
        offset_y = (thumb_h - crop_thumb.height) // 2
        montage.paste(crop_thumb, (10 + i * (thumb_w + 10) + offset_x, thumb_h + 70 + offset_y))

    montage.save(MONTAGE_FILE)
    logger.info(f"Saved visual comparison montage to {MONTAGE_FILE}")


def main():
    CROPPED_DIR.mkdir(parents=True, exist_ok=True)
    all_imgs = sorted(list(RAW_DIR.glob("*.jpg")))
    logger.info(f"Found {len(all_imgs)} floor plans in {RAW_DIR}")

    t0 = time.time()
    records = []

    # Process in parallel using ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_fn = {executor.submit(process_plan, p): p for p in all_imgs}
        for future in concurrent.futures.as_completed(future_to_fn):
            fn = future_to_fn[future].name
            try:
                rec = future.result()
                records.append(rec)
                if len(records) % 25 == 0 or len(records) == len(all_imgs):
                    logger.info(f"Cropped {len(records)} / {len(all_imgs)} plans...")
            except Exception as e:
                logger.error(f"Error cropping {fn}: {e}")

    records.sort(key=lambda r: r["filename"])

    manifest = {
        "total_plans": len(records),
        "source_directory": str(RAW_DIR),
        "output_directory": str(CROPPED_DIR),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "avg_crop_width": round(float(np.mean([r["cropped_size"][0] for r in records])), 1),
            "avg_crop_height": round(float(np.mean([r["cropped_size"][1] for r in records])), 1),
            "avg_top_removed_px": round(float(np.mean([r["removed_margins"]["top_px"] for r in records])), 1),
            "avg_bottom_removed_px": round(float(np.mean([r["removed_margins"]["bottom_px"] for r in records])), 1),
        },
        "plans": records,
    }

    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    logger.info(f"Saved crop manifest with {len(records)} entries to {MANIFEST_FILE}")
    logger.info(f"All 233 plans cropped in {time.time() - t0:.2f} seconds.")

    # Generate comparison montage
    generate_montage(records)


if __name__ == "__main__":
    main()
